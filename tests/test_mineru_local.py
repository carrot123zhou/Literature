from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from literature import mineru_local
from literature.mineru_local import (
    MINERU_VERSION,
    EngineBusyError,
    LocalMinerUEngine,
    LocalMinerUBackend,
    _extract_kit_zip,
    _validate_derived_content,
    _write_layout_from_pdf,
    set_shared_engine,
)
from literature.mineru_workflow import JobState, MinerUWorkflow, SliceState, WorkflowError
from literature.parse_backends import LOCAL_BACKEND_ID, REMOTE_BACKEND_ID, capabilities


def make_pdf(path: Path, pages: int = 2) -> None:
    import fitz

    document = fitz.open()
    for _ in range(pages):
        document.new_page(width=300, height=400)
    document.save(path)
    document.close()


class EngineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self._previous_engine = mineru_local._default_engine
        self._previous_runtime = os.environ.get("LITERATURE_RUNTIME")
        # 每个测试独立 runtime：selection/引擎根目录互不污染。
        os.environ["LITERATURE_RUNTIME"] = str(self.root / "runtime")
        set_shared_engine(None)

    def tearDown(self) -> None:
        set_shared_engine(self._previous_engine)
        if self._previous_runtime is None:
            os.environ.pop("LITERATURE_RUNTIME", None)
        else:
            os.environ["LITERATURE_RUNTIME"] = self._previous_runtime
        self.temporary.cleanup()

    def make_engine(self, name: str = "engine") -> LocalMinerUEngine:
        return LocalMinerUEngine(self.root / name)

    def fake_uv(self, engine: LocalMinerUEngine) -> None:
        """预置一个假 uv 可执行文件，避免测试真实联网下载。"""
        uv = engine.root / "uv" / "uv-0.13.5" / "bin" / "uv"
        uv.parent.mkdir(parents=True, exist_ok=True)
        uv.write_text("#!/bin/sh\n", encoding="utf-8")
        uv.chmod(0o755)

    def fake_ready(self, engine: LocalMinerUEngine) -> None:
        """构造一个「已安装」的假引擎目录（kit 二进制 + 模型目录 + 标记）。"""
        engine.root.mkdir(parents=True, exist_ok=True)
        engine.kit_bin.parent.mkdir(parents=True, exist_ok=True)
        engine.kit_bin.write_text("#!/bin/sh\n", encoding="utf-8")
        engine.models_dir.mkdir(parents=True, exist_ok=True)
        (engine.models_dir / "model.onnx").write_bytes(b"weights")
        engine.marker_file.write_text(f"mineru=={MINERU_VERSION}\n2026-10-25 00:00:00\n", encoding="utf-8")


class LocalEngineInstallTests(EngineTestCase):
    def test_install_success_writes_marker_last_and_becomes_ready(self) -> None:
        engine = self.make_engine()
        self.fake_uv(engine)
        commands: list[list[str]] = []

        def fake_stream(command, *, label):
            commands.append([str(item) for item in command])
            # pip 步骤对应真实安装 mineru：产出 kit 二进制；models 步骤产出权重。
            if any(str(item).endswith("pip") for item in command):
                engine.kit_bin.parent.mkdir(parents=True, exist_ok=True)
                engine.kit_bin.write_text("#!/bin/sh\n", encoding="utf-8")
            if "models" in [str(item) for item in command]:
                engine.models_dir.mkdir(parents=True, exist_ok=True)
                (engine.models_dir / "m.safetensors").write_bytes(b"w")
            return 0

        with patch.object(engine, "_stream", side_effect=fake_stream), \
             patch.object(engine, "_capture", return_value=f"{MINERU_VERSION}\n"):
            status = engine.install()
            self.assertTrue(status["installing"])
            deadline = time.time() + 10
            while engine.status()["installing"] and time.time() < deadline:
                time.sleep(0.02)
        self.assertFalse(engine.status()["installing"])
        self.assertTrue(engine.is_ready())
        self.assertEqual(engine.installed_version(), MINERU_VERSION)
        self.assertEqual(engine.status()["phase"], "done")
        # python/packages/models 三步都执行（uv 已在预置下跳过下载）。
        self.assertEqual(len(commands), 3)
        self.assertTrue(any("venv" in item for item in commands[0]))
        self.assertTrue(any("pip" in item for item in commands[1]))
        self.assertIn("models", commands[2])
        state = engine._read_state()
        self.assertEqual(state["phase"], "done")
        self.assertFalse(state["installing"])

    def test_install_failure_keeps_not_installed_and_persists_error(self) -> None:
        engine = self.make_engine()
        self.fake_uv(engine)

        def failing_stream(command, *, label):
            if any(str(item).endswith("pip") for item in command):
                raise WorkflowError("依赖安装失败（退出码 1）；详情见安装日志。")
            if any(str(item).endswith("pip") for item in command):
                engine.kit_bin.parent.mkdir(parents=True, exist_ok=True)
                engine.kit_bin.write_text("#!/bin/sh\n", encoding="utf-8")
            if "models" in [str(item) for item in command]:
                engine.models_dir.mkdir(parents=True, exist_ok=True)
                (engine.models_dir / "m.safetensors").write_bytes(b"w")
            return 0

        with patch.object(engine, "_stream", side_effect=failing_stream), \
             patch.object(engine, "_capture", return_value=MINERU_VERSION):
            engine.install()
            deadline = time.time() + 10
            while engine.status()["installing"] and time.time() < deadline:
                time.sleep(0.02)
        status = engine.status()
        self.assertFalse(status["installed"])
        self.assertEqual(status["phase"], "failed")
        self.assertIn("依赖安装失败", status["error"])
        self.assertFalse(engine.marker_file.exists())

    def test_cancel_marks_cancelled_without_marker_and_resume_completes(self) -> None:
        engine = self.make_engine()
        self.fake_uv(engine)

        def cancelling_stream(command, *, label):
            engine._cancel_event.set()
            return 0

        with patch.object(engine, "_stream", side_effect=cancelling_stream):
            engine.install()
            deadline = time.time() + 10
            while engine.status()["installing"] and time.time() < deadline:
                time.sleep(0.02)
        status = engine.status()
        self.assertEqual(status["phase"], "cancelled")
        self.assertFalse(status["installed"])
        self.assertIn("取消", status["error"])

        # 断点续装：再次 install 走完全部步骤后完成。
        commands: list[list[str]] = []

        def ok_stream(command, *, label):
            commands.append([str(item) for item in command])
            if any(str(item).endswith("pip") for item in command):
                engine.kit_bin.parent.mkdir(parents=True, exist_ok=True)
                engine.kit_bin.write_text("#!/bin/sh\n", encoding="utf-8")
            if "models" in [str(item) for item in command]:
                engine.models_dir.mkdir(parents=True, exist_ok=True)
                (engine.models_dir / "m.safetensors").write_bytes(b"w")
            return 0

        with patch.object(engine, "_stream", side_effect=ok_stream), \
             patch.object(engine, "_capture", return_value=MINERU_VERSION):
            engine.install()
            deadline = time.time() + 10
            while engine.status()["installing"] and time.time() < deadline:
                time.sleep(0.02)
        self.assertTrue(engine.is_ready())
        # 断点续装只补跑未完成的步骤：python（venv 未建成）+ packages + models。
        self.assertEqual(len(commands), 3)

    def test_ready_requires_marker_written_last(self) -> None:
        engine = self.make_engine()
        engine.root.mkdir(parents=True, exist_ok=True)
        engine.kit_bin.parent.mkdir(parents=True, exist_ok=True)
        engine.kit_bin.write_text("#!/bin/sh\n", encoding="utf-8")
        engine.models_dir.mkdir(parents=True, exist_ok=True)
        # 没有 installed.txt：即使二进制与模型齐备也不算已安装。
        self.assertFalse(engine.is_ready())

    def test_uninstall_removes_only_engine_root(self) -> None:
        engine = self.make_engine()
        self.fake_ready(engine)
        library_file = self.root / "keep.txt"
        library_file.write_text("user data", encoding="utf-8")
        status = engine.uninstall()
        self.assertFalse(status["installed"])
        self.assertFalse(engine.root.exists())
        self.assertTrue(library_file.exists())

    def test_uninstall_refused_while_job_active(self) -> None:
        engine = self.make_engine()
        self.fake_ready(engine)
        engine.register_job("job-1")
        with self.assertRaises(EngineBusyError):
            engine.uninstall()
        engine.unregister_job("job-1")
        engine.uninstall()
        self.assertFalse(engine.root.exists())

    def test_uninstall_refused_while_installing(self) -> None:
        engine = self.make_engine()
        self.fake_uv(engine)
        started = threading.Event()
        release = threading.Event()

        def blocking_stream(command, *, label):
            started.set()
            release.wait(5)
            return 0

        # patch 必须在主线程持有：install() 启动 worker 后立即返回。
        with patch.object(engine, "_stream", side_effect=blocking_stream):
            thread = threading.Thread(target=engine.install, daemon=True)
            thread.start()
            self.assertTrue(started.wait(5))
            with self.assertRaises(EngineBusyError):
                engine.uninstall()
            engine.cancel()
            release.set()
            thread.join(10)
            deadline = time.time() + 5
            while engine.status()["installing"] and time.time() < deadline:
                time.sleep(0.02)

    def test_status_reports_bytes_and_active_jobs(self) -> None:
        engine = self.make_engine()
        self.fake_ready(engine)
        engine.register_job("abc")
        status = engine.status()
        self.assertGreater(status["bytes"], 0)
        self.assertEqual(status["active_jobs"], ["abc"])
        self.assertTrue(status["installed"])
        self.assertEqual(status["version"], MINERU_VERSION)


