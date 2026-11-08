#!/usr/bin/env python3
"""
shotty — fast region screenshot + annotate for KDE Wayland (multi-monitor)

  hotkey → overlay on every monitor → drag region → pen/arrow annotate
  Enter / Ctrl+C        → save PNG to ~/Pictures/Screenshots + copy image
  Ctrl+Shift+C          → save PNG + copy file *path* instead
  Ctrl+Z                → undo stroke
  Esc / right-click     → back to selection, then quit

Capture: KWin ScreenShot2 (raw pixels over a pipe — fast) with
xdg-desktop-portal fallback.  The capture is one big image of the whole
monitor layout rendered at a uniform scale; each overlay window shows the
slice for its own monitor (capture_px = gtk_logical_coord × F where
F = capture_width / logical_layout_width).
"""

import os, sys, socket

SOCK = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "shotty.sock")


def _trigger_daemon() -> bool:
    """Poke a running daemon. Returns True if one answered."""
    s = socket.socket(socket.AF_UNIX)
    try:
        s.settimeout(1.0)
        s.connect(SOCK)
        s.sendall(b"shot")
        return True
    except OSError:
        return False
    finally:
        s.close()


# Fast exit path BEFORE any heavy imports: if a daemon is already running,
# just poke it (few ms) and quit.
if __name__ == "__main__" and "--daemon" not in sys.argv:
    if _trigger_daemon():
        sys.exit(0)

import dbus
import dbus.mainloop.glib
dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)  # before GLib/GTK init

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, GLib

import cairo
import subprocess, datetime, math
from urllib.parse import urlparse
from PIL import Image, ImageDraw

SAVE_DIR = os.path.expanduser("~/Pictures/Screenshots")

COLORS = [
    ("Red",    (1.00, 0.20, 0.20, 1.0)),
    ("Orange", (1.00, 0.58, 0.10, 1.0)),
    ("Yellow", (1.00, 0.92, 0.10, 1.0)),
    ("Green",  (0.20, 0.88, 0.30, 1.0)),
    ("Blue",   (0.25, 0.55, 1.00, 1.0)),
    ("White",  (1.00, 1.00, 1.00, 1.0)),
    ("Black",  (0.00, 0.00, 0.00, 1.0)),
]
SIZES = [(2, "S"), (4, "M"), (8, "L"), (14, "XL")]

ST_SELECT = 0
ST_DRAW   = 1


# ─── capture ──────────────────────────────────────────────────────────────────

# QImage::Format enum -> PIL raw mode (little-endian byte order).
# Premultiplied variants are treated as straight: desktop captures are opaque.
QIMAGE_RAW_MODES = {
    4: "BGRX", 5: "BGRA", 6: "BGRA",
    16: "RGBX", 17: "RGBA", 18: "RGBA",
}


def kwin_screenshot() -> Image.Image | None:
    """KWin ScreenShot2: raw pixels over a pipe. Needs the launching .desktop
    file to contain X-KDE-DBUS-Restricted-Interfaces=org.kde.KWin.ScreenShot2."""
    try:
        bus   = dbus.SessionBus()
        iface = dbus.Interface(
            bus.get_object("org.kde.KWin", "/org/kde/KWin/ScreenShot2"),
            "org.kde.KWin.ScreenShot2")
        r, w = os.pipe()
        try:
            res = iface.CaptureWorkspace(
                {"native-resolution": dbus.Boolean(True)},
                dbus.types.UnixFd(w))
        finally:
            os.close(w)
        fmt = int(res.get("format", 6))
        raw_mode = QIMAGE_RAW_MODES.get(fmt)
        if raw_mode is None:
            os.close(r)
            raise ValueError(f"unsupported QImage format {fmt}")
        width, height = int(res["width"]), int(res["height"])
        stride = int(res.get("stride", width * 4))
        buf = bytearray()
        with os.fdopen(r, "rb") as f:
            while len(buf) < stride * height:
                chunk = f.read(stride * height - len(buf))
                if not chunk:
                    break
                buf += chunk
        if len(buf) < stride * height:
            return None
        return Image.frombuffer("RGBA", (width, height), bytes(buf),
                                "raw", raw_mode, stride, 1).convert("RGBA")
    except Exception as e:
        print(f"KWin fast path unavailable ({e}); using portal.", file=sys.stderr)
        return None


