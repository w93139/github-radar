#!/bin/zsh
set -euo pipefail

PLUGIN_ROOT="${0:A:h:h}"
OUTPUT_DIR="$PLUGIN_ROOT/dist"
APP_PATH="$OUTPUT_DIR/GitHub Radar.app"
VERSION=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$PLUGIN_ROOT/macos/Info.plist")
ARCH=$(/usr/bin/uname -m)
BASE_NAME="GitHub-Radar-${VERSION}-macOS-${ARCH}"
STAGING=$(/usr/bin/mktemp -d "$OUTPUT_DIR/release.XXXXXX")

cleanup() {
  /bin/rm -rf "$STAGING"
}
trap cleanup EXIT

"$PLUGIN_ROOT/scripts/build_macos_app.sh" >/dev/null
/usr/bin/ditto "$APP_PATH" "$STAGING/GitHub Radar.app"
/bin/ln -s /Applications "$STAGING/Applications"

/usr/bin/ditto -c -k --sequesterRsrc --keepParent \
  "$APP_PATH" "$OUTPUT_DIR/${BASE_NAME}.zip"
/usr/bin/hdiutil create -quiet -ov -format UDZO \
  -volname "GitHub Radar" -srcfolder "$STAGING" \
  "$OUTPUT_DIR/${BASE_NAME}.dmg"

(
  cd "$OUTPUT_DIR"
  /usr/bin/shasum -a 256 "${BASE_NAME}.zip" "${BASE_NAME}.dmg" > "${BASE_NAME}-SHA256SUMS.txt"
)

echo "$OUTPUT_DIR/${BASE_NAME}.dmg"
echo "$OUTPUT_DIR/${BASE_NAME}.zip"
echo "$OUTPUT_DIR/${BASE_NAME}-SHA256SUMS.txt"
