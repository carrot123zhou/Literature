# macOS 客户端构建

当前客户端使用 Tauri 2 + 内嵌 React 工作区 + PyInstaller 单文件后端。内置后端与前端是发布必需资源；发布态不会回退到开发仓库或系统 Python。

## 构建

要求 macOS、Xcode Command Line Tools、Rust/Cargo、Node/pnpm 和 Python 3.12。当前构建机生成 `aarch64-apple-darwin`，未生成 Intel 或通用包。

打包解释器必须是独立标准 CPython。若开发 `.venv` 共用带额外证书文件依赖的工具运行时，不可直接用于发布。
可用 `LITERATURE_BUILD_PYTHON=/绝对路径/打包环境/bin/python` 指定构建解释器（适用于
`build_desktop.sh` 与 `build_review_bundle.sh`）。该环境须安装当前项目及打包依赖，尤其是
`beautifulsoup4` / `soupsieve`；PyInstaller 的 collect-all 对缺包只告警，构建脚本现改为预检失败。
`build_sidecar.py` 同时拦截已知不可移植的 SSL 构造器，并在产出后直接执行冻结二进制
`--check-tls`，验证包内真实 TLS/CA 路径和证书校验；不能以构建环境能联网替代包内验证。

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev,packaging]'
pnpm --dir frontend install --frozen-lockfile
./scripts/build_desktop.sh
```

依次执行：

1. `scripts/build_sidecar.py` 生成 `src-tauri/binaries/literature-sidecar-<target-triple>` 和 `manifest.json`（源码指纹、二进制哈希、目标架构、大小）。PyInstaller 缓存留在仓库 `build/sidecar/cache/`。
2. Vite 将 PDF.js worker、WASM、ICC、CMap、标准字体及许可证从锁定的 `pdfjs-dist` 复制进产物，不再维护第二份静态副本。
3. Tauri 用 `tauri.bundle.conf.json` 将后端嵌入 `.app/Contents/MacOS/`，前端资源编入应用。
4. 对应用做 adhoc 签名并校验，安装到 `launcher/Literature.app`。构建原包位于 `src-tauri/target/release/bundle/macos/Literature.app`。

运行 `./scripts/setup_macos_launcher.sh` 可更新桌面 Finder 替身；`--app-only` 只安装仓库内客户端。旧 JXA 浏览器启动器和 `start_webui.sh` 已移除。

安装更新前须完全退出 Literature，等待内置后端结束。安装脚本在复制前和替换前检查目标客户端/后端进程，仍在运行则拒绝替换。PyInstaller 按需加载模块时会重新读取启动路径中的压缩归档；运行中替换程序会使旧偏移读取新版归档，触发 `Error -3 while decompressing data: incorrect header check`。若此前已发生这种替换，退出并重新打开客户端后再重试失败任务。

### 环境回退

当机器上没有名为 `node` 的可执行文件（只能借用其他应用内置的 node）时：

- 借用的 node 与 npm 包内的 native 模块（`rollup.darwin-arm64.node`、`cli.darwin-arm64.node` 等）若 Team ID 不一致，Node 会拒绝 `dlopen`。可对临时 node 副本与这些 `.node` 做 adhoc 重签（`codesign --force --sign -`）后使用。
- `@tauri-apps/cli/tauri.js` 靠 `process.argv[0]` 的 basename 判断调用方式；node 副本名不是 `node` 时会把 node 路径当成子命令。用 `node scripts/tauri-run.cjs build ...` 代替 `tauri build`。
- 前端产物已先行构建时，可用 `--config '{"build":{"beforeBuildCommand":""}}'` 跳过 Tauri 内的 pnpm 构建步骤（需与 `src-tauri/tauri.bundle.conf.json` 一并传入）。

#### Tauri CLI 的 native 模块找不到时（实测 2026-10-14）

`frontend/node_modules` 可能处于 **pnpm 虚拟店完整但链接不全** 的状态：`@tauri-apps/cli-darwin-arm64` 的实体在
`frontend/node_modules/.pnpm/@tauri-apps+cli-darwin-arm64@<ver>/node_modules/@tauri-apps/cli-darwin-arm64/cli.darwin-arm64.node`，
但 `frontend/node_modules/@tauri-apps/cli/node_modules/` 下没有指向它的链接，于是 `require('@tauri-apps/cli-darwin-arm64')` 直接失败。

不必重装依赖：`@tauri-apps/cli/index.js` 首先读取 `NAPI_RS_NATIVE_LIBRARY_PATH`，把它指向该 `.node` 的**绝对路径**即可。

没有 `pnpm`/`npm` 时，完整构建可以只用 node + 仓库自带的 .venv：

```bash
NODE_BIN=<任意可用的 node bin 目录>
export NAPI_RS_NATIVE_LIBRARY_PATH="$PWD/frontend/node_modules/.pnpm/@tauri-apps+cli-darwin-arm64@2.12.1/node_modules/@tauri-apps/cli-darwin-arm64/cli.darwin-arm64.node"
"$NODE_BIN/node" frontend/node_modules/typescript/bin/tsc --noEmit            # 类型检查
(cd frontend && "$NODE_BIN/node" node_modules/vite/bin/vite.js build)         # 前端产物
"${LITERATURE_BUILD_PYTHON:-.venv/bin/python}" scripts/build_sidecar.py       # PyInstaller sidecar（标准 CPython）
"$NODE_BIN/node" scripts/tauri-run.cjs build \
  --config src-tauri/tauri.bundle.conf.json \
  --config '{"build":{"beforeBuildCommand":""}}'                          # 打包 .app