def portal_screenshot() -> Image.Image | None:
    bus    = dbus.SessionBus()
    portal = dbus.Interface(
        bus.get_object("org.freedesktop.portal.Desktop",
                       "/org/freedesktop/portal/desktop"),
        "org.freedesktop.portal.Screenshot")
    loop, uri = GLib.MainLoop(), [None]
    handle = portal.Screenshot("", {"interactive": dbus.Boolean(False)})

    def on_response(response, results):
        if response == 0:
            uri[0] = results.get("uri")
        loop.quit()

    bus.add_signal_receiver(on_response, signal_name="Response",
                            dbus_interface="org.freedesktop.portal.Request",
                            path=handle)
    GLib.timeout_add_seconds(10, loop.quit)
    loop.run()
    if not uri[0]:
        return None
    path = urlparse(str(uri[0])).path
    if not os.path.exists(path):
        return None
    img = Image.open(path).convert("RGBA")
    try:
        os.unlink(path)
    except OSError:
        pass
    return img


# ─── helpers ──────────────────────────────────────────────────────────────────

def pil_to_surface(img: Image.Image) -> cairo.ImageSurface:
    data = bytearray(img.tobytes("raw", "BGRA"))
    return cairo.ImageSurface.create_for_data(
        data, cairo.FORMAT_RGB24, img.width, img.height, img.width * 4)

def _rounded_rect(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x+w-r, y+r,   r, -math.pi/2, 0)
    cr.arc(x+w-r, y+h-r, r, 0,          math.pi/2)
    cr.arc(x+r,   y+h-r, r, math.pi/2,  math.pi)
    cr.arc(x+r,   y+r,   r, math.pi,    3*math.pi/2)
    cr.close_path()

def _arrow_head(p1, p2, width):
    dx, dy = p2[0]-p1[0], p2[1]-p1[1]
    L = math.hypot(dx, dy)
    if L < 1:
        return []
    ux, uy = dx/L, dy/L
    hl, ang = max(14, width*4), 0.42
    ca, sa = math.cos(ang), math.sin(ang)
    out = []
    for sign in (+1, -1):
        rx = -(ca*ux - sign*sa*uy)
        ry = -(ca*uy + sign*sa*ux)
        out.append((p2, (p2[0]+rx*hl, p2[1]+ry*hl)))
    return out


# ─── overlay window (one per monitor) ─────────────────────────────────────────

