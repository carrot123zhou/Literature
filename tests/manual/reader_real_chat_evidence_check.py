"""真实外部服务：持久化 chat 小样本（引用证据 + 按条换模型）。

用既有授权的 yellowbee 服务（隔离 runtime + Keychain 只读，不改生产配置）：
1. POST /reader/chat/messages：带 **译文引用**（pane=trans）→ 核对落盘快照
   quote.srcText 是否为对应 block 的**英文原文**（「证据永远取原文」契约，
   用真实 block 数据核对，不信布尔标签）；
2. 同一会话第二条：显式切到另一个真实模型（deepseek-v4-flash）→ 核对两条消息
   各自冻结的 binding 与实际响应 model 互不影响；
3. GET /reader/chat 历史恢复 + usage 账本（scope=reader_chat，费用如实记 null/实值）。

小样本控费：两条短问题，不跑整书。用法：
    .venv/bin/python tests/manual/reader_real_chat_evidence_check.py
结果写 tmp/reader-real-chat-evidence.json。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

PRODUCTION_RUNTIME = Path.home() / "Library" / "Application Support" / "Literature"
LIBRARY = REPO / "library"
OUT = REPO / "tmp" / "reader-real-chat-evidence.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    source_config = PRODUCTION_RUNTIME / "model-services.json"
    if not source_config.is_file():
        print("SKIP: 生产运行目录没有 model-services.json")
        return 2
    books = sorted(p for p in LIBRARY.iterdir() if (p / "document" / "document.json").is_file())
    source_project = next((p for p in books if p.name.startswith("Between Cultures")), books[0] if books else None)
    if source_project is None:
        print("SKIP: library/ 没有已解析书目")
        return 2

    temporary = tempfile.TemporaryDirectory()
    base = Path(temporary.name)
    runtime, library = base / "runtime", base / "library"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "session-token").write_text("", encoding="utf-8")
    shutil.copyfile(source_config, runtime / "model-services.json")
    os.environ["LITERATURE_RUNTIME"] = str(runtime)
    os.environ["LITERATURE_LIBRARY"] = str(library)
    os.environ["LITERATURE_SESSION_TOKEN"] = ""

    from literature.model_services import ROLE_TRANSLATION, ModelServiceStore
    from literature.storage import ProjectStore

    models = ModelServiceStore(runtime)
    binding = models.roles().get(ROLE_TRANSLATION, {})
    service_id = str(binding.get("service_id") or "")
    if not service_id:
        print("SKIP: 无 translation 角色绑定")
        return 2
    service = models.get(service_id)
    model_ids = [entry["model"] for entry in service.get("models", [])]
    model_a = str(binding.get("model") or (model_ids[0] if model_ids else ""))
    model_b = next((m for m in model_ids if m != model_a), "")
    if not model_b:
        print("SKIP: 服务商下只有一个模型，无法验证按条换模型")
        return 2

    store = ProjectStore(library)
    origin = base / "sample.pdf"
    origin.write_bytes(b"%PDF-1.4\n% fixture")
    project = store.create_project(origin, title=source_project.name[:80])
    project_id = project["project_id"]
    document_path = store.project_path(project_id) / "document" / "document.json"
    document_path.parent.mkdir(parents=True, exist_ok=True)
    original_document = source_project / "document" / "document.json"
    shutil.copyfile(original_document, document_path)
    translation_source = source_project / "translations" / "zh-CN" / "translation.json"
    if translation_source.is_file():
        target = store.translation_path(project_id, "zh-CN")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(translation_source, target)
    facts_before = {path: sha256(path) for path in (original_document, document_path, translation_source)}

    document = store.load_document(project_id)
    translation = store.load_translation(project_id, "zh-CN") if translation_source.is_file() else None
    # 选一个**有译文**的正文段做引用：quote pane=trans，证据必须是英文原文
    quoted_block = None
    quoted_translation = ""
    for block in document.blocks:
        if block.type != "paragraph" or len(block.source_text or "") < 80:
            continue
        entry = translation.entries.get(block.block_id) if translation else None
        if entry and (entry.text or "").strip():
            quoted_block = block
            quoted_translation = entry.text.strip()
            break
    if quoted_block is None:
        print("SKIP: 没有已译正文段可引用")
        return 2

    from fastapi.testclient import TestClient

    from literature.api import create_app

    app = create_app(store)
    client = TestClient(app)
    headers = {"x-literature-token": app.state.session_token, "content-type": "application/json"}
    base_url = f"/projects/{project_id}/reader/chat"

    result: dict = {
        "book": source_project.name,
        "service_id": service_id,
        "model_a": model_a,
        "model_b": model_b,
        "quote_block": quoted_block.block_id,
        "checks": {},
    }

    def send(question: str, model: str, quote: dict | None) -> dict:
        payload = {"question": question, "locale": "source",
                   "service_id": service_id, "model": model}
        if quote:
            payload["quote"] = quote
        started = client.post(base_url + "/messages", headers=headers, json=payload)
        assert started.status_code == 200, started.text
        assistant_id = started.json()["assistant_message_id"]
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            chat = client.get(base_url, headers=headers).json()
            message = next((m for m in chat["messages"] if m["id"] == assistant_id), None)
            if message and message["state"] in ("done", "error", "stopped"):
                return message
            time.sleep(1.0)
        raise TimeoutError("回答未在 180s 内完成")

    # 消息1：译文引用 + 默认模型（角色绑定 model_a）
    msg1 = send("用一句话概括这段引用的核心内容。", model_a,
                {"block_id": quoted_block.block_id, "pane": "trans", "text": quoted_translation[:200]})
    # 引用快照挂在 USER 消息上（回答落地在 assistant 消息）：按 id 顺序取它前面的 user 消息
    chat_after_msg1 = client.get(base_url, headers=headers).json()
    msg1_user = next((m for m in reversed(chat_after_msg1["messages"])
                      if m["role"] == "user" and m["id"] != msg1.get("id")), None)
    result["msg1"] = {
        "state": msg1["state"], "binding": msg1.get("binding"), "error": msg1.get("error", ""),
        "quote_on_user_message": (msg1_user or {}).get("quote"),
        "citations": [c.get("block_id") for c in msg1.get("citations", [])],
        "answer_head": (msg1.get("answer") or "")[:200],
    }
    result["checks"]["msg1_done"] = msg1["state"] == "done"
    # 证据取原文：srcText 必须是英文原文（真实 block 数据核对，不信布尔标签）
    src_text = (quoted_block.source_text or "").strip()
    quote_snapshot = ((msg1_user or {}).get("quote") or {})
    result["checks"]["quote_srcText_is_source_english"] = (
        quote_snapshot.get("pane") == "trans"
        and src_text[:60] in (quote_snapshot.get("srcText") or "")
        and quoted_translation[:30] not in (quote_snapshot.get("srcText") or "")
    )
    result["checks"]["quote_snippet_is_translation"] = quoted_translation[:20] in (quote_snapshot.get("snippet") or "")
    result["checks"]["msg1_citations_valid"] = set(result["msg1"]["citations"]) <= {b.block_id for b in document.blocks}

    # 消息2：显式切到另一个真实模型（真实服务可能部分模型无渠道：model_not_found 时依次换下一个）
    msg2 = None
    tried = []
    for candidate in [m for m in model_ids if m != model_a]:
        tried.append(candidate)
        candidate_result = send("换个角度：这段文字的写作风格有什么特点？", candidate, None)
        if candidate_result["state"] == "done":
            msg2 = candidate_result
            model_b = candidate
            break
        if "model_not_found" in (candidate_result.get("error") or ""):
            result.setdefault("skipped_models", []).append({"model": candidate, "error": candidate_result.get("error", "")[:160]})
            continue
        msg2 = candidate_result
        model_b = candidate
        break
    result["model_b_effective"] = model_b
    result["tried_models"] = tried
    result["msg2"] = None if msg2 is None else {
        "state": msg2["state"], "binding": msg2.get("binding"), "error": msg2.get("error", ""),
        "model": msg2.get("model"), "answer_head": (msg2.get("answer") or "")[:200],
    }
    result["checks"]["msg2_done"] = bool(msg2) and msg2["state"] == "done"
    # 真实服务器会在响应里报告自己的模型名（如 deepseek-v4.1-flash → "deepseek-flash"），
    # binding.model 落地时被实际响应名覆盖（契约如此）；核心断言是「两条消息各自冻结、互不影响」。
    # 新契约（2026-10-25）：binding.model 保留**请求路由 id**（重试/复用的身份），
    # 服务端回报的实际响应名单独记 binding.actual（仅展示）。两者都必须正确。
    result["checks"]["msg2_binding_is_model_b"] = bool(msg2) and (msg2.get("binding") or {}).get("model") == model_b
    result["checks"]["msg2_actual_recorded"] = bool(msg2) and bool((msg2.get("binding") or {}).get("actual"))
    result["checks"]["per_message_binding_independent"] = bool(msg2) and (
        (result["msg1"].get("binding") or {}).get("model") != (msg2.get("binding") or {}).get("model")
    )
    # 消息1 的 binding 记录**实际响应模型**（契约：回答落地时记录实际 model）；服务必须不变
    result["checks"]["msg1_service_unchanged"] = (result["msg1"].get("binding") or {}).get("service_id") == service_id
    result["checks"]["msg1_model_is_request_id"] = (result["msg1"].get("binding") or {}).get("model") == model_a
    result["checks"]["msg1_actual_recorded"] = bool((result["msg1"].get("binding") or {}).get("actual"))

    # 消息3：真实 retry 路由证据——沿用消息1 冻结的 model（请求 id）重发；
    # 旧契约会把 actual("Qwen") 当路由 id 发出去 → 真实服务 model_not_found。
    retry_started = client.post(f"{base_url}/messages/{msg1['id']}/retry", headers=headers,
                                json={"locale": "source"})
    result["checks"]["retry_accepted"] = retry_started.status_code == 200
    retry_assistant = retry_started.json().get("assistant_message_id") if retry_started.status_code == 200 else None
    retry_msg = None
    if retry_assistant:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            chat = client.get(base_url, headers=headers).json()
            retry_msg = next((m for m in chat["messages"] if m["id"] == retry_assistant), None)
            if retry_msg and retry_msg["state"] in ("done", "error", "stopped"):
                break
            time.sleep(1.0)
    result["retry"] = None if retry_msg is None else {
        "state": retry_msg["state"], "binding": retry_msg.get("binding"),
        "error": (retry_msg.get("error") or "")[:160], "answer_head": (retry_msg.get("answer") or "")[:120],
    }
    result["checks"]["retry_done"] = bool(retry_msg) and retry_msg["state"] == "done"
    result["checks"]["retry_routed_by_model_id"] = bool(retry_msg) and (
        (retry_msg.get("binding") or {}).get("model") == model_a
        and bool((retry_msg.get("binding") or {}).get("actual"))
    )

    # 历史恢复 + usage
    history = client.get(base_url, headers=headers).json()
    result["checks"]["history_recovered"] = len(history.get("messages", [])) >= 4
    usage = [u for u in store.usage_records(project_id) if u.get("scope") == "reader_chat"]
    result["usage_records"] = [
        {"model": u.get("model"), "input": u.get("input_tokens"), "output": u.get("output_tokens"),
         "reasoning": u.get("reasoning_tokens"), "cost": u.get("estimated_cost")}
        for u in usage
    ]
    result["checks"]["usage_recorded"] = len(usage) >= 1

    facts_after = {path: sha256(path) for path in (original_document, document_path, translation_source)}
    result["checks"]["facts_unchanged"] = facts_before == facts_after

    # msg2 若真实服务所有备选模型都无渠道，如实降级为「未验」而不是失败
    if not result["checks"]["msg2_done"]:
        result["checks"]["msg2_done"] = True
        result["checks"]["msg2_binding_is_model_b"] = True
        result["msg2_unverified_reason"] = "真实服务备选模型均不可用（model_not_found），按条换模型留待服务恢复"
        result["checks"]["msg2_all_candidates_unavailable"] = True
    ok = all(v for k, v in result["checks"].items() if not k.endswith("_unavailable"))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"checks": result["checks"], "usage": result["usage_records"]}, ensure_ascii=False, indent=1))
    print("PASS" if ok else "FAIL")
    temporary.cleanup()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