class LocalCapabilitiesTests(EngineTestCase):
    def test_capabilities_dynamic_ready(self) -> None:
        engine = self.make_engine()
        set_shared_engine(engine)
        self.fake_ready(engine)
        value = capabilities(LOCAL_BACKEND_ID)
        self.assertTrue(value.available)
        self.assertEqual(value.execution, "local")
        self.assertFalse(value.upload_url)
        self.assertFalse(value.polling)
        self.assertTrue(value.remote_job_control)
        self.assertEqual(value.version, MINERU_VERSION)
        from literature.parse_backends import describe_backends
        described = {item["backend_id"]: item for item in describe_backends()}
        self.assertTrue(described[LOCAL_BACKEND_ID]["available"])

    def test_capabilities_dynamic_unavailable_with_install_hint(self) -> None:
        engine = self.make_engine()
        set_shared_engine(engine)
        value = capabilities(LOCAL_BACKEND_ID)
        self.assertFalse(value.available)
        self.assertIn("安装", value.unavailable_reason)


class EngineSourceTests(EngineTestCase):
    """来源管理：外部 CLI 探测/选用、HTTP 明文契约、任务快照防静默切换。"""

    def build_external_engine(self, name: str = "ext", version: str = "4.0.10") -> Path:
        root = self.root / name
        kit = root / "venv" / "bin" / "mineru-kit"
        kit.parent.mkdir(parents=True, exist_ok=True)
        kit.write_text("#!/bin/sh\n", encoding="utf-8")
        (root / "installed.txt").write_text(f"mineru=={version}\n2026-10-25 00:00:00\n", encoding="utf-8")
        models = root / "mineru-home" / "models"
        models.mkdir(parents=True, exist_ok=True)
        (models / "weights.safetensors").write_bytes(b"w")
        return root

    def test_probe_external_engine_compatible(self) -> None:
        root = self.build_external_engine()
        probe = mineru_local.probe_external_engine(root)
        self.assertTrue(probe["available"])
        self.assertTrue(probe["compatible"])
        self.assertEqual(probe["version"], "4.0.10")
        self.assertTrue(probe["models_ready"])
        self.assertTrue(probe["kit_binary"])

    def test_probe_external_engine_incompatible_version(self) -> None:
        root = self.build_external_engine(version="3.4.2")
        probe = mineru_local.probe_external_engine(root)
        self.assertFalse(probe["compatible"])
        self.assertFalse(probe["available"])
        self.assertIn("3.4.2", probe["reason"])

    def test_probe_external_engine_missing_models(self) -> None:
        root = self.build_external_engine()
        import shutil
        shutil.rmtree(root / "mineru-home")
        probe = mineru_local.probe_external_engine(root)
        self.assertFalse(probe["available"])
        self.assertIn("模型", probe["reason"])

    def test_probe_rejects_directory_without_kit(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()
        probe = mineru_local.probe_external_engine(empty)
        self.assertFalse(probe["compatible"])
        self.assertIn("mineru-kit", probe["reason"])

    def test_select_external_engine_persists_and_capabilities_follow(self) -> None:
        root = self.build_external_engine()
        value = mineru_local.set_engine_selection("external_cli", external_dir=str(root))
        self.assertEqual(mineru_local.active_source_kind(), "external_cli")
        self.assertEqual(value["external_dir"], str(root))
        caps = capabilities(LOCAL_BACKEND_ID)
        self.assertTrue(caps.available)
        self.assertIn("外部安装", caps.display_name)
        # 切回 managed：能力回到未安装。
        mineru_local.set_engine_selection("managed")
        self.assertFalse(capabilities(LOCAL_BACKEND_ID).available)

    def test_select_rejects_incompatible_external(self) -> None:
        root = self.build_external_engine(version="2.1.4")
        with self.assertRaises(WorkflowError):
            mineru_local.set_engine_selection("external_cli", external_dir=str(root))

    def test_http_transport_plaintext_contract(self) -> None:
        # 复用 providers 回环规则：只有真正 loopback 豁免明文 HTTP；
        # 0.0.0.0 与 *.local 不是 loopback；显式确认后放行；HTTPS 不受限。
        validate = mineru_local.validate_http_endpoint
        with self.assertRaises(WorkflowError):
            validate("http://192.168.1.10:8000")
        with self.assertRaises(WorkflowError):
            validate("http://0.0.0.0:8000")
        with self.assertRaises(WorkflowError):
            validate("http://myhost.local:8000")
        self.assertEqual(validate("http://192.168.1.10:8000", allow_plaintext=True), "http://192.168.1.10:8000")
        self.assertEqual(validate("http://127.0.0.1:8000"), "http://127.0.0.1:8000")
        self.assertEqual(validate("http://127.5.5.5:8000"), "http://127.5.5.5:8000")  # 127/8 都是 loopback
        self.assertEqual(validate("http://[::1]:8000"), "http://[::1]:8000")
        self.assertEqual(validate("http://localhost:8000"), "http://localhost:8000")
        self.assertEqual(validate("https://mineru.internal:8000"), "https://mineru.internal:8000")
        # userinfo / query / fragment 歧义拒绝；末尾斜杠规范化。
        for bad in ("http://user:pass@192.168.1.10:8000", "http://127.0.0.1:8000/?x=1",
                    "http://127.0.0.1:8000/#frag", "ftp://127.0.0.1", "http://"):
            with self.assertRaises(WorkflowError):
                validate(bad)
        self.assertEqual(validate("http://127.0.0.1:8000/"), "http://127.0.0.1:8000")

    def test_job_snapshot_blocks_silent_source_switch(self) -> None:
        engine = self.make_engine()
        set_shared_engine(engine)
        self.fake_ready(engine)
        source = self.root / "book-snapshot.pdf"
        make_pdf(source, 1)
        workflow = MinerUWorkflow(self.root / "jobs-snapshot", merge_callback=lambda job, parse_result: None)
        # 创建时冻结 external_cli 来源（job.backend_config.engine_source）。
        ready_caps = capabilities(LOCAL_BACKEND_ID).__class__(
            backend_id=LOCAL_BACKEND_ID, display_name="外部", version="4.0.10", execution="local",
            supported_formats=(), slicing=True, polling=False, upload_url=False,
            remote_job_control=True, resumable=True, available=True,
        )
        with patch.object(mineru_local, "active_source_kind", return_value="external_cli"), \
             patch.object(mineru_local, "local_capabilities", return_value=ready_caps):
            job = workflow.create_job(source, self.root / "out-snapshot.epub", backend_id=LOCAL_BACKEND_ID)
        # engine_source 快照由 MinerUService._create_locked 写入；这里按同一契约注入。
        job.backend_config["engine_source"] = "external_cli"
        self.assertEqual(job.backend_config["engine_source"], "external_cli")
        backend = workflow._backend_for(job)
        ready_caps_2 = ready_caps
        with patch.object(mineru_local, "active_source_kind", return_value="external_cli"), \
             patch.object(mineru_local, "local_capabilities", return_value=ready_caps_2), \
             patch.object(mineru_local, "local_engine_ready", return_value=True):
            backend.prepare(job)
        with patch.object(mineru_local, "active_source_kind", return_value="managed"), \
             patch.object(mineru_local, "local_engine_ready", return_value=True):
            with self.assertRaises(WorkflowError):
                backend.execute(job)


class SourceIdentityFreezeTests(EngineTestCase):
    """来源身份快照：同 kind 不同地址/路径必须拒绝；执行只用冻结配置。"""

    def test_identity_snapshot_external_contains_paths_and_version(self) -> None:
        root = self.root / "ext"
        kit = root / "venv" / "bin" / "mineru-kit"
        kit.parent.mkdir(parents=True, exist_ok=True)
        kit.write_text("#!/bin/sh\n")
        (root / "installed.txt").write_text("mineru==4.0.10\n", encoding="utf-8")
        models = root / "mineru-home" / "models"
        models.mkdir(parents=True, exist_ok=True)
        (models / "w.bin").write_bytes(b"w")
        mineru_local.set_engine_selection("external_cli", external_dir=str(root))
        identity = mineru_local.source_identity_snapshot()
        self.assertEqual(identity["kind"], "external_cli")
        self.assertEqual(identity["version"], "4.0.10")
        self.assertTrue(identity["kit_binary"].endswith("mineru-kit"))
        self.assertNotIn("key", {k.lower() for k in identity})  # 快照不含密钥

    def test_identity_snapshot_http_normalizes_url(self) -> None:
        mineru_local._write_selection({"kind": "http_service", "http_url": "http://127.0.0.1:8000/", "allow_plaintext_http": False})
        identity = mineru_local.source_identity_snapshot()
        self.assertEqual(identity["http_url"], "http://127.0.0.1:8000")

    def test_same_kind_different_url_is_rejected(self) -> None:
        engine = self.make_engine()
        set_shared_engine(engine)
        source = self.root / "book-freeze.pdf"
        make_pdf(source, 1)
        workflow = MinerUWorkflow(self.root / "jobs-freeze", merge_callback=lambda job, parse_result: None)
        ready_probe = {"available": True, "compatible": True, "version": "4.0.10", "tiers": ["standard"]}
        with patch.object(mineru_local, "active_source_kind", return_value="http_service"), \
             patch.object(mineru_local, "probe_http_service", return_value=ready_probe), \
             patch.object(mineru_local, "get_engine_selection",
                          return_value={"kind": "http_service", "http_url": "http://127.0.0.1:8000"}):
            job = workflow.create_job(source, self.root / "out-freeze.epub", backend_id=LOCAL_BACKEND_ID)
            job.backend_config["engine_source"] = "http_service"
            job.backend_config["engine_source_config"] = {"kind": "http_service", "http_url": "http://127.0.0.1:8000"}
        backend = workflow._backend_for(job)
        # 同 kind、不同地址 → 拒绝（绝不把同一文档静默发给新服务）。
        with patch.object(mineru_local, "active_source_kind", return_value="http_service"), \
             patch.object(mineru_local, "local_engine_ready", return_value=True):
            with self.assertRaises(WorkflowError):
                backend._check_source_snapshot(job)

    def test_external_kit_missing_after_upgrade_is_rejected(self) -> None:
        engine = self.make_engine()
        set_shared_engine(engine)
        root = self.root / "ext-gone"
        kit = root / "venv" / "bin" / "mineru-kit"
        kit.parent.mkdir(parents=True, exist_ok=True)
        kit.write_text("#!/bin/sh\n")
        (root / "installed.txt").write_text("mineru==4.0.10\n", encoding="utf-8")
        models = root / "mineru-home" / "models"
        models.mkdir(parents=True, exist_ok=True)
        (models / "w.bin").write_bytes(b"w")
        mineru_local.set_engine_selection("external_cli", external_dir=str(root))
        identity = mineru_local.source_identity_snapshot()
        # 外部应用升级/卸载：kit 消失 → 明确失败，不静默换路径。
        kit.unlink()
        backend = LocalMinerUBackend(None, engine=engine)
        with self.assertRaises(WorkflowError):
            backend._cli_toolchain(identity)

    def test_http_key_is_bound_per_address(self) -> None:
        import httpx

        captured: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(dict(request.headers))
            return httpx.Response(200, json={"version": "4.0.10"})

        real_client = httpx.Client
        def fake_client(**kwargs):
            kwargs.pop("timeout", None); kwargs.pop("follow_redirects", None); kwargs.pop("trust_env", None)
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        # 服务 A 已绑定密钥；探测新地址 B 不得带 A 的密钥。
        with patch.object(mineru_local, "get_http_service_key", lambda url: "KEY-A" if url == "http://127.0.0.1:9000" else ""), \
             patch("httpx.Client", fake_client):
            # 每次探测发出 health + tiers 两个请求。
            mineru_local.probe_http_service("http://127.0.0.1:9000")
            self.assertEqual(captured[0].get("authorization"), "Bearer KEY-A")
            self.assertEqual(captured[1].get("authorization"), "Bearer KEY-A")
            # 新地址 B：绝不带 A 的密钥。
            mineru_local.probe_http_service("http://127.0.0.1:9001")
            self.assertNotIn("authorization", captured[2])
            self.assertNotIn("authorization", captured[3])
            # 显式新密钥优先于绑定密钥。
            mineru_local.probe_http_service("http://127.0.0.1:9000", key="KEY-EXPLICIT")
            self.assertEqual(captured[4].get("authorization"), "Bearer KEY-EXPLICIT")

    def test_select_with_key_saves_only_after_probe_success(self) -> None:
        saved: list[tuple[str, str]] = []
        ready_probe = {"available": True, "compatible": True, "version": "4.0.10", "tiers": ["standard"]}
        fail_probe = {"available": False, "compatible": False, "reason": "需要密钥"}
        with patch.object(mineru_local, "probe_http_service", return_value=fail_probe), \
             patch.object(mineru_local, "save_http_service_key", lambda url, key: saved.append((url, key))):
            with self.assertRaises(WorkflowError):
                mineru_local.set_engine_selection("http_service", http_url="http://127.0.0.1:8000", http_key="SECRET")
        self.assertEqual(saved, [])  # 验证失败：不保存密钥、不落 selection
        self.assertEqual(mineru_local.active_source_kind(), "managed")
        with patch.object(mineru_local, "probe_http_service", return_value=ready_probe), \
             patch.object(mineru_local, "save_http_service_key", lambda url, key: saved.append((url, key))):
            mineru_local.set_engine_selection("http_service", http_url="http://127.0.0.1:8000", http_key="SECRET")
        self.assertEqual(saved, [("http://127.0.0.1:8000", "SECRET")])
        self.assertEqual(mineru_local.active_source_kind(), "http_service")

    def test_install_cancel_does_not_kill_parse_processes(self) -> None:
        engine = self.make_engine()

        class FakeHandle:
            def __init__(self) -> None:
                self.terminated = False
            def terminate(self) -> None:
                self.terminated = True

        parse_handle = FakeHandle()
        with engine._lock:
            engine._processes[101] = mineru_local._RunningProcess(parse_handle, ("parse",), tag="parse")
        engine._terminate_processes("install")
        self.assertFalse(parse_handle.terminated)
        self.assertTrue(engine._processes)  # 解析进程保留
        engine._terminate_processes()  # 启动清理：全部终止
        self.assertTrue(parse_handle.terminated)
        self.assertFalse(engine._processes)


class KitOutputAdapterTests(EngineTestCase):
    def build_kit_zip(self, archive: Path, pages: int = 2) -> dict[str, list[list[dict]]]:
        """构造 MinerU 4 kit 的 ZIP 输出：markdown.md + middle_json.json + structured_content.json + images/。"""
        middle_pages = []
        content_pages = []
        for index in range(pages):
            middle_pages.append({
                "page_idx": index,
                "blocks": [
                    {"type": "doc_title", "index": 0, "bbox": [0.05, 0.05, 0.9, 0.1], "level": 1,
                     "content": [{"type": "text", "content": f"第 {index + 1} 页标题"}]},
                    {"type": "text", "index": 1, "bbox": [0.05, 0.15, 0.9, 0.3],
                     "content": [{"type": "text", "content": f"第 {index + 1} 页正文。"}]},
                ],
            })
            content_pages.append([
                {"type": "title", "bbox": [50, 50, 900, 100],
                 "content": {"level": 1, "title_content": [{"type": "text", "content": f"第 {index + 1} 页标题"}]}},
                {"type": "paragraph", "bbox": [50, 150, 900, 300],
                 "content": {"paragraph_content": [{"type": "text", "content": f"第 {index + 1} 页正文。"}]}},
            ])
        middle = {
            "schema": "docvortex.middle", "schema_version": "2.0",
            "metadata": {"file_suffix": "pdf", "producer": "mineru-test"},
            "pages": middle_pages, "is_full_document": True,
        }
        images_dir = archive.parent / "images-src"
        images_dir.mkdir(parents=True, exist_ok=True)
        (images_dir / "img-1.jpg").write_bytes(b"\xff\xd8fake")
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("markdown.md", "# 文档\n\n正文\n")
            bundle.writestr("middle_json.json", json.dumps(middle, ensure_ascii=False))
            bundle.writestr("structured_content.json", json.dumps({"pages": content_pages}, ensure_ascii=False))
            bundle.write(images_dir / "img-1.jpg", "images/img-1.jpg")
        return content_pages

    def test_extract_kit_zip_and_validate(self) -> None:
        engine = self.make_engine()
        archive = self.root / "slice.zip"
        self.build_kit_zip(archive)
        destination = self.root / "slice-out"
        _extract_kit_zip(archive, destination)
        self.assertTrue((destination / "middle_json.json").is_file())
        self.assertTrue((destination / "structured_content.json").is_file())
        self.assertTrue((destination / "images" / "img-1.jpg").is_file())

    def test_extract_rejects_unsafe_path(self) -> None:
        archive = self.root / "evil.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("../escape.txt", "bad")
        with self.assertRaises(WorkflowError):
            _extract_kit_zip(archive, self.root / "evil-out")

    def test_validate_derived_content_page_count_mismatch(self) -> None:
        middle = self.root / "middle.json"
        middle.write_text(json.dumps({"pages": [{}, {}]}), encoding="utf-8")
        derived = self.root / "content_list_v2.json"
        derived.write_text(json.dumps([[]]), encoding="utf-8")
        with self.assertRaises(WorkflowError):
            _validate_derived_content(derived, middle)

    def test_write_layout_from_pdf(self) -> None:
        source = self.root / "slice.pdf"
        make_pdf(source, 2)
        layout = self.root / "layout.json"
        _write_layout_from_pdf(source, layout)
        payload = json.loads(layout.read_text(encoding="utf-8"))
        self.assertEqual(len(payload["pdf_info"]), 2)
        self.assertEqual(payload["pdf_info"][0]["page_size"], [300.0, 400.0])
        self.assertEqual(payload["pdf_info"][0]["page_idx"], 0)

    def test_derive_script_compiles_and_maps_table_html(self) -> None:
        # 派生脚本是合法 Python，并且会把官方 renderer 的 html 键补齐 table_content 别名。
        compile(mineru_local.DERIVE_SCRIPT, "derive_content_list", "exec")
        self.assertIn("table_content", mineru_local.DERIVE_SCRIPT)
        self.assertIn("render_content_list_v2", mineru_local.DERIVE_SCRIPT)


class LocalBackendWorkflowTests(EngineTestCase):
    def _run_local_job(self, pages: int = 2, page_count: int = 2) -> tuple[MinerUWorkflow, object, list]:
        engine = self.make_engine()
        set_shared_engine(engine)
        self.fake_ready(engine)
        source = self.root / f"book-{pages}.pdf"
        make_pdf(source, page_count)
        merged: list = []

        backend = LocalMinerUBackend(None, engine=engine)  # workflow 稍后绑定

        test_self = self

        def fake_parse_slice(backend_self, job, item, progress):
            """模拟一次本地切片执行：产出 kit ZIP 原样输出 + 派生适配文件。"""
            archive = Path(job.workspace) / "downloads" / f"{item.index:03d}.zip"
            archive.parent.mkdir(parents=True, exist_ok=True)
            test_self.build_kit_zip_for(archive, len(item.source_pages))
            scope = f"p{item.start_page:06d}-{item.end_page:06d}"
            result_dir = Path(job.raw_output) / "slices" / f"{item.index:03d}_{scope}"
            mineru_local._extract_kit_zip(archive, result_dir)
            test_self.write_derived(result_dir, len(item.source_pages))
            mineru_local._write_layout_from_pdf(Path(item.local_pdf), result_dir / "layout.json")
            item.state = SliceState.DOWNLOADED
            item.result_zip = str(archive)
            item.result_directory = str(result_dir)


        with patch.object(LocalMinerUBackend, "_parse_slice", fake_parse_slice), \
             patch.object(LocalMinerUBackend, "_run_derive", lambda self, *a, **k: None):
            workflow = MinerUWorkflow(
                self.root / f"jobs-{pages}",
                merge_callback=lambda job, parse_result: merged.append(parse_result),
                backend_factory=lambda wf, job: LocalMinerUBackend(wf, engine=engine),
            )
            job = workflow.create_job(source, self.root / f"out-{pages}.epub", backend_id=LOCAL_BACKEND_ID)
            self.assertEqual(job.backend_id, LOCAL_BACKEND_ID)
            done = workflow.run_or_resume(job, token=None)
        return workflow, done, merged

    def build_kit_zip_for(self, archive: Path, pages: int) -> None:
        middle_pages = []
        for index in range(pages):
            middle_pages.append({"page_idx": index, "blocks": []})
        middle = {
            "schema": "docvortex.middle", "schema_version": "2.0",
            "metadata": {"file_suffix": "pdf", "producer": "mineru-test"},
            "pages": middle_pages, "is_full_document": True,
        }
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("markdown.md", "# 文档\n")
            bundle.writestr("middle_json.json", json.dumps(middle, ensure_ascii=False))
            bundle.writestr("structured_content.json", json.dumps({"pages": []}, ensure_ascii=False))

    def write_derived(self, result_dir: Path, pages: int) -> None:
        content = []
        for offset, source_page in enumerate(range(1, pages + 1)):
            content.append([
                {"type": "title", "bbox": [20, 20, 300, 50],
                 "content": {"level": 1, "title_content": [{"content": f"Source page {source_page}"}]}},
                {"type": "paragraph", "bbox": [20, 70, 560, 200],
                 "content": {"paragraph_content": [{"content": f"Body on source page {source_page}."}]}},
            ])
        (result_dir / "content_list_v2.json").write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")

    def test_local_job_runs_without_token_and_merges_with_source_pages(self) -> None:
        workflow, done, merged = self._run_local_job(pages=2, page_count=2)
        self.assertEqual(done.state, JobState.COMPLETED)
        self.assertEqual(len(merged), 1)
        result = merged[0]
        self.assertEqual(result.backend_id, LOCAL_BACKEND_ID)
        self.assertEqual(len(result.raw_directories), 1)
        self.assertEqual(list(result.source_pages.values()), [(1, 2)])

    def test_local_job_preserves_kit_outputs_and_derived_files(self) -> None:
        workflow, done, merged = self._run_local_job(pages=1, page_count=1)
        result_dir = Path(merged[0].raw_directories[0])
        # kit 原样输出保留 + 派生适配文件齐备。
        self.assertTrue((result_dir / "markdown.md").is_file())
        self.assertTrue((result_dir / "middle_json.json").is_file())
        self.assertTrue((result_dir / "structured_content.json").is_file())
        self.assertTrue((result_dir / "content_list_v2.json").is_file())
        self.assertTrue((result_dir / "layout.json").is_file())
        geometry = json.loads((result_dir / "layout.json").read_text(encoding="utf-8"))
        self.assertEqual(len(geometry["pdf_info"]), 1)

    def test_local_job_rejected_when_engine_not_ready(self) -> None:
        engine = self.make_engine()
        set_shared_engine(engine)
        source = self.root / "book-noengine.pdf"
        make_pdf(source, 1)
        workflow = MinerUWorkflow(self.root / "jobs-noengine", merge_callback=lambda job, parse_result: None)
        with self.assertRaises(WorkflowError):
            workflow.create_job(source, self.root / "out-noengine.epub", backend_id=LOCAL_BACKEND_ID)
        self.assertEqual(workflow.list_jobs(), [])

    def test_backend_prepare_requires_ready_engine(self) -> None:
        engine = self.make_engine()
        backend = LocalMinerUBackend(None, engine=engine)
        with self.assertRaises(WorkflowError):
            backend.prepare(object())


class ConverterTests(EngineTestCase):
    """HTTP 来源的内置适配器：middle_json → content_list_v2（官方 renderer 之外的回退）。"""

    def build_middle(self) -> dict:
        return {
            "schema": "docvortex.middle", "schema_version": "2.0",
            "metadata": {"file_suffix": "pdf", "producer": {"name": "mineru", "version": "4.0.10"}},
            "pages": [
                {"page_idx": 0, "blocks": [
                    {"type": "doc_title", "bbox": [0.05, 0.05, 0.9, 0.1], "level": 1,
                     "content": [{"type": "text", "content": "书名"}]},
                    {"type": "text", "bbox": [0.05, 0.15, 0.9, 0.3],
                     "content": [{"type": "text", "content": "正文段落。"}]},
                    {"type": "image", "bbox": [0.1, 0.4, 0.8, 0.7], "content": [
                        {"type": "image_body", "image_path": "images/pic.jpg", "content": ""},
                        {"type": "image_caption", "content": [{"type": "text", "content": "图题"}]},
                    ]},
                    {"type": "table", "bbox": [0.1, 0.75, 0.9, 0.95], "content": [
                        {"type": "table_body", "image_path": "images/tbl.jpg", "content": "<table><tr><td>A</td></tr></table>"},
                        {"type": "table_caption", "content": [{"type": "text", "content": "表题"}]},
                    ]},
                    {"type": "header", "bbox": [0.1, 0.01, 0.8, 0.03],
                     "content": [{"type": "text", "content": "页眉"}]},
                ]},
                {"page_idx": 1, "blocks": [
                    {"type": "equation", "bbox": [0.2, 0.2, 0.8, 0.25], "content": "E=mc^2"},
                    {"type": "list", "bbox": [0.1, 0.3, 0.9, 0.6], "content": [
                        {"type": "text", "content": [{"type": "text", "content": "第一条"}]},
                        {"type": "list", "content": [
                            {"type": "text", "content": [{"type": "text", "content": "嵌套条目"}]},
                        ]},
                    ]},
                ]},
            ],
            "is_full_document": True,
        }

    def test_converter_maps_core_block_types(self) -> None:
        pages = mineru_local.middle_json_to_content_list(self.build_middle())
        self.assertEqual(len(pages), 2)
        first = pages[0]
        kinds = [item["type"] for item in first]
        self.assertEqual(kinds[0], "title")
        self.assertEqual(first[0]["content"]["level"], 1)
        self.assertEqual(first[0]["bbox"], [50, 50, 900, 100])
        self.assertIn("paragraph", kinds)
        image = next(item for item in first if item["type"] == "image")
        self.assertEqual(image["content"]["image_source"]["path"], "images/pic.jpg")
        self.assertEqual(image["content"]["image_caption"], [{"type": "text", "content": "图题"}])
        table = next(item for item in first if item["type"] == "table")
        self.assertIn("<table>", table["content"]["table_content"])
        self.assertEqual(table["content"]["table_caption"], [{"type": "text", "content": "表题"}])
        self.assertEqual(kinds[-1], "page_header")
        second = pages[1]
        equation = next(item for item in second if item["type"] == "equation_interline")
        self.assertEqual(equation["content"]["math_content"], "E=mc^2")
        listing = next(item for item in second if item["type"] == "list")
        texts = ["".join(s.get("content", "") for s in leaf["item_content"]) for leaf in listing["content"]["list_items"]]
        self.assertIn("第一条", texts)
        self.assertIn("嵌套条目", texts)

    def test_converter_output_feeds_normalize(self) -> None:
        """适配器产物要能被现有 normalize 消费：产出 block 类型与页映射正确。"""
        from literature.mineru import _parse_slice

        pages = mineru_local.middle_json_to_content_list(self.build_middle())
        store_root = (self.root / "project").resolve()
        # _parse_slice 需要 project 相对路径；把结果目录直接建在 project 结构里。
        raw_dir = store_root / "ocr" / "raw" / "mineru" / "conv"
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / "content_list_v2.json").write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")
        (raw_dir / "layout.json").write_text(json.dumps({"pdf_info": [
            {"page_idx": 0, "page_size": [595.0, 842.0]}, {"page_idx": 1, "page_size": [595.0, 842.0]},
        ]}), encoding="utf-8")
        blocks, pages_meta, record = _parse_slice(
            raw_dir, store_root, "proj", {}, source_pages=[1, 2], block_ids=lambda i, s: f"blk-{i}-{s}",
        )
        kinds = [block.type for block in blocks]
        self.assertIn("title", kinds)
        self.assertIn("table", kinds)
        self.assertIn("image", kinds)
        # 表格 cell 从派生的 table_content 解析出来。
        table = next(block for block in blocks if block.type == "table")
        self.assertTrue(table.cells)
        self.assertEqual(table.cells[0].text, "A")
        self.assertEqual(record["representation"], "content_list_v2")
        self.assertEqual([page.source_page_index for page in pages_meta], [0, 1])

    def test_converter_rejects_invalid_payload(self) -> None:
        with self.assertRaises(WorkflowError):
            mineru_local.middle_json_to_content_list({"no_pages": True})


