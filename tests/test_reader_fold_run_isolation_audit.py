"""fold-run 隔离与线程生命周期审计（2026-10-25 worker e3foun0rroyo8t7vuewcgi99）。

背景：2026-10-25 首次全量 pytest 中 tests/test_reader_fold_run.py 两例失败——
- FoldRunApiTests.test_start_run_without_fold_model_is_400_with_guidance（200 != 400）
- FoldRunApiTests.test_unknown_locale_is_400（200 not in (400, 422)）
伴随 PytestUnhandledThreadExceptionWarning：线程 reader-fold-run-fr-bb274 在
provider_factory 处抛 ProviderError「尚未配置 c349021456994beda29d5170e0169255 API key」
（真实 32-hex service id，非本测试播种），且 _fail_run 二次抛
StorageError「项目不存在：782ba712004e40a1ad7916ad52b7e077」。

本文件用**确定性实验**固化两条根因，不以复跑变绿消除：

1. 共享 runtime 角色绑定泄漏（200 的来源）：tests/conftest.py 给**全进程**一个共享
   LITERATURE_RUNTIME；任何用 `create_app` 且未做 per-test runtime 覆盖的测试，其
   ModelServiceStore 读到的是整套测试共享的 model-services.json。只要任何一个先行的
   测试在该共享 runtime 写入 reader_fold 角色绑定，后续「未覆盖」的 fold-runs POST
   就会放行（200）并真的启动后台 run——这就是 run-1 两个 400 断言拿到 200 的机制。
   （当前 test_reader_fold_run.FoldRunApiTests.setUp 已加 per-test 覆盖作为缓解；
   本文件验证向量真实存在 + 缓解有效，防回归。）

2. run 线程生命周期无 join（unhandled thread exception 的来源）：ReaderJobRunner 的
   run_thread 在 finally 里只清 _running/_run_jobs，无 shutdown/join；测试的
   TemporaryDirectory 清理后线程仍存活，provider_factory 与 _fail_run 先后抛
   ProviderError → StorageError，成为未捕获线程异常。本文件用阻塞 provider +
   threading.excepthook 捕获做确定性复现，并钉住 current_run 的崩溃窗口语义
   （线程死 → running 按 paused 呈现，可 resume）。

运行：`.venv/bin/python -m pytest tests/test_reader_fold_run_isolation_audit.py -q`
"""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from literature.model_services import ModelServiceStore
from literature.storage import ProjectStore

from tests.test_reader_fold_run import make_document, make_translation


def _seed_project(root: Path) -> tuple[ProjectStore, str]:
    """建一个带 2 节正文与 zh-CN 译文的隔离项目。"""
    store = ProjectStore(root)
    source = root / "book.pdf"
    source.write_bytes(b"%PDF-1.4\n% fixture")
    project = store.create_project(source, title="Silk Road Silver")
    pid = project["project_id"]
    document = make_document(pid)
    store.save_document(document)
    store.save_translation(make_translation(document))
    return store, pid


def _seed_service_and_role(runtime_root, *, model: str = "fake-model") -> str:
    """在指定 runtime 写入一个服务 + reader_fold 角色绑定（等价于「早前测试留下的全局状态」），返回 service_id。"""
    models = ModelServiceStore(runtime_root)
    created = models.create({
        "name": "audit-fake", "preset": "custom",
        "base_url": "http://127.0.0.1:1/v1",   # 指向必然拒绝连接的本地端口：不发起任何真实外部请求
        "models": [{"model": model}],
    })
    service_id = str(created["service_id"])
    models.set_roles({"reader_fold": {"service_id": service_id, "model": model}})
    return service_id


