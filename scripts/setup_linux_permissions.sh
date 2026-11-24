#!/usr/bin/env bash
# Sirius Linux (X11 & Wayland) Permissions & Optional System Dependencies Helper
set -euo pipefail

echo "=== Sirius Linux Environment & Permissions Setup ==="
echo "0. Native Audio & Tauri v2 Desktop Build System Packages (Ubuntu 24.04 / Debian):"
echo "   sudo apt update && sudo apt install -y libwebkit2gtk-4.1-dev libgtk-3-dev libayatana-appindicator3-dev librsvg2-dev libssl-dev patchelf libportaudio2 espeak-ng wl-clipboard xclip ydotool"
echo ""
echo "1. Checking AT-SPI2 Desktop Accessibility bus..."
gsettings set org.gnome.desktop.interface toolkit-accessibility true 2>/dev/null || true

if [[ "${XDG_SESSION_TYPE:-}" == "wayland" ]]; then
  echo "2. Wayland session detected: configuring /dev/uinput access for smooth cursor/keyboard synthesis..."
  if [[ ! -w /dev/uinput ]]; then
    echo "   Run the following once with sudo to enable non-root Wayland uinput:"
    echo "   sudo usermod -aG input \"\$USER\""
    echo "   echo 'KERNEL==\"uinput\", GROUP=\"input\", MODE=\"0660\", OPTIONS+=\"static_node=uinput\"' | sudo tee /etc/udev/rules.d/80-sirius-uinput.rules"
    echo "   sudo udevadm control --reload-rules && sudo udevadm trigger"
  else
    echo "   /dev/uinput is already writable!"
  fi
fi

echo "3. Running sirius --doctor check..."
cd "$(dirname "$0")/../backend"
uv run sirius --doctor
