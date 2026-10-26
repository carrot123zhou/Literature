from __future__ import annotations

import shutil
import json
import hashlib
import subprocess
import sys
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
WORK = ROOT / "build" / "sidecar"
DIST = ROOT / "dist" / "sidecar"
BINARIES = ROOT / "src-tauri" / "binaries"


def validate_build_python() -> None:
    """Reject an interpreter whose SSL constructor depends on unbundled files."""
    import ssl
    code = getattr(ssl.SSLContext.__new__, "__code__", None)
    if code and "codex-rejected-cas.pem" in code.co_consts:
        raise RuntimeError(
            "构建 Python 的 ssl 模块依赖 codex-rejected-cas.pem，冻结后会导致模型与 HTTPS 全部失败。"
            "请用独立的标准 CPython 3.12 构建环境运行本脚本（不要用当前 .venv）。"
        )
    context = ssl.create_default_context()
    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
        raise RuntimeError("构建环境的 TLS 证书校验不可用。")
    # --collect-all only warns when a package is missing. Treat missing runtime
    # dependencies as a build failure instead of silently losing table context.
    import importlib
    for name in ("PyInstaller", "bs4", "soupsieve", "pymupdf", "docx", "httpx", "uvicorn"):
        try:
            importlib.import_module(name)
        except ImportError as error:
            raise RuntimeError(f"构建环境缺少必需模块 {name}；请在同一 Python 环境安装项目打包依赖。") from error


def target_triple() -> str:
    result = subprocess.run(
        ["rustc", "--print", "host-tuple"], capture_output=True, text=True, check=True,
    )
    value = result.stdout.strip()
    if not value:
        raise RuntimeError("rustc 没有返回 target triple。")
    return value


def main() -> None:
    validate_build_python()
    from literature.runtime_revision import source_revision
    WORK.mkdir(parents=True, exist_ok=True)
    revision = WORK / "runtime-revision.txt"
    revision.write_text(source_revision(), encoding="utf-8")
    os.environ["PYINSTALLER_CONFIG_DIR"] = str(WORK / "cache")
    # PyInstaller 的 --add-data 分隔符在 POSIX 上是 ':'、Windows 上是 ';'。
    separator = ";" if os.name == "nt" else ":"
    suffix = ".exe" if os.name == "nt" else ""
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
        "--name", "literature-sidecar", "--distpath", str(DIST), "--workpath", str(WORK),
        "--specpath", str(WORK), "--collect-all", "pymupdf", "--collect-all", "docx",
        # 阅读模块的 TextFold 上游快照（含 LICENSE/NOTICE/VENDOR.md 数据文件）。
        "--collect-all", "third_party.textfold",
        # bs4 只在 ladder.tables_of() 内部按需 import（PyInstaller 静态分析看不到），
        # 不显式收集的话冻结包里「章节表格上下文」会静默缺失。
        "--collect-all", "bs4", "--collect-all", "soupsieve",
        "--add-data", f"{revision}{separator}literature",
        "--hidden-import", "uvicorn.logging", "--hidden-import", "uvicorn.loops.auto",
        "--hidden-import", "uvicorn.protocols.http.auto", "--hidden-import", "uvicorn.protocols.websockets.auto",
        str(ROOT / "literature" / "sidecar.py"),
    ], cwd=ROOT, check=True)
    source = DIST / f"literature-sidecar{suffix}"
    # Test the frozen stdlib and bundled CA files, not just the build environment.
    subprocess.run([str(source), "--check-tls"], check=True, timeout=30)
    BINARIES.mkdir(parents=True, exist_ok=True)
    destination = BINARIES / f"literature-sidecar-{target_triple()}{suffix}"
    shutil.copy2(source, destination)
    if os.name != "nt":
        destination.chmod(0o755)
    manifest = {"target": target_triple(), "runtime_revision": revision.read_text(), "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(), "size": destination.stat().st_size}
    (BINARIES / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(destination)


if __name__ == "__main__":
    main()
