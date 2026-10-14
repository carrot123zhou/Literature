#!/usr/bin/env python3
"""对真实模型服务验证“关思考字段”是否真的生效（只发极小的探测请求）。

为什么需要它：`enable_thinking` / `chat_template_kwargs.enable_thinking` /
`reasoning_effort=minimal` 三者中到底哪个被服务接受、接受后思考是否真的停下来，
只有问服务本身才知道。判定标准是 `reasoning_tokens / output_tokens`——服务返回 200
并不代表它认了这个字段。

结论由生产代码 `literature.providers.probe_thinking_off` 直接给出，因此脚本与
客户端启动翻译时的行为一致；脚本只额外把“生效写法的实际 reasoning 占比”打出来，
方便对比优化前后的体感。

用法（只读 runtime 里的服务配置与 Keychain 密钥，不改动任何状态）：

    .venv/bin/python tests/manual/thinking_probe_check.py
    .venv/bin/python tests/manual/thinking_probe_check.py --service <service_id> --model qwen3.8-flash-next

退出码 0 表示探测到可用的关思考写法。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from literature.model_services import ROLE_TRANSLATION, ModelServiceStore  # noqa: E402
from literature.providers import (  # noqa: E402
    PROBE_MAX_TOKENS,
    PROBE_REASONING_SHARE,
    PROBE_SYSTEM,
    PROBE_USER,
    THINKING_OFF_CANDIDATES,
    THINKING_FIELD_VARIANTS,
    disabled_reasoning_effort,
    infer_reasoning_style,
    probe_thinking_off,
)


def resolve_service(store: ModelServiceStore, explicit: str) -> dict:
    if explicit:
        return store.get(explicit)
    service_id = store.roles().get(ROLE_TRANSLATION, "")
    if not service_id:
        raise SystemExit("没有配置翻译模型服务；请先在客户端里配置，或用 --service 指定。")
    return store.get(service_id)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", default="", help="service_id；默认取翻译角色绑定的服务")
    parser.add_argument("--model", default="", help="覆盖模型 ID")
    args = parser.parse_args()

    store = ModelServiceStore()
    service = resolve_service(store, args.service)
    service_id = str(service["service_id"])
    model = args.model or str(service.get("model") or "")
    style = infer_reasoning_style(model)
    print(f"服务：{service.get('name')}  service_id={service_id}")
    print(f"Base URL：{service.get('base_url')}")
    print(f"模型：{model}  推断协议：{style or '(推断不出，探测跳过)'}")
    if not style:
        print("\n推断不出思考协议：翻译任务不会发送任何思考字段，无需探测。")
        return 0
    effort = disabled_reasoning_effort(style)
    print(f"关思考 effort：{effort}")
    for variant in THINKING_OFF_CANDIDATES.get(style, ()):
        print(f"  · {variant:28s} 请求体字段：{THINKING_FIELD_VARIANTS[variant]}")

    provider = store.config_for(service_id, timeout=60.0)
    try:
        policy = probe_thinking_off(provider)
    finally:
        provider.close()

    print()
    if policy is None:
        print("结论：探测被临时故障（限流/5xx/网络/认证）打断，结论未知。")
        print("     客户端不会把这次结果写进缓存，下次作业会重新探测。")
        return 2
    if not policy:
        print("结论：所有候选写法都没能让思考停下来（被 400 拒收，或返回 200 但 reasoning 依旧高）。")
        print("     客户端会记住这个结论并停止发送思考字段；建议换用非思考模型。")
        return 1

    variant = policy["variant"]
    print(f"结论：生效写法是 `{variant}`（请求体：{THINKING_FIELD_VARIANTS[variant]}）。")
    print("     客户端会自动缓存并在后续作业里使用，无需手工配置。")

    # 再用生效写法测一次，打印真实的 reasoning 占比，便于对比优化前后。
    confirm = store.config_for(service_id, timeout=60.0)
    try:
        child = confirm.with_reasoning(style=policy["style"], effort=policy["effort"], variant=variant)
        child = child.with_max_output_tokens(PROBE_MAX_TOKENS)
        if hasattr(child, "_stream_disabled"):
            child._stream_disabled = True
        child.timeout = min(float(child.timeout or 60.0), 60.0)
        response = child.complete(system=PROBE_SYSTEM, user=PROBE_USER)
        output = int(response.output_tokens or 0)
        reasoning = int(response.reasoning_tokens or 0)
        share = (reasoning / output) if output else 0.0
        print(f"     实测：reasoning={reasoning} output={output} 占比={share:.0%}"
              f"（阈值 {PROBE_REASONING_SHARE:.0%}）")
    finally:
        confirm.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
