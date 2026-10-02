#!/bin/zsh

set -eu

CREATE_DESKTOP_ALIAS=true
if [[ "${1:-}" == "--app-only" ]]; then
  CREATE_DESKTOP_ALIAS=false
  shift
fi
if (( $# != 0 )); then
  print -u2 -- "用法：${0:t} [--app-only]"
  exit 2
fi

PROJECT_ROOT="${0:A:h:h}"
SOURCE_SCRIPT="$PROJECT_ROOT/scripts/Literature.js"
SOURCE_ICON="$PROJECT_ROOT/src-tauri/icons/icon.png"
TARGET_DIR="$PROJECT_ROOT/launcher"
TARGET_APP="$TARGET_DIR/Literature.app"
DESKTOP_ALIAS="${HOME}/Desktop/Literature"
BUILD_DIR="$(/usr/bin/mktemp -d "${TMPDIR%/}/literature-launcher.XXXXXX")"
BUILD_APP="$BUILD_DIR/Literature.app"
PREVIOUS_APP="$BUILD_DIR/previous-Literature.app"

cleanup() {
  /bin/rm -rf -- "$BUILD_DIR"
}
trap cleanup EXIT

for source_file in "$SOURCE_SCRIPT" "$SOURCE_ICON"; do
  if [[ ! -f "$source_file" ]]; then
    print -u2 -- "缺少启动器源文件：$source_file"
    exit 1
  fi
done

/usr/bin/sips -s format icns "$SOURCE_ICON" --out "$BUILD_DIR/Literature.icns" >/dev/null
/usr/bin/osacompile -l JavaScript -o "$BUILD_APP" "$SOURCE_SCRIPT"

PLIST="$BUILD_APP/Contents/Info.plist"
set_plist_value() {
  local key="$1"
  local type="$2"
  local value="$3"
  /usr/libexec/PlistBuddy -c "Set :$key $value" "$PLIST" 2>/dev/null || \
    /usr/libexec/PlistBuddy -c "Add :$key $type $value" "$PLIST"
}

set_plist_value CFBundleName string Literature
set_plist_value CFBundleDisplayName string Literature
set_plist_value CFBundleIdentifier string local.literature.web-launcher
set_plist_value CFBundleShortVersionString string 0.2.0
set_plist_value CFBundleVersion string 2
set_plist_value CFBundleIconFile string Literature.icns
set_plist_value LSUIElement bool true
/usr/libexec/PlistBuddy -c "Delete :CFBundleIconName" "$PLIST" 2>/dev/null || true
/bin/cp "$BUILD_DIR/Literature.icns" "$BUILD_APP/Contents/Resources/Literature.icns"

/usr/bin/plutil -lint "$PLIST" >/dev/null
/usr/bin/codesign --force --deep --sign - "$BUILD_APP" >/dev/null
/usr/bin/codesign --verify --deep --strict "$BUILD_APP"

/bin/mkdir -p "$TARGET_DIR"
if [[ -e "$TARGET_APP" ]]; then
  /bin/mv "$TARGET_APP" "$PREVIOUS_APP"
fi
if ! /bin/mv "$BUILD_APP" "$TARGET_APP"; then
  if [[ -e "$PREVIOUS_APP" ]]; then
    /bin/mv "$PREVIOUS_APP" "$TARGET_APP"
  fi
  print -u2 -- "无法更新：$TARGET_APP"
  exit 1
fi

print -r -- "已生成：$TARGET_APP"

if [[ "$CREATE_DESKTOP_ALIAS" != true ]]; then
  exit 0
fi

if [[ -e "$DESKTOP_ALIAS" ]]; then
  resolved_alias="$(/usr/bin/osascript - "$DESKTOP_ALIAS" <<'APPLESCRIPT'
on run argv
  set aliasPath to POSIX file (item 1 of argv)
  tell application "Finder"
    set aliasItem to item aliasPath
    set targetItem to original item of aliasItem
    return POSIX path of (targetItem as alias)
  end tell
end run
APPLESCRIPT
)" || {
    print -u2 -- "桌面上已存在无法安全复用的项目：$DESKTOP_ALIAS"
    exit 1
  }
  resolved_alias="$(/bin/realpath "${resolved_alias%/}")"
  target_app_real="$(/bin/realpath "$TARGET_APP")"
  previous_app_real="$(/bin/realpath "$PREVIOUS_APP" 2>/dev/null || true)"
  if [[ "$resolved_alias" != "$target_app_real" && "$resolved_alias" != "$previous_app_real" ]]; then
    print -u2 -- "桌面替身指向其他位置：$resolved_alias"
    exit 1
  fi
  /bin/rm -f -- "$DESKTOP_ALIAS"
fi

/usr/bin/osascript - "$TARGET_APP" <<'APPLESCRIPT'
on run argv
  set targetApp to POSIX file (item 1 of argv)
  tell application "Finder"
    set createdAlias to make new alias file at desktop to targetApp
    set name of createdAlias to "Literature"
  end tell
end run
APPLESCRIPT

print -r -- "已创建桌面替身：$DESKTOP_ALIAS"
