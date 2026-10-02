#!/bin/zsh

set -u

PROJECT_ROOT="${LITERATURE_PROJECT_ROOT:-/Users/jiangwei/CdoexProject/Literature}"
PYTHON_PACKAGES="$PROJECT_ROOT/.venv/lib/python3.12/site-packages"
VITE_ENTRY="$PROJECT_ROOT/frontend/node_modules/vite/bin/vite.js"
SESSION_TOKEN="literature-dev"
LAUNCHER_DIR="${LITERATURE_LAUNCHER_DIR:-${HOME}/Library/Application Support/Literature/launcher}"
LOG_DIR="${LITERATURE_LOG_DIR:-${HOME}/Library/Logs/Literature}"
STATE_FILE="$LAUNCHER_DIR/ports.env"
LOCK_DIR="$LAUNCHER_DIR/launch.lock"
LIBRARY_DIR="${LITERATURE_LIBRARY:-$PROJECT_ROOT/library}"

mkdir -p "$LAUNCHER_DIR" "$LOG_DIR"

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

notify_error() {
  /usr/bin/osascript -e 'display alert "Literature 启动失败" message "请查看 ~/Library/Logs/Literature 中的日志。" as critical' >/dev/null 2>&1 || true
}

notify_restricted_launch() {
  /usr/bin/osascript -e 'display alert "Literature 无法安全启动" message "当前启动环境不能完整访问 Literature 项目仓库或外部 MinerU 服务。请确认项目仓库位置后，再从 Finder 中重新打开 Literature.app。" as critical' >/dev/null 2>&1 || true
}

library_access_ready() {
  local probe
  local visible=false
  /bin/mkdir -p "$LIBRARY_DIR" 2>/dev/null || return 1
  probe="$LIBRARY_DIR/.literature-access-$$-$RANDOM.tmp"
  print -r -- "Literature library access probe" > "$probe" 2>/dev/null || return 1
  if /bin/ls -A "$LIBRARY_DIR" 2>/dev/null | /usr/bin/grep -Fqx -- "${probe:t}"; then
    visible=true
  fi
  /bin/rm -f "$probe" 2>/dev/null || true
  [[ "$visible" == true ]]
}

# A launcher process inherited from a restricted development host can appear
# healthy on loopback while being unable to enumerate the project library or
# reach MinerU. Refuse that unsafe mode before it creates half-visible data.
if [[ "${CODEX_SANDBOX_NETWORK_DISABLED:-0}" == "1" ]]; then
  print -r -- "[$(/bin/date '+%Y-%m-%dT%H:%M:%S%z')] 启动进程继承了受限网络环境，已拒绝启动。" >> "$LOG_DIR/launcher.log"
  notify_restricted_launch
  exit 1
fi
if ! library_access_ready; then
  print -r -- "[$(/bin/date '+%Y-%m-%dT%H:%M:%S%z')] 无法完整读写或枚举项目库：$LIBRARY_DIR" >> "$LOG_DIR/launcher.log"
  notify_restricted_launch
  exit 1
fi

