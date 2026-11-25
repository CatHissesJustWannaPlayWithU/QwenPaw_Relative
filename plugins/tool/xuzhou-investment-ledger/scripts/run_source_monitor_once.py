#!/usr/bin/env python3
"""以现有登录用户身份，手动触发一次已启用的徐州招商来源监控。

这是给本机验收使用的固定用途脚本：它不能传入任意 Cron 任务、网站、搜索词、身份或
招商数据。登录密码和短期令牌只用于本次本机请求，既不会打印，也不会写入文件。
"""

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


# 任务 ID 是服务器创建固定来源监控计划时的常量。脚本不提供命令行参数，避免被用来
# 触发其他任务；实际来源范围、频率限制、robots 与证据保护仍由服务器端工具复核。
SOURCE_MONITOR_JOB_ID = "xuzhou-investment-public-source-monitor-v1"
SOURCE_MONITOR_SCHEDULE_STATUS_ENDPOINT = (
    "/api/xuzhou-investment-ledger/source-monitor-schedule/"
)
SOURCE_MONITOR_OPERATION_SCOPE = "authenticated_user_fixed_manual_source_monitor_run_only"


def _enabled_fixed_schedule(schedule_response: object) -> dict[str, Any]:
    """验证服务器返回的计划确为已启用的固定来源监控计划并返回其状态。

    输入是服务器认证后产生的只读计划响应；输出是已验证的计划字典。此函数不接受
    客户端任务 ID 或计划参数：只有计划已配置、已启用且 ID 完全等于本模块常量时才允许
    后续执行，因而手动运行不能绕过用户此前的暂停决定，也不能改写证据、来源 URL、原始
    摘录、采集时间或评分依据。
    """
    schedule = _shared.schedule_from_authenticated_response(schedule_response)
    if schedule.get("configured") is not True:
        raise _shared.UserFacingError("来源监控计划尚未启用。请先执行 --enable。")
    if schedule.get("enabled") is not True:
        raise _shared.UserFacingError("来源监控计划当前已暂停。请先执行 --enable，确认后再运行。")
    if schedule.get("job_id") != SOURCE_MONITOR_JOB_ID:
        raise _shared.UserFacingError("来源监控计划 ID 与固定安全配置不一致，已停止手动执行。")
    return schedule


def run_source_monitor_once() -> dict[str, Any]:
    """以服务端认证的当前用户触发一次固定来源监控，并返回启动确认和即时运行状态。

    输入来自本机交互登录，操作者身份只由服务器登录响应确认；输出仅含服务器生成的启动
    结果、任务 ID 和运行状态。函数不接收 ``reviewer_user_id``、Cron、工具参数、URL、
    搜索词、证据、线索或评分，因此不能借“手动执行”伪造身份、扩大采集范围、自动推进
    线索或改写原始证据链。实际运行是异步的：``started`` 仅表示服务器已受理，不表示本轮
    已找到线索；应稍后通过计划状态查看 ``last_run_at`` 与 ``last_status``。
    """
    auth_status = _shared._request_json("/api/auth/status")
    if auth_status.get("enabled") is not True or auth_status.get("has_users") is not True:
        raise _shared.UserFacingError("QwenPaw 登录验证未启用或尚无登录用户，已停止手动执行。")

    token = _shared.authenticate_existing_user()
    try:
        schedule_response = _shared._request_json(
            SOURCE_MONITOR_SCHEDULE_STATUS_ENDPOINT,
            bearer_token=token,
        )
        schedule = _enabled_fixed_schedule(schedule_response)
        trigger_result = _shared._request_json(
            f"/api/cron/jobs/{SOURCE_MONITOR_JOB_ID}/run",
            method="POST",
            bearer_token=token,
            # 当前平台的 Cron 工具会在同一事件循环中执行少量同步公开来源访问；服务器
            # 可能要等本轮结束才能回传 HTTP 响应。这里最多等待 5 分钟（与服务端 240 秒
            # 工具超时留有余量），避免 15 秒超时后用户误以为未执行而重复触发。
            timeout_seconds=300,
        )
        if trigger_result.get("started") is not True:
            raise _shared.UserFacingError("服务未确认已启动来源监控，请勿重复执行并联系工程师核验。")
        runtime_state = _shared._request_json(
            f"/api/cron/jobs/{SOURCE_MONITOR_JOB_ID}/state",
            bearer_token=token,
        )
    finally:
        token = ""

    return {
        "status": "已请求立即执行徐州招商后台来源监控",
        "job_id": SOURCE_MONITOR_JOB_ID,
        "trigger": "manual",
        "trigger_result": {"started": True},
        "runtime_state": runtime_state,
        "schedule": {
            "configured": schedule["configured"],
            "enabled": schedule["enabled"],
            "fixed_cron": schedule.get("fixed_cron"),
            "timezone": schedule.get("timezone"),
        },
        "operation_scope": SOURCE_MONITOR_OPERATION_SCOPE,
    }


def main() -> int:
    """运行无参数的固定手动触发流程；额外参数一律拒绝，认证或服务失败即停止。"""
    if sys.argv[1:]:
        print("未操作：此脚本不接受参数。", file=sys.stderr)
        return 1
    try:
        result = run_source_monitor_once()
    except KeyboardInterrupt:
        print("\n已取消；未触发后台来源监控。", file=sys.stderr)
        return 1
    except _shared.UserFacingError as exc:
        print(f"未操作：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
