#!/bin/sh
# Installs the udev rule (needs sudo) and a menu entry for the current user.
set -e
here=$(cd "$(dirname "$0")" && pwd)

sudo install -m 644 "$here/70-hxflight.rules" /etc/udev/rules.d/70-hxflight.rules
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=hidraw

chmod +x "$here/hxflight.py"
mkdir -p "$HOME/.local/share/applications"
cat > "$HOME/.local/share/applications/hxflight.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=HyperX Cloud Flight
Comment=Battery, microphone and volume control for the HyperX Cloud Flight
Icon=audio-headset
Exec=$here/hxflight.py
Categories=AudioVideo;Audio;Settings;
EOF

echo "Done. Start it from the menu or run: $here/hxflight.py"
