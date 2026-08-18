"""小红书热点插件内部使用的数据模型。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ProviderResponse:
    """第三方数据源原始响应及与追溯有关的元数据。"""

    payload: dict[str, Any]
    provider_name: str
    source_url: str
    fetched_at: str
    status_code: int
    response_headers: dict[str, str]


@dataclass(frozen=True)
class HotspotItem:
    """经过校验后可用于展示、存储和导出的一条热搜记录。"""

    rank: int
    title: str
    keyword: str | None
    hot_value: int | None
    hot_text: str | None
    trend_label: str | None
    description: str | None
    source_url: str | None
    note_id: str | None
    updated_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HotspotSnapshot:
    """一次采集后形成的规范化热点快照。"""

    platform: str
    capture_id: str
    captured_at: str
    provider_name: str
    source_url: str
    source_updated_at: str | None
    requested_force_refresh: bool
    provider_cached: bool | None
    items: tuple[HotspotItem, ...]
    warnings: tuple[str, ...]
    snapshot_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["items"] = [item.to_dict() for item in self.items]
        data["warnings"] = list(self.warnings)
        return data


@dataclass(frozen=True)
class StoredSnapshot:
    """已经写入本地工作区的快照文件信息。"""

    snapshot_path: str
    raw_path: str
    snapshot_sha256: str
    raw_sha256: str


@dataclass(frozen=True)
class StoredPersonalizedReport:
    """已经写入当前智能体工作区的个性化报告文件信息。"""

    report_id: str
    report_path: str
    markdown_path: str
    workbook_path: str
    report_sha256: str
    markdown_sha256: str
    workbook_sha256: str
