#!/usr/bin/env bash
# 构建「阅读模块审查产物」：前端 → sidecar → Tauri .app → adhoc 签名 → 资源/哈希核对。
#
# 只产出 `src-tauri/target/release/bundle/macos/Literature.app`（审查副本），
# **不**安装到 launcher/、**不**启动客户端。环境回退（无系统 node）与 PACKAGING.md 一致。
#
# 用法：scripts/build_review_bundle.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
APP="src-tauri/target/release/bundle/macos/Literature.app"
BUILD_PYTHON="${LITERATURE_BUILD_PYTHON:-$ROOT/.venv/bin/python}"

# 1) node：优先系统 node；没有就借 ms-playwright-go 的副本（PACKAGING.md 环境回退）。
if command -v node >/dev/null 2>&1; then
  NODE="$(command -v node)"
else
  NODE="$HOME/Library/Caches/ms-playwright-go/1.57.0/node"
fi
if [ ! -x "$NODE" ]; then
  echo "FAIL: 找不到可用的 node（系统与 ms-playwright-go 均不可用）" >&2
  exit 1
fi
# Tauri CLI 的 native 模块需要显式指向（pnpm 虚拟店链接不全时的官方回退）。
NATIVE="$(ls -d frontend/node_modules/.pnpm/@tauri-apps+cli-darwin-arm64@*/node_modules/@tauri-apps/cli-darwin-arm64/cli.darwin-arm64.node 2>/dev/null | head -1 || true)"
if [ -n "$NATIVE" ]; then
  export NAPI_RS_NATIVE_LIBRARY_PATH="$ROOT/$NATIVE"
fi

echo "==> 1/6 类型检查"
(cd frontend && "$NODE" node_modules/typescript/bin/tsc --noEmit)

echo "==> 2/6 前端生产构建"
(cd frontend && "$NODE" node_modules/vite/bin/vite.js build)

echo "==> 3/6 全 app 横向溢出 + 导航可用性（含阅读器内部）"
.venv/bin/python scripts/app_shell_overflow_check.py | tail -3

echo "==> 4/6 Python sidecar"
"$BUILD_PYTHON" scripts/build_sidecar.py

echo "==> 5/6 Tauri 打包（跳过内部前端重建）"
"$NODE" scripts/tauri-run.cjs build \
  --config src-tauri/tauri.bundle.conf.json \
  --config '{"build":{"beforeBuildCommand":""}}'

echo "==> 6/6 adhoc 签名 + 核对"
/usr/bin/codesign --force --deep --sign - "$APP"
/usr/bin/codesign --verify --deep --strict "$APP"

SIDECAR="$APP/Contents/MacOS/literature-sidecar"
BIN_SHA="$(shasum -a 256 "$SIDECAR" | awk '{print $1}')"
BIN_SIZE="$(stat -f%z "$SIDECAR")"
MANIFEST_SHA="$(python3 -c 'import json;print(json.load(open("src-tauri/binaries/manifest.json"))["sha256"])')"
MANIFEST_SIZE="$(python3 -c 'import json;print(json.load(open("src-tauri/binaries/manifest.json"))["size"])')"
REVISION="$(python3 -c 'import json;print(json.load(open("src-tauri/binaries/manifest.json"))["runtime_revision"])')"
if [ "$BIN_SHA" != "$MANIFEST_SHA" ] || [ "$BIN_SIZE" != "$MANIFEST_SIZE" ]; then
  echo "FAIL: 包内 sidecar 与 manifest 不一致（$BIN_SHA/$BIN_SIZE vs $MANIFEST_SHA/$MANIFEST_SIZE）" >&2
  exit 1
fi
EMBEDDED_JS="$(strings -a "$APP/Contents/MacOS/literature" | grep -oE 'assets/index-[A-Za-z0-9_-]+\.js' | sort -u | tr '\n' ' ')"
EMBEDDED_CSS="$(strings -a "$APP/Contents/MacOS/literature" | grep -oE 'assets/index-[A-Za-z0-9_-]+\.css' | sort -u | tr '\n' ' ')"
DIST_JS="$(ls frontend/dist/assets | grep -E '^index-.*\.js$' | tr '\n' ' ')"
DIST_CSS="$(ls frontend/dist/assets | grep -E '^index-.*\.css$' | tr '\n' ' ')"
if [ "${EMBEDDED_JS// /}" != "assets/${DIST_JS// /}" ] || [ "${EMBEDDED_CSS// /}" != "assets/${DIST_CSS// /}" ]; then
  echo "FAIL: 内嵌前端与 frontend/dist 不一致（内嵌 $EMBEDDED_JS$EMBEDDED_CSS / dist $DIST_JS$DIST_CSS）" >&2
  exit 1
fi

echo "==> 冻结 sidecar 阅读模块验收（真实书目副本、本地折叠、不调模型）"
.venv/bin/python scripts/reader_frozen_check.py --binary "$SIDECAR" | tail -3

cat <<EOF

================ 审查产物标识 ================
app              : $APP ($(du -sh "$APP" | awk '{print $1}'))
sidecar sha256   : $BIN_SHA
sidecar size     : $BIN_SIZE
runtime_revision : $REVISION
内嵌前端         : $EMBEDDED_JS$EMBEDDED_CSS
dist 前端        : $DIST_JS$DIST_CSS
签名             : adhoc（codesign --verify --deep --strict 通过）
边界             : 未安装到 launcher、未启动客户端
=============================================
EOF
