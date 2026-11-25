#!/usr/bin/env python3
"""由本机管理员按平台登录用户名维护徐州招商共享台账团队名册。

本脚本是无前端场景的运维入口：只调用已部署的 ``/team/members`` 管理 API，不直接读取或
编辑 team_members.json，不创建/停用账号，不接受 user_id，也不读取、写入或迁移招商台账。
成员身份始终由服务器平台用户目录按用户名解析，管理员身份也由本次登录令牌在服务端复核。
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
TEAM_API_PATH = "/api/xuzhou-investment-ledger/team/members"


class UserFacingError(RuntimeError):
    """表示可以直接显示给操作者的身份、参数、网络或服务器拒绝错误。"""


def _request_json(
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, str] | None = None,
    bearer_token: str | None = None,
) -> dict[str, Any]:
    """向固定本机服务发送最小 JSON 请求，令牌只在内存中存活本次命令。

    输入只能是本脚本写死的认证/团队路由、可选单一用户名 JSON 和短期 Bearer 令牌；输出为
    服务端 JSON 对象。函数不接受用户 ID、角色、名册路径、审计、线索、证据、URL 或密钥，
    因此终端参数无法伪造成员身份或改写业务数据。HTTP 错误仅提取服务端 detail，不记录
    Authorization 头；网络/格式异常均失败关闭。
    """
    if not path.startswith("/"):
        raise UserFacingError("内部安全校验失败：接口路径必须以 / 开头。")
    headers = {"Accept": "application/json", "X-Agent-Id": AGENT_ID}
    body: bytes | None = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    request = Request(f"{API_BASE_URL}{path}", data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            raw_response = response.read().decode("utf-8")
    except HTTPError as exc:
        try:
            error_payload = json.loads(exc.read().decode("utf-8"))
            detail = error_payload.get("detail") if isinstance(error_payload, dict) else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            detail = None
        reason = detail if isinstance(detail, str) and detail else "未返回可用原因"
        raise UserFacingError(f"服务器拒绝请求（HTTP {exc.code}）：{reason}") from exc
    except (URLError, TimeoutError) as exc:
        raise UserFacingError("无法连接或等待本机 QwenPaw 服务，请确认服务已启动后重试。") from exc
    try:
        response_payload = json.loads(raw_response)
    except json.JSONDecodeError as exc:
        raise UserFacingError("本机服务返回的不是有效 JSON，已停止操作。") from exc
    if not isinstance(response_payload, dict):
        raise UserFacingError("本机服务返回格式异常，已停止操作。")
    return response_payload


def _authenticate_existing_admin() -> str:
    """交互式登录管理员并返回只供本次名册请求使用的短期令牌。

    输入是终端的管理员用户名和隐藏密码；输出为服务端签发的令牌。账号是否存在、是否启用
    及是否为 admin 均由服务端认证响应决定，脚本不接受命令行角色/user_id/令牌，也不打印
    或保存密码和令牌。普通用户或异常响应会在任何名册请求之前被拒绝。
    """
    username = input("现有管理员用户名：").strip()
    if not username:
        raise UserFacingError("管理员用户名不能为空。")
    password = getpass.getpass("管理员密码（输入时不会显示）：")
    if not password:
        raise UserFacingError("管理员密码不能为空。")
    response = _request_json(
        "/api/auth/login",
        method="POST",
        payload={"username": username, "password": password},
    )
    token = response.get("token")
    user = response.get("user")
    if not isinstance(token, str) or not token or not isinstance(user, dict):
        raise UserFacingError("登录响应缺少必要字段，已停止操作。")
    if user.get("role") != "admin":
        raise UserFacingError("当前登录用户不是管理员，不能维护招商团队名册。")
    return token


def _require_enabled_authentication() -> None:
    """确认本机服务启用登录且已有账号；不读取或修改名册、台账或用户账号。"""
    auth_status = _request_json("/api/auth/status")
    if auth_status.get("enabled") is not True or auth_status.get("has_users") is not True:
        raise UserFacingError("QwenPaw 登录验证未就绪，不能维护招商团队名册。")


def _validated_username(argument: object) -> str:
    """校验命令行唯一允许的业务参数——已存在平台账号的用户名。

    输入是 ``--add`` 或 ``--remove`` 后的用户名文本，输出为去除首尾空白的用户名。函数拒绝
    空白、超长与控制字符，但不自行把用户名解析成 user_id：真实用户 ID 只能由服务器
    ``get_user_by_username`` 查询。函数不访问网络、名册、账号或台账。
    """
    if not isinstance(argument, str) or not (username := argument.strip()):
        raise UserFacingError("用户名不能为空。")
    if len(username) > 100 or any(ord(char) < 0x20 or ord(char) == 0x7F for char in username):
        raise UserFacingError("用户名格式不合法。")
    return username


def manage_team(action: str, username: str | None = None) -> dict[str, Any]:
    """以服务器认证管理员身份列出、添加或移除一名共享台账成员。

    输入是仅由 ``main`` 固定解析出的 ``list``/``add``/``remove`` 动作及可选用户名；输出为
    服务端脱敏名册响应。``list`` 不带用户名，``add``/``remove`` 只带用户名，绝不发送
    user_id、角色或审计；服务器路由会重新验证管理员、按用户名解析用户 ID、原子追加授权
    审计并保护既有业务台账。无论成功或失败，短期令牌均在函数结束时丢弃。
    """
    if action not in {"list", "add", "remove"}:
        raise UserFacingError("团队名册动作不受支持。")
    _require_enabled_authentication()
    token = _authenticate_existing_admin()
    try:
        if action == "list":
            return _request_json(TEAM_API_PATH, bearer_token=token)
        if username is None:
            raise UserFacingError("添加或移除成员必须提供用户名。")
        path = TEAM_API_PATH if action == "add" else f"{TEAM_API_PATH}/remove"
        return _request_json(
            path,
            method="POST",
            bearer_token=token,
            payload={"username": username},
        )
    finally:
        token = ""


def main() -> int:
    """解析严格的三种命令行形态，拒绝无参数和任何 user_id/角色类扩展参数。"""
    try:
        arguments = sys.argv[1:]
        if arguments == ["--list"]:
            result = manage_team("list")
        elif len(arguments) == 2 and arguments[0] == "--add":
            result = manage_team("add", _validated_username(arguments[1]))
        elif len(arguments) == 2 and arguments[0] == "--remove":
            result = manage_team("remove", _validated_username(arguments[1]))
        else:
            raise UserFacingError(
                "用法：--list；--add <已有登录用户名>；或 --remove <已有登录用户名>。"
            )
    except KeyboardInterrupt:
        print("\n已取消；未执行名册操作。", file=sys.stderr)
        return 1
    except UserFacingError as exc:
        print(f"未操作：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
