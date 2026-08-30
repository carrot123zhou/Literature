from __future__ import annotations

import argparse
import json
from pathlib import Path

from .exporters import export_all
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

    export = commands.add_parser("export")
    export.add_argument("project_id")
    export.add_argument("--mode", choices=["source", "translation", "bilingual"], default="translation")
    export.add_argument("--formats", nargs="+", default=["html", "md+json", "epub", "docx"])

    args = parser.parse_args()
    store = ProjectStore(args.library)
    if args.command == "create":
        result = store.create_project(args.pdf, title=args.title, source_language=args.source_language)
    elif args.command == "import-mineru":
        result = import_mineru(store, args.project_id, args.paths).to_dict()
    elif args.command == "show":
        result = store.load_document(args.project_id).to_dict()
    else:
        result = export_all(store, args.project_id, mode=args.mode, formats=args.formats)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