PYTHON_BIN=""
EXPECTED_API_REVISION=""
for candidate in \
  "$PROJECT_ROOT/.venv/bin/python" \
  /Users/jiangwei/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3; do
  if [[ "$candidate" == "$PROJECT_ROOT/.venv/bin/python" \
    && -f "$PROJECT_ROOT/.venv/pyvenv.cfg" \
    ]] && /usr/bin/grep -Fq -- "${HOME}/Documents/" "$PROJECT_ROOT/.venv/pyvenv.cfg"; then
    print -r -- "[$(/bin/date '+%Y-%m-%dT%H:%M:%S%z')] 已跳过仍绑定 Documents 的旧虚拟环境。" >> "$LOG_DIR/launcher.log"
    continue
  fi
  if [[ -x "$candidate" ]]; then
    candidate_revision="$("$candidate" "$PROJECT_ROOT/literature/runtime_revision.py" 2>/dev/null)"
    if (( ${#candidate_revision} == 20 )) && [[ "$candidate_revision" != *[!0-9a-f]* ]]; then
      PYTHON_BIN="$candidate"
      EXPECTED_API_REVISION="$candidate_revision"
      break
    fi
  fi
done

if [[ -z "$PYTHON_BIN" || -z "$EXPECTED_API_REVISION" || ! -d "$PYTHON_PACKAGES" || ! -f "$VITE_ENTRY" ]]; then
  print -r -- "[$(/bin/date '+%Y-%m-%dT%H:%M:%S%z')] 缺少 Python 虚拟环境或前端依赖。" >> "$LOG_DIR/launcher.log"
  notify_error
  exit 1
fi

valid_port() {
  [[ "$1" == <-> ]] && (( $1 >= 1024 && $1 <= 65535 ))
}

port_in_use() {
  /usr/bin/nc -z 127.0.0.1 "$1" >/dev/null 2>&1
}

choose_port() {
  local candidate="$1"
  local upper="$2"
  while (( candidate <= upper )); do
    if ! port_in_use "$candidate"; then
      print -r -- "$candidate"
      return 0
    fi
    (( candidate += 1 ))
  done
  return 1
}

api_ready() {
  local response
  response="$(/usr/bin/curl --silent --fail --max-time 1 "http://127.0.0.1:$1/health" 2>/dev/null)" || return 1
  [[ "$response" == *'"status":"ok"'* \
    && "$response" == *'"intake_items_v1"'* \
    && "$response" == *'"runtime_revision":"'"$EXPECTED_API_REVISION"'"'* ]]
}

web_ready() {
  local response
  response="$(/usr/bin/curl --silent --fail --max-time 1 "http://127.0.0.1:$1/" 2>/dev/null)" || return 1
  [[ "$response" == *'<title>Literature</title>'* ]]
}

open_webui() {
  if [[ "${LITERATURE_LAUNCHER_NO_OPEN:-0}" != "1" ]]; then
    /usr/bin/open "http://127.0.0.1:$1/"
  fi
}

wait_until() {
  local kind="$1"
  local port="$2"
  local attempts=0
  while (( attempts < 80 )); do
    if "$kind" "$port"; then
      return 0
    fi
    /bin/sleep 0.25
    (( attempts += 1 ))
  done
  return 1
}

listener_pid() {
  /usr/sbin/lsof -nP -t -iTCP:"$1" -sTCP:LISTEN 2>/dev/null | /usr/bin/head -n 1
}

stop_managed_process() {
  local pid="$1"
  local port="$2"
  local marker="$3"
  local command
  local attempts=0
  [[ "$pid" == <-> ]] || return 0
  command="$(/bin/ps -p "$pid" -o command= 2>/dev/null)" || return 0
  [[ "$command" == *"$marker"* ]] || return 0
  [[ "$(listener_pid "$port")" == "$pid" ]] || return 0
  /bin/kill "$pid" 2>/dev/null || return 0
  while (( attempts < 30 )) && /bin/kill -0 "$pid" 2>/dev/null; do
    /bin/sleep 0.1
    (( attempts += 1 ))
  done
}

saved_api=""
saved_web=""
saved_api_pid=""
saved_web_pid=""
if [[ -f "$STATE_FILE" ]]; then
  saved_api="$(/usr/bin/sed -n 's/^API_PORT=//p' "$STATE_FILE" | /usr/bin/head -n 1)"
  saved_web="$(/usr/bin/sed -n 's/^WEB_PORT=//p' "$STATE_FILE" | /usr/bin/head -n 1)"
  saved_api_pid="$(/usr/bin/sed -n 's/^API_PID=//p' "$STATE_FILE" | /usr/bin/head -n 1)"
  saved_web_pid="$(/usr/bin/sed -n 's/^WEB_PID=//p' "$STATE_FILE" | /usr/bin/head -n 1)"
fi

if [[ -z "$saved_api" ]] && api_ready 41731; then
  saved_api=41731
fi
if [[ -z "$saved_web" ]] && web_ready 1420; then
  saved_web=1420
fi

if valid_port "$saved_api" && valid_port "$saved_web" && api_ready "$saved_api" && web_ready "$saved_web"; then
  [[ "$saved_api_pid" == <-> ]] || saved_api_pid="$(listener_pid "$saved_api")"
  [[ "$saved_web_pid" == <-> ]] || saved_web_pid="$(listener_pid "$saved_web")"
  {
    print -r -- "API_PORT=$saved_api"
    print -r -- "WEB_PORT=$saved_web"
    print -r -- "API_PID=$saved_api_pid"
    print -r -- "WEB_PID=$saved_web_pid"
    print -r -- "API_REVISION=$EXPECTED_API_REVISION"
  } > "$STATE_FILE"
  open_webui "$saved_web"
  exit 0
fi

# A managed API with a different source fingerprint is stale. Stop only the
# exact saved listener and only after validating its command line.
if valid_port "$saved_api" && port_in_use "$saved_api" && ! api_ready "$saved_api"; then
  stop_managed_process "$saved_api_pid" "$saved_api" "-m literature.api"
fi
# Any path reaching this point must restart Vite so it uses the selected API
# port. As above, never stop an unrecognised process that happens to own it.
if valid_port "$saved_web" && port_in_use "$saved_web"; then
  stop_managed_process "$saved_web_pid" "$saved_web" "$VITE_ENTRY"
fi

NODE_BIN=""
for candidate in \
  /opt/homebrew/bin/node \
  /usr/local/bin/node \
  /Users/jiangwei/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node; do
  if [[ -x "$candidate" ]]; then
    NODE_BIN="$candidate"
    break
  fi
done
if [[ -z "$NODE_BIN" ]]; then
  NODE_BIN="$(command -v node 2>/dev/null || true)"
fi
if [[ -z "$NODE_BIN" || ! -x "$NODE_BIN" ]]; then
  print -r -- "[$(/bin/date '+%Y-%m-%dT%H:%M:%S%z')] 找不到 Node.js。" >> "$LOG_DIR/launcher.log"
  notify_error
  exit 1
fi

api_port="$saved_api"
start_api=true
api_pid="$saved_api_pid"
if valid_port "$api_port" && api_ready "$api_port"; then
  start_api=false
  [[ "$api_pid" == <-> ]] || api_pid="$(listener_pid "$api_port")"
elif ! valid_port "$api_port" || port_in_use "$api_port"; then
  api_port="$(choose_port 41731 41750)" || { notify_error; exit 1; }
fi

web_port="$saved_web"
if ! valid_port "$web_port" || port_in_use "$web_port"; then
  web_port="$(choose_port 1420 1440)" || { notify_error; exit 1; }
fi

if [[ "$start_api" == true ]]; then
  print -r -- "[$(/bin/date '+%Y-%m-%dT%H:%M:%S%z')] 启动 API：http://127.0.0.1:$api_port" >> "$LOG_DIR/launcher.log"
  PYTHONPATH="$PROJECT_ROOT:$PYTHON_PACKAGES" LITERATURE_LIBRARY="$LIBRARY_DIR" LITERATURE_PORT="$api_port" LITERATURE_SESSION_TOKEN="$SESSION_TOKEN" \
    /usr/bin/nohup "$PYTHON_BIN" -m literature.api >> "$LOG_DIR/api.log" 2>&1 </dev/null &
  api_pid=$!
fi

if ! wait_until api_ready "$api_port"; then
  print -r -- "[$(/bin/date '+%Y-%m-%dT%H:%M:%S%z')] API 启动超时。" >> "$LOG_DIR/launcher.log"
  notify_error
  exit 1
fi

print -r -- "[$(/bin/date '+%Y-%m-%dT%H:%M:%S%z')] 启动 WebUI：http://127.0.0.1:$web_port" >> "$LOG_DIR/launcher.log"
VITE_API_URL="http://127.0.0.1:$api_port" VITE_LITERATURE_TOKEN="$SESSION_TOKEN" \
  /usr/bin/nohup "$NODE_BIN" "$VITE_ENTRY" "$PROJECT_ROOT/frontend" --host 127.0.0.1 --port "$web_port" --strictPort >> "$LOG_DIR/webui.log" 2>&1 </dev/null &
web_pid=$!

if ! wait_until web_ready "$web_port"; then
  print -r -- "[$(/bin/date '+%Y-%m-%dT%H:%M:%S%z')] WebUI 启动超时。" >> "$LOG_DIR/launcher.log"
  notify_error
  exit 1
fi

{
  print -r -- "API_PORT=$api_port"
  print -r -- "WEB_PORT=$web_port"
  print -r -- "API_PID=$api_pid"
  print -r -- "WEB_PID=$web_pid"
  print -r -- "API_REVISION=$EXPECTED_API_REVISION"
} > "$STATE_FILE"

open_webui "$web_port"
/usr/bin/osascript -e 'display notification "本地服务已经就绪" with title "Literature"' >/dev/null 2>&1 || true
