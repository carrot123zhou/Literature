"""阅读对话：引用证据来源与（可选）真实服务端到端验收。

两个模式：

1. **默认（离线，不调模型）**：验证「引用来自译文栏时，证据仍然是原文」。
   直接调用 `literature.reader.build_ask_prompt()`（与 `/reader/ask`、`/reader/chat` 同一条组装路径），
   检查 citations 每一条：
   - `source_derived`（**唯一硬判据**）：citation.text 去掉摘要省略号后，必须逐字出现在对应 block 的
     `source_text` 里（允许是它的**裁剪**，例如 `_block_excerpt` 的截断）。
   - `equals_translation`：不得等于该 block 的译文（译文不能当证据）。
   - `has_cjk`：**只作辅助信息，不作判据**。原文本可以是中文（或中英混排），用「有没有中文」判证据来源
     会在中文书上直接误判；这里仅在默认夹具（英文书 + 中文译文）下作为可读性提示输出。

2. **`--live`（真实模型，一次请求）**：走完整 API 路径（`/reader/chat/messages`）问一句，
   再对**落盘后的消息**做同样的证据检查，并报告 usage/费用（无单价时为 null，不写 0）。

用法：
    .venv/bin/python scripts/reader_real_chat_check.py                  # 离线证据检查
    .venv/bin/python scripts/reader_real_chat_check.py --live           # 真实模型一次
    .venv/bin/python scripts/reader_real_chat_check.py --live --project <library 下的书名子串>

边界：只读复制生产 `model-services.json`、密钥仅从本机 Keychain 读；不写生产库、不改生产配置。
背景（2026-10-25）：早前 /tmp 里的临时脚本用硬编码 `"Silver" in text` 判定来源，在本书上必然为 false，
把「脚本自身的判据」误报成了 `quote_evidence_is_source:false`。本脚本用「逐字子串 vs 原文/译文」的正确判据。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
PRODUCTION_RUNTIME = Path.home() / "Library" / "Application Support" / "Literature"


def norm(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


def pick_book(pattern: str, need_translation: bool = True) -> Path:
    candidates = [path for path in sorted((REPO / "library").iterdir())
                  if (path / "document" / "document.json").is_file()]
    if need_translation:
        candidates = [path for path in candidates if (path / "translations" / "zh-CN" / "translation.json").is_file()]
    if pattern:
        candidates = [path for path in candidates if pattern.casefold() in path.name.casefold()]
    if not candidates:
        raise SystemExit(f"library/ 里没有符合条件的书（pattern={pattern!r}）")
    return candidates[0]


def prepare(base: Path, book: Path) -> tuple[str, Path]:
    runtime, library = base / "runtime", base / "library"
    runtime.mkdir(parents=True, exist_ok=True)
    library.mkdir(parents=True, exist_ok=True)
    (runtime / "session-token").write_text("", encoding="utf-8")
    source = PRODUCTION_RUNTIME / "model-services.json"
    if source.is_file():
        shutil.copyfile(source, runtime / "model-services.json")
    os.environ["LITERATURE_RUNTIME"] = str(runtime)
    os.environ["LITERATURE_LIBRARY"] = str(library)
    os.environ["LITERATURE_SESSION_TOKEN"] = ""

    from literature.storage import ProjectStore

    store = ProjectStore(library)
    origin = base / "sample.pdf"
    origin.write_bytes(b"%PDF-1.4\n% reader chat fixture")
    project = store.create_project(origin, title=book.name[:60])
    project_id = str(project["project_id"])
    target = store.project_path(project_id) / "document" / "document.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(book / "document" / "document.json", target)
    translation = book / "translations" / "zh-CN" / "translation.json"
    if translation.is_file():
        into = store.translation_path(project_id, "zh-CN")
        into.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(translation, into)
    return project_id, library


def quote_of(store, project_id: str) -> dict:
    """取一个「有译文、原文足够长」的段落，引用**译文**一侧（最容易把译文误当证据的场景）。"""
    document = store.load_document(project_id)
    translation = store.load_translation(project_id, "zh-CN")
    for block in document.blocks:
        if block.type != "paragraph" or len(block.source_text or "") < 200:
            continue
        entry = translation.entries.get(block.block_id)
        if entry is None or not (entry.text or "").strip():
            continue
        return {"block_id": block.block_id, "pane": "trans", "text": (entry.text or "")[:120]}
    raise SystemExit("这本书里没有「有译文的长正文段」可用于引用测试")


def evidence(store, project_id: str, citations: list[dict]) -> dict:
    from literature.reader import _block_excerpt  # noqa: F401  (同一个摘要函数)

    document = store.load_document(project_id)
    translation = store.load_translation(project_id, "zh-CN")
    blocks = {block.block_id: block for block in document.blocks}
    rows = []
    for item in citations:
        block = blocks.get(item.get("block_id", ""))
        if block is None:
            rows.append({"block_id": item.get("block_id"), "missing_block": True, "source_derived": False})
            continue
        entry = translation.entries.get(block.block_id)
        excerpt = norm(item.get("text", "")).rstrip("…")
        rows.append({
            "block_id": block.block_id,
            "source_derived": bool(excerpt) and excerpt in norm(block.source_text),
            "equals_translation": bool(entry and (entry.text or "").strip()
                                       and excerpt == norm(entry.text)[: len(excerpt)]),
            # 语言只作辅助信息：原文本完全可能是中文，不能拿「有中文」当成「用了译文」的判据。
            "has_cjk_hint": bool(re.search(r"[\u4e00-\u9fff]", item.get("text", ""))),
            "citation_text": item.get("text", "")[:80],
            "source_text": (block.source_text or "")[:80],
        })
    return {
        "rows": rows,
        "all_source_derived": bool(rows) and all(row.get("source_derived") for row in rows),
        "any_translation_as_evidence": any(row.get("equals_translation") for row in rows),
        # 判据只用「与 block 源文逐字/裁剪一致」，不用语言。下面两项仅作夹具说明。
        "criterion": "citation.text 去掉摘要省略号后必须是 block.source_text 的逐字子串（允许裁剪）",
        "fixture_note": "默认夹具为英文原书 + 中文译文；has_cjk_hint 只在这类夹具里有参考意义。",
    }


def offline(book: Path) -> dict:
    base = Path(tempfile.mkdtemp(prefix="reader-chat-offline-"))
    project_id, _library = prepare(base, book)
    from literature.reader import _sections_for_ask, build_ask_prompt
    from literature.storage import ProjectStore

    store = ProjectStore(os.environ["LITERATURE_LIBRARY"])
    quote = quote_of(store, project_id)
    document = store.load_document(project_id)
    display, source = _sections_for_ask(store, project_id, "zh-CN")
    _system, user, citations, nums = build_ask_prompt(
        document, display, question="这段话的核心主张是什么？请用中文回答。",
        quote=quote, locale="zh-CN", source_sections=source,
    )
    report = {
        "mode": "offline", "book": book.name[:60], "quote": quote,
        "citations": citations, "evidence": evidence(store, project_id, citations),
        "prompt_contains_source_paragraph": (document.block(quote["block_id"]).source_text or "")[:60] in user,
        "prompt_contains_translation_with_label": "in the reader's language" in user,
    }
    report["ok"] = bool(report["evidence"]["all_source_derived"]) and not report["evidence"]["any_translation_as_evidence"]
    return report


def live(book: Path) -> dict:
    base = Path(tempfile.mkdtemp(prefix="reader-chat-live-"))
    project_id, _library = prepare(base, book)
    from fastapi.testclient import TestClient

    from literature.api import create_app
    from literature.storage import ProjectStore

    store = ProjectStore(os.environ["LITERATURE_LIBRARY"])
    quote = quote_of(store, project_id)
    client = TestClient(create_app(store), raise_server_exceptions=False)
    headers = {"x-literature-token": client.app.state.session_token}
    started = client.post(f"/projects/{project_id}/reader/chat/messages", headers=headers, json={
        "question": "这段话的核心主张是什么？请用中文回答。", "locale": "zh-CN", "quote": quote,
    })
    if started.status_code != 200:
        return {"mode": "live", "error": started.text[:400], "ok": False}
    assistant_id = started.json()["assistant_message_id"]
    deadline = time.time() + 600
    message = {}
    while time.time() < deadline:
        chat = client.get(f"/projects/{project_id}/reader/chat", headers=headers).json()
        message = next(item for item in chat["messages"] if item["id"] == assistant_id)
        if message["state"] != "sending":
            break
        time.sleep(1)
    records = store.usage_records(project_id)
    chat_usage = [row for row in records if row["scope"] == "reader_chat"]
    out = sum(row["output_tokens"] for row in chat_usage)
    reasoning = sum(row["reasoning_tokens"] for row in chat_usage)
    report = {
        "mode": "live", "book": book.name[:60], "quote": quote,
        "state": message.get("state"), "error": message.get("error", ""),
        "model": message.get("model"), "binding": message.get("binding"),
        "answer": (message.get("answer") or "")[:600],
        "citations": message.get("citations") or [],
        "evidence": evidence(store, project_id, message.get("citations") or []),
        "usage": {
            "calls": len(chat_usage), "output": out, "reasoning": reasoning,
            "reasoning_share": round(reasoning / out, 3) if out else None,
            "cost": [row["estimated_cost"] for row in chat_usage],
        },
        "chat_file": (store.project_path(project_id) / "reader" / "chat.json").is_file(),
    }
    report["ok"] = (
        report["state"] == "done"
        and bool(report["citations"])
        and report["evidence"]["all_source_derived"]
        and not report["evidence"]["any_translation_as_evidence"]
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="真实模型一次（默认只做离线证据检查）")
    parser.add_argument("--book", default="Between Cultures")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    book = pick_book(args.book)
    report = live(book) if args.live else offline(book)
    out = REPO / (args.out or ("tmp/reader-real-chat.json" if args.live else "tmp/reader-quote-evidence.json"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "mode": report.get("mode"),
        "ok": report.get("ok"),
        "citations": [row["block_id"] for row in report.get("citations") or []],
        "all_source_derived": (report.get("evidence") or {}).get("all_source_derived"),
        "any_translation_as_evidence": (report.get("evidence") or {}).get("any_translation_as_evidence"),
        "criterion": (report.get("evidence") or {}).get("criterion"),
        "state": report.get("state"),
        "usage": report.get("usage"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("PASS" if report.get("ok") else "FAIL")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
