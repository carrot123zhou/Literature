#!/bin/zsh
set -eu
PROJECT_ROOT="${0:A:h:h}"
cd "$PROJECT_ROOT"
.venv/bin/python scripts/build_sidecar.py
pnpm --dir frontend desktop:build
/usr/bin/codesign --force --deep --sign - src-tauri/target/release/bundle/macos/Literature.app
./scripts/setup_macos_launcher.sh --app-only
