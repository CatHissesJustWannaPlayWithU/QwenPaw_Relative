"""生成小红书热点采集的 QwenPaw Cron 任务规格。"""

from __future__ import annotations

from typing import Any


DEFAULT_DAILY_CRON = "0 9 * * *"
DEFAULT_TIMEZONE = "Asia/Shanghai"

# 每日热点采集是固定程序任务，直接调用受限工具，不经大模型决定或转述。
# 提示词保留给人工从控制台以“智能体任务”方式临时复核时使用。
DAILY_COLLECTION_PROMPT = """你正在执行无人值守的小红书热点日采集任务。

只调用一次 collect_xhs_hotspots 工具，参数必须是：
- limit=10
- force_refresh=true
- allow_paid_source=false

只允许使用已配置的免费 UAPI 来源。不得调用 AIDATA 付费来源，不得使用浏览器搜索、网页爬取或其他工具，也不得编造热点数据。

工具返回 ok=false 时，如实报告失败原因和“本次未保存数据”。工具返回 ok=true 时，只简要报告 capture_id、captured_at、source_updated_at、snapshot_path、raw_path、前 3 条标题和 warnings。"""


def build_daily_collection_job_payload(
    *,
    user_id: str,
    session_id: str,
    cron: str = DEFAULT_DAILY_CRON,
    timezone: str = DEFAULT_TIMEZONE,
    enabled: bool = False,
) -> dict[str, Any]:
    """生成可提交给 ``POST /api/cron/jobs`` 的每日采集任务。

    默认 ``enabled=False``，因为必须先人工触发一次并检查真实快照，
    再把任务切换为每日自动运行。
    """
    normalized_user_id = user_id.strip()
    normalized_session_id = session_id.strip()
    normalized_cron = cron.strip()
    normalized_timezone = timezone.strip()
    if not normalized_user_id:
        raise ValueError("user_id 不能为空")
    if not normalized_session_id:
        raise ValueError("session_id 不能为空")
    if not normalized_cron:
        raise ValueError("cron 表达式不能为空")
    if not normalized_timezone:
        raise ValueError("timezone 不能为空")

    return {
        "name": "小红书热点每日采集（免费来源）",
        "enabled": enabled,
        "schedule": {
            "type": "cron",
            "cron": normalized_cron,
            "timezone": normalized_timezone,
        },
        "task_type": "tool",
        "tool": {
            "name": "collect_xhs_hotspots",
            "arguments": {
                "limit": 10,
                "force_refresh": True,
                "allow_paid_source": False,
            },
        },
        "dispatch": {
            "type": "channel",
            "channel": "console",
            "target": {
                "user_id": normalized_user_id,
                "session_id": normalized_session_id,
            },
            "mode": "final",
            "silent": True,
            "meta": {"purpose": "xhs_hotspot_daily_collection"},
        },
        "save_result_to_inbox": True,
        "runtime": {
            "max_concurrency": 1,
            "timeout_seconds": 120,
            "misfire_grace_seconds": 600,
            # 工具任务本身不会创建会话；保留该值以统一 Cron 任务规格。
            "share_session": False,
            # CronExecutor 仍会检查 cron_safe 标记与当前 Agent 工具启用状态。
            "tool_safety": True,
        },
        "meta": {
            "source": "xhs-hotspot-tool",
            "provider_policy": "uapi_free_only",
        },
    }