class FoldRunIsolationAudit(unittest.TestCase):
    """两个确定性复现 + 一个回归守护。全程隔离目录、无 Keychain、无外部网络。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = self.temporary.name
        # 「共享 runtime」模拟 conftest 的全进程共享目录；「测试私有 runtime」模拟 per-test 覆盖。
        self.shared_runtime = os.path.join(self.base, "shared-runtime")
        self.private_runtime = os.path.join(self.base, "private-runtime")
        os.makedirs(self.shared_runtime, exist_ok=True)
        os.makedirs(self.private_runtime, exist_ok=True)
        self._previous = {key: os.environ.get(key) for key in ("LITERATURE_RUNTIME", "LITERATURE_LIBRARY")}

    def tearDown(self) -> None:
        for key, value in self._previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    # ---- 复现 1：共享 runtime 角色绑定泄漏 → 400 断言拿到 200 ------------------

    def test_shared_runtime_role_binding_makes_unbound_post_return_200(self) -> None:
        """向量证明：app 未做 per-test runtime 覆盖时，共享 runtime 的 reader_fold 绑定
        让「无模型也应 400」的 POST fold-runs 放行（200）并启动后台 run。

        这正是 run-1 中 FoldRunApiTests 两例拿到 200 的机制。当前产品代码若改为
        在 create_app 内强制隔离 runtime，本断言应随之更新为 400。"""
        from fastapi.testclient import TestClient
        from literature.api import create_app
        import literature.model_services as ms

        _seed_service_and_role(self.shared_runtime)
        library = os.path.join(self.base, "library-shared")
        store, pid = _seed_project(Path(library))
        # 模拟「测试只隔离了 library、runtime 仍指向共享目录」的 app：
        os.environ["LITERATURE_RUNTIME"] = self.shared_runtime
        os.environ["LITERATURE_LIBRARY"] = library
        # 无 Keychain 触碰：api_key 直接给空（与测试机读不到 key 的 run-1 路径一致）。
        original_api_key = ms.ModelServiceStore.api_key
        ms.ModelServiceStore.api_key = lambda self, sid: ""
        try:
            client = TestClient(create_app(store))
            headers = {"x-literature-token": client.app.state.session_token}
            response = client.post(f"/projects/{pid}/reader/fold-runs", json={}, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            run = response.json()["run"]
            self.assertEqual(run["status"], "running")
            # run 是真的启动了：等它因缺 key 落 failed（后台线程因此存在过）。
            deadline = time.time() + 10
            status = ""
            while time.time() < deadline:
                status = str(client.get(f"/projects/{pid}/reader/fold-run", headers=headers).json().get("status"))
                if status in ("failed", "ready"):
                    break
                time.sleep(0.05)
            self.assertEqual(status, "failed")
        finally:
            ms.ModelServiceStore.api_key = original_api_key
        self._wait_threads_gone()

    # ---- 回归守护：per-test runtime 覆盖后，共享绑定不再泄漏 --------------------

    def test_per_test_runtime_override_blocks_shared_binding(self) -> None:
        """当前缓解有效：app 的 runtime 指向测试私有目录时，共享 runtime 的绑定不可见，
        无模型 POST 仍 400。若本例失败，说明 per-test 隔离被移除/失效。"""
        from fastapi.testclient import TestClient
        from literature.api import create_app

        _seed_service_and_role(self.shared_runtime)
        library = os.path.join(self.base, "library-private")
        store, pid = _seed_project(Path(library))
        os.environ["LITERATURE_RUNTIME"] = self.private_runtime
        os.environ["LITERATURE_LIBRARY"] = library
        client = TestClient(create_app(store))
        headers = {"x-literature-token": client.app.state.session_token}
        response = client.post(f"/projects/{pid}/reader/fold-runs", json={}, headers=headers)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("折叠模型", str(response.json().get("detail", "")))

    # ---- 复现 2：run 线程跨测试存活 + _fail_run 命中已删项目 --------------------

    def test_run_thread_outlives_project_and_fail_run_hits_deleted_project(self) -> None:
        """项目目录删除后 run 线程的生命周期清理（2026-10-25 已修复，本例钉住修复后语义）：

        - 线程在项目被删后安静退出：provider 抛错 → _fail_run 吞掉 StorageError（记 warning），
          **不再产生未捕获线程异常**（run-1 的原始缺陷）；
        - GET fold-run 对已删项目返回 **404**（项目不存在），不再是 StorageError 400/500 噪声；
        - 线程死后 current_run 把 running 按 paused 呈现（可 resume 语义不变）。"""
        from fastapi.testclient import TestClient
        from literature.api import create_app
        import literature.model_services as ms

        library = os.path.join(self.base, "library-thread")
        store, pid = _seed_project(Path(library))
        os.environ["LITERATURE_RUNTIME"] = self.private_runtime
        os.environ["LITERATURE_LIBRARY"] = library
        _seed_service_and_role(self.private_runtime)
        original_api_key = ms.ModelServiceStore.api_key
        ms.ModelServiceStore.api_key = lambda self, sid: "audit-not-a-real-key"

        entered_first_call = threading.Event()
        release = threading.Event()
        try:
            client = TestClient(create_app(store))
            app = client.app
            original_provider_from = app.state.job_manager.provider_from

            class BlockingProvider:
                config = type("C", (), {"provider": "audit", "model": "fake-model"})()

                def complete(self, *, system: str, user: str):
                    entered_first_call.set()
                    self.assertTrue(release.wait(timeout=10))
                    raise RuntimeError("audit: provider failed after release")

                def close(self) -> None:
                    pass

            def provider_from(*args, **kwargs):
                return BlockingProvider()

            app.state.job_manager.provider_from = provider_from
            headers = {"x-literature-token": app.state.session_token}
            response = client.post(f"/projects/{pid}/reader/fold-runs", json={"locale": "zh-CN"}, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(entered_first_call.wait(timeout=10), "provider 未进入首次调用")

            captured: list[tuple[threading.Thread, BaseException]] = []
            original_hook = threading.excepthook
            # 钩子必须在删除项目目录之前装好：线程可能在 rmtree 重试窗口内就命中
            # StorageError 并死亡（这正是被复现的竞争本身）。
            threading.excepthook = lambda args: captured.append((args.thread, args.exc_value))  # type: ignore[assignment]
            try:
                # 模拟测试结束：项目目录整体删除（TemporaryDirectory cleanup 等价物）。
                # 线程可能仍在写目录（这本身就是「线程跨测试存活」的副作用），重试删净。
                project_dir = store.project_path(pid)   # 目录删除后 project_path 会 StorageError，先解析
                deadline = time.time() + 10
                last_error: Exception | None = None
                while time.time() < deadline:
                    try:
                        shutil.rmtree(project_dir)
                        last_error = None
                        break
                    except OSError as error:
                        last_error = error
                        time.sleep(0.05)
                self.assertIsNone(last_error, f"项目目录始终删不净（线程持续写入）：{last_error}")
                release.set()
                deadline = time.time() + 10
                while time.time() < deadline and any(
                    thread.name.startswith("reader-fold-run-") for thread in threading.enumerate()
                ):
                    time.sleep(0.05)
            finally:
                threading.excepthook = original_hook
                app.state.job_manager.provider_from = original_provider_from

            # 修复后语义：**没有未捕获线程异常**（_fail_run 吞掉已删项目的 StorageError，
            # provider 错误只进日志）。若本断言失败，说明生命周期清理被回退。
            self.assertEqual(captured, [], captured)
            # GET fold-run 对已删项目 → 404（项目不存在），前端轮询可干净处理。
            snapshot = client.get(f"/projects/{pid}/reader/fold-run", headers=headers)
            self.assertEqual(snapshot.status_code, 404, snapshot.text)
            self.assertIn("项目不存在", str(snapshot.json().get("detail", "")))
        finally:
            ms.ModelServiceStore.api_key = original_api_key
            release.set()
        self._wait_threads_gone()

    # ---- 工具 ----------------------------------------------------------------

    def _wait_threads_gone(self, timeout: float = 10.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not any(thread.name.startswith("reader-fold-run-") for thread in threading.enumerate()):
                return
            time.sleep(0.05)
        self.fail("fold-run 线程在超时后仍未退出（线程泄漏）")


if __name__ == "__main__":
    unittest.main()
