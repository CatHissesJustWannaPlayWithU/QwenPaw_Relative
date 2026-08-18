"""把已保存的热点快照按用户确认过的兴趣偏好生成可追溯简报。"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .storage import (
    load_preference_profile,
    save_personalized_report,
    save_preference_profile,
)
from .xlsx_export import render_personalized_report_xlsx


class ReportGenerationError(ValueError):
    """偏好内容或已保存快照不能用于生成报告时抛出。"""


# 这是第一版可解释的本地分类词表；报告不会把关键词匹配伪装成大模型判断。
_TOPIC_CATALOG: dict[str, tuple[str, ...]] = {
    "AI行业": (
        "ai",
        "人工智能",
        "大模型",
        "智能体",
        "chatgpt",
        "deepseek",
        "机器人",
        "芯片",
        "算法",
        "科技",
    ),
    "娱乐八卦": (
        "明星",
        "综艺",
        "电视剧",
        "电影",
        "偶像",
        "八卦",
        "演唱会",
        "恋综",
    ),
    "商业财经": ("创业", "融资", "商业", "经济", "股市", "公司", "品牌", "消费"),
    "旅行生活": ("旅行", "旅游", "出片", "美食", "穿搭", "家居", "城市", "拍照"),
    "健康运动": ("健康", "运动", "健身", "跑步", "减肥", "养生", "医疗"),
}
_PROFILE_VERSION = 1
_MAX_MEMORY_SUMMARY_LENGTH = 2_000
_MAX_LOOKBACK_DAYS = 30


def _now() -> str:
    """生成 UTC 时区的 ISO 8601 时间字符串，作为偏好和报告的生成时间。"""
    return datetime.now(timezone.utc).isoformat()


def _normalized_text(value: str) -> str:
    """统一文本中的空白和大小写，保证兴趣关键词匹配时不受输入格式影响。"""
    return re.sub(r"\s+", " ", value.strip()).lower()


def _clamp_weight(value: Any) -> int:
    """把用户传入的主题权重校验为 1 到 5 的整数，拒绝越界或无法转换的值。"""
    try:
        weight = int(value)
    except (TypeError, ValueError) as exc:
        raise ReportGenerationError("偏好权重必须是 1 到 5 的整数") from exc
    if not 1 <= weight <= 5:
        raise ReportGenerationError("偏好权重必须在 1 到 5 之间")
    return weight


def _derived_weight(summary: str, topic: str, default: int) -> int:
    """从用户确认过的自然语言中识别简单的“优先/较少”表达。"""
    aliases = "|".join(re.escape(keyword) for keyword in _TOPIC_CATALOG[topic])
    # 兴趣表达常用逗号分开，例如“更关注旅行和美食，娱乐八卦不太关心”。
    # 只在含有当前主题关键词的分段内判断，避免相邻主题互相影响。
    topic_clauses = [
        clause
        for clause in re.split(r"[，,。；;！!？?\n]", summary)
        if re.search(aliases, clause)
    ]
    for clause in topic_clauses:
        if re.search(
            rf"(?:(?:不太|较少|少看|不关注).{{0,8}}(?:{aliases})|"
            rf"(?:{aliases}).{{0,8}}(?:不太|较少|少看|不关注))",
            clause,
        ):
            return 1
    for clause in topic_clauses:
        if re.search(
            rf"(?:(?:{aliases}).{{0,18}}(?:优先|更多|较多|关注|主要)|"
            rf"(?:优先|更多|较多|关注|主要).{{0,18}}(?:{aliases}))",
            clause,
        ):
            return max(default, 4)
    return default


def build_preference_profile(
    *,
    memory_summary: str,
    topic_weights: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """从经过用户确认的记忆摘要提炼兴趣标签，不保留摘要原文。"""
    normalized_summary = _normalized_text(memory_summary)
    if not normalized_summary:
        raise ReportGenerationError("请提供经过用户确认的偏好或记忆摘要")
    if len(normalized_summary) > _MAX_MEMORY_SUMMARY_LENGTH:
        raise ReportGenerationError("偏好或记忆摘要不能超过 2000 个字符")
    explicit_weights = topic_weights or {}
    if not isinstance(explicit_weights, dict):
        raise ReportGenerationError("topic_weights 必须是“主题: 权重”的对象")

    topics: list[dict[str, Any]] = []
    for topic, keywords in _TOPIC_CATALOG.items():
        mentioned = [keyword for keyword in keywords if keyword in normalized_summary]
        if topic in explicit_weights:
            weight = _clamp_weight(explicit_weights[topic])
        elif mentioned:
            weight = _derived_weight(normalized_summary, topic, 3)
        else:
            continue
        topics.append(
            {
                "topic": topic,
                "weight": weight,
                "keywords": list(keywords),
                "matched_memory_keywords": mentioned,
            }
        )

    unknown_topics = set(explicit_weights) - set(_TOPIC_CATALOG)
    if unknown_topics:
        raise ReportGenerationError(
            "不支持的偏好主题：" + "、".join(sorted(unknown_topics))
        )
    if not topics:
        raise ReportGenerationError(
            "没有识别到可用偏好主题；请使用 AI行业、娱乐八卦、商业财经、旅行生活或健康运动。"
        )
    topics.sort(key=lambda item: (-int(item["weight"]), str(item["topic"])))
    return {
        "schema_version": _PROFILE_VERSION,
        "updated_at": _now(),
        "source": "user_approved_memory_summary",
        "topics": topics,
    }


def save_preferences_from_memory(
    *,
    workspace_dir: Path,
    memory_summary: str,
    topic_weights: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    """保存仅含兴趣标签的偏好档案，原始记忆摘要在处理后即被丢弃。"""
    profile = build_preference_profile(
        memory_summary=memory_summary,
        topic_weights=topic_weights,
    )
    path = save_preference_profile(workspace_dir=workspace_dir, profile=profile)
    return profile, path


def build_default_preference_profile() -> dict[str, Any]:
    """生成不落盘的综合版偏好，供首次请求简报的用户直接使用。"""
    return {
        "schema_version": _PROFILE_VERSION,
        "updated_at": _now(),
        "source": "general_default",
        "topics": [
            {
                "topic": topic,
                "weight": 3,
                "keywords": list(keywords),
                "matched_memory_keywords": [],
            }
            for topic, keywords in _TOPIC_CATALOG.items()
        ],
    }


def _load_daily_snapshots(*, workspace_dir: Path, lookback_days: int) -> list[dict[str, Any]]:
    """从当前工作区读取近期真实快照，每个自然日只保留最新的一份。"""
    if not 1 <= lookback_days <= _MAX_LOOKBACK_DAYS:
        raise ReportGenerationError("报告回看天数必须在 1 到 30 天之间")
    root = workspace_dir / "hotspot_data" / "xiaohongshu"
    snapshots: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.snapshot.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or data.get("platform") != "xiaohongshu":
            continue
        captured_at = data.get("captured_at")
        items = data.get("items")
        if not isinstance(captured_at, str) or not isinstance(items, list):
            continue
        snapshots.append({"path": str(path), "data": data})

    selected: list[dict[str, Any]] = []
    seen_days: set[str] = set()
    for snapshot in snapshots:
        captured_at = str(snapshot["data"]["captured_at"])
        day = captured_at[:10]
        if day in seen_days:
            continue
        seen_days.add(day)
        selected.append(snapshot)
        if len(selected) >= lookback_days:
            break
    if not selected:
        raise ReportGenerationError("还没有可用于生成报告的热点快照")
    return list(reversed(selected))


def _score_item(item: dict[str, Any], topics: list[dict[str, Any]]) -> tuple[int, list[str]]:
    """按标题命中的偏好主题累计权重，并返回总分和命中的主题名称。"""
    title = _normalized_text(str(item.get("title", "")))
    score = 0
    matched_topics: list[str] = []
    for topic in topics:
        if any(keyword in title for keyword in topic["keywords"]):
            score += int(topic["weight"])
            matched_topics.append(str(topic["topic"]))
    return score, matched_topics


def _item_for_report(item: dict[str, Any], topics: list[dict[str, Any]]) -> dict[str, Any]:
    """把原始热点条目转换为报告条目，并附上可解释的个性化匹配结果。"""
    score, matched_topics = _score_item(item, topics)
    return {
        "source_rank": item.get("rank"),
        "title": item.get("title"),
        "hot_value": item.get("hot_value"),
        "hot_text": item.get("hot_text"),
        "trend_label": item.get("trend_label"),
        "source_url": item.get("source_url"),
        "matched_topics": matched_topics,
        "personalization_score": score,
    }


def _render_markdown(report: dict[str, Any]) -> str:
    """将结构化报告渲染为纯文本 Markdown，供人工检查和邮件正文复用。"""
    lines = [
        "# 小红书个性化热点简报",
        "",
        f"生成时间：{report['generated_at']}",
        f"数据覆盖：最近请求 {report['requested_days']} 天，已获得 {report['available_days']} 天有效快照。",
    ]
    if not report["coverage_complete"]:
        lines.append("说明：历史数据尚未积累完整，以下内容仅基于当前已经保存的真实快照。")
    lines.extend(["", "## 关注方向"])
    for topic in report["preference_profile"]["topics"]:
        lines.append(f"- {topic['topic']}")

    lines.extend(["", "## 优先呈现的热点"])
    priority_items = report["priority_items"]
    if priority_items:
        for index, item in enumerate(priority_items, start=1):
            topics = "、".join(item["matched_topics"])
            lines.append(
                f"{index}. {item['title']}（原榜单第 {item['source_rank']} 名；匹配：{topics}）"
            )
            if item.get("source_url"):
                lines.append(f"   来源：{item['source_url']}")
    else:
        lines.append("当前热点标题没有命中已确认的兴趣关键词，因此按原始热榜顺序展示全部热点。")

    lines.extend(["", "## 最新快照全部热点"])
    for item in report["latest_items"]:
        suffix = ""
        if item["matched_topics"]:
            suffix = "；匹配：" + "、".join(item["matched_topics"])
        lines.append(f"- 原榜单第 {item['source_rank']} 名：{item['title']}{suffix}")

    lines.extend(["", "## 数据来源与追溯"])
    for source in report["sources"]:
        lines.append(
            f"- {source['captured_at']}；来源：{source['source_url']}；"
            f"快照：{source['snapshot_path']}"
        )
    lines.append("")
    return "\n".join(lines)


def generate_personalized_report(
    *,
    workspace_dir: Path,
    lookback_days: int = 7,
    preference_profile: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str, Any]:
    """用已保存或临时的偏好档案和真实快照生成可复查的报告。"""
    profile = preference_profile or load_preference_profile(workspace_dir=workspace_dir)
    if profile is None:
        raise ReportGenerationError("尚未保存用户偏好；请先调用 save_xhs_report_preferences")
    topics = profile.get("topics")
    if not isinstance(topics, list) or not topics:
        raise ReportGenerationError("偏好档案缺少有效主题")
    snapshots = _load_daily_snapshots(
        workspace_dir=workspace_dir,
        lookback_days=lookback_days,
    )
    latest = snapshots[-1]["data"]
    latest_items = [
        _item_for_report(item, topics)
        for item in latest.get("items", [])
        if isinstance(item, dict)
    ]
    latest_items.sort(key=lambda item: int(item.get("source_rank") or 10_000))
    priority_items = [item for item in latest_items if item["personalization_score"] > 0]
    priority_items.sort(
        key=lambda item: (
            -int(item["personalization_score"]),
            int(item.get("source_rank") or 10_000),
        )
    )
    sources = [
        {
            "capture_id": snapshot["data"].get("capture_id"),
            "captured_at": snapshot["data"].get("captured_at"),
            "source_updated_at": snapshot["data"].get("source_updated_at"),
            "source_url": snapshot["data"].get("source_url"),
            "snapshot_path": snapshot["path"],
        }
        for snapshot in snapshots
    ]
    report = {
        "schema_version": 1,
        "report_id": f"xhs-report-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}",
        "generated_at": _now(),
        "requested_days": lookback_days,
        "available_days": len(snapshots),
        "coverage_complete": len(snapshots) >= lookback_days,
        "preference_profile": profile,
        "priority_items": priority_items,
        "latest_items": latest_items,
        "sources": sources,
        "warnings": [],
    }
    if not report["coverage_complete"]:
        report["warnings"].append("历史热点快照不足，尚不能代表完整的七日趋势。")
    if not priority_items:
        report["warnings"].append("当前热点未命中已确认的兴趣关键词，已保留原始榜单顺序。")
    markdown = _render_markdown(report)
    workbook = render_personalized_report_xlsx(report)
    stored = save_personalized_report(
        workspace_dir=workspace_dir,
        report=report,
        markdown=markdown,
        workbook=workbook,
    )
    return report, markdown, stored
