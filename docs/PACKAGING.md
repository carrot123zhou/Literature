# macOS 打包

开发态由 Tauri 启动仓库 `.venv` 中的 Python。发布态先用 PyInstaller 生成自包含 API sidecar，再让 Tauri 将它放进 `.app`。这一布局遵循 Tauri 2 的 `externalBin` target-triple 命名约定。

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev,packaging]'
.venv/bin/python scripts/build_sidecar.py
cd frontend && pnpm install && pnpm build
cd ../src-tauri && cargo tauri build --config tauri.bundle.conf.json
```

`scripts/build_sidecar.py` 会生成：

```text
src-tauri/binaries/literature-sidecar-<target-triple>
```

Rust 启动顺序为：`LITERATURE_SIDECAR` 显式路径、`.app/Contents/MacOS/literature-sidecar`、`LITERATURE_PYTHON`、仓库 `.venv`、系统 `python3`。因此开发态和发布态共用同一套 FastAPI 会话令牌与 loopback 安全边界。

当前尚未执行签名、公证或干净机器安装测试；这些步骤需要 Developer ID 凭据。打包产物不应提交到 Git。
