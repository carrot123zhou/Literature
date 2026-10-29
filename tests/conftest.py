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


def local_fold_section(section):
    """确定性本地折叠（测试 fixture 专用，非产品路径；generate 不调用）。

    从 literature.reader 迁出（Lead 指示：生产模块不得保留可执行的本地摘录路径）。
    直接调用 **third_party 上游原件** `tf_ladder.local()`——确定性分词/锚计算，非 AI 摘录。
    生产 adapter 的 apply_local 已删除；只有这里和其它测试允许执行 local。"""
    from third_party.textfold import ladder as tf_ladder
    from third_party.textfold import tokens as tf_tokens
    from literature.textfold_adapter import MIN_FOLD_WORDS, section_view
    from literature.reader import _ladder_for_section

    doc, chunk = _ladder_for_section(section)
    if not chunk["units"]:
        return {**section, "units": []}
    for block_id in chunk["units"]:
        unit = doc["units"][block_id]
        if tf_tokens.words(unit["toks"]) < MIN_FOLD_WORDS:
            unit["lad"] = tf_ladder.suppressed()
            unit["suppressed"] = True
            unit["source"] = "suppressed"
        else:
            unit["lad"] = tf_ladder.local(unit, doc["atoms"], doc.get("is_cjk", False))
            unit["source"] = "local"
    # 章节层 take（与上游 apply_local 业务行为一致，纯 fixture 形状）：取首个有 brief 的 unit。
    first = next((doc["units"][bid]["lad"] for bid in chunk["units"]
                  if doc["units"][bid]["lad"].get("brief")), None)
    if first:
        take = [dict(tk) for tk in first["brief"][: max(1, first.get("tn") or 0)]]
        chunk["lad"] = {
            "take": take,
            "topic": [tk["k"] for tk in (first.get("topic") or [])
                      if isinstance(tk.get("k"), int) and 0 <= tk["k"] < len(take)],
        }
    return section_view(doc, chunk)