/usr/bin/codesign --force --deep --sign - src-tauri/target/release/bundle/macos/Literature.app
./scripts/setup_macos_launcher.sh --app-only
```

验收要点（两处都能在安装前查）：`.app/Contents/MacOS/literature-sidecar` 的 sha256/大小须与 `src-tauri/binaries/manifest.json` 一致；
Tauri v2 把前端资源**压缩后编进 `Contents/MacOS/literature`**，所以不能靠 grep 中文文案确认版本，
用 `strings -a <app 二进制> | grep -oE 'assets/index-[A-Za-z0-9_-]+\.js'` 对比 `frontend/dist/assets/` 的文件名。

## 运行契约

- 随机 loopback 端口和会话令牌由 Rust 传给内置后端；前端通过受限 IPC 取得连接信息，并核对能力及鉴权。
- 保持单实例；再次启动聚焦已有窗口。
- 后端拥有独立进程组，正常退出清理进程；后端监测 App PID，应对 App 被结束后的孤儿进程。
- 运行日志记录在 `~/Library/Logs/Literature/sidecar.log`，令牌与密钥不写日志。
- 当前 Python 运行库要求 macOS 15，bundle 的最低版本与其一致。
- 发布态不使用 `LITERATURE_PYTHON` 或 `LITERATURE_SIDECAR`；这些覆盖只供开发态。

## 验收状态

2026-10-03 已实际生成约 59 MiB 的 Apple Silicon `.app`，签名校验通过。打包后端在临时资源库通过鉴权、原生文件元数据、离线规范化、四格式导出与 raw 哈希校验；客户端重复启动与 App 结束后的后端回收通过。同日重新构建完整客户端（前端 `index-JADs8ymS.js` + sidecar `runtime_revision` c85e4dc0…）并重装到 `launcher/Literature.app`，启动后 `/health` 与构建清单一致。

原生窗口自动化的 AX 读取持续超时，文件对话框、拖放、另存与 Finder 按钮尚未完成真实点击验收。默认首页启动曾成功执行已有 MinerU Token 的连接检查；本轮没有提交真实文献解析或调用模型。没有 Developer ID 签名、公证、跨机器安装或 Intel 构建验收，不能把本地 adhoc 包等同于正式对外发行包。