class Overlay(Gtk.Window):

    def __init__(self, app, mon_index: int, img: Image.Image):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.app  = app
        self.img  = img                       # this monitor's slice (capture px)
        self.SW, self.SH = img.size
        self.bg   = pil_to_surface(img)
        self._bg_scaled = None            # background pre-scaled to device px

        self.state     = ST_SELECT
        self.sel       = None                 # (x1,y1,x2,y2) window coords
        self.sel_start = None
        self.drag_sel  = False
        self.strokes: list[dict] = []
        self.cur:     dict | None = None
        self.drawing   = False
        self._tb_hits: list[tuple] = []

        self.set_decorated(False)
        self.set_app_paintable(True)
        self.set_keep_above(True)
        vis = self.get_screen().get_rgba_visual()
        if vis:
            self.set_visual(vis)

        da = Gtk.DrawingArea()
        da.connect("draw",                 self._on_draw)
        da.connect("button-press-event",   self._on_press)
        da.connect("button-release-event", self._on_release)
        da.connect("motion-notify-event",  self._on_motion)
        da.add_events(Gdk.EventMask.BUTTON_PRESS_MASK |
                      Gdk.EventMask.BUTTON_RELEASE_MASK |
                      Gdk.EventMask.POINTER_MOTION_MASK)
        self.add(da)
        self.connect("key-press-event", self.app.on_key)

        self.fullscreen_on_monitor(self.get_screen(), mon_index)
        self.show_all()
        self._set_cursor("crosshair")

    # ── misc ─────────────────────────────────────────────────────────────────

    def _set_cursor(self, name):
        win = self.get_window()
        if win:
            win.set_cursor(Gdk.Cursor.new_from_name(self.get_display(), name))

    def _scale(self):
        alloc = self.get_child().get_allocation()
        return self.SW / max(1, alloc.width), self.SH / max(1, alloc.height)

    def _queue_rect(self, x1, y1, x2, y2, pad):
        self.get_child().queue_draw_area(
            int(min(x1, x2) - pad), int(min(y1, y2) - pad),
            int(abs(x2 - x1) + 2*pad), int(abs(y2 - y1) + 2*pad))

    def _stroke_bbox(self, s):
        xs = [p[0] for p in s["points"]]
        ys = [p[1] for p in s["points"]]
        pad = s["width"] + max(14, s["width"]*4) + 4
        return (min(xs)-pad, min(ys)-pad, max(xs)+pad, max(ys)+pad)

    def clear_selection(self):
        if self.sel or self.state == ST_DRAW or self.strokes:
            self.state, self.sel, self.drag_sel = ST_SELECT, None, False
            self.strokes, self.cur, self.drawing = [], None, False
            self._set_cursor("crosshair")
            self.get_child().queue_draw()

    # ── drawing ──────────────────────────────────────────────────────────────

    def _bg_for(self, W, H):
        """Background scaled to device resolution exactly once — after this
        every repaint is a plain 1:1 blit (no per-frame rescaling)."""
        dsf = self.get_scale_factor()
        dw, dh = W * dsf, H * dsf
        s = self._bg_scaled
        if s is None or s.get_width() != dw or s.get_height() != dh:
            s = cairo.ImageSurface(cairo.FORMAT_RGB24, dw, dh)
            c = cairo.Context(s)
            c.scale(dw / self.SW, dh / self.SH)
            c.set_source_surface(self.bg, 0, 0)
            c.get_source().set_filter(cairo.FILTER_GOOD)
            c.paint()
            s.set_device_scale(dsf, dsf)
            self._bg_scaled = s
        return s

    def _on_draw(self, widget, cr):
        alloc = widget.get_allocation()
        W, H = alloc.width, alloc.height

        cr.set_source_surface(self._bg_for(W, H), 0, 0)
        cr.get_source().set_filter(cairo.FILTER_NEAREST)
        cr.paint()

        if self.state == ST_SELECT:
            self._draw_select(cr, W, H)
        else:
            self._draw_dim(cr, W, H)
            self._draw_strokes(cr)
            self._draw_toolbar(cr, W, H)

    def _draw_select(self, cr, W, H):
        if self.sel:
            x1, y1, x2, y2 = self.sel
            rx, ry = min(x1, x2), min(y1, y2)
            rw, rh = abs(x2-x1), abs(y2-y1)
            cr.set_source_rgba(0, 0, 0, 0.45)
            for b in [(0, 0, W, ry), (0, ry+rh, W, H-ry-rh),
                      (0, ry, rx, rh), (rx+rw, ry, W-rx-rw, rh)]:
                cr.rectangle(*b)
            cr.fill()
            if rw > 0 and rh > 0:
                cr.set_source_rgba(1, 1, 1, 0.85)
                cr.set_line_width(1.5)
                cr.rectangle(rx+.5, ry+.5, rw-1, rh-1)
                cr.stroke()
                lbl = f"{int(rw*self.SW/W)} × {int(rh*self.SH/H)}"
                cr.set_font_size(12)
                te = cr.text_extents(lbl)
                lx = rx + rw/2 - te.width/2
                ly = ry - 8 if ry > 24 else ry + rh + 18
                cr.set_source_rgba(0, 0, 0, .72)
                cr.rectangle(lx-5, ly-te.height-2, te.width+10, te.height+7)
                cr.fill()
                cr.set_source_rgba(1, 1, 1, 1)
                cr.move_to(lx, ly)
                cr.show_text(lbl)
        else:
            cr.set_source_rgba(0, 0, 0, 0.45)
            cr.rectangle(0, 0, W, H)
            cr.fill()

        if not self.drag_sel and self.app.active is None:
            msg = "Drag to select a region   •   Esc to quit"
            cr.set_font_size(14)
            te = cr.text_extents(msg)
            mx, my = W/2 - te.width/2, H/2
            cr.set_source_rgba(0, 0, 0, .65)
            cr.rectangle(mx-12, my-te.height-4, te.width+24, te.height+12)
            cr.fill()
            cr.set_source_rgba(1, 1, 1, .9)
            cr.move_to(mx, my+4)
            cr.show_text(msg)

    def _draw_dim(self, cr, W, H):
        x1, y1, x2, y2 = self.sel
        rx, ry = min(x1, x2), min(y1, y2)
        rw, rh = abs(x2-x1), abs(y2-y1)
        cr.set_source_rgba(0, 0, 0, 0.55)
        for b in [(0, 0, W, ry), (0, ry+rh, W, H-ry-rh),
                  (0, ry, rx, rh), (rx+rw, ry, W-rx-rw, rh)]:
            cr.rectangle(*b)
        cr.fill()
        cr.set_source_rgba(1, 1, 1, 0.5)
        cr.set_line_width(1)
        cr.rectangle(rx+.5, ry+.5, rw-1, rh-1)
        cr.stroke()

    def _draw_strokes(self, cr):
        for s in self.strokes + ([self.cur] if self.cur else []):
            r, g, b, a = s["color"]
            cr.set_source_rgba(r, g, b, a)
            cr.set_line_width(s["width"])
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.set_line_join(cairo.LINE_JOIN_ROUND)
            pts = s["points"]
            if s["type"] == "pen":
                if len(pts) == 1:
                    cr.arc(pts[0][0], pts[0][1], s["width"]/2, 0, 2*math.pi)
                    cr.fill()
                else:
                    cr.move_to(*pts[0])
                    for p in pts[1:]:
                        cr.line_to(*p)
                    cr.stroke()
            elif s["type"] == "arrow" and len(pts) >= 2:
                p1, p2 = pts[0], pts[-1]
                cr.move_to(*p1); cr.line_to(*p2); cr.stroke()
                for a1, b1 in _arrow_head(p1, p2, s["width"]):
                    cr.move_to(*a1); cr.line_to(*b1); cr.stroke()

    # ── toolbar ──────────────────────────────────────────────────────────────

    def _draw_toolbar(self, cr, W, H):
        self._tb_hits = []
        TH, PAD, TYOFF, SW_R, FONT = 40, 10, 8, 10, 12

        items  = [("tool", "pen", "Pen"), ("tool", "arrow", "Arrow"),
                  ("sep", None, None)]
        items += [("color", rgba, name) for name, rgba in COLORS]
        items += [("sep", None, None)]
        items += [("size", sz, lbl) for sz, lbl in SIZES]
        items += [("sep", None, None),
                  ("action", "undo",   "Undo"),
                  ("action", "resel",  "Reselect"),
                  ("action", "save",   "Save & Copy"),
                  ("action", "cancel", "X")]

        cr.set_font_size(FONT)
        widths = []
        for kind, val, disp in items:
            if kind == "sep":      widths.append(14)
            elif kind == "color":  widths.append(SW_R*2 + 6)
            elif kind == "size":   widths.append(28)
            else:                  widths.append(int(cr.text_extents(disp).width) + 18)

        total_w = PAD*2 + sum(widths)
        tx, ty  = W/2 - total_w/2, TYOFF

        cr.set_source_rgba(0.10, 0.10, 0.10, 0.93)
        _rounded_rect(cr, tx, ty, total_w, TH, 7)
        cr.fill()
        cr.set_source_rgba(1, 1, 1, 0.10)
        _rounded_rect(cr, tx+.5, ty+.5, total_w-1, TH-1, 7)
        cr.stroke()

        cx, midy = tx + PAD, ty + TH/2
        for (kind, val, disp), iw in zip(items, widths):
            if kind == "sep":
                mid = cx + iw/2
                cr.set_source_rgba(1, 1, 1, 0.13)
                cr.set_line_width(1)
                cr.move_to(mid, ty+7); cr.line_to(mid, ty+TH-7); cr.stroke()
                cx += iw
                continue

            if kind == "color":
                if val == self.app.color:
                    cr.set_source_rgba(1, 1, 1, 0.9)
                    cr.arc(cx+SW_R, midy, SW_R+3, 0, 2*math.pi); cr.fill()
                r2, g2, b2, _ = val
                cr.set_source_rgba(r2, g2, b2, 1)
                cr.arc(cx+SW_R, midy, SW_R, 0, 2*math.pi); cr.fill()
                self._tb_hits.append((cx, ty+4, iw, TH-8, ("color", val)))

            elif kind == "size":
                sel = (val == self.app.size)
                dot = min(val/2 + 1, 9)
                cr.set_source_rgba(1, 1, 1, 0.9 if sel else 0.45)
                cr.arc(cx+iw/2, midy, dot, 0, 2*math.pi); cr.fill()
                if sel:
                    cr.set_source_rgba(1, 1, 1, 0.25)
                    cr.arc(cx+iw/2, midy, dot+3, 0, 2*math.pi); cr.stroke()
                self._tb_hits.append((cx, ty+4, iw, TH-8, ("size", val)))

            else:
                sel = (kind == "tool" and val == self.app.tool)
                if sel:
                    cr.set_source_rgba(1, 1, 1, 0.18)
                    _rounded_rect(cr, cx+2, ty+5, iw-4, TH-10, 4); cr.fill()
                elif val == "save":
                    cr.set_source_rgba(0.15, 0.70, 0.30, 0.45)
                    _rounded_rect(cr, cx+2, ty+5, iw-4, TH-10, 4); cr.fill()
                elif val == "cancel":
                    cr.set_source_rgba(0.70, 0.15, 0.15, 0.40)
                    _rounded_rect(cr, cx+2, ty+5, iw-4, TH-10, 4); cr.fill()
                te = cr.text_extents(disp)
                cr.set_source_rgba(1, 1, 1, 1.0 if sel else 0.82)
                cr.move_to(cx + iw/2 - te.width/2,
                           midy - te.y_bearing - te.height/2)
                cr.show_text(disp)
                self._tb_hits.append((cx, ty+3, iw, TH-6, (kind, val)))
            cx += iw

    def _tb_hit(self, mx, my):
        for x, y, w, h, action in self._tb_hits:
            if x <= mx <= x+w and y <= my <= y+h:
                return action
        return None

    # ── input ────────────────────────────────────────────────────────────────

    def _on_press(self, widget, ev):
        if ev.button == 3:
            self.app.on_cancel(self)
            return
        if ev.button != 1:
            return

        if self.state == ST_SELECT:
            self.app.set_active(self)
            self.sel_start = (ev.x, ev.y)
            self.sel       = (ev.x, ev.y, ev.x, ev.y)
            self.drag_sel  = True
            widget.queue_draw()

        elif self.state == ST_DRAW:
            hit = self._tb_hit(ev.x, ev.y)
            if hit:
                kind, val = hit
                if   kind == "color":  self.app.color = val
                elif kind == "size":   self.app.size  = val
                elif kind == "tool":
                    self.app.tool = val
                    self._set_cursor("pencil" if val == "pen" else "default")
                elif kind == "action":
                    if   val == "undo":   self._undo()
                    elif val == "resel":  self.app.reset_all()
                    elif val == "save":   self.app.save(self)
                    elif val == "cancel": self.app.quit("Cancelled.")
                widget.queue_draw()
                return
            self.drawing = True
            self.cur = {"type": self.app.tool, "color": self.app.color,
                        "width": self.app.size, "points": [(ev.x, ev.y)]}
            bb = self._stroke_bbox(self.cur)
            self._queue_rect(bb[0], bb[1], bb[2], bb[3], 0)

    def _on_release(self, widget, ev):
        if ev.button != 1:
            return
        if self.state == ST_SELECT and self.drag_sel:
            self.drag_sel = False
            if self.sel:
                x1, y1, x2, y2 = self.sel
                if abs(x2-x1) > 5 and abs(y2-y1) > 5:
                    self.state = ST_DRAW
                    self._set_cursor("pencil")
                else:
                    self.sel = None
                    self.app.set_active(None)
            widget.queue_draw()
        elif self.state == ST_DRAW and self.drawing:
            self.drawing = False
            if self.cur:
                if self.cur["type"] == "arrow":
                    self.cur["points"] = [self.cur["points"][0], (ev.x, ev.y)]
                bb = self._stroke_bbox(self.cur)
                self.strokes.append(self.cur)
                self.cur = None
                self._queue_rect(bb[0], bb[1], bb[2], bb[3], 0)

    def _queue_outline(self, x1, y1, x2, y2, pad=3):
        """Invalidate just the 4 border strips of a rect (not its interior)."""
        self._queue_rect(x1, y1, x2, y1, pad)   # top
        self._queue_rect(x1, y2, x2, y2, pad)   # bottom
        self._queue_rect(x1, y1, x1, y2, pad)   # left
        self._queue_rect(x2, y1, x2, y2, pad)   # right

    def _on_motion(self, widget, ev):
        if self.state == ST_SELECT and self.drag_sel:
            old = self.sel
            x1, y1 = self.sel_start
            self.sel = (x1, y1, ev.x, ev.y)
            ox2, oy2 = (old[2], old[3]) if old else (ev.x, ev.y)
            # strips where the moving edges swept (dim toggles there)
            ys_all = (y1, oy2, ev.y)
            xs_all = (x1, ox2, ev.x)
            self._queue_rect(min(ox2, ev.x), min(ys_all),
                             max(ox2, ev.x), max(ys_all), 3)      # vertical band
            self._queue_rect(min(xs_all), min(oy2, ev.y),
                             max(xs_all), max(oy2, ev.y), 3)      # horizontal band
            # old + new borders
            self._queue_outline(x1, y1, ox2, oy2)
            self._queue_outline(x1, y1, ev.x, ev.y)
            # size label strip (above/below top edge, moves with centre)
            top = min(y1, oy2, ev.y)
            self._queue_rect(min(xs_all), top - 30, max(xs_all), top + 30, 3)
            bot = max(y1, oy2, ev.y)
            self._queue_rect(min(xs_all), bot - 5, max(xs_all), bot + 30, 3)
        elif self.state == ST_DRAW and self.drawing and self.cur:
            if self.cur["type"] == "pen":
                prev = self.cur["points"][-1]
                self.cur["points"].append((ev.x, ev.y))
                self._queue_rect(prev[0], prev[1], ev.x, ev.y,
                                 self.cur["width"] + 4)
            else:
                ob = self._stroke_bbox(self.cur)
                self.cur["points"] = [self.cur["points"][0], (ev.x, ev.y)]
                nb = self._stroke_bbox(self.cur)
                self._queue_rect(min(ob[0], nb[0]), min(ob[1], nb[1]),
                                 max(ob[2], nb[2]), max(ob[3], nb[3]), 0)

    def _undo(self):
        if self.strokes:
            self.strokes.pop()
            self.get_child().queue_draw()

    # ── export ───────────────────────────────────────────────────────────────

    def render_selection(self) -> Image.Image | None:
        """Crop + annotations composited, in native capture pixels."""
        if not self.sel:
            return None
        sx, sy = self._scale()
        x1, y1, x2, y2 = self.sel
        rx, ry = int(min(x1, x2)*sx), int(min(y1, y2)*sy)
        rw, rh = int(abs(x2-x1)*sx), int(abs(y2-y1)*sy)
        rx = max(0, min(rx, self.SW)); rw = min(rw, self.SW-rx)
        ry = max(0, min(ry, self.SH)); rh = min(rh, self.SH-ry)
        if rw <= 0 or rh <= 0:
            return None

        out = self.img.crop((rx, ry, rx+rw, ry+rh))
        if not self.strokes:
            return out.convert("RGB")

        ov   = Image.new("RGBA", (rw, rh), (0, 0, 0, 0))
        draw = ImageDraw.Draw(ov, "RGBA")
        for s in self.strokes:
            r2, g2, b2, a2 = s["color"]
            col = (int(r2*255), int(g2*255), int(b2*255), int(a2*255))
            w   = max(1, int(s["width"] * sx))
            pts = [(int(px*sx)-rx, int(py*sy)-ry) for px, py in s["points"]]
            if s["type"] == "pen":
                if len(pts) == 1:
                    x, y = pts[0]
                    draw.ellipse([x-w//2, y-w//2, x+w//2, y+w//2], fill=col)
                else:
                    draw.line(pts, fill=col, width=w, joint="curve")
                    for x, y in (pts[0], pts[-1]):
                        draw.ellipse([x-w//2, y-w//2, x+w//2, y+w//2], fill=col)
            elif s["type"] == "arrow" and len(pts) >= 2:
                p1, p2 = pts[0], pts[-1]
                draw.line([p1, p2], fill=col, width=w)
                dx, dy = p2[0]-p1[0], p2[1]-p1[1]
                L = math.hypot(dx, dy)
                if L > 1:
                    ux, uy = dx/L, dy/L
                    hl, ang = max(14, w*4), 0.42
                    ca, sa = math.cos(ang), math.sin(ang)
                    for sign in (+1, -1):
                        hx = -(ca*ux - sign*sa*uy)
                        hy = -(ca*uy + sign*sa*ux)
                        draw.line([p2, (int(p2[0]+hx*hl), int(p2[1]+hy*hl))],
                                  fill=col, width=w)
        return Image.alpha_composite(out, ov).convert("RGB")


# ─── app controller ───────────────────────────────────────────────────────────

class App:

    def __init__(self, capture: Image.Image, on_done=None):
        self.tool, self.color, self.size = "pen", COLORS[0][1], 4
        self.active: Overlay | None = None
        self.on_done = on_done

        display = Gdk.Display.get_default()
        mons = [display.get_monitor(i) for i in range(display.get_n_monitors())]
        geos = [m.get_geometry() for m in mons]

        # uniform scale factor between GTK logical layout and capture pixels
        lay_w = max(g.x + g.width  for g in geos)
        lay_h = max(g.y + g.height for g in geos)
        F = max(capture.width / lay_w, capture.height / lay_h)

        self.windows: list[Overlay] = []
        for i, g in enumerate(geos):
            x, y = round(g.x * F), round(g.y * F)
            w, h = round(g.width * F), round(g.height * F)
            x = max(0, min(x, capture.width));  w = min(w, capture.width - x)
            y = max(0, min(y, capture.height)); h = min(h, capture.height - y)
            if w <= 0 or h <= 0:
                continue
            self.windows.append(Overlay(self, i, capture.crop((x, y, x+w, y+h))))

    # ── coordination ─────────────────────────────────────────────────────────

    def set_active(self, win: Overlay | None):
        if self.active is win:
            return
        self.active = win
        for w in self.windows:
            if w is not win:
                w.clear_selection()

    def reset_all(self):
        self.active = None
        for w in self.windows:
            w.clear_selection()
            w.get_child().queue_draw()

    def on_cancel(self, win: Overlay):
        if win.state == ST_DRAW:
            self.reset_all()
        else:
            self.quit("Cancelled.")

    def quit(self, msg=None):
        if msg:
            print(msg)
        for w in self.windows:
            w.destroy()
        self.windows = []
        if self.on_done:
            self.on_done()
        else:
            Gtk.main_quit()

    # ── keys (connected on every window) ─────────────────────────────────────

    def on_key(self, widget, ev):
        k     = ev.keyval
        ctrl  = bool(ev.state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(ev.state & Gdk.ModifierType.SHIFT_MASK)
        act   = self.active if (self.active and self.active.state == ST_DRAW) else None

        if k == Gdk.KEY_Escape:
            if act:
                self.reset_all()
            else:
                self.quit("Cancelled.")
        elif k in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and act:
            self.save(act)
        elif ctrl and k in (Gdk.KEY_c, Gdk.KEY_C, Gdk.KEY_s, Gdk.KEY_S) and act:
            self.save(act, path_only=shift)
        elif ctrl and k in (Gdk.KEY_z, Gdk.KEY_Z) and act:
            act._undo()
        return True

    # ── save ─────────────────────────────────────────────────────────────────

    def save(self, win: Overlay, path_only=False):
        img = win.render_selection()
        if img is None:
            return
        os.makedirs(SAVE_DIR, exist_ok=True)
        ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(SAVE_DIR, f"shotty_{ts}.png")
        img.save(path, "PNG", compress_level=3)

        if path_only:
            subprocess.Popen(["wl-copy"], stdin=subprocess.PIPE)\
                      .communicate(path.encode())
            print(f"✓ Saved  → {path}\n✓ Copied → clipboard (path)")
        else:
            with open(path, "rb") as f:
                subprocess.Popen(["wl-copy", "--type", "image/png"],
                                 stdin=subprocess.PIPE).communicate(f.read())
            print(f"✓ Saved  → {path}\n✓ Copied → clipboard (image)")
        self.quit()


# ─── entry point ──────────────────────────────────────────────────────────────

class Daemon:
    """Stays resident so subsequent hotkey presses are instant."""

    def __init__(self):
        self.session: App | None = None
        try:
            os.unlink(SOCK)
        except OSError:
            pass
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.bind(SOCK)
        self.sock.listen(4)
        GLib.io_add_watch(self.sock.fileno(), GLib.IO_IN, self._on_conn)

    def _on_conn(self, fd, cond):
        conn, _ = self.sock.accept()
        conn.close()
        self.trigger()
        return True

    def trigger(self):
        if self.session:            # already showing an overlay
            return
        capture = kwin_screenshot() or portal_screenshot()
        if capture is None:
            print("ERROR: screenshot failed.", file=sys.stderr)
            return
        session = App(capture, on_done=self._done)
        if session.windows:
            self.session = session

    def _done(self):
        self.session = None


def main():
    GLib.set_prgname("shotty")
    Gtk.Window.set_default_icon_name("shotty")
    daemon = Daemon()
    if "--daemon" not in sys.argv:
        # launched by hotkey with no daemon running: take the shot now,
        # then stay resident for instant next launches
        daemon.trigger()
    Gtk.main()


if __name__ == "__main__":
    main()
