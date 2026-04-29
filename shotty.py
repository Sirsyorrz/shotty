#!/usr/bin/env python3
"""
shotty — single-window screenshot
  1. Portal grabs full screen (KDE Wayland)
  2. Fullscreen overlay: drag to select region
  3. Region locked → floating toolbar appears, draw freely (pen / arrow)
  4. Enter  → saves ~/Pictures/Screenshots/shotty_*.png + clipboard
  5. Esc    → back to selection (or quit if already in selection)
"""

import dbus
import dbus.mainloop.glib
dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)  # must be before any GLib/GTK init

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, GdkPixbuf, GLib

import cairo
import subprocess, sys, os, io, datetime, math
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

ST_SELECT = 0   # dragging to choose region
ST_DRAW   = 1   # region locked, drawing


# ─── portal screenshot ────────────────────────────────────────────────────────

def portal_screenshot() -> str | None:
    bus    = dbus.SessionBus()
    portal = dbus.Interface(
        bus.get_object("org.freedesktop.portal.Desktop",
                       "/org/freedesktop/portal/desktop"),
        "org.freedesktop.portal.Screenshot",
    )
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
    return urlparse(str(uri[0])).path if uri[0] else None


# ─── helpers ──────────────────────────────────────────────────────────────────

def _rounded_rect(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x+w-r, y+r,   r,  -math.pi/2, 0)
    cr.arc(x+w-r, y+h-r, r,  0,           math.pi/2)
    cr.arc(x+r,   y+h-r, r,  math.pi/2,  math.pi)
    cr.arc(x+r,   y+r,   r,  math.pi,    3*math.pi/2)
    cr.close_path()

def _arrow_lines(p1, p2, width):
    """Return list of (a, b) line segments for the arrowhead."""
    dx, dy = p2[0]-p1[0], p2[1]-p1[1]
    L = math.hypot(dx, dy)
    if L < 1:
        return []
    ux, uy = dx/L, dy/L
    hl  = max(14, width * 4)
    ang = 0.42
    ca, sa = math.cos(ang), math.sin(ang)
    segs = []
    for sign in (+1, -1):
        rx = -(ca*ux - sign*sa*uy)
        ry = -(ca*uy + sign*sa*ux)
        segs.append((p2, (p2[0]+rx*hl, p2[1]+ry*hl)))
    return segs


# ─── main window ─────────────────────────────────────────────────────────────

