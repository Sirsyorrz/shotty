# shotty

Lightweight Wayland region screenshot tool with annotation support.

## Install

```sh
bash install.sh
```

Bind a shortcut:

**Hyprland** (`~/.config/hypr/hyprland.conf`):
```
bind = SUPER SHIFT, S, exec, shotty
```

**KDE Plasma** — Settings → Shortcuts → Add Command → `shotty`

**GNOME** — Settings → Keyboard → Custom Shortcuts → Command: `shotty`

**Sway** (`~/.config/sway/config`):
```
bindsym $mod+Shift+s exec shotty
```

## Usage

1. Run `shotty` — drag to select a region
2. Annotate with pen or arrow tool
3. Save when done

### Keybindings

| Key | Action |
|-----|--------|
| `Enter` / `Ctrl+S` / `Ctrl+C` | Save file + copy image to clipboard |
| `Ctrl+Shift+S` / `Ctrl+Shift+C` | Save file + copy file path to clipboard |
| `Ctrl+Z` | Undo last stroke |
| `Esc` | Quit |

### Toolbar

- **Pen / Arrow** — drawing tools
- **Color swatches** — Red, Orange, Yellow, Green, Blue, White, Black
- **Brush sizes** — S / M / L / XL
- **Undo / Reselect / Save & Copy / X**

Screenshots are saved to `~/Pictures/Screenshots/`.

## Dependencies

- `python-pillow`, `python-gobject`, GTK3, `wl-clipboard`

All installed automatically by `install.sh`.