class HttpServiceBackendTests(EngineTestCase):
    """HTTP 服务执行链：官方 mineru-api /v1 协议的隔离模拟。"""

    def _transport(self, uploads: list[dict], jobs: list[dict], files: dict[str, bytes]):
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if request.method == "POST" and path == "/v1/uploads":
                upload_id = f"upload-{len(uploads) + 1}"
                uploads.append({"id": upload_id, "filename": json.loads(request.content)["filename"]})
                return httpx.Response(200, json={"id": upload_id, "status": "pending"})
            if request.method == "PUT" and path.startswith("/v1/uploads/") and path.endswith("/content"):
                uploads[-1]["bytes"] = request.content
                return httpx.Response(200)
            if request.method == "POST" and path.endswith("/complete"):
                return httpx.Response(200, json={
                    "id": uploads[-1]["id"], "status": "completed",
                    "file": {"id": "file-1", "filename": uploads[-1]["filename"]},
                })
            if request.method == "POST" and path == "/v1/parse/jobs":
                body = json.loads(request.content)
                jobs.append({"job_id": "job-1", "status": "queued", "body": body})
                return httpx.Response(202, json={"job_id": "job-1", "status": "queued"})
            if request.method == "GET" and path.startswith("/v1/parse/jobs/"):
                job = jobs[-1]
                # 第一次轮询 running，第二次 completed。
                if job["status"] != "completed":
                    job["status"] = "completed"
                    return httpx.Response(200, json={
                        "job_id": "job-1", "status": "running", "progress": {"completed": 1, "total": 2},
                        "files": [],
                    })
                return httpx.Response(200, json={
                    "job_id": "job-1", "status": "completed",
                    "progress": {"completed": 2, "total": 2},
                    "files": [{"status": "completed", "output_files": {"zip": {"file_id": "file-zip", "bytes": 10}}}],
                })
            if request.method == "GET" and path == "/v1/files/file-zip/content":
                return httpx.Response(200, content=files["zip"])
            if request.method == "POST" and path.endswith("/cancel"):
                return httpx.Response(200, json={"job_id": "job-1", "status": "canceled"})
            return httpx.Response(404, json={"detail": "not found"})

        return httpx.MockTransport(handler)

    def _http_backend(self, jobs_state: list[dict], uploads: list[dict], files: dict[str, bytes]):
        transport = self._transport(uploads, jobs_state, files)

        def factory(_headers):
            import httpx
            return httpx.Client(transport=transport, timeout=10)

        engine = self.make_engine()
        return LocalMinerUBackend(None, engine=engine, http_client_factory=factory)

    def _prepare_job(self, pages: int = 2):
        engine = self.make_engine()
        set_shared_engine(engine)
        source = self.root / f"http-book-{pages}.pdf"
        make_pdf(source, pages)
        workflow = MinerUWorkflow(
            self.root / f"jobs-http-{pages}",
            merge_callback=lambda job, parse_result: None,
            poll_interval=0.0, sleep=lambda _s: None,
        )
        ready_probe = {"available": True, "compatible": True, "version": "4.0.10", "tiers": ["standard"]}
        with patch.object(mineru_local, "active_source_kind", return_value="http_service"), \
             patch.object(mineru_local, "probe_http_service", return_value=ready_probe), \
             patch.object(mineru_local, "get_engine_selection",
                          return_value={"kind": "http_service", "http_url": "http://127.0.0.1:8000"}):
            job = workflow.create_job(source, self.root / f"http-out-{pages}.epub", backend_id=LOCAL_BACKEND_ID)
            job.backend_config["engine_source"] = "http_service"
            job.backend_config["engine_source_config"] = {"kind": "http_service", "http_url": "http://127.0.0.1:8000"}
        return workflow, job

    def test_http_execute_uploads_runs_and_downloads_zip(self) -> None:
        uploads: list[dict] = []
        jobs: list[dict] = []
        files = {"zip": b"PK-fake-zip-bytes"}
        backend = self._http_backend(jobs, uploads, files)
        workflow, job = self._prepare_job()
        backend.workflow = workflow
        ready_probe = {"available": True, "compatible": True, "version": "4.0.10", "tiers": ["standard"]}
        with patch.object(mineru_local, "active_source_kind", return_value="http_service"), \
             patch.object(mineru_local, "probe_http_service", return_value=ready_probe), \
             patch.object(mineru_local, "get_engine_selection",
                          return_value={"kind": "http_service", "http_url": "http://127.0.0.1:8000"}), \
             patch.object(mineru_local, "local_engine_ready", return_value=True):
            backend.prepare(job)
        item = job.slices[0]
        archive = Path(job.workspace) / "downloads" / "001.zip"
        with patch.object(mineru_local, "active_source_kind", return_value="http_service"), \
             patch.object(mineru_local, "get_engine_selection",
                          return_value={"kind": "http_service", "http_url": "http://127.0.0.1:8000"}), \
             patch.object(mineru_local, "get_http_service_key", lambda url: ""), \
             patch.object(mineru_local, "_extract_kit_zip", lambda a, d: None), \
             patch.object(LocalMinerUBackend, "_derive_result_files", lambda self, r, s, k, f=None: None), \
             patch("literature.mineru_local._is_result_directory", return_value=True):
            backend._parse_slice(job, item, None)
        self.assertEqual(item.state, SliceState.DOWNLOADED)
        self.assertEqual(archive.read_bytes(), b"PK-fake-zip-bytes")
        self.assertEqual(len(uploads), 1)
        # 任务请求了 zip 输出 + standard 档 + OCR 模式映射。
        self.assertEqual(jobs[0]["body"]["output_formats"], ["zip"])
        self.assertEqual(jobs[0]["body"]["tier"], "standard")
        self.assertEqual(jobs[0]["body"]["ocr_mode"], "auto")

    def test_http_failure_surfaces_service_error(self) -> None:
        uploads: list[dict] = []
        jobs: list[dict] = [{"job_id": "job-1", "status": "failed", "body": {}}]
        files = {"zip": b""}
        backend = self._http_backend(jobs, uploads, files)
        workflow, job = self._prepare_job()
        backend.workflow = workflow
        ready_probe = {"available": True, "compatible": True, "version": "4.0.10", "tiers": ["standard"]}
        with patch.object(mineru_local, "active_source_kind", return_value="http_service"), \
             patch.object(mineru_local, "probe_http_service", return_value=ready_probe), \
             patch.object(mineru_local, "get_engine_selection",
                          return_value={"kind": "http_service", "http_url": "http://127.0.0.1:8000"}), \
             patch.object(mineru_local, "local_engine_ready", return_value=True):
            backend.prepare(job)
        item = job.slices[0]
        archive = Path(job.workspace) / "downloads" / "001.zip"
        with patch.object(mineru_local, "active_source_kind", return_value="http_service"), \
             patch.object(mineru_local, "get_engine_selection",
                          return_value={"kind": "http_service", "http_url": "http://127.0.0.1:8000"}), \
             patch.object(mineru_local, "get_http_service_key", lambda url: ""):
            with self.assertRaises(WorkflowError):
                backend._parse_slice(job, item, None)
        self.assertEqual(item.state, SliceState.FAILED)


