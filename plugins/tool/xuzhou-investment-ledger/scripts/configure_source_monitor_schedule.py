#!/usr/bin/env python3
"""以现有登录用户身份启用、暂停或查看徐州招商后台来源监控。

脚本不直接写 Cron 文件或招商台账，而是只调用运行中插件的认证接口。服务端会从登录令牌
恢复当前用户身份，并固定工具、频率、时区和访问范围；本脚本不能提交用户 ID、Cron、URL、
搜索词、证据、线索或评分字段。
"""

from __future__ import annotations

import getpass
import json
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_BASE_URL = "http://127.0.0.1:8088"
AGENT_ID = "default"
REQUEST_TIMEOUT_SECONDS = 15
_ALLOWED_ACTIONS = frozenset({"--enable", "--pause", "--status"})


class UserFacingError(RuntimeError):
    """表示可直接告知本机操作者的认证、服务或输入错误。"""


def _request_json(
    path: str,
    *,
    method: str = "GET",
    bearer_token: str | None = None,
    timeout_seconds: int = REQUEST_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """调用固定本机服务并返回 JSON 对象；令牌只存在于本次请求头，不打印或落盘。

    输入为固定本机路径、HTTP 方法、短期认证令牌和有限的等待秒数，输出为服务端 JSON。
    ``timeout_seconds`` 只控制本机等待时长，不能改变服务端任务、Cron、来源或证据；范围
    限制在 1 至 300 秒，避免调用方借此制造无限等待。网络超时会转换为受控业务错误，
    不泄露 Python 调用栈、令牌或密码，也不暗示请求一定未被服务器受理。
    """
    if not path.startswith("/"):
        raise UserFacingError("内部安全校验失败：接口路径必须以 / 开头。")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int):
        raise UserFacingError("内部安全校验失败：请求等待时间必须是整数秒。")
    if not 1 <= timeout_seconds <= 300:
        raise UserFacingError("内部安全校验失败：请求等待时间必须在 1 至 300 秒之间。")
    headers = {"Accept": "application/json", "X-Agent-Id": AGENT_ID}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    request = Request(f"{API_BASE_URL}{path}", headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            raw_response = response.read().decode("utf-8")
    except TimeoutError as exc:
        raise UserFacingError(
            "本机 QwenPaw 服务响应超时；请求可能已被服务器受理，请先查看计划状态，"
            "不要重复提交。"
        ) from exc
    except HTTPError as exc:
        try:
            error_payload = json.loads(exc.read().decode("utf-8"))
            detail = error_payload.get("detail") if isinstance(error_payload, dict) else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            detail = None
        detail_text = detail if isinstance(detail, str) and detail else "未返回可用原因"
        raise UserFacingError(f"服务器拒绝请求（HTTP {exc.code}）：{detail_text}") from exc
    except URLError as exc:
        raise UserFacingError(
            "无法连接本机 QwenPaw 服务（http://127.0.0.1:8088）。请确认 QwenPaw 已启动。"
        ) from exc
    try:
        response_payload = json.loads(raw_response)
    except json.JSONDecodeError as exc:
        raise UserFacingError("本机服务返回的不是有效 JSON，已停止操作。") from exc
    if not isinstance(response_payload, dict):
        raise UserFacingError("本机服务返回格式异常，已停止操作。")
    return response_payload


def authenticate_existing_user() -> str:
    """交互式登录并返回短期令牌；身份完全由服务端认证，不接受命令行用户 ID。"""
    username = input("现有登录用户名：").strip()
    if not username:
        raise UserFacingError("登录用户名不能为空。")
    password = getpass.getpass("登录密码（输入时不会显示）：")
    if not password:
        raise UserFacingError("登录密码不能为空。")
    # urllib 的认证请求需要发送 JSON；单独放在此处避免其他计划接口出现任何请求体。
    payload = json.dumps({"username": username, "password": password}, ensure_ascii=False).encode(
        "utf-8"
    )
    request = Request(
        f"{API_BASE_URL}/api/auth/login",
        data=payload,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Agent-Id": AGENT_ID,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            login_payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise UserFacingError(f"登录被服务器拒绝（HTTP {exc.code}）。") from exc
    except (TimeoutError, URLError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UserFacingError("无法完成本机登录，已停止计划操作。") from exc
    if not isinstance(login_payload, dict):
        raise UserFacingError("登录响应格式异常，已停止计划操作。")
    token = login_payload.get("token")
    user = login_payload.get("user")
    if not isinstance(token, str) or not token or not isinstance(user, dict):
        raise UserFacingError("登录响应缺少必要字段，已停止计划操作。")
    if user.get("role") not in {"admin", "user"}:
        raise UserFacingError("当前登录用户角色不支持后台监控计划操作。")
    return token


def schedule_from_authenticated_response(response: object) -> dict[str, Any]:
    """归一化已认证计划接口的历史嵌套与当前平铺响应，返回服务器生成的计划状态。

    输入只能是本机服务认证后返回的 JSON 对象，输出是其中的计划状态字典。早期接口曾把
    状态放在 ``schedule`` 字段，当前接口为避免重复包装而将 ``configured``、``enabled``
    等字段平铺在顶层；本函数兼容两种服务端格式，绝不从客户端补造计划、用户 ID、Cron、
    时区或工具参数。它只读取响应，不会触发任务，更不会修改 evidence、来源 URL、原始
    摘录、采集时间、评分依据或任何线索数据。
    """
    if not isinstance(response, dict) or response.get("ok") is not True:
        raise UserFacingError("服务未确认计划操作成功，请勿重复执行并联系工程师核验。")
    nested_schedule = response.get("schedule")
    if isinstance(nested_schedule, dict):
        return nested_schedule
    # 当前已部署接口把状态平铺在响应顶层。必须至少存在 configured，避免把错误 JSON
    # 误当成计划状态；复制后移除传输层的 ok 标记，调用方始终得到统一的计划对象。
    if isinstance(response.get("configured"), bool):
        # 响应中的 changed 和 operation_scope 属于本次操作元数据，不能混入稳定的 schedule
        # 对象；只保留服务端公开的计划状态白名单，防止未来接口新增字段被错误地当成计划配置。
        schedule_keys = (
            "configured",
            "job_id",
            "enabled",
            "fixed_cron",
            "timezone",
            "tool_name",
            "tool_arguments",
            "runtime_state",
        )
        return {key: response[key] for key in schedule_keys if key in response}
    raise UserFacingError("来源监控计划状态格式异常，已停止计划操作。")


def configure_source_monitor_schedule(action: str) -> dict[str, Any]:
    """以当前认证用户执行固定启用/暂停/查看操作，并返回脱敏服务端结果。

    输入只能是三个固定动作，输出为服务端生成的计划状态与审计。脚本先确认本机认证启用，
    再以短期令牌调用插件；不传递操作者 ID、Cron、时区、工具参数、会话、URL、证据或线索。
    服务端仍会校验工作区归属、计划 ID 和固定工具参数，计划操作不能改写原始证据链。
    """
    if action not in _ALLOWED_ACTIONS:
        raise UserFacingError("用法仅支持 --enable、--pause 或 --status。")
    auth_status = _request_json("/api/auth/status")
    if auth_status.get("enabled") is not True or auth_status.get("has_users") is not True:
        raise UserFacingError("QwenPaw 登录验证未启用或尚无登录用户，已停止计划操作。")
    token = authenticate_existing_user()
    endpoint = {
        "--enable": "/api/xuzhou-investment-ledger/source-monitor-schedule/enable",
        "--pause": "/api/xuzhou-investment-ledger/source-monitor-schedule/pause",
        "--status": "/api/xuzhou-investment-ledger/source-monitor-schedule/",
    }[action]
    method = "GET" if action == "--status" else "POST"
    try:
        result = _request_json(endpoint, method=method, bearer_token=token)
    finally:
        token = ""
    schedule = schedule_from_authenticated_response(result)
    return {
        "status": "后台来源监控计划操作已完成",
        "action": action,
        "changed": result.get("changed"),
        "schedule": schedule,
        "operation_scope": result.get("operation_scope"),
    }


def main() -> int:
    """运行一个固定计划动作；未知参数、认证失败或服务拒绝均失败关闭。"""
    arguments = sys.argv[1:]
    if len(arguments) != 1:
        print("未操作：用法为 --enable、--pause 或 --status。", file=sys.stderr)
        return 1
    try:
        result = configure_source_monitor_schedule(arguments[0])
    except KeyboardInterrupt:
        print("\n已取消；未操作后台计划。", file=sys.stderr)
        return 1
    except UserFacingError as exc:
        print(f"未操作：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