class Shotty(Gtk.Window):

    def __init__(self, pixbuf: GdkPixbuf.Pixbuf):
        super().__init__()
        self.pixbuf = pixbuf
        self.SW = pixbuf.get_width()
        self.SH = pixbuf.get_height()

        self.state        = ST_SELECT
        self.sel          = None          # (x1,y1,x2,y2) screen coords
        self.sel_start    = None
        self.drag_sel     = False

        self.strokes: list[dict] = []
        self.cur:     dict|None  = None
        self.drawing  = False

        self.tool  = "pen"            # "pen" | "arrow"
        self.color = COLORS[0][1]     # red
        self.size  = 4

        self._tb_hits: list[tuple] = []   # (x,y,w,h, action_tuple)

        self._build()

    # ── window setup ──────────────────────────────────────────────────────────

    def _build(self):
        self.set_decorated(False)
        self.set_app_paintable(True)
        self.fullscreen()
        self.set_keep_above(True)
        vis = self.get_screen().get_rgba_visual()
        if vis:
            self.set_visual(vis)

        da = Gtk.DrawingArea()
        da.connect("draw",                   self._on_draw)
        da.connect("button-press-event",     self._on_press)
        da.connect("button-release-event",   self._on_release)
        da.connect("motion-notify-event",    self._on_motion)
        da.add_events(Gdk.EventMask.BUTTON_PRESS_MASK |
                      Gdk.EventMask.BUTTON_RELEASE_MASK |
                      Gdk.EventMask.POINTER_MOTION_MASK)
        self.add(da)
        self.connect("key-press-event", self._on_key)
        self.show_all()
        self._set_cursor("crosshair")

    def _set_cursor(self, name: str):
        win = self.get_window()
        if win:
            win.set_cursor(Gdk.Cursor.new_from_name(self.get_display(), name))

    # ── scale helpers ─────────────────────────────────────────────────────────

    def _scale(self):
        alloc = self.get_child().get_allocation()
        return self.SW / alloc.width, self.SH / alloc.height

    def _sel_px(self):
        """Selection in screenshot pixel coords."""
        if not self.sel:
            return None
        sx, sy = self._scale()
        x1, y1, x2, y2 = self.sel
        rx, ry = int(min(x1,x2)*sx), int(min(y1,y2)*sy)
        rw, rh = int(abs(x2-x1)*sx), int(abs(y2-y1)*sy)
        rx = max(0, min(rx, self.SW));  rw = min(rw, self.SW-rx)
        ry = max(0, min(ry, self.SH));  rh = min(rh, self.SH-ry)
        return rx, ry, rw, rh

    # ── draw ──────────────────────────────────────────────────────────────────

    def _on_draw(self, widget, cr: cairo.Context):
        alloc = widget.get_allocation()
        W, H  = alloc.width, alloc.height
        scx   = W / self.SW
        scy   = H / self.SH

        # screenshot background
        cr.save()
        cr.scale(scx, scy)
        Gdk.cairo_set_source_pixbuf(cr, self.pixbuf, 0, 0)
        cr.paint()
        cr.restore()

        if self.state == ST_SELECT:
            self._draw_select_overlay(cr, W, H, scx, scy)
        else:
            self._draw_dim_outside(cr, W, H)
            self._draw_strokes(cr)
            self._draw_toolbar(cr, W, H)

    # ── selection overlay ─────────────────────────────────────────────────────

    def _draw_select_overlay(self, cr, W, H, scx, scy):
        cr.set_source_rgba(0, 0, 0, 0.48)
        cr.rectangle(0, 0, W, H)
        cr.fill()

        if self.sel:
            x1, y1, x2, y2 = self.sel
            rx, ry = min(x1,x2), min(y1,y2)
            rw, rh = abs(x2-x1), abs(y2-y1)
            if rw > 0 and rh > 0:
                # re-expose selection area
                cr.save()
                cr.rectangle(rx, ry, rw, rh)
                cr.clip()
                cr.scale(scx, scy)
                Gdk.cairo_set_source_pixbuf(cr, self.pixbuf, 0, 0)
                cr.paint()
                cr.restore()
                # border
                cr.set_source_rgba(1, 1, 1, 0.85)
                cr.set_line_width(1.5)
                cr.rectangle(rx+.5, ry+.5, rw-1, rh-1)
                cr.stroke()
                # size label
                lbl = f"{int(rw/scx)} × {int(rh/scy)}"
                cr.set_font_size(12)
                te  = cr.text_extents(lbl)
                lx  = rx + rw/2 - te.width/2
                ly  = ry - 8 if ry > 24 else ry + rh + 18
                cr.set_source_rgba(0, 0, 0, .72)
                cr.rectangle(lx-5, ly-te.height-2, te.width+10, te.height+7)
                cr.fill()
                cr.set_source_rgba(1, 1, 1, 1)
                cr.move_to(lx, ly)
                cr.show_text(lbl)

        if not self.drag_sel:
            msg = "Drag to select a region   •   Esc to quit"  # noqa: E501
            cr.set_font_size(14)
            te  = cr.text_extents(msg)
            mx, my = W/2 - te.width/2, H/2
            cr.set_source_rgba(0, 0, 0, .65)
            cr.rectangle(mx-12, my-te.height-4, te.width+24, te.height+12)
            cr.fill()
            cr.set_source_rgba(1, 1, 1, .9)
            cr.move_to(mx, my+4)
            cr.show_text(msg)

    # ── draw-mode: dim outside selection ─────────────────────────────────────

    def _draw_dim_outside(self, cr, W, H):
        if not self.sel:
            return
        x1, y1, x2, y2 = self.sel
        rx, ry = min(x1,x2), min(y1,y2)
        rw, rh = abs(x2-x1), abs(y2-y1)
        a = 0.55
        cr.set_source_rgba(0, 0, 0, a)
        for bx, by, bw, bh in [
            (0,  0,   W,  ry),
            (0,  ry+rh, W, H-ry-rh),
            (0,  ry,   rx, rh),
            (rx+rw, ry, W-rx-rw, rh),
        ]:
            cr.rectangle(bx, by, bw, bh)
            cr.fill()
        cr.set_source_rgba(1, 1, 1, 0.5)
        cr.set_line_width(1)
        cr.rectangle(rx+.5, ry+.5, rw-1, rh-1)
        cr.stroke()

    # ── strokes ───────────────────────────────────────────────────────────────

    def _draw_strokes(self, cr: cairo.Context):
        for s in self.strokes + ([self.cur] if self.cur else []):
            if not s:
                continue
            r, g, b, a = s["color"]
            cr.set_source_rgba(r, g, b, a)
            cr.set_line_width(s["width"])
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.set_line_join(cairo.LINE_JOIN_ROUND)
            pts = s["points"]

            if s["type"] == "pen":
                if len(pts) == 1:
                    x, y = pts[0]
                    cr.arc(x, y, s["width"]/2, 0, 2*math.pi)
                    cr.fill()
                elif len(pts) > 1:
                    cr.move_to(*pts[0])
                    for p in pts[1:]:
                        cr.line_to(*p)
                    cr.stroke()

            elif s["type"] == "arrow" and len(pts) >= 2:
                p1, p2 = pts[0], pts[-1]
                cr.move_to(*p1)
                cr.line_to(*p2)
                cr.stroke()
                for seg_a, seg_b in _arrow_lines(p1, p2, s["width"]):
                    cr.move_to(*seg_a)
                    cr.line_to(*seg_b)
                    cr.stroke()

    # ── toolbar (drawn with Cairo, hit-tested manually) ───────────────────────

    def _draw_toolbar(self, cr: cairo.Context, W, H):
        self._tb_hits = []

        TH   = 40          # toolbar height
        PAD  = 10          # inner horizontal padding
        GAP  = 6           # gap between items
        TYOFF = 8          # distance from top of screen
        SW_R = 10          # colour swatch radius
        FONT = 12

        # ── collect items ─────────────────────────────────────────────────
        # Each item: (kind, value, display)
        # kind: "tool" | "color" | "size" | "sep" | "action"
        items = []
        items += [("tool",  "pen",   "Pen"),
                  ("tool",  "arrow", "Arrow")]
        items.append(("sep", None, None))
        items += [("color", rgba, name) for name, rgba in COLORS]
        items.append(("sep", None, None))
        items += [("size",  sz,   lbl)  for sz, lbl in SIZES]
        items.append(("sep", None, None))
        items += [("action", "undo",   "Undo"),
                  ("action", "resel",  "Reselect"),
                  ("action", "save",   "Save & Copy"),
                  ("action", "cancel", "X")]

        cr.set_font_size(FONT)

        # ── measure each item ─────────────────────────────────────────────
        widths = []
        for kind, val, disp in items:
            if kind == "sep":
                widths.append(14)
            elif kind == "color":
                widths.append(SW_R*2 + GAP)
            elif kind == "size":
                widths.append(28)
            else:  # tool / action
                te = cr.text_extents(disp)
                widths.append(int(te.width) + 18)

        total_w = PAD*2 + sum(widths)
        tx = W/2 - total_w/2
        ty = TYOFF

        # ── toolbar background ────────────────────────────────────────────
        cr.set_source_rgba(0.10, 0.10, 0.10, 0.93)
        _rounded_rect(cr, tx, ty, total_w, TH, 7)
        cr.fill()
        cr.set_source_rgba(1, 1, 1, 0.10)
        _rounded_rect(cr, tx+.5, ty+.5, total_w-1, TH-1, 7)
        cr.stroke()

        # ── draw items ────────────────────────────────────────────────────
        cx   = tx + PAD
        midy = ty + TH / 2

        for (kind, val, disp), iw in zip(items, widths):

            if kind == "sep":
                mid = cx + iw/2
                cr.set_source_rgba(1, 1, 1, 0.13)
                cr.set_line_width(1)
                cr.move_to(mid, ty+7)
                cr.line_to(mid, ty+TH-7)
                cr.stroke()
                cx += iw
                continue

            # bounding box for hit test
            bx, bw = cx, iw

            if kind == "color":
                sel = (val == self.color)
                if sel:
                    cr.set_source_rgba(1, 1, 1, 0.9)
                    cr.arc(cx + SW_R, midy, SW_R+3, 0, 2*math.pi)
                    cr.fill()
                r2, g2, b2, _ = val
                cr.set_source_rgba(r2, g2, b2, 1)
                cr.arc(cx + SW_R, midy, SW_R, 0, 2*math.pi)
                cr.fill()
                self._tb_hits.append((bx, ty+4, bw, TH-8, ("color", val)))

            elif kind == "size":
                sel  = (val == self.size)
                dot  = min(val/2 + 1, 9)
                cr.set_source_rgba(1, 1, 1, 0.9 if sel else 0.45)
                cr.arc(cx + iw/2, midy, dot, 0, 2*math.pi)
                cr.fill()
                if sel:
                    cr.set_source_rgba(1, 1, 1, 0.25)
                    cr.arc(cx + iw/2, midy, dot+3, 0, 2*math.pi)
                    cr.stroke()
                self._tb_hits.append((bx, ty+4, bw, TH-8, ("size", val)))

            else:  # tool / action
                sel = (kind == "tool" and val == self.tool)
                # button tint
                if sel:
                    cr.set_source_rgba(1, 1, 1, 0.18)
                    _rounded_rect(cr, cx+2, ty+5, iw-4, TH-10, 4)
                    cr.fill()
                elif kind == "action" and val == "save":
                    cr.set_source_rgba(0.15, 0.70, 0.30, 0.45)
                    _rounded_rect(cr, cx+2, ty+5, iw-4, TH-10, 4)
                    cr.fill()
                elif kind == "action" and val == "cancel":
                    cr.set_source_rgba(0.70, 0.15, 0.15, 0.40)
                    _rounded_rect(cr, cx+2, ty+5, iw-4, TH-10, 4)
                    cr.fill()

                te   = cr.text_extents(disp)
                cr.set_font_size(FONT)
                cr.set_source_rgba(1, 1, 1, 1.0 if sel else 0.82)
                cr.move_to(cx + iw/2 - te.width/2,
                           midy - te.y_bearing - te.height/2)
                cr.show_text(disp)
                self._tb_hits.append((bx, ty+3, bw, TH-6, (kind, val)))

            cx += iw

    def _tb_hit(self, mx, my):
        for x, y, w, h, action in self._tb_hits:
            if x <= mx <= x+w and y <= my <= y+h:
                return action
        return None

    # ── input ─────────────────────────────────────────────────────────────────

    def _on_press(self, widget, ev):
        if ev.button == 3:
            self._do_cancel()
            return
        if ev.button != 1:
            return

        if self.state == ST_SELECT:
            self.sel_start = (ev.x, ev.y)
            self.sel       = (ev.x, ev.y, ev.x, ev.y)
            self.drag_sel  = True

        elif self.state == ST_DRAW:
            hit = self._tb_hit(ev.x, ev.y)
            if hit:
                kind, val = hit
                if   kind == "color":  self.color = val
                elif kind == "size":   self.size  = val
                elif kind == "tool":
                    self.tool = val
                    self._set_cursor("pencil" if val == "pen" else "default")
                elif kind == "action":
                    if   val == "undo":   self._do_undo()
                    elif val == "resel":  self._do_resel()
                    elif val == "save":   self._do_save()
                    elif val == "cancel": print("Cancelled."); Gtk.main_quit()
                self.get_child().queue_draw()
                return

            # start a stroke
            self.drawing = True
            self.cur = {
                "type":   self.tool,
                "color":  self.color,
                "width":  self.size,
                "points": [(ev.x, ev.y)],
            }

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
            self.get_child().queue_draw()

        elif self.state == ST_DRAW and self.drawing:
            self.drawing = False
            if self.cur:
                if self.tool == "arrow":
                    self.cur["points"] = [self.cur["points"][0], (ev.x, ev.y)]
                self.strokes.append(self.cur)
                self.cur = None
            self.get_child().queue_draw()

    def _on_motion(self, widget, ev):
        if self.state == ST_SELECT and self.drag_sel:
            x1, y1 = self.sel_start
            self.sel = (x1, y1, ev.x, ev.y)
            widget.queue_draw()

        elif self.state == ST_DRAW and self.drawing and self.cur:
            if self.tool == "pen":
                self.cur["points"].append((ev.x, ev.y))
            else:   # arrow: keep only start + current end
                self.cur["points"] = [self.cur["points"][0], (ev.x, ev.y)]
            widget.queue_draw()

    def _on_key(self, widget, ev):
        k    = ev.keyval
        ctrl  = bool(ev.state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(ev.state & Gdk.ModifierType.SHIFT_MASK)
        if k in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if self.state == ST_DRAW:
                self._do_save()
        elif k == Gdk.KEY_Escape:
            print("Cancelled.")
            Gtk.main_quit()
        elif ctrl and shift and k in (Gdk.KEY_S, Gdk.KEY_s, Gdk.KEY_C, Gdk.KEY_c):
            if self.state == ST_DRAW:
                self._do_save(path_only=True)
        elif ctrl and k in (Gdk.KEY_s, Gdk.KEY_S):
            if self.state == ST_DRAW:
                self._do_save()
        elif ctrl and k in (Gdk.KEY_c, Gdk.KEY_C):
            if self.state == ST_DRAW:
                self._do_save()
        elif ctrl and k in (Gdk.KEY_z, Gdk.KEY_Z):
            self._do_undo()

    # ── actions ───────────────────────────────────────────────────────────────

    def _do_undo(self):
        if self.strokes:
            self.strokes.pop()
            self.get_child().queue_draw()

    def _do_resel(self):
        """Go back to region selection, keep annotations."""
        self.state    = ST_SELECT
        self.sel      = None
        self.strokes  = []
        self.cur      = None
        self.drag_sel = False
        self._set_cursor("crosshair")
        self.get_child().queue_draw()

    def _do_cancel(self):
        if self.state == ST_DRAW:
            self._do_resel()
        else:
            print("Cancelled.")
            Gtk.main_quit()

    def _do_save(self, path_only=False):
        coords = self._sel_px()
        if not coords:
            return
        rx, ry, rw, rh = coords
        if rw <= 0 or rh <= 0:
            return

        sx, sy = self._scale()

        # Build base image from pixbuf
        data = self.pixbuf.get_pixels()
        mode = "RGBA" if self.pixbuf.get_has_alpha() else "RGB"
        full = Image.frombytes(mode, (self.SW, self.SH), data,
                               "raw", mode, self.pixbuf.get_rowstride()).convert("RGBA")

        # Render annotation strokes onto full-resolution image
        ov   = Image.new("RGBA", (self.SW, self.SH), (0, 0, 0, 0))
        draw = ImageDraw.Draw(ov, "RGBA")

        for s in self.strokes:
            r2, g2, b2, a2 = s["color"]
            col = (int(r2*255), int(g2*255), int(b2*255), int(a2*255))
            w   = s["width"]
            pts = [(int(px*sx), int(py*sy)) for px, py in s["points"]]

            if s["type"] == "pen":
                if len(pts) == 1:
                    x, y = pts[0]
                    draw.ellipse([x-w//2, y-w//2, x+w//2, y+w//2], fill=col)
                else:
                    for i in range(len(pts)-1):
                        draw.line([pts[i], pts[i+1]], fill=col, width=w)
                    for x, y in pts:
                        draw.ellipse([x-w//2, y-w//2, x+w//2, y+w//2], fill=col)

            elif s["type"] == "arrow" and len(pts) >= 2:
                p1, p2 = pts[0], pts[-1]
                draw.line([p1, p2], fill=col, width=w)
                dx2, dy2 = p2[0]-p1[0], p2[1]-p1[1]
                L = math.hypot(dx2, dy2)
                if L > 1:
                    ux, uy = dx2/L, dy2/L
                    hl = max(14, w*4)
                    ang = 0.42
                    ca, sa = math.cos(ang), math.sin(ang)
                    for sign in (+1, -1):
                        hx = -(ca*ux - sign*sa*uy)
                        hy = -(ca*uy + sign*sa*ux)
                        ep = (int(p2[0]+hx*hl), int(p2[1]+hy*hl))
                        draw.line([p2, ep], fill=col, width=w)

        final   = Image.alpha_composite(full, ov)
        cropped = final.crop((rx, ry, rx+rw, ry+rh)).convert("RGB")

        # Save
        os.makedirs(SAVE_DIR, exist_ok=True)
        ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(SAVE_DIR, f"shotty_{ts}.png")
        cropped.save(path, "PNG")

        if path_only:
            # Copy the file path as plain text
            proc = subprocess.Popen(["wl-copy"],
                                    stdin=subprocess.PIPE)
            proc.communicate(path.encode())
            print(f"✓ Saved  → {path}")
            print( "✓ Copied → clipboard (path)")
        else:
            # Copy the image itself
            buf = io.BytesIO()
            cropped.save(buf, "PNG")
            buf.seek(0)
            proc = subprocess.Popen(["wl-copy", "--type", "image/png"],
                                    stdin=subprocess.PIPE)
            proc.communicate(buf.getvalue())
            print(f"✓ Saved  → {path}")
            print( "✓ Copied → clipboard (image)")
        Gtk.main_quit()


# ─── entry point ──────────────────────────────────────────────────────────────

def main():
    print("Taking screenshot via portal…")
    path = portal_screenshot()
    if not path or not os.path.exists(path):
        print("ERROR: portal screenshot failed or was cancelled.", file=sys.stderr)
        sys.exit(1)

    pixbuf = GdkPixbuf.Pixbuf.new_from_file(path)

    win = Shotty(pixbuf)
    win.connect("destroy", Gtk.main_quit)
    Gtk.main()

    try:
        os.unlink(path)
    except OSError:
        pass


if __name__ == "__main__":
    main()
