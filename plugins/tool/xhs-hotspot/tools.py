"""暴露给 QwenPaw 智能体调用的热点采集工具。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from qwenpaw.config.context import get_current_workspace_dir
from qwenpaw.plugins import get_tool_config

from .core import SnapshotValidationError, normalize_snapshot
from .provider import (
    AidataHotspotClient,
    HotspotProviderError,
    UapiHotspotClient,
)
from .storage import (
    list_recent_reports,
    list_recent_snapshots,
    load_preference_profile,
    save_snapshot,
)
from .email_delivery import EmailDeliveryError, send_personalized_report_email
from .reporting import (
    ReportGenerationError,
    build_default_preference_profile,
    generate_personalized_report,
    save_preferences_from_memory,
)


def _tool_config() -> dict[str, Any]:
    """读取当前智能体中本插件的配置，不接收客户端传入的密钥或路径。"""
    config = get_tool_config("collect_xhs_hotspots")
    return config if isinstance(config, dict) else {}


def _workspace_dir() -> Path:
    """固定使用当前智能体工作区，保证采集数据随智能体隔离。"""
    workspace_dir = get_current_workspace_dir()
    if workspace_dir is None:
        raise RuntimeError("未找到当前智能体工作区，不能保存热点数据")
    return workspace_dir.resolve()


def _email_tool_config() -> dict[str, Any]:
    """读取邮件工具配置；其中只允许保存凭据引用，不能保存密码。"""
    config = get_tool_config("send_xhs_personalized_report_email")
    return config if isinstance(config, dict) else {}


def _build_provider_client(
    config: dict[str, Any],
    *,
    allow_paid_source: bool = False,
) -> AidataHotspotClient | UapiHotspotClient:
    """按配置选择免费主来源或已明确确认的付费备用来源。"""
    source = str(config.get("source", "uapi_free")).strip()
    timeout_seconds = float(config.get("timeout_seconds", 20))
    if source == "uapi_free":
        return UapiHotspotClient(
            base_url=str(
                config.get("uapi_api_url", UapiHotspotClient.DEFAULT_BASE_URL)
            ),
            timeout_seconds=timeout_seconds,
        )
    if source == "aidata_paid":
        # 定时任务不能仅因配置被修改就产生费用。
        # 调用方必须在本次工具调用中明确传入 True，才能启用付费来源。
        if allow_paid_source is not True:
            raise HotspotProviderError(
                "AIDATA 是付费备用来源；请在人工确认后显式传入 "
                "allow_paid_source=true。"
            )
        return AidataHotspotClient(
            api_key=str(config.get("aidata_api_key", config.get("api_key", ""))),
            base_url=str(
                config.get(
                    "aidata_api_url",
                    "https://aidata.vip/api/v1/data/xiaohongshu/trending",
                )
            ),
            timeout_seconds=timeout_seconds,
        )
    if source == "60s_free":
        raise HotspotProviderError(
            "60s 免费来源当前服务端持续返回 500，已停止使用；请改为 uapi_free。"
        )
    raise HotspotProviderError("未知数据源；仅支持 uapi_free 或 aidata_paid")


async def collect_xhs_hotspots(
    limit: int = 10,
    force_refresh: bool = True,
    allow_paid_source: bool = False,
) -> dict[str, Any]:
    """采集、校验并保存小红书热搜前 N 条快照。

    返回值只提供安全的结果摘要和文件位置，绝不会回显 API Key。
    """
    try:
        config = _tool_config()
        client = _build_provider_client(
            config,
            allow_paid_source=allow_paid_source,
        )
        response = await client.fetch(force_refresh=force_refresh)
        snapshot = normalize_snapshot(
            response,
            limit=limit,
            force_refresh=force_refresh,
        )
        stored = save_snapshot(
            workspace_dir=_workspace_dir(),
            snapshot=snapshot,
            provider_response=response,
        )
    except (
        HotspotProviderError,
        SnapshotValidationError,
        OSError,
        RuntimeError,
        ValueError,
    ) as exc:
        return {"ok": False, "error": str(exc)}

    return {
        "ok": True,
        "platform": snapshot.platform,
        "capture_id": snapshot.capture_id,
        "captured_at": snapshot.captured_at,
        "provider_name": snapshot.provider_name,
        "source_url": snapshot.source_url,
        "source_updated_at": snapshot.source_updated_at,
        "provider_cached": snapshot.provider_cached,
        "top_items": [item.to_dict() for item in snapshot.items],
        "warnings": list(snapshot.warnings),
        "snapshot_fingerprint": snapshot.snapshot_fingerprint,
        "snapshot_path": stored.snapshot_path,
        "raw_path": stored.raw_path,
        "snapshot_sha256": stored.snapshot_sha256,
        "raw_sha256": stored.raw_sha256,
    }


async def list_xhs_hotspot_snapshots(limit: int = 7) -> dict[str, Any]:
    """列出当前智能体保存的近期热点快照，不读取其他智能体工作区。"""
    try:
        snapshots = list_recent_snapshots(workspace_dir=_workspace_dir(), limit=limit)
    except (OSError, RuntimeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "snapshots": snapshots}


async def save_xhs_report_preferences(
    memory_summary: str,
    topic_weights: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """保存用户确认过的兴趣摘要。

    ``memory_summary`` 应是用户与模型对话记忆的简短、经确认摘要。
    函数只落盘主题、权重和匹配关键词，不保存摘要原文或完整聊天记录。
    """
    try:
        profile, path = save_preferences_from_memory(
            workspace_dir=_workspace_dir(),
            memory_summary=memory_summary,
            topic_weights=topic_weights,
        )
    except (OSError, ReportGenerationError, RuntimeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "updated_at": profile["updated_at"],
        "topics": profile["topics"],
        "preference_path": path,
        "privacy_note": "仅保存主题、权重和关键词；未保存记忆摘要原文或完整聊天记录。",
    }


async def generate_xhs_personalized_report(lookback_days: int = 7) -> dict[str, Any]:
    """依据当前智能体中已确认的兴趣偏好和真实快照生成简报。"""
    try:
        report, _markdown, stored = generate_personalized_report(
            workspace_dir=_workspace_dir(),
            lookback_days=lookback_days,
        )
    except (OSError, ReportGenerationError, RuntimeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "report_id": report["report_id"],
        "generated_at": report["generated_at"],
        "available_days": report["available_days"],
        "requested_days": report["requested_days"],
        "coverage_complete": report["coverage_complete"],
        "priority_items": report["priority_items"],
        "warnings": report["warnings"],
        "report_path": stored.report_path,
        "markdown_path": stored.markdown_path,
        "report_file_path": stored.workbook_path,
        "report_file_name": Path(stored.workbook_path).name,
        "report_sha256": stored.report_sha256,
    }


async def create_xhs_briefing(
    preference_hint: str | None = None,
    lookback_days: int = 7,
) -> dict[str, Any]:
    """为普通用户生成简报，隐藏工具名、权重和内部分类等实现细节。

    有已保存偏好时直接复用；用户在本次对话中明确表达关注方向时，才把简短
    偏好保存下来；首次没有偏好时生成不落盘的综合版简报，避免要求用户理解权重。
    """
    try:
        workspace_dir = _workspace_dir()
        hint = str(preference_hint or "").strip()
        preference_path: str | None = None
        if hint:
            profile, preference_path = save_preferences_from_memory(
                workspace_dir=workspace_dir,
                memory_summary=hint,
            )
            preference_status = "updated_from_confirmed_request"
            preference_note = "已根据你刚刚明确表达的关注方向更新后续简报。"
        else:
            profile = load_preference_profile(workspace_dir=workspace_dir)
            if profile is None:
                profile = build_default_preference_profile()
                preference_status = "general_without_saved_preference"
                preference_note = (
                    "这是综合版简报；下次只需自然地说“我更关注旅行和美食”，"
                    "系统会据此调整后续内容。"
                )
            else:
                preference_status = "used_saved_preference"
                preference_note = "已按你此前确认的关注方向生成简报。"

        report, markdown, stored = generate_personalized_report(
            workspace_dir=workspace_dir,
            lookback_days=lookback_days,
            preference_profile=profile,
        )
    except (OSError, ReportGenerationError, RuntimeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}

    priority_items = [
        {
            "source_rank": item["source_rank"],
            "title": item["title"],
            "hot_text": item["hot_text"],
            "trend_label": item["trend_label"],
            "source_url": item["source_url"],
        }
        for item in report["priority_items"]
    ]
    return {
        "ok": True,
        "report_id": report["report_id"],
        "briefing_mode": (
            "综合版" if preference_status == "general_without_saved_preference" else "个性化"
        ),
        "preference_status": preference_status,
        "preference_note": preference_note,
        "focus_topics": [topic["topic"] for topic in profile["topics"]],
        "requested_days": report["requested_days"],
        "available_days": report["available_days"],
        "coverage_complete": report["coverage_complete"],
        "priority_items": priority_items,
        "sources": report["sources"],
        "warnings": report["warnings"],
        "briefing_markdown": markdown,
        "report_path": stored.report_path,
        "markdown_path": stored.markdown_path,
        "report_file_path": stored.workbook_path,
        "report_file_name": Path(stored.workbook_path).name,
        "preference_path": preference_path,
    }


async def list_xhs_personalized_reports(limit: int = 10) -> dict[str, Any]:
    """列出当前智能体生成过的简报摘要，供人工确认后再发送邮件。"""
    try:
        reports = list_recent_reports(workspace_dir=_workspace_dir(), limit=limit)
    except (OSError, RuntimeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "reports": reports}


async def send_xhs_personalized_report_email(
    report_id: str,
    recipient_email: str,
) -> dict[str, Any]:
    """人工确认报告和收件人后，通过加密 SMTP 发送正文及 Excel 简报附件。"""
    try:
        config = _email_tool_config()
        return await send_personalized_report_email(
            workspace_dir=_workspace_dir(),
            report_id=report_id,
            recipient_email=recipient_email,
            credential_ref=str(config.get("smtp_credential_ref", "")),
        )
    except (OSError, EmailDeliveryError, RuntimeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
