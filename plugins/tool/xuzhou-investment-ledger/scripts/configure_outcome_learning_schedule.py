#!/usr/bin/env python3
"""以现有登录用户身份启用、暂停或查看徐州招商每日结果学习计划。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


_SOURCE_SCHEDULE_HELPER_PATH = Path(__file__).with_name("configure_source_monitor_schedule.py")
_shared_spec = importlib.util.spec_from_file_location(
    "xuzhou_source_monitor_schedule_shared_helper",
    _SOURCE_SCHEDULE_HELPER_PATH,
)
if _shared_spec is None or _shared_spec.loader is None:
    raise RuntimeError("无法加载本机认证计划辅助模块")
_shared = importlib.util.module_from_spec(_shared_spec)
_shared_spec.loader.exec_module(_shared)


_ALLOWED_ACTIONS = frozenset({"--enable", "--pause", "--status"})
_ENDPOINTS = {
    "--enable": "/api/xuzhou-investment-ledger/outcome-learning-schedule/enable",
    "--pause": "/api/xuzhou-investment-ledger/outcome-learning-schedule/pause",
    "--status": "/api/xuzhou-investment-ledger/outcome-learning-schedule/",
}


def configure_outcome_learning_schedule(action: str) -> dict[str, Any]:
    """以当前认证用户对固定每日学习计划执行启用、暂停或查看。

    输入只能为三个固定动作，输出仅保留服务端生成的脱敏状态。登录、令牌保存、认证状态
    检查和 HTTP 错误处理复用来源监控的本机辅助模块；调用时不传递用户 ID、Cron、权重、
    规则、反馈、工具参数、URL、线索或证据。服务端会从短期令牌取得真实身份，并固定每天
    03:35（上海时区）的受限工具任务；计划本身不会立即学习或改写原始证据链。
    """
    if action not in _ALLOWED_ACTIONS:
        raise _shared.UserFacingError("用法仅支持 --enable、--pause 或 --status。")
    auth_status = _shared._request_json("/api/auth/status")
    if auth_status.get("enabled") is not True or auth_status.get("has_users") is not True:
        raise _shared.UserFacingError("QwenPaw 登录验证未启用或尚无登录用户，已停止计划操作。")
    token = _shared.authenticate_existing_user()
    try:
        result = _shared._request_json(
            _ENDPOINTS[action],
            method="GET" if action == "--status" else "POST",
            bearer_token=token,
        )
    finally:
        token = ""
    schedule = _shared.schedule_from_authenticated_response(result)
    return {
        "status": "每日结果学习计划操作已完成",
        "action": action,
        "changed": result.get("changed"),
        "schedule": schedule,
        "operation_scope": result.get("operation_scope"),
    }


def main() -> int:
    """执行固定每日学习计划动作；输入异常或认证失败一律失败关闭。"""
    arguments = sys.argv[1:]
    if len(arguments) != 1:
        print("未操作：用法为 --enable、--pause 或 --status。", file=sys.stderr)
        return 1
    try:
        result = configure_outcome_learning_schedule(arguments[0])
    except KeyboardInterrupt:
        print("\n已取消；未操作每日结果学习计划。", file=sys.stderr)
        return 1
    except _shared.UserFacingError as exc:
        print(f"未操作：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
