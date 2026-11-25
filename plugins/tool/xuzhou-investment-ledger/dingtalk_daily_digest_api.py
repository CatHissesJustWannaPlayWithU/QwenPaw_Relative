"""管理员专用的钉钉日报开关；收件人只能由部署管理员预先绑定。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from qwenpaw.app.agent_context import get_agent_for_request
from qwenpaw.app.principal import Principal, get_current_principal

from .dingtalk_daily_digest import (
    enable_dingtalk_daily_digest_subscription,
    list_dingtalk_daily_digest_subscriptions,
    pause_dingtalk_daily_digest_subscription,
    run_dingtalk_daily_digest_now,
)
from .shared_workspace import shared_ledger_dir_for_request


router = APIRouter()


def _require_admin(request: Request) -> Principal:
    principal = get_current_principal(request)
    if not principal.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="只有管理员可以管理钉钉日报订阅",
        )
    return principal


def _translate_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.get("/subscriptions", status_code=status.HTTP_200_OK)
async def get_dingtalk_daily_digest_subscriptions(request: Request) -> dict[str, Any]:
    """返回脱敏启用和投递状态；不返回收件人、会话、正文、网页或原始证据。"""
    _require_admin(request)
    ledger_dir = shared_ledger_dir_for_request(request)
    try:
        return {"ok": True, "subscriptions": list_dingtalk_daily_digest_subscriptions(ledger_dir)}
    except (OSError, RuntimeError, ValueError) as exc:
        raise _translate_error(exc) from exc


@router.post("/enable", status_code=status.HTTP_200_OK)
async def enable_dingtalk_daily_digest(request: Request) -> dict[str, Any]:
    """启用部署时绑定的唯一钉钉会话；请求不接受任何收件人、会话或消息字段。"""
    principal = _require_admin(request)
    ledger_dir = shared_ledger_dir_for_request(request)
    workspace = await get_agent_for_request(request)
    try:
        return {
            "ok": True,
            "subscription": await enable_dingtalk_daily_digest_subscription(
                workspace,
                ledger_dir=ledger_dir,
                authenticated_admin_user_id=principal.user_id,
            ),
        }
    except (OSError, RuntimeError, ValueError) as exc:
        raise _translate_error(exc) from exc


@router.post("/subscriptions/{subscription_id}/pause", status_code=status.HTTP_200_OK)
async def pause_dingtalk_daily_digest(subscription_id: str, request: Request) -> dict[str, Any]:
    """暂停日报及其 09:05 重试，不删除台账或投递审计。"""
    principal = _require_admin(request)
    ledger_dir = shared_ledger_dir_for_request(request)
    workspace = await get_agent_for_request(request)
    try:
        return {
            "ok": True,
            "subscription": await pause_dingtalk_daily_digest_subscription(
                workspace,
                ledger_dir=ledger_dir,
                authenticated_admin_user_id=principal.user_id,
                subscription_id=subscription_id,
            ),
        }
    except (OSError, RuntimeError, ValueError) as exc:
        raise _translate_error(exc) from exc


@router.post("/subscriptions/{subscription_id}/run", status_code=status.HTTP_202_ACCEPTED)
async def run_dingtalk_daily_digest(subscription_id: str, request: Request) -> dict[str, Any]:
    """触发一次固定日报以供上线验收；不允许自定义文本或收件人。"""
    _require_admin(request)
    ledger_dir = shared_ledger_dir_for_request(request)
    workspace = await get_agent_for_request(request)
    try:
        return {
            "ok": True,
            **(await run_dingtalk_daily_digest_now(
                workspace,
                ledger_dir=ledger_dir,
                subscription_id=subscription_id,
            )),
        }
    except (OSError, RuntimeError, ValueError) as exc:
        raise _translate_error(exc) from exc
