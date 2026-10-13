from __future__ import annotations

import argparse
import json
from pathlib import Path

from .mineru import import_mineru
from .storage import ProjectStore


def main() -> None:
    parser = argparse.ArgumentParser(prog="literature")
    parser.add_argument("--library", default=None, help="Literature 项目库目录")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create")
    create.add_argument("pdf")
    create.add_argument("--title", default="")
    create.add_argument("--source-language", default="auto")

    ingest = commands.add_parser("import-mineru")
    ingest.add_argument("project_id")
    ingest.add_argument("paths", nargs="+")

    show = commands.add_parser("show")
    show.add_argument("project_id")

    args = parser.parse_args()
    store = ProjectStore(args.library)
    if args.command == "create":
        result = store.create_project(args.pdf, title=args.title, source_language=args.source_language)
    elif args.command == "import-mineru":
        result = import_mineru(store, args.project_id, args.paths).to_dict()
    elif args.command == "show":
        result = store.load_document(args.project_id).to_dict()
    else:  # pragma: no cover - argparse 已限制取值
        raise SystemExit(f"未知命令：{args.command}")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
