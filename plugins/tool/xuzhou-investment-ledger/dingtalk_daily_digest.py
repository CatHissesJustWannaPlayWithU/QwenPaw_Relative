"""管理员绑定的钉钉日报：固定时间、固定内部会话、只发计数和最多五个来源标题。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from qwenpaw.app.agent_context import get_current_user_id
from qwenpaw.app.crons.models import (
    CronJobSpec,
    CronToolRequest,
    DispatchSpec,
    DispatchTarget,
    JobRuntimeSpec,
    ScheduleSpec,
)

from .shared_workspace import shared_ledger_root
from .storage import load_ledger, save_ledger


DAILY_DIGEST_TOOL_NAME = "deliver_xuzhou_dingtalk_daily_digest"
DAILY_DIGEST_SCOPE = "xuzhou_internal_dingtalk_daily_digest_only"
DAILY_DIGEST_PRIMARY_CRON = "0 9 * * *"
DAILY_DIGEST_RETRY_CRON = "5 9 * * *"
DAILY_DIGEST_TIMEZONE = "Asia/Shanghai"
DAILY_DIGEST_OPERATION_SCOPE = "admin_bound_internal_dingtalk_daily_digest_only"
DAILY_DIGEST_CHAT_ID_ENV = "XUZHOU_DINGTALK_DAILY_CHAT_ID"
_SUBSCRIPTIONS_FIELD = "dingtalk_daily_digest_subscriptions"
_DELIVERY_AUDIT_FIELD = "dingtalk_daily_digest_delivery_audit"
_MUTATION_LOCK = asyncio.Lock()
_CHINA_TZ = ZoneInfo(DAILY_DIGEST_TIMEZONE)
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, *parts: str) -> str:
    joined_parts = "\x00".join(parts)
    digest = hashlib.sha256(joined_parts.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}-{digest}"


def _required_server_text(value: object, field_name: str, *, max_length: int = 256) -> str:
    if not isinstance(value, str) or not (normalized := value.strip()):
        raise ValueError(f"{field_name}不能为空")
    if len(normalized) > max_length or _CONTROL_CHARACTERS.search(normalized):
        raise ValueError(f"{field_name}格式无效")
    return normalized


def _records(ledger: dict[str, Any], field_name: str, label: str) -> list[dict[str, Any]]:
    records = ledger.get(field_name)
    if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
        raise ValueError(f"招商线索台账中的{label}格式异常")
    return records


def _subscriptions(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    return _records(ledger, _SUBSCRIPTIONS_FIELD, "钉钉日报订阅记录")


def _delivery_audit(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    return _records(ledger, _DELIVERY_AUDIT_FIELD, "钉钉日报投递审计")


def _evidence_snapshot(ledger: dict[str, Any]) -> str:
    evidence = ledger.get("evidence")
    if not isinstance(evidence, list):
        raise ValueError("招商线索台账中的evidence必须是列表")
    return json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _subscription_id(agent_id: str, recipient_user_id: str) -> str:
    return _stable_id("xuzhou-dingtalk-digest", agent_id, recipient_user_id)


def _job_id(subscription_id: str, *, retry: bool) -> str:
    return f"{subscription_id}-{'retry' if retry else 'primary'}"


def _job_for_subscription(subscription: dict[str, Any], *, retry: bool, enabled: bool) -> CronJobSpec:
    subscription_id = _required_server_text(subscription.get("subscription_id"), "subscription_id")
    user_id = _required_server_text(subscription.get("recipient_user_id"), "钉钉会话用户标识")
    session_id = _required_server_text(subscription.get("recipient_session_id"), "钉钉会话标识")
    return CronJobSpec(
        id=_job_id(subscription_id, retry=retry),
        name="徐州招商钉钉日报失败重试（09:05）" if retry else "徐州招商钉钉日报（每日09:00）",
        enabled=enabled,
        schedule=ScheduleSpec(
            type="cron",
            cron=DAILY_DIGEST_RETRY_CRON if retry else DAILY_DIGEST_PRIMARY_CRON,
            timezone=DAILY_DIGEST_TIMEZONE,
        ),
        task_type="tool",
        tool=CronToolRequest(name=DAILY_DIGEST_TOOL_NAME, arguments={"run_scope": DAILY_DIGEST_SCOPE}),
        dispatch=DispatchSpec(
            type="channel",
            channel="dingtalk",
            target=DispatchTarget(user_id=user_id, session_id=session_id),
            mode="final",
            silent=False,
            meta={"purpose": "xuzhou_internal_daily_digest", "subscription_id": subscription_id, "retry": retry},
        ),
        save_result_to_inbox=False,
        runtime=JobRuntimeSpec(
            max_concurrency=1,
            timeout_seconds=90,
            misfire_grace_seconds=900,
            share_session=False,
            tool_safety=True,
        ),
        meta={
            "purpose": "xuzhou_internal_daily_digest",
            "operation_scope": DAILY_DIGEST_OPERATION_SCOPE,
            "subscription_id": subscription_id,
            "retry": retry,
        },
    )


def _assert_managed_job(job: CronJobSpec, subscription: dict[str, Any], *, retry: bool) -> None:
    expected = _job_for_subscription(subscription, retry=retry, enabled=job.enabled)
    if (
        job.id != expected.id
        or job.task_type != "tool"
        or job.tool is None
        or job.tool.name != DAILY_DIGEST_TOOL_NAME
        or job.tool.arguments != {"run_scope": DAILY_DIGEST_SCOPE}
        or job.schedule.type != "cron"
        or job.schedule.cron != expected.schedule.cron
        or job.schedule.timezone != DAILY_DIGEST_TIMEZONE
        or job.dispatch.channel != "dingtalk"
        or job.dispatch.target != expected.dispatch.target
        or job.dispatch.silent is not False
        or job.meta.get("operation_scope") != DAILY_DIGEST_OPERATION_SCOPE
        or job.meta.get("subscription_id") != subscription.get("subscription_id")
        or job.meta.get("retry") is not retry
    ):
        raise RuntimeError("发现同名但不受控的钉钉日报计划，拒绝覆盖，请管理员人工核验")


def _manager_or_raise(workspace: object) -> Any:
    manager = getattr(workspace, "cron_manager", None)
    if manager is None:
        raise RuntimeError("当前智能体的后台定时服务未初始化，无法登记钉钉日报")
    return manager


def _public_subscription(subscription: dict[str, Any], ledger: dict[str, Any]) -> dict[str, object]:
    subscription_id = _required_server_text(subscription.get("subscription_id"), "subscription_id")
    latest = next(
        (item for item in reversed(_delivery_audit(ledger)) if item.get("subscription_id") == subscription_id),
        None,
    )
    return {
        "subscription_id": subscription_id,
        "enabled": subscription.get("enabled") is True,
        "schedule": {"primary": DAILY_DIGEST_PRIMARY_CRON, "retry_once": DAILY_DIGEST_RETRY_CRON, "timezone": DAILY_DIGEST_TIMEZONE},
        "latest_delivery": (
            {key: latest.get(key) for key in ("report_date", "status", "attempt_number", "created_at", "completed_at")}
            if latest is not None else None
        ),
    }


async def _configured_target(workspace: object) -> dict[str, str]:
    """只接受部署管理员写入容器环境的唯一会话 ID，HTTP 客户端无法选择或替换收件人。"""
    chat_id = _required_server_text(
        os.environ.get(DAILY_DIGEST_CHAT_ID_ENV),
        "钉钉日报已绑定会话",
    )
    chat_manager = getattr(workspace, "chat_manager", None)
    if chat_manager is None:
        raise RuntimeError("当前智能体的会话服务未初始化，无法读取钉钉会话")
    chat = await chat_manager.get_chat(chat_id)
    if chat is None or getattr(chat, "channel", None) != "dingtalk":
        raise ValueError("未找到可订阅的钉钉会话；请先在钉钉中向机器人发送一条消息")
    return {
        "chat_id": _required_server_text(chat.id, "钉钉会话编号"),
        "recipient_user_id": _required_server_text(chat.user_id, "钉钉会话用户标识"),
        "recipient_session_id": _required_server_text(chat.session_id, "钉钉会话标识"),
    }


async def enable_dingtalk_daily_digest_subscription(
    workspace: object, *, ledger_dir: Path, authenticated_admin_user_id: str
) -> dict[str, object]:
    """管理员启用部署时已绑定的唯一会话；HTTP 请求不能选择、替换或枚举收件人。"""
    admin_id = _required_server_text(authenticated_admin_user_id, "管理员身份")
    target = await _configured_target(workspace)
    agent_id = _required_server_text(getattr(workspace, "agent_id", None), "当前智能体ID")
    manager = _manager_or_raise(workspace)
    async with _MUTATION_LOCK:
        ledger = load_ledger(ledger_dir)
        evidence_before = _evidence_snapshot(ledger)
        subscription_id = _subscription_id(agent_id, target["recipient_user_id"])
        subscription = next((item for item in _subscriptions(ledger) if item.get("subscription_id") == subscription_id), None)
        if subscription is None:
            subscription = {
                "subscription_id": subscription_id,
                "channel": "dingtalk",
                "recipient_user_id": target["recipient_user_id"],
                "recipient_session_id": target["recipient_session_id"],
                "chat_id": target["chat_id"],
                "enabled": False,
                "created_at": _utc_now(),
                "created_by_admin_user_id": admin_id,
                "operation_scope": DAILY_DIGEST_OPERATION_SCOPE,
            }
            _subscriptions(ledger).append(subscription)
        else:
            # 只有管理员重新选择已存在的会话，才可更新同一人的投递会话。
            subscription.update({"recipient_session_id": target["recipient_session_id"], "chat_id": target["chat_id"], "enabled": False})
        subscription["updated_at"] = _utc_now()
        subscription["updated_by_admin_user_id"] = admin_id
        if _evidence_snapshot(ledger) != evidence_before:
            raise RuntimeError("钉钉日报订阅操作不得修改原始招商证据链")
        # 先落盘 disabled；注册计划过程异常时不会产生可投递订阅。
        save_ledger(ledger_dir, ledger)  # type: ignore[arg-type]
        for retry in (False, True):
            desired = _job_for_subscription(subscription, retry=retry, enabled=True)
            existing = await manager.get_job(desired.id or "")
            if existing is not None:
                _assert_managed_job(existing, subscription, retry=retry)
            await manager.create_or_replace_job(desired)
        ledger = load_ledger(ledger_dir)
        subscription = next(item for item in _subscriptions(ledger) if item.get("subscription_id") == subscription_id)
        subscription["enabled"] = True
        subscription["updated_at"] = _utc_now()
        subscription["updated_by_admin_user_id"] = admin_id
        if _evidence_snapshot(ledger) != evidence_before:
            raise RuntimeError("钉钉日报订阅操作不得修改原始招商证据链")
        save_ledger(ledger_dir, ledger)  # type: ignore[arg-type]
        return _public_subscription(subscription, ledger)


async def pause_dingtalk_daily_digest_subscription(
    workspace: object, *, ledger_dir: Path, authenticated_admin_user_id: str, subscription_id: str
) -> dict[str, object]:
    _required_server_text(authenticated_admin_user_id, "管理员身份")
    subscription_id = _required_server_text(subscription_id, "subscription_id")
    manager = _manager_or_raise(workspace)
    async with _MUTATION_LOCK:
        ledger = load_ledger(ledger_dir)
        evidence_before = _evidence_snapshot(ledger)
        subscription = next((item for item in _subscriptions(ledger) if item.get("subscription_id") == subscription_id), None)
        if subscription is None:
            raise ValueError("未找到该钉钉日报订阅")
        for retry in (False, True):
            desired = _job_for_subscription(subscription, retry=retry, enabled=False)
            existing = await manager.get_job(desired.id or "")
            if existing is not None:
                _assert_managed_job(existing, subscription, retry=retry)
            await manager.create_or_replace_job(desired)
        subscription["enabled"] = False
        subscription["updated_at"] = _utc_now()
        if _evidence_snapshot(ledger) != evidence_before:
            raise RuntimeError("钉钉日报订阅操作不得修改原始招商证据链")
        save_ledger(ledger_dir, ledger)  # type: ignore[arg-type]
        return _public_subscription(subscription, ledger)


async def run_dingtalk_daily_digest_now(workspace: object, *, ledger_dir: Path, subscription_id: str) -> dict[str, object]:
    """管理员上线验证使用；执行的仍是同一固定日报，非任意消息发送接口。"""
    subscription_id = _required_server_text(subscription_id, "subscription_id")
    ledger = load_ledger(ledger_dir)
    subscription = next((item for item in _subscriptions(ledger) if item.get("subscription_id") == subscription_id), None)
    if subscription is None or subscription.get("enabled") is not True:
        raise ValueError("该钉钉日报订阅未启用")
    manager = _manager_or_raise(workspace)
    job_id = _job_id(subscription_id, retry=False)
    job = await manager.get_job(job_id)
    if job is None:
        raise RuntimeError("日报主任务不存在，请先重新启用订阅")
    _assert_managed_job(job, subscription, retry=False)
    await manager.run_job(job_id)
    return {"subscription_id": subscription_id, "started": True}


def list_dingtalk_daily_digest_subscriptions(ledger_dir: Path) -> list[dict[str, object]]:
    ledger = load_ledger(ledger_dir)
    return [_public_subscription(item, ledger) for item in _subscriptions(ledger)]


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return timestamp.astimezone(timezone.utc) if timestamp.tzinfo else None


def _daily_window(now: datetime | None = None) -> tuple[datetime, datetime, str]:
    current = (now or datetime.now(_CHINA_TZ)).astimezone(_CHINA_TZ)
    cutoff = datetime.combine(current.date(), time(9), tzinfo=_CHINA_TZ)
    end = cutoff if current >= cutoff else cutoff - timedelta(days=1)
    start = end - timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc), end.date().isoformat()


def _in_window(ledger: dict[str, Any], field_name: str, timestamp_field: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for item in _records(ledger, field_name, field_name):
        timestamp = _parse_timestamp(item.get(timestamp_field))
        if timestamp is not None and start <= timestamp < end:
            selected.append(item)
    return sorted(selected, key=lambda item: str(item.get(timestamp_field, "")), reverse=True)


def _safe_title(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    title = " ".join(_CONTROL_CHARACTERS.sub(" ", value).split())
    return title[:120] if title else None


def _source_diversity_note(ledger: dict[str, Any], *, end: datetime) -> str | None:
    """Build a short, link-free concentration warning for the private digest.

    This is an operational reminder only.  It does not filter, downgrade or
    delete candidates, and it deliberately omits raw URLs and contact data.
    """
    candidates = _records(ledger, "discovery_candidates", "发现候选")
    started = end - timedelta(days=7)
    domains: list[str] = []
    for candidate in candidates:
        discovered_at = _parse_timestamp(candidate.get("discovered_at"))
        source_url = candidate.get("source_url")
        if discovered_at is None or not (started <= discovered_at < end) or not isinstance(source_url, str):
            continue
        from urllib.parse import urlparse

        domain = (urlparse(source_url).hostname or "").casefold().rstrip(".")
        if domain:
            domains.append(domain)
    if len(domains) < 3:
        return None
    counts: dict[str, int] = {}
    for domain in domains:
        counts[domain] = counts.get(domain, 0) + 1
    dominant_domain, dominant_count = max(counts.items(), key=lambda item: (item[1], item[0]))
    share = dominant_count / len(domains)
    if share <= 0.60:
        return None
    return f"来源提示：近7天 {share * 100:.0f}% 待核验候选来自 {dominant_domain}，建议优先补充独立来源。"


def _digest_text(ledger: dict[str, Any], *, start: datetime, end: datetime, report_date: str) -> tuple[str, dict[str, int]]:
    groups = (
        ("正式公开证据", "evidence", "created_at"),
        ("待核验候选", "discovery_candidates", "discovered_at"),
        ("原始观察", "candidate_observations", "collected_at"),
        ("待确认草稿", "evidence_drafts", "created_at"),
    )
    selected = [(label, _in_window(ledger, field, timestamp, start, end)) for label, field, timestamp in groups]
    counts = {field: len(items) for (_, field, _), (_, items) in zip(groups, selected)}
    lines = [
        f"【徐州招商｜今日新增动态】{report_date}",
        f"统计窗口：{start.astimezone(_CHINA_TZ):%m-%d %H:%M} 至 {end.astimezone(_CHINA_TZ):%m-%d %H:%M}（北京时间）",
        f"正式公开证据 {counts['evidence']} 条｜待核验候选 {counts['discovery_candidates']} 条｜原始观察 {counts['candidate_observations']} 条｜待确认草稿 {counts['evidence_drafts']} 条",
    ]
    titles: list[str] = []
    for label, items in selected:
        for item in items:
            title = _safe_title(item.get("source_title"))
            if title and title not in titles:
                titles.append(title)
            if len(titles) == 5:
                break
        if len(titles) == 5:
            break
    if titles:
        lines.append("新增来源标题（最多 5 条）：")
        lines.extend(f"- {title}" for title in titles)
    else:
        lines.append("本窗口暂无新增记录。")
    if diversity_note := _source_diversity_note(ledger, end=end):
        lines.append(diversity_note)
    lines.append("仅供内部公开信息监测与人工复核；不构成事实确认、招商结论或对外联系。")
    return "\n".join(lines), counts


async def deliver_xuzhou_dingtalk_daily_digest(run_scope: str) -> dict[str, object]:
    """Cron 专用工具，返回经批准的计数与最多五个来源标题，供固定钉钉目标投递。"""
    if run_scope != DAILY_DIGEST_SCOPE:
        raise ValueError("钉钉日报只能由固定Cron范围调用")
    recipient_user_id = _required_server_text(get_current_user_id(), "当前钉钉会话用户标识")
    ledger_dir = shared_ledger_root()
    ledger = load_ledger(ledger_dir)
    matching = [
        item for item in _subscriptions(ledger)
        if item.get("enabled") is True and item.get("channel") == "dingtalk"
        and item.get("recipient_user_id") == recipient_user_id
        and item.get("operation_scope") == DAILY_DIGEST_OPERATION_SCOPE
    ]
    if len(matching) != 1:
        return {"ok": False, "error": "当前钉钉会话未被管理员订阅，拒绝生成日报"}
    subscription_id = _required_server_text(matching[0].get("subscription_id"), "subscription_id")
    start, end, report_date = _daily_window()
    same_day = [item for item in _delivery_audit(ledger) if item.get("subscription_id") == subscription_id and item.get("report_date") == report_date]
    if any(item.get("status") == "sent" for item in same_day):
        return {"ok": True, "cron_delivery": None, "status": "already_sent"}
    # 未确认投递结果时不二次发送，避免日报重复；09:05 只会重试已明确失败的 09:00 投递。
    if any(item.get("status") == "pending" for item in same_day):
        return {"ok": True, "cron_delivery": None, "status": "pending_confirmation"}
    text, counts = _digest_text(ledger, start=start, end=end, report_date=report_date)
    attempt_number = len(same_day) + 1
    delivery_id = _stable_id("xuzhou-dingtalk-delivery", subscription_id, report_date, str(attempt_number))
    _delivery_audit(ledger).append({
        "delivery_id": delivery_id,
        "subscription_id": subscription_id,
        "report_date": report_date,
        "attempt_number": attempt_number,
        "status": "pending",
        "created_at": _utc_now(),
        "completed_at": None,
        "delivery_error": None,
        "summary_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "summary_counts": counts,
        "operation_scope": DAILY_DIGEST_OPERATION_SCOPE,
    })
    save_ledger(ledger_dir, ledger)  # type: ignore[arg-type]
    return {"ok": True, "cron_delivery": {"text": text, "delivery_id": delivery_id}}


async def record_xuzhou_dingtalk_daily_digest_delivery(*, result: object, status: str, error: str | None) -> None:
    """Cron 通道投递完成后只回写本次审计状态，供 09:05 的失败重试判断。"""
    if status not in {"sent", "failed"} or not isinstance(result, dict):
        return
    delivery = result.get("cron_delivery")
    if not isinstance(delivery, dict) or not isinstance(delivery.get("delivery_id"), str):
        return
    ledger_dir = shared_ledger_root()
    ledger = load_ledger(ledger_dir)
    record = next((item for item in _delivery_audit(ledger) if item.get("delivery_id") == delivery["delivery_id"] and item.get("status") == "pending"), None)
    if record is None:
        return
    record["status"] = status
    record["completed_at"] = _utc_now()
    record["delivery_error"] = (error or "")[:500] if status == "failed" else None
    save_ledger(ledger_dir, ledger)  # type: ignore[arg-type]
