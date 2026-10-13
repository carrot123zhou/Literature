#!/usr/bin/env bash
# 启动浏览器开发环境：本地 API + Vite 前端（无需 Tauri）。
# 用法：./scripts/dev_frontend.sh   （LITERATURE_LIBRARY 可指定资源库；默认用配置库）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# 系统没有 node 时回退到仓库内置运行时（scripts 首次会下载）。
if ! command -v node >/dev/null 2>&1; then
  if [ -x "$ROOT/.tools/node/bin/node" ]; then
    export PATH="$ROOT/.tools/node/bin:$PATH"
  else
    echo "未找到 node；请安装 Node.js ≥ 20，或先解压 .tools/node。" >&2
    exit 1
  fi
fi

TOKEN="${LITERATURE_DEV_TOKEN:-literature-dev}"
PORT="${LITERATURE_PORT:-41731}"
export LITERATURE_SESSION_TOKEN="$TOKEN"
export LITERATURE_PORT="$PORT"

( cd "$ROOT" && .venv/bin/python -m literature.api > /tmp/literature-api.log 2>&1 & echo $! > /tmp/literature-api.pid )
( cd "$ROOT/frontend" && VITE_LITERATURE_TOKEN="$TOKEN" VITE_API_URL="http://127.0.0.1:$PORT" \
    node_modules/.bin/vite --host 127.0.0.1 --port 1420 --strictPort > /tmp/literature-vite.log 2>&1 & echo $! > /tmp/literature-vite.pid )

echo "Literature API: http://127.0.0.1:$PORT  (token: $TOKEN)"
echo "Literature UI : http://127.0.0.1:1420/"
echo "日志：/tmp/literature-api.log  /tmp/literature-vite.log"
echo "停止：kill \$(cat /tmp/literature-api.pid) \$(cat /tmp/literature-vite.pid)"
