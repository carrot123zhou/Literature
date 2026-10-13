#!/bin/zsh
set -eu
CREATE_DESKTOP_ALIAS=true
if [[ "${1:-}" == "--app-only" ]]; then CREATE_DESKTOP_ALIAS=false; shift; fi
if (( $# != 0 )); then print -u2 -- "用法：${0:t} [--app-only]"; exit 2; fi
PROJECT_ROOT="${0:A:h:h}"
SOURCE_APP="$PROJECT_ROOT/src-tauri/target/release/bundle/macos/Literature.app"
TARGET_APP="$PROJECT_ROOT/launcher/Literature.app"
assert_app_stopped() {
  local process_list process_command
  process_list="$(/bin/ps -axo command=)" || {
    print -u2 -- "无法检查客户端进程，未替换安装包。"
    exit 2
  }
  for process_command in "${(@f)process_list}"; do
    if [[ "$process_command" == "$TARGET_APP/Contents/MacOS/"* ]]; then
      print -u2 -- "Literature 或内置后端仍在运行。请完全退出客户端后再安装更新，避免运行中的模块读取新旧不一致的程序包。"
      exit 2
    fi
  done
}
if [[ ! -d "$SOURCE_APP" ]]; then print -u2 -- "请先运行 scripts/build_desktop.sh 构建客户端。"; exit 1; fi
assert_app_stopped
/usr/bin/codesign --verify --deep --strict "$SOURCE_APP"
/bin/mkdir -p "$PROJECT_ROOT/launcher"
BUILD_DIR="$(/usr/bin/mktemp -d "$PROJECT_ROOT/launcher/.install.XXXXXX")"
trap '/bin/rm -rf -- "$BUILD_DIR"' EXIT
/usr/bin/ditto "$SOURCE_APP" "$BUILD_DIR/Literature.app"
assert_app_stopped
if [[ -e "$TARGET_APP" ]]; then /bin/mv "$TARGET_APP" "$BUILD_DIR/previous.app"; fi
if ! /bin/mv "$BUILD_DIR/Literature.app" "$TARGET_APP"; then
  if [[ -e "$BUILD_DIR/previous.app" ]]; then /bin/mv "$BUILD_DIR/previous.app" "$TARGET_APP"; fi
  exit 1
fi
print -r -- "已安装独立客户端：$TARGET_APP"
if [[ "$CREATE_DESKTOP_ALIAS" != true ]]; then exit 0; fi
/usr/bin/osascript - "$TARGET_APP" <<'APPLESCRIPT'
on run argv
  set targetApp to POSIX file (item 1 of argv)
  tell application "Finder"
    if exists file "Literature" of desktop then
      set oldAlias to file "Literature" of desktop
      if class of oldAlias is not alias file then error "桌面同名项目不是替身，未覆盖。"
      set oldTarget to POSIX path of (original item of oldAlias as alias)
      if oldTarget does not end with "/launcher/Literature.app/" then error "桌面替身指向其他应用，未覆盖。"
      delete oldAlias
    end if
    set createdAlias to make new alias file at desktop to targetApp
    set name of createdAlias to "Literature"
  end tell
end run
APPLESCRIPT
