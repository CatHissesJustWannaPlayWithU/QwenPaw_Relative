"""小红书热点采集插件入口。"""

from __future__ import annotations

from qwenpaw.plugins.api import PluginApi

from .schedule import build_daily_collection_job_payload
from .tools import (
    collect_xhs_hotspots,
    create_xhs_briefing,
    generate_xhs_personalized_report,
    list_xhs_hotspot_snapshots,
    list_xhs_personalized_reports,
    save_xhs_report_preferences,
    send_xhs_personalized_report_email,
)


def _register_governance_tools() -> None:
    """登记本插件工具的治理身份，避免被未知工具策略拦截。"""
    from qwenpaw.governance.tool_registry import DEFAULT_REGISTRY

    # 采集与邮件会产生外部网络动作；其余工具只读写当前智能体工作区，
    # 不接受任意路径或任意命令，因此登记为内部工具。
    tool_metadata = {
        "collect_xhs_hotspots": ("CollectXhsHotspots", "network"),
        "list_xhs_hotspot_snapshots": (
            "ListXhsHotspotSnapshots",
            "internal",
        ),
        "build_xhs_daily_collection_job": (
            "BuildXhsDailyCollectionJob",
            "internal",
        ),
        "save_xhs_report_preferences": (
            "SaveXhsReportPreferences",
            "internal",
        ),
        "generate_xhs_personalized_report": (
            "GenerateXhsPersonalizedReport",
            "internal",
        ),
        "create_xhs_briefing": ("CreateXhsBriefing", "internal"),
        "list_xhs_personalized_reports": (
            "ListXhsPersonalizedReports",
            "internal",
        ),
        "send_xhs_personalized_report_email": (
            "SendXhsPersonalizedReportEmail",
            "network",
        ),
    }
    for python_name, (policy_name, tool_type) in tool_metadata.items():
        DEFAULT_REGISTRY.register(policy_name, tool_type, "")
        DEFAULT_REGISTRY.register_python_name(python_name, policy_name)


class XiaohongshuHotspotPlugin:
    """把热点采集能力注册给 QwenPaw。"""

    def register(self, api: PluginApi) -> None:
        """注册采集、偏好报告和人工邮件发送工具。"""
        _register_governance_tools()
        api.register_tool(
            tool_name="collect_xhs_hotspots",
            tool_func=collect_xhs_hotspots,
            description="免费优先采集、校验并保存小红书热搜前十条，保留原始响应和可追溯快照。",
            icon="📈",
            cron_safe=True,
            cron_fixed_arguments={
                "limit": 10,
                "force_refresh": True,
                "allow_paid_source": False,
            },
        )
        api.register_tool(
            tool_name="list_xhs_hotspot_snapshots",
            tool_func=list_xhs_hotspot_snapshots,
            description="列出当前智能体已保存的小红书热点快照，用于检查定时采集是否连续。",
            icon="🗂️",
        )
        api.register_tool(
            tool_name="build_xhs_daily_collection_job",
            tool_func=build_daily_collection_job_payload,
            description="生成默认关闭的每日采集 Cron 任务 JSON；需人工验证一次后再启用。",
            icon="⏰",
        )
        api.register_tool(
            tool_name="save_xhs_report_preferences",
            tool_func=save_xhs_report_preferences,
            description="从用户确认过的对话记忆摘要中保存兴趣主题和权重，不保存聊天原文。",
            icon="🧭",
        )
        api.register_tool(
            tool_name="create_xhs_briefing",
            tool_func=create_xhs_briefing,
            description="根据用户自然语言请求和已确认偏好生成可追溯简报；首次没有偏好时生成综合版。",
            icon="📰",
        )
        api.register_tool(
            tool_name="generate_xhs_personalized_report",
            tool_func=generate_xhs_personalized_report,
            description="依据已保存偏好和真实热点快照生成可追溯的个性化简报。",
            icon="📝",
        )
        api.register_tool(
            tool_name="list_xhs_personalized_reports",
            tool_func=list_xhs_personalized_reports,
            description="列出已生成的个性化简报，供人工确认内容和覆盖天数。",
            icon="📚",
        )
        api.register_tool(
            tool_name="send_xhs_personalized_report_email",
            tool_func=send_xhs_personalized_report_email,
            description="人工确认报告与收件人后，使用加密 SMTP 凭据发送纯文本热点简报。",
            icon="✉️",
        )


# 插件加载器要求入口模块导出名为 plugin 的实例。
plugin = XiaohongshuHotspotPlugin()
