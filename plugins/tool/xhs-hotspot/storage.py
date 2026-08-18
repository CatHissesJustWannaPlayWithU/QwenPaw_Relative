"""热点快照的本地、原子化存储实现。"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import re

from .core import canonical_json_bytes, serialise_raw_response
from .models import (
    HotspotSnapshot,
    ProviderResponse,
    StoredPersonalizedReport,
    StoredSnapshot,
)


_REPORT_ID_PATTERN = re.compile(r"^xhs-report-[0-9TZ-]+-[a-f0-9]{8}$")


def _atomic_write(path: Path, content: bytes) -> None:
    """先写临时文件再替换目标文件，避免中断留下半份 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _digest(content: bytes) -> str:
    """计算文件内容的 SHA-256 摘要，用于报告和邮件回执的完整性追溯。"""
    return hashlib.sha256(content).hexdigest()


def save_snapshot(
    *,
    workspace_dir: Path,
    snapshot: HotspotSnapshot,
    provider_response: ProviderResponse,
) -> StoredSnapshot:
    """把原始响应和规范化快照保存到当前智能体工作区中。"""
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    target_dir = workspace_dir / "hotspot_data" / "xiaohongshu" / day
    raw_content = canonical_json_bytes(serialise_raw_response(provider_response))
    snapshot_content = canonical_json_bytes(snapshot.to_dict())
    raw_path = target_dir / f"{snapshot.capture_id}.raw.json"
    snapshot_path = target_dir / f"{snapshot.capture_id}.snapshot.json"
    _atomic_write(raw_path, raw_content)
    _atomic_write(snapshot_path, snapshot_content)
    return StoredSnapshot(
        snapshot_path=str(snapshot_path),
        raw_path=str(raw_path),
        snapshot_sha256=_digest(snapshot_content),
        raw_sha256=_digest(raw_content),
    )


def list_recent_snapshots(*, workspace_dir: Path, limit: int = 7) -> list[dict[str, Any]]:
    """读取当前智能体最近保存的规范化快照摘要。"""
    if not 1 <= limit <= 100:
        raise ValueError("快照条数必须在 1 到 100 之间")
    root = workspace_dir / "hotspot_data" / "xiaohongshu"
    if not root.exists():
        return []
    paths = sorted(root.rglob("*.snapshot.json"), reverse=True)[:limit]
    summaries: list[dict[str, Any]] = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        items = data.get("items") if isinstance(data, dict) else []
        summaries.append(
            {
                "capture_id": data.get("capture_id"),
                "captured_at": data.get("captured_at"),
                "source_updated_at": data.get("source_updated_at"),
                "top_titles": [item.get("title") for item in items[:3] if isinstance(item, dict)],
                "item_count": len(items) if isinstance(items, list) else 0,
                "snapshot_path": str(path),
            }
        )
    return summaries


def _hotspot_root(workspace_dir: Path) -> Path:
    """返回当前智能体专属的热点数据根目录。"""
    return workspace_dir / "hotspot_data" / "xiaohongshu"


def preference_profile_path(*, workspace_dir: Path) -> Path:
    """偏好摘要只保存在当前智能体工作区，不与其他用户共享。"""
    return _hotspot_root(workspace_dir) / "report_preferences.json"


def save_preference_profile(*, workspace_dir: Path, profile: dict[str, Any]) -> str:
    """原子保存已提炼的兴趣偏好，不保存原始对话或完整记忆文本。"""
    path = preference_profile_path(workspace_dir=workspace_dir)
    _atomic_write(path, canonical_json_bytes(profile))
    return str(path)


def load_preference_profile(*, workspace_dir: Path) -> dict[str, Any] | None:
    """读取当前智能体的偏好摘要；不存在时返回 None。"""
    path = preference_profile_path(workspace_dir=workspace_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"偏好摘要文件无法读取：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("偏好摘要文件格式错误")
    return data


def _report_root(workspace_dir: Path) -> Path:
    """返回当天个性化报告和邮件回执的专属存储目录。"""
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _hotspot_root(workspace_dir) / "reports" / day


def save_personalized_report(
    *,
    workspace_dir: Path,
    report: dict[str, Any],
    markdown: str,
    workbook: bytes,
) -> StoredPersonalizedReport:
    """保存结构化、Markdown 与 Excel 报告，供查看、下载和邮件发送复用。"""
    report_id = str(report.get("report_id", ""))
    if not _REPORT_ID_PATTERN.fullmatch(report_id):
        raise ValueError("报告 ID 格式不合法")
    target_dir = _report_root(workspace_dir)
    report_content = canonical_json_bytes(report)
    markdown_content = markdown.encode("utf-8")
    report_path = target_dir / f"{report_id}.report.json"
    markdown_path = target_dir / f"{report_id}.report.md"
    workbook_path = target_dir / f"{report_id}.xlsx"
    _atomic_write(report_path, report_content)
    _atomic_write(markdown_path, markdown_content)
    _atomic_write(workbook_path, workbook)
    return StoredPersonalizedReport(
        report_id=report_id,
        report_path=str(report_path),
        markdown_path=str(markdown_path),
        workbook_path=str(workbook_path),
        report_sha256=_digest(report_content),
        markdown_sha256=_digest(markdown_content),
        workbook_sha256=_digest(workbook),
    )


def load_personalized_report(
    *,
    workspace_dir: Path,
    report_id: str,
) -> tuple[dict[str, Any], str, Path]:
    """按报告 ID 读取同一工作区内的报告，拒绝路径穿越输入。"""
    normalized_id = report_id.strip()
    if not _REPORT_ID_PATTERN.fullmatch(normalized_id):
        raise ValueError("报告 ID 格式不合法")
    root = _hotspot_root(workspace_dir) / "reports"
    matches = sorted(root.rglob(f"{normalized_id}.report.json"), reverse=True)
    if not matches:
        raise ValueError("未找到指定的个性化报告")
    report_path = matches[0]
    markdown_path = report_path.with_suffix("").with_suffix(".report.md")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        markdown = markdown_path.read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"报告文件无法读取：{exc}") from exc
    if not isinstance(report, dict):
        raise ValueError("报告文件格式错误")
    return report, markdown, report_path


def list_recent_reports(*, workspace_dir: Path, limit: int = 10) -> list[dict[str, Any]]:
    """列出当前工作区已生成的报告摘要，不返回邮件收件人信息。"""
    if not 1 <= limit <= 100:
        raise ValueError("报告条数必须在 1 到 100 之间")
    root = _hotspot_root(workspace_dir) / "reports"
    if not root.exists():
        return []
    reports: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.report.json"), reverse=True)[:limit]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        reports.append(
            {
                "report_id": data.get("report_id"),
                "generated_at": data.get("generated_at"),
                "available_days": data.get("available_days"),
                "coverage_complete": data.get("coverage_complete"),
                "report_path": str(path),
            }
        )
    return reports


def save_email_receipt(
    *,
    workspace_dir: Path,
    report_id: str,
    receipt: dict[str, Any],
) -> str:
    """保存不含明文收件人和密码的发送审计回执。"""
    normalized_id = report_id.strip()
    if not _REPORT_ID_PATTERN.fullmatch(normalized_id):
        raise ValueError("报告 ID 格式不合法")
    target_dir = _report_root(workspace_dir)
    path = target_dir / f"{normalized_id}.email-receipt.json"
    _atomic_write(path, canonical_json_bytes(receipt))
    return str(path)
