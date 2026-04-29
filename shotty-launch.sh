#!/bin/bash
LOG=/tmp/shotty.log
exec > "$LOG" 2>&1

echo "=== $(date) ==="
echo "USER=$USER"
echo "DBUS_SESSION_BUS_ADDRESS=$DBUS_SESSION_BUS_ADDRESS"
echo "WAYLAND_DISPLAY=$WAYLAND_DISPLAY"
echo "XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR"
echo "GDK_BACKEND=$GDK_BACKEND"

export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$(id -u)/bus}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
export GDK_BACKEND=wayland

echo "--- running ---"
exec /usr/bin/python3 /home/sirs/Documents/GitHub/shotty/shotty.py
