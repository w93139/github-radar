#!/bin/zsh
set -euo pipefail

PLUGIN_ROOT="${0:A:h:h}"
APP_NAME="GitHub Radar.app"
OUTPUT_DIR="$PLUGIN_ROOT/dist"
APP_DIR="$OUTPUT_DIR/$APP_NAME"
CONTENTS="$APP_DIR/Contents"
RESOURCES="$CONTENTS/Resources"
PAYLOAD="$RESOURCES/GitHubRadar"
ICONSET="$OUTPUT_DIR/AppIcon.iconset"

rm -rf "$APP_DIR" "$ICONSET"
mkdir -p "$CONTENTS/MacOS" "$RESOURCES" "$PAYLOAD/scripts" "$PAYLOAD/assets" "$ICONSET"

/usr/bin/swiftc -O -framework AppKit -framework Foundation -framework WebKit \
  "$PLUGIN_ROOT/macos/GitHubRadarApp.swift" \
  "$PLUGIN_ROOT/macos/main.swift" \
  -o "$CONTENTS/MacOS/GitHubRadar"

cp "$PLUGIN_ROOT/macos/Info.plist" "$CONTENTS/Info.plist"
cp "$PLUGIN_ROOT/scripts/github_radar.py" "$PAYLOAD/scripts/github_radar.py"
cp "$PLUGIN_ROOT/scripts/github_radar_ui.py" "$PAYLOAD/scripts/github_radar_ui.py"
cp -R "$PLUGIN_ROOT/ui" "$PAYLOAD/ui"
cp "$PLUGIN_ROOT/assets/logo.png" "$PAYLOAD/assets/logo.png"
cp "$PLUGIN_ROOT/assets/icon.png" "$PAYLOAD/assets/icon.png"
cp "$PLUGIN_ROOT/assets/icon.png" "$RESOURCES/StatusIcon.png"

SOURCE_ICON="$PLUGIN_ROOT/assets/concepts/icon-macos-master.png"
for size in 16 32 128 256 512; do
  /usr/bin/sips -z "$size" "$size" "$SOURCE_ICON" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
  double=$((size * 2))
  /usr/bin/sips -z "$double" "$double" "$SOURCE_ICON" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
/usr/bin/iconutil -c icns "$ICONSET" -o "$RESOURCES/AppIcon.icns"
rm -rf "$ICONSET"

/usr/bin/codesign --force --deep --sign - "$APP_DIR" >/dev/null
echo "$APP_DIR"
