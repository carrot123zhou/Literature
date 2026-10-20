"""导出真脚注的真实书回归：库内每本书导出 EPUB+DOCX，统计绑定率与残留标记，然后删掉导出记录。

用法（不写 raw/document/translations，只临时写 exports/ 再清理）：

    .venv/bin/python tests/manual/export_footnote_check.py
    .venv/bin/python tests/manual/export_footnote_check.py "Making Transcendents"

校验内容：
- EPUB：``aside epub:type="footnote"`` 数量 == 绑定注数；全文（含 nav）不残留 ``^{`` 标记。
- DOCX：``word/footnotes.xml`` 里的脚注数 == 绑定注数；每个 ``w:footnoteReference`` 的 id
  只出现一次（Word 不允许重复引用，重复会被修文档）；正文不残留 ``^{``；
  用 python-docx 复开确认文件可解析。
"""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from literature.exporters import build_plan, create_export, delete_export  # noqa: E402
from literature.storage import ProjectStore  # noqa: E402


def check_project(store: ProjectStore, project_id: str, title: str, locale: str) -> bool:
    document = store.load_document(project_id)
    plan = build_plan(document.blocks)
    notes = [block for block in document.blocks if block.type == "footnote"]
    mode = "bilingual" if (store.project_path(project_id) / "translations" / locale).is_dir() else "source"
    value = create_export(store, project_id, formats=["epub", "docx"], mode=mode, locale=locale)
    root = Path(value["path"])
    ok = True
    try:
        with zipfile.ZipFile(next(root.glob("**/*.epub"))) as archive:
            names = [n for n in archive.namelist() if n.endswith((".xhtml", ".opf"))]
            text = "".join(archive.read(name).decode("utf-8") for name in names)
        asides = len(re.findall(r'<aside class="footnotes" epub:type="footnote"', text))
        refs = len(re.findall(r'epub:type="noteref"', text))
        aside_ids = re.findall(r'<aside class="footnotes" epub:type="footnote" id="([^"]+)"', text)
        duplicate_ids = {i for i in aside_ids if aside_ids.count(i) > 1}
        # 每个 noteref 的 href 都要能解析（含跨文件 text/section-XXXX.xhtml#fn-…）。
        anchors: dict[str, set[str]] = {}
        broken = 0
        for name in [n for n in archive.namelist() if n.startswith("EPUB/text/") and n.endswith(".xhtml")]:
            page = archive.read(name).decode("utf-8")
            anchors[name] = set(re.findall(r'id="(fn-[^"]+)"', page))
        for name in anchors:
            page = archive.read(name).decode("utf-8")
            for match in re.finditer(r'<a class="noteref"[^>]*href="([^"]+)"', page):
                href = match.group(1)
                if href.startswith("#"):
                    broken += href[1:] not in anchors[name]
                else:
                    target, fragment = href.split("#", 1)
                    broken += fragment not in anchors.get(f"EPUB/{target}", set())
        ok &= asides == len(plan.links) and "^{" not in text
        ok &= not duplicate_ids and broken == 0
        print(f"  EPUB  asides={asides} 生成noteref={refs} 重复aside id={len(duplicate_ids)} "
              f"断链={broken} 残留标记={text.count('^{')}")

        docx = next(root.glob("**/*.docx"))
        with zipfile.ZipFile(docx) as archive:
            document_xml = archive.read("word/document.xml").decode("utf-8")
            has_footnotes = "word/footnotes.xml" in archive.namelist()
            footnotes_xml = archive.read("word/footnotes.xml").decode("utf-8") if has_footnotes else ""
            content_types = archive.read("[Content_Types].xml").decode("utf-8")
        footnote_ids = re.findall(r"<w:footnote w:id=\"(\d+)\">", footnotes_xml)
        reference_ids = re.findall(r'w:footnoteReference[^>]*w:id="(\d+)"', document_xml)
        duplicates = {value for value in reference_ids if reference_ids.count(value) > 1}
        ok &= len(footnote_ids) == len(plan.links)
        ok &= not duplicates
        if plan.links:  # 无绑定注文时不建 footnotes 部件
            ok &= "footnotes+xml" in content_types
        ok &= "^{" not in document_xml and "^{" not in footnotes_xml
        print(
            f"  DOCX  footnotes={len(footnote_ids)} 引用={len(reference_ids)} "
            f"重复引用={len(duplicates)} 残留标记={document_xml.count('^{') + footnotes_xml.count('^{')}"
        )
        from docx import Document as WordDocument

        WordDocument(str(docx))
        print("  DOCX  可由 python-docx 复开")
    finally:
        delete_export(store, project_id, value["export_id"])
    print(f"{'PASS' if ok else 'FAIL'} {title[:48]} 注={len(notes)} 绑定={len(plan.links)} 模式={mode}")
    return ok


def main() -> int:
    needle = sys.argv[1] if len(sys.argv) > 1 else ""
    store = ProjectStore(ROOT / "library")
    results = []
    for meta in store.list_projects():
        if needle and needle not in meta["title"]:
            continue
        print(f"\n{meta['title']}")
        results.append(check_project(store, meta["project_id"], meta["title"], "zh-CN"))
    if not results:
        print("没有匹配的项目")
        return 1
    print(f"\n{sum(results)}/{len(results)} PASS")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
