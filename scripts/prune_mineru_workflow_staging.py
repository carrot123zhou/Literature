"""清理导出/TBL 时代遗留的 MinerU 暂存副本（`exports/.mineru-workflow_*/`）。

背景：2026-10-13 移除了内置导出与外部 TBL，但旧项目里由它写下的
`exports/.mineru-workflow_mineru_job/slices/**` 仍然留着——那是**封存前**的原始下载目录，
内容与 `ocr/raw/mineru/**`（不可变证据层，manifest 记录 sha256）逐字节相同。

安全边界（本脚本只做这一件事）：
- 只遍历 `<项目>/exports/.mineru-workflow_*/`；绝不改动 `ocr/`、`document/`、`translations/`、
  `source/`、`assets/` 或 `exports/` 下的用户产物（如已生成的 EPUB）。
- 只删除 **sha256 与 manifest 中已封存文件完全一致** 的文件；任何不匹配的文件
  （例如记录原始作业路径的 `job.json`）一律保留。
- 默认只预览；必须显式加 `--apply` 才删除。
- `library/.trash` 属于用户自己的回收站，不在本脚本范围内。

用法：
    python3 scripts/prune_mineru_workflow_staging.py                 # 预览
    python3 scripts/prune_mineru_workflow_staging.py --apply         # 执行
    python3 scripts/prune_mineru_workflow_staging.py --library <path>
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGING_GLOB = ".mineru-workflow*"
PROTECTED_DIRS = {"ocr", "document", "translations", "source", "assets"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sealed_hashes(project: Path) -> set[str]:
    manifest = project / "ocr" / "raw" / "manifest.json"
    if not manifest.is_file():
        return set()
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {str(item.get("sha256")) for slice_info in value.get("slices", [])
            for item in slice_info.get("files", []) if item.get("sha256")}


def staging_roots(project: Path) -> list[Path]:
    exports = project / "exports"
    if not exports.is_dir():
        return []
    return [entry for entry in sorted(exports.glob(STAGING_GLOB)) if entry.is_dir()]


def staging_files(project: Path) -> list[Path]:
    for root in staging_roots(project):
        yield from (path for path in root.rglob("*") if path.is_file() and not path.is_symlink())


def prune_empty_directories(root: Path) -> int:
    """自底向上删掉 staging 树里的空目录（顶层连 job.json 一起保留时不会被删）。"""
    removed = 0
    for directory in sorted((path for path in root.rglob("*") if path.is_dir()),
                            key=lambda value: -len(value.parts)):
        if not any(directory.iterdir()):
            directory.rmdir()
            removed += 1
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--library", default=str(ROOT / "library"), help="项目库根目录（默认 ./library）")
    parser.add_argument("--apply", action="store_true", help="真正删除（默认只预览）")
    args = parser.parse_args()

    library = Path(args.library).expanduser().resolve()
    if not library.is_dir():
        print(f"项目库不存在：{library}")
        return 1

    total_bytes = 0
    total_files = 0
    kept_files = 0
    for project in sorted(path for path in library.iterdir() if path.is_dir()):
        if project.name.startswith(".") or any(part in PROTECTED_DIRS for part in project.relative_to(library).parts):
            continue
        files = list(staging_files(project))
        if not files:
            continue
        sealed = sealed_hashes(project)
        removable, kept = [], []
        for path in files:
            (removable if sha256_file(path) in sealed else kept).append(path)
        kept_files += len(kept)
        if args.apply:
            for path in removable:
                path.unlink()
            for root in staging_roots(project):
                prune_empty_directories(root)
        if not removable:
            continue
        size = sum(path.stat().st_size for path in removable)
        total_bytes += size
        total_files += len(removable)
        print(f"{'remove' if args.apply else 'preview'} {len(removable):4d} 个文件 / {size / 1024 / 1024:7.1f} MiB  {project.name}")

    verb = "已删除" if args.apply else "可回收"
    print(f"\n{verb}：{total_files} 个文件 / {total_bytes / 1024 / 1024:.1f} MiB（保留 {kept_files} 个不在封存 manifest 中的文件）")
    if not args.apply and total_bytes:
        print("加 --apply 才会真正删除。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
