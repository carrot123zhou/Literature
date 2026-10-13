"""可完成整条 MinerU 流水线的假客户端（不联网、不读 Keychain）。

用于隔离的真实 API + 浏览器全流程验收：create_batch → 可选失败一次 → 轮询 done →
下载含 content_list_v2 的 ZIP → 合并。状态保存在类级注册表，因此中断后新客户端
仍能继续轮询同一批次。
"""
from __future__ import annotations

import io
import json
import threading
import zipfile
from pathlib import Path

from literature.mineru_workflow import BatchSubmission


class FakeMinerU:
    lock = threading.RLock()
    results: dict[str, list[int]] = {}
    batches: dict[str, list[str]] = {}
    attempts: dict[str, int] = {}
    fail_once_pages: set[int] = set()
    fail_always_pages: set[int] = set()
    executed: list[str] = []
    uploads: int = 0
    # 非 None 时 create_batch 先等待该事件释放（用于确定性验证调序执行顺序）。
    hold: object = None

    @classmethod
    def reset(cls) -> None:
        with cls.lock:
            cls.results = {}
            cls.batches = {}
            cls.attempts = {}
            cls.fail_once_pages = set()
            cls.fail_always_pages = set()
            cls.executed = []
            cls.uploads = 0


def _content_list(pages: list[int]) -> list[list[dict]]:
    content = []
    for source_page in pages:
        content.append([
            {"type": "title", "bbox": [30, 30, 300, 60],
             "content": {"level": 1, "title_content": [{"content": f"Chapter on page {source_page}"}]}},
            {"type": "paragraph", "bbox": [30, 80, 380, 200],
             "content": {"paragraph_content": [{"content": f"Body text on source page {source_page}."}]}},
        ])
    return content


def write_result_zip(target: Path, pages: list[int]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    content = _content_list(pages)
    geometry = {"pdf_info": [{"page_idx": index, "page_size": [420, 595]} for index in range(len(pages))]}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("result_content_list_v2.json", json.dumps(content))
        archive.writestr("result_middle.json", json.dumps(geometry))
    target.write_bytes(buffer.getvalue())


class FakeMinerUClient:
    def __init__(self, *_args, **_kwargs) -> None:
        self.base_url = "fake://mineru"

    @property
    def auth_headers(self) -> dict[str, str]:
        return {}

    def close(self) -> None:
        pass

    def check_token(self) -> None:
        pass

    def create_batch(self, slices, job) -> BatchSubmission:
        if FakeMinerU.hold is not None:
            FakeMinerU.hold.wait(timeout=60)
        with FakeMinerU.lock:
            batch_id = f"batch-{len(FakeMinerU.batches) + 1}"
            data_ids = []
            urls = []
            for item in slices:
                FakeMinerU.results[item.data_id] = list(item.source_pages)
                data_ids.append(item.data_id)
                urls.append(f"upload://{item.data_id}")
            FakeMinerU.batches[batch_id] = data_ids
            FakeMinerU.executed.append(batch_id)
            return BatchSubmission(batch_id, tuple(urls))

    def upload_file(self, upload_url: str, path) -> None:
        data_id = upload_url.split("://", 1)[1]
        with FakeMinerU.lock:
            FakeMinerU.uploads += 1
            FakeMinerU.attempts[data_id] = FakeMinerU.attempts.get(data_id, 0) + 1
            pages = FakeMinerU.results.get(data_id, [])
            attempt = FakeMinerU.attempts[data_id]
            if FakeMinerU.fail_always_pages and any(page in FakeMinerU.fail_always_pages for page in pages):
                raise RuntimeError(f"fake MinerU：源页 {pages} 恒定失败")
            # fail_once_pages 表示“该切片的源页集合恰好等于此集合”时首次上传失败；
            # 用精确集合而非 any，避免影响其他也包含该页的完整解析切片。
            if attempt == 1 and FakeMinerU.fail_once_pages and set(pages) == set(FakeMinerU.fail_once_pages):
                raise RuntimeError(f"fake MinerU：源页 {pages} 首次上传失败")

    def get_batch(self, batch_id: str) -> list[dict]:
        with FakeMinerU.lock:
            return [
                {"data_id": data_id, "file_name": f"{data_id}.pdf", "state": "done",
                 "full_zip_url": f"zip://{data_id}"}
                for data_id in FakeMinerU.batches.get(batch_id, [])
            ]

    def download_result(self, url: str, target, **_kwargs):
        data_id = url.split("://", 1)[1]
        pages = FakeMinerU.results.get(data_id, [])
        write_result_zip(Path(target), pages)
        return Path(target)


def fake_client_factory(_token: str) -> FakeMinerUClient:
    return FakeMinerUClient()
