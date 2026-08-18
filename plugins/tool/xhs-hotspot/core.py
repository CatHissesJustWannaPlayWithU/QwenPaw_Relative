"""热点原始数据的提取、规范化和质量校验。"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from .models import HotspotItem, HotspotSnapshot, ProviderResponse


class SnapshotValidationError(ValueError):
    """原始热点数据没有达到保存要求时抛出。"""


def _find_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """兼容常见的 data/items、data/hot_list 等返回包装方式。"""
    current: Any = payload
    for _ in range(4):
        if isinstance(current, list):
            if all(isinstance(item, dict) for item in current):
                return current
            break
        if not isinstance(current, dict):
            break
        for key in ("items", "hot_list", "list", "data"):
            candidate = current.get(key)
            if isinstance(candidate, list):
                if all(isinstance(item, dict) for item in candidate):
                    return candidate
            if isinstance(candidate, dict):
                current = candidate
                break
        else:
            break
    raise SnapshotValidationError("未能从 API 响应中识别热点列表字段")


def _as_non_empty_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip().lower()
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if match is None:
        return None
    multiplier = 1
    if "亿" in text:
        multiplier = 100_000_000
    elif "万" in text or "w" in text:
        multiplier = 10_000
    return int(round(float(match.group()) * multiplier))


def _pick(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in record and record[key] is not None:
            return record[key]
    return None


def _pick_nested(record: dict[str, Any], parent_key: str, *keys: str) -> Any:
    """读取嵌套对象中的第一个非空字段，兼容不同数据源的扩展字段。"""
    nested = record.get(parent_key)
    if not isinstance(nested, dict):
        return None
    return _pick(nested, *keys)


def _source_updated_at(payload: dict[str, Any], records: list[dict[str, Any]]) -> str | None:
    value = _pick(payload, "updated_at", "update_time", "timestamp")
    if value is None and records:
        value = _pick(records[0], "updated_at", "update_time", "timestamp")
    return _as_non_empty_text(value)


def _snapshot_fingerprint(items: Iterable[HotspotItem]) -> str:
    canonical = "\n".join(f"{item.rank}:{item.title}" for item in items)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalize_snapshot(
    response: ProviderResponse,
    *,
    limit: int = 10,
    force_refresh: bool = True,
) -> HotspotSnapshot:
    """将来源响应转化为严格的前 N 条热点快照。

    只有排名连续、标题非空且标题不重复的前 N 条数据才允许进入存储层。
    """
    if not 1 <= limit <= 50:
        raise SnapshotValidationError("热点条数必须在 1 到 50 之间")

    records = _find_items(response.payload)
    normalized: list[HotspotItem] = []
    for index, record in enumerate(records):
        title = _as_non_empty_text(_pick(record, "title", "name", "keyword", "word"))
        if title is None:
            continue
        rank = _as_int(_pick(record, "rank", "index", "position", "order"))
        if rank is None:
            rank = index + 1
        normalized.append(
            HotspotItem(
                rank=rank,
                title=title,
                keyword=_as_non_empty_text(_pick(record, "keyword", "word")),
                hot_value=_as_int(_pick(record, "hot_value", "hot", "score", "heat")),
                hot_text=_as_non_empty_text(
                    _pick(record, "hot_text", "hot_value_text", "hot_value", "score")
                ),
                trend_label=_as_non_empty_text(
                    _pick(record, "word_type", "label")
                    or _pick_nested(record, "extra", "type", "label")
                ),
                description=_as_non_empty_text(_pick(record, "desc", "description", "content")),
                source_url=_as_non_empty_text(_pick(record, "url", "link")),
                note_id=_as_non_empty_text(_pick(record, "note_id", "id")),
                updated_at=_as_non_empty_text(_pick(record, "updated_at", "update_time")),
            )
        )

    normalized.sort(key=lambda item: item.rank)
    top_items = normalized[:limit]
    if len(top_items) < limit:
        raise SnapshotValidationError(
            f"接口仅返回 {len(top_items)} 条有效热点，少于要求保存的前 {limit} 条"
        )

    ranks = [item.rank for item in top_items]
    expected_ranks = list(range(1, limit + 1))
    if ranks != expected_ranks:
        raise SnapshotValidationError(
            f"热点排名不连续，期望 {expected_ranks}，实际为 {ranks}"
        )

    titles = [item.title for item in top_items]
    if len(set(titles)) != len(titles):
        raise SnapshotValidationError("热点标题重复，疑似数据源异常，本次数据未保存")

    warnings: list[str] = []
    source_updated_at = _source_updated_at(response.payload, records)
    if source_updated_at is None:
        warnings.append("数据源未提供更新时间，只能以本次采集时间作为快照时间。")
    provider_cached = response.payload.get("cached")
    if provider_cached is True and force_refresh:
        warnings.append("本次请求了强制刷新，但数据源仍标记为缓存结果。")
    if any(item.source_url is None for item in top_items):
        warnings.append("部分热点没有可直接打开的来源链接。")
    if all(item.hot_value is None for item in top_items):
        warnings.append("数据源未提供热度数值，只能按榜单排名分析。")
    if response.provider_name == "uapi_free":
        warnings.append(
            "本快照来自 UAPI 第三方公开接口；请保留原始响应，并在周报中注明第三方来源。"
        )

    captured_at = response.fetched_at or datetime.now(timezone.utc).isoformat()
    return HotspotSnapshot(
        platform="xiaohongshu",
        capture_id=f"xhs-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}",
        captured_at=captured_at,
        provider_name=response.provider_name,
        source_url=response.source_url,
        source_updated_at=source_updated_at,
        requested_force_refresh=force_refresh,
        provider_cached=provider_cached if isinstance(provider_cached, bool) else None,
        items=tuple(top_items),
        warnings=tuple(warnings),
        snapshot_fingerprint=_snapshot_fingerprint(top_items),
    )


def serialise_raw_response(response: ProviderResponse) -> dict[str, Any]:
    """生成可追溯的原始响应存档，同时排除任何请求鉴权信息。

    ``provider_name`` 与来源 URL 一起写入原始档案，方便脱离快照文件时
    仍能直接判断数据来自哪个第三方服务。
    """
    return {
        "provider_name": response.provider_name,
        "source_url": response.source_url,
        "fetched_at": response.fetched_at,
        "status_code": response.status_code,
        "response_headers": response.response_headers,
        "payload": response.payload,
    }


def canonical_json_bytes(data: dict[str, Any]) -> bytes:
    """用稳定 JSON 表示生成哈希，保证相同内容得到相同摘要。"""
    return json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
