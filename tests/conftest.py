"""统一测试隔离：把库根与运行时目录指向一次性临时目录。

`literature.api` 在导入时（以及 `create_app()`）会读取 ``LITERATURE_LIBRARY`` /
``LITERATURE_RUNTIME``，并可能创建真实的 ``session-token``、uploads 目录与空项目库。
若不隔离，测试收集阶段就会写进用户真实的 ``~/CdoexProject/Literature/library``
与 ``~/Library/Application Support/Literature``。

这里在收集任何测试模块之前改环境变量；函数级 autouse fixture 再按测试重设一次，
这样个别用例在 setUp/tearDown 里 set/pop 环境变量后，也不会让后续用例回落到真实目录。
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile

import pytest

_TEMP_ROOT = tempfile.mkdtemp(prefix="literature-tests-")
os.environ["LITERATURE_LIBRARY"] = os.path.join(_TEMP_ROOT, "library")
os.environ["LITERATURE_RUNTIME"] = os.path.join(_TEMP_ROOT, "runtime")
os.environ["LITERATURE_SESSION_TOKEN"] = "test-session-token"
atexit.register(shutil.rmtree, _TEMP_ROOT, True)


@pytest.fixture(autouse=True)
def _isolate_literature_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LITERATURE_LIBRARY", os.path.join(_TEMP_ROOT, "library"))
    monkeypatch.setenv("LITERATURE_RUNTIME", os.path.join(_TEMP_ROOT, "runtime"))
    monkeypatch.setenv("LITERATURE_SESSION_TOKEN", "test-session-token")
