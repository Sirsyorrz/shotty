#!/usr/bin/env bash
# shotty install script
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── deps ──────────────────────────────────────────────────────────────────────
echo "→ Checking dependencies..."
missing=()
command -v grim  &>/dev/null || missing+=(grim)
command -v slurp &>/dev/null || missing+=(slurp)

if [[ ${#missing[@]} -gt 0 ]]; then
    echo "  Installing: ${missing[*]}"
    sudo pacman -S --noconfirm "${missing[@]}"
fi

python3 -c "from PIL import Image" 2>/dev/null || {
    echo "  Installing python-pillow..."
    sudo pacman -S --noconfirm python-pillow
}

echo "  ✓ Dependencies OK"

# ── symlink to PATH ───────────────────────────────────────────────────────────
mkdir -p "$HOME/.local/bin"
ln -sf "$SCRIPT_DIR/shotty.py" "$HOME/.local/bin/shotty"
chmod +x "$SCRIPT_DIR/shotty.py"
echo "  ✓ Symlinked → ~/.local/bin/shotty"

ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
mkdir -p "$ICON_DIR"
cp "$SCRIPT_DIR/shotty.svg" "$ICON_DIR/shotty.svg"
echo "  ✓ Icon installed"

# ── .desktop file ─────────────────────────────────────────────────────────────
DESKTOP_DIR="$HOME/.local/share/applications"
mkdir -p "$DESKTOP_DIR"
# NOTE: Exec must start with the real python binary — KWin authorizes the
# ScreenShot2 fast path by matching /proc/pid/exe against desktop Exec lines.
cat > "$DESKTOP_DIR/shotty.desktop" <<EOF
[Desktop Entry]
Name=shotty
Comment=Region screenshot with annotation
Exec=/usr/bin/python3 $SCRIPT_DIR/shotty.py
Icon=shotty
StartupWMClass=shotty
Type=Application
Categories=Graphics;
Keywords=screenshot;region;capture;
X-KDE-DBUS-Restricted-Interfaces=org.kde.KWin.ScreenShot2
EOF
echo "  ✓ Desktop entry written"

# ── autostart daemon (instant hotkey response) ────────────────────────────
mkdir -p "$HOME/.config/autostart"
cat > "$HOME/.config/autostart/shotty.desktop" <<EOF
[Desktop Entry]
Name=shotty daemon
Icon=shotty
Exec=/usr/bin/python3 $SCRIPT_DIR/shotty.py --daemon
Type=Application
X-KDE-DBUS-Restricted-Interfaces=org.kde.KWin.ScreenShot2
EOF
kbuildsycoca6 2>/dev/null || true
echo "  ✓ Autostart daemon entry written"

# ── screenshots dir ───────────────────────────────────────────────────────────
mkdir -p "$HOME/Pictures/Screenshots"
echo "  ✓ Save dir: ~/Pictures/Screenshots"

echo ""
echo "✓ shotty installed!"
echo ""
echo "  Bind a shortcut to:  shotty"
echo "  e.g. in Hyprland:    bind = SUPER SHIFT, S, exec, shotty"
echo "       in KDE:          System Settings → Shortcuts → Custom Shortcut"
echo "       in GNOME:        Settings → Keyboard → Custom Shortcuts"