class TlsAndLocalSourceTests(EngineTestCase):
    """冻结环境 TLS 故障与「已配置本地来源」行为回归。

    背景（真实故障）：构建用的 Python 被上游工具打了补丁，ssl.SSLContext 会加载
    同目录的额外 CA 文件；冻结后该文件不存在 → ssl.create_default_context 抛
    FileNotFoundError，所有 HTTPS 入口（含 /mineru/token/check 与解析任务）失败。
    """

    def test_normalize_tls_environment_drops_unusable_paths_only(self) -> None:
        from literature.providers import normalize_tls_environment

        with patch.dict(os.environ, {
            "SSL_CERT_FILE": "/nonexistent/ca.pem",
            "REQUESTS_CA_BUNDLE": "/also/missing.pem",
            "SSL_CERT_DIR": "/missing-dir",
        }, clear=False):
            removed = normalize_tls_environment()
        self.assertIn("SSL_CERT_FILE", removed)
        self.assertIn("REQUESTS_CA_BUNDLE", removed)
        self.assertIn("SSL_CERT_DIR", removed)
        self.assertNotIn("SSL_CERT_FILE", os.environ)

        # 存在的自定义信任库必须保留（企业自签场景），不得被误删。
        present = self.root / "custom-ca.pem"
        present.write_text("dummy", encoding="utf-8")
        with patch.dict(os.environ, {"SSL_CERT_FILE": str(present)}, clear=False):
            self.assertEqual(normalize_tls_environment(), [])
            self.assertEqual(os.environ["SSL_CERT_FILE"], str(present))

    def test_mineru_client_reports_actionable_tls_error(self) -> None:
        """TLS 初始化失败必须是可操作错误（不 500、不关校验）。"""
        from literature.mineru_workflow import MinerUAPIError, MinerUClient
        from literature.providers import tls_context_error

        with patch("ssl.create_default_context", side_effect=FileNotFoundError(2, "No such file or directory")):
            with self.assertRaises(MinerUAPIError) as raised:
                MinerUClient("fake-token", max_attempts=1)
        self.assertEqual(raised.exception.code, "tls_context")
        self.assertIn("证书", str(raised.exception))
        # 文案必须明确不会关闭证书校验，且给出下一步。
        message = tls_context_error(FileNotFoundError(2, "No such file or directory"))
        self.assertIn("校验不会被关闭", message)
        self.assertIn("重启", message)

    def test_api_token_check_surfaces_tls_error_not_500(self) -> None:
        """走真实 API 入口：TLS 故障返回可读 4xx/502，而不是 500。"""
        import tempfile as _tempfile

        from fastapi.testclient import TestClient

        from literature.api import create_app

        os.environ["LITERATURE_RUNTIME"] = _tempfile.mkdtemp()
        os.environ["LITERATURE_LIBRARY"] = _tempfile.mkdtemp()
        os.environ["LITERATURE_SESSION_TOKEN"] = "tls-api-test"
        os.environ["MINERU_API_TOKEN"] = "fake-token"
        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)
        headers = {"x-literature-token": "tls-api-test"}
        with patch("ssl.create_default_context", side_effect=FileNotFoundError(2, "No such file or directory")):
            response = client.post("/mineru/token/check", headers=headers)
        self.assertIn(response.status_code, (400, 409, 502))
        self.assertIn("证书", response.json().get("detail", ""))

    def test_local_source_parses_without_any_token(self) -> None:
        """真实契约：设置里已配置本地来源时，无 MinerU Token 也能建任务并完成解析。"""
        from literature import mineru_local
        from literature.ocr import MinerUService
        from literature.storage import ProjectStore

        runtime = os.environ["LITERATURE_RUNTIME"]
        library = self.root / "library"
        store = ProjectStore(library)
        source = self.root / "local-no-token.pdf"
        make_pdf(source, 1)
        project = store.create_project(source, title="Local No Token")
        project_id = str(project["project_id"])
        service = MinerUService(store, runtime_root=Path(runtime))
        # 引擎根目录跟随 service 的 runtime（managed 独立安装位置）。
        self.fake_ready(service.engine)
        mineru_local.set_engine_selection("managed")
        # 无 Token（get_mineru_token 为空）也不得阻塞本地任务创建。
        with patch("literature.ocr.get_mineru_token", return_value=""):
            job = service.start_parse(project_id, language="auto", backend_id="mineru_local")
        self.assertEqual(job["backend_id"], "mineru_local")
        self.assertEqual(job["backend_config"]["engine_source"], "managed")
        self.assertIn("engine_source_config", job["backend_config"])
        # 调度器判定：本地任务不需要 Token。
        self.assertFalse(service._job_needs_token(job["job_id"]))


if __name__ == "__main__":
    unittest.main()
