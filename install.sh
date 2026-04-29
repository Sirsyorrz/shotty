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

# ── .desktop file ─────────────────────────────────────────────────────────────
DESKTOP_DIR="$HOME/.local/share/applications"
mkdir -p "$DESKTOP_DIR"
cat > "$DESKTOP_DIR/shotty.desktop" <<EOF
[Desktop Entry]
Name=shotty
Comment=Region screenshot with annotation
Exec=$HOME/.local/bin/shotty
Icon=accessories-screenshot
Type=Application
Categories=Graphics;
Keywords=screenshot;region;capture;
NoDisplay=true
EOF
echo "  ✓ Desktop entry written"

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
