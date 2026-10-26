"""fold UI 修正后的前端验收探针（Lead 指定四项；前端在途重构完成后运行）。

前置：designer 完成折叠 UI 修正（真实 /reader/fold-runs 契约、移除生产 fixture 与计时假进度）。
本探针对「钩子尚未落地」的检查项**如实 SKIP 并注明原因**，绝不以旧 dist 复跑得绿。

四项检查（对照 Lead 指令与旧实现缺陷）：
1. ghost 动画实际可见：旧宿主 CSS 隐藏了上游 `#ghosts`；切层时 morph 影子层必须真实渲染。
2. 跨栏 hover 只亮小蓝点且移出清理：旧 bridge 对 .u 整段加 cite-lit（整段着色）；
   正确行为 = 只点亮 `.u .dot`（上游原生），mouseleave/切换后点必须熄灭。
3. notes 真实保存/刷新恢复/错误反馈：旧 bridge 把 /api/notes/* 路由到
   `https://literature-bridge.invalid` 并 catch 后返回**假 200**（notes: []）——
   保存必须走宿主 overlay 契约持久化；后端/宿主失败时 UI 必须给出错误反馈而非静默吞掉。
4. 当前书五层文字/公式/主题菜单计算色：上游按层级给 --c-* 计算色（浅色/纸色/深色三态）；
   必须用**当前书**经真实后端生成的 paper 数据验证，不得用 gallery fixture。

运行（前端落地后）：
  .venv/bin/python tests/manual/reader_foldrun_integration_check.py   # 先确认后端数据链路
  .venv/bin/python tests/manual/reader_fold_ui_fix_browser_check.py   # 本探针
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.manual.engine_ui_browser_check import free_port, prepare_dist, start_web  # noqa: E402
from tests.manual.reader_foldrun_integration_check import (  # noqa: E402
    FoldRunApiFlowBase,  # 复用夹具思路；浏览器探针需要常驻 server，故在此自建
)


def main() -> int:
    raise SystemExit(
        "本探针依赖 designer 的折叠 UI 修正落地（真实 fold-run 接线 + ghosts/ bridge 修正）。\n"
        "当前在途前端未完成，运行只会得到成片 SKIP —— 按约定不以此充当验收。\n"
        "落地后请把本文件的 main() 替换为完整 playwright 流程（见同目录 reader_ui_browser_check.py 的\n"
        "start_web/prepare_dist 夹具与 tests/manual/reader_foldrun_integration_check.py 的后端数据准备），\n"
        "并按模块 docstring 的四项检查逐项断言。"
    )


if __name__ == "__main__":
    main()
