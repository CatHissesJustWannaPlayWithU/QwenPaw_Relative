"""候选页面明确官网链接的低频第二来源补证。

本模块只处理已经保存、已验证哈希的候选页面快照：从 HTML 中抽取文本明确标为“官网”
或“官方网站”的跨域链接，遵守目标页 robots 并复用受控公网抓取器保存第二份原始快照。
它不会用模型搜索、不会提交公司名到任意搜索引擎、不会登录或绕过验证码。更重要的是，
“页面称为官网”的链接也不能自动证明网址归属、企业中国业务、徐州关联、融资事实或投资
意愿；本模块只产生待人工核验的补证材料，绝不创建正式证据或招商线索。
"""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

from .candidate_observation import (
    _candidate_observation_lists,
    _read_verified_observation_snapshot,
)
from .candidate_review import invalid_discovery_candidate_ids
from .source_capture import fetch_public_source_for_observation
from .source_feed_discovery import check_robots_access
from .storage import (
    candidate_corroboration_snapshot_path,
    ledger_path,
    load_ledger,
    save_candidate_corroboration_snapshot,
    save_ledger,
)


# 每个六小时监控周期至多再访问两条明确官网链接。该预算独立于原候选观察上限，防止网页
# 上大批外链或恶意页面把“补证”扩张为爬虫；用户、模型和来源目录都不能调大此数字。
MAX_OFFICIAL_LINK_CORROBORATIONS_PER_RUN = 2
MAX_CORROBORATION_SNAPSHOT_RECORDS = 500
MAX_CORROBORATION_SNAPSHOT_STORAGE_BYTES = 128 * 1024 * 1024
MAX_CORROBORATION_ERROR_LENGTH = 500
MAX_RETURN_VISIBLE_TEXT_CHARS = 12_000
CORROBORATION_OPERATION_SCOPE = "automatic_candidate_official_link_corroboration_pending_human_review_only"
OFFICIAL_LINK_SELECTOR_REASON = "parent_snapshot_explicit_official_website_anchor"
OFFICIAL_LINK_LABEL_TERMS = ("官网", "官方网站", "公司官网", "企业官网", "official website")


def _utc_now() -> str:
    """返回服务器生成的补证尝试时间，不接受网页、模型或浏览器给出的时间。"""
    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, payload: dict[str, object]) -> str:
    """按服务器候选和快照内容生成稳定审计 ID，禁止外部指定补证身份。"""
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


class _OfficialSiteLinkExtractor(HTMLParser):
    """仅从 HTML 锚文本中提取明确标注官网的链接，不执行脚本或猜测 URL 归属。"""

    _IGNORED_TAGS = {"script", "style", "noscript", "template", "svg"}

    def __init__(self) -> None:
        """初始化最小 HTML 解析状态；输入为空，输出为可调用的纯内存提取器。"""
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self._open_anchor_href: str | None = None
        self._open_anchor_text: list[str] = []
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """记录非忽略区内的锚链接；只读取 href 和可见标签，不发起网络请求。"""
        normalized_tag = tag.casefold()
        if normalized_tag in self._IGNORED_TAGS:
            self._ignored_depth += 1
            return
        if self._ignored_depth != 0 or normalized_tag != "a":
            return
        attrs_by_name = {name.casefold(): value for name, value in attrs}
        href = attrs_by_name.get("href")
        self._open_anchor_href = href.strip() if isinstance(href, str) else None
        self._open_anchor_text = []

    def handle_data(self, data: str) -> None:
        """累积锚的可见标签文字；文本不会被当作企业事实或写入台账。"""
        if self._ignored_depth == 0 and self._open_anchor_href is not None:
            self._open_anchor_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        """关闭锚时保存 href 与标签；离开忽略标签时恢复安全的解析边界。"""
        normalized_tag = tag.casefold()
        if normalized_tag in self._IGNORED_TAGS and self._ignored_depth > 0:
            self._ignored_depth -= 1
            return
        if self._ignored_depth != 0 or normalized_tag != "a" or self._open_anchor_href is None:
            return
        label = " ".join("".join(self._open_anchor_text).split())
        self.links.append((self._open_anchor_href, label))
        self._open_anchor_href = None
        self._open_anchor_text = []


def _corroboration_attempts(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    """读取第二来源补证审计列表，结构异常时拒绝猜测或修复历史记录。"""
    attempts = ledger.get("candidate_corroboration_attempts")
    if not isinstance(attempts, list):
        raise ValueError("招商线索台账中的 candidate_corroboration_attempts 必须是列表")
    return attempts


def _protected_business_snapshot(ledger: dict[str, Any]) -> dict[str, str]:
    """序列化补证流程绝不能改写的候选、证据、评分和人工业务记录。

    输入为已通过整本 HMAC 校验的台账，输出为受保护列表的稳定 JSON 副本。函数不会保存或
    访问网络；运行结束前若任一字段变化即失败，确保第二来源采集只能追加独立审计，不能
    修改候选 URL/摘要、原始快照、正式 evidence、业务状态、评分和结果反馈。
    """
    protected_fields = (
        "discovery_runs",
        "discovery_candidates",
        "candidate_observations",
        "candidate_scope_exclusions",
        "candidate_business_reviews",
        "leads",
        "evidence",
        "evidence_drafts",
        "score_snapshots",
        "outcome_feedback",
    )
    snapshot: dict[str, str] = {}
    for field_name in protected_fields:
        value = ledger.get(field_name)
        if not isinstance(value, list):
            raise ValueError(f"招商线索台账中的 {field_name} 必须是列表")
        snapshot[field_name] = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return snapshot


def _assert_protected_business_records_unchanged(
    before: dict[str, str], ledger: dict[str, Any]
) -> None:
    """在保存前确认补证任务只追加自身审计，不能越权写业务或证据字段。"""
    if _protected_business_snapshot(ledger) != before:
        raise RuntimeError("候选第二来源补证不得修改候选、快照、线索、证据、评分或人工判断")


def _base_domain(hostname: str) -> str:
    """返回保守的域名后缀比较键，仅用于阻止与原来源同站的伪第二来源。"""
    labels = [label for label in hostname.casefold().strip(".").split(".") if label]
    return ".".join(labels[-2:]) if len(labels) >= 2 else ".".join(labels)


def _candidate_official_links(parent_url: str, raw_content: bytes) -> list[str]:
    """从父快照 HTML 提取明确官网的独立公网 HTTP(S) 链接。

    输入为已验证父快照 URL 和原始 HTML 字节，输出为最多若干去重候选 URL。只接受锚文本
    明示“官网”等固定词且与父页面不同的基础域名；相对链接、JavaScript、凭据 URL、同站
    营销页和没有标签的外链均不会返回。公网 DNS、robots 和重定向校验仍由后续受控抓取器
    执行，因此本函数不联网、不确认企业归属，也不写台账。
    """
    if not isinstance(parent_url, str) or not isinstance(raw_content, bytes):
        raise ValueError("父候选快照格式异常")
    parent_hostname = (urlparse(parent_url).hostname or "").casefold().rstrip(".")
    if not parent_hostname:
        raise ValueError("父候选快照缺少公开来源域名")
    extractor = _OfficialSiteLinkExtractor()
    extractor.feed(raw_content.decode("utf-8", errors="replace"))
    parent_domain = _base_domain(parent_hostname)
    results: list[str] = []
    seen: set[str] = set()
    for href, anchor_text in extractor.links:
        if not any(term.casefold() in anchor_text.casefold() for term in OFFICIAL_LINK_LABEL_TERMS):
            continue
        resolved = urljoin(parent_url, href)
        parsed = urlparse(resolved)
        hostname = (parsed.hostname or "").casefold().rstrip(".")
        if (
            parsed.scheme not in {"http", "https"}
            or not hostname
            or parsed.username
            or parsed.password
            or _base_domain(hostname) == parent_domain
        ):
            continue
        normalized = urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", parsed.query, ""))
        if normalized not in seen:
            results.append(normalized)
            seen.add(normalized)
    return results


def _latest_observations_by_candidate(
    observations: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """按采集时间选取每个候选最新原始观察，保留旧快照而不回写或覆盖。"""
    latest: dict[str, dict[str, Any]] = {}
    for observation in observations:
        candidate_id = observation.get("candidate_id")
        if not isinstance(candidate_id, str):
            continue
        current = latest.get(candidate_id)
        if current is None or str(observation.get("collected_at", "")) > str(current.get("collected_at", "")):
            latest[candidate_id] = observation
    return latest


def _existing_storage_bytes(attempts: list[dict[str, Any]]) -> int:
    """汇总已保存补证快照的实际 JSON 大小，格式异常即停止，避免错估容量。"""
    total = 0
    for attempt in attempts:
        size = attempt.get("snapshot_storage_bytes")
        if size is None:
            continue
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError("候选第二来源补证快照大小格式异常")
        total += size
    return total


def _serialized_snapshot_size(payload: dict[str, object]) -> int:
    """计算即将写入的 JSON 快照字节数，只用于本地资源配额，不解释页面内容。"""
    return len(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")) + 1


def _attempt_record(
    *,
    candidate_id: str,
    parent_observation: dict[str, Any],
    attempted_at: str,
    outcome: str,
    source_url: str | None = None,
    source_title: str | None = None,
    raw_sha256: str | None = None,
    byte_size: int | None = None,
    snapshot_storage_bytes: int | None = None,
    snapshot_relative_path: str | None = None,
    error: str | None = None,
) -> dict[str, object]:
    """构造一条只追加补证审计，不向其中复制候选或正式证据可写字段。"""
    parent_observation_id = parent_observation.get("observation_id")
    parent_raw_sha256 = parent_observation.get("raw_sha256")
    if not isinstance(parent_observation_id, str) or not isinstance(parent_raw_sha256, str):
        raise ValueError("父候选观察缺少服务器标识或哈希")
    source_hostname = (urlparse(source_url).hostname or "").casefold() if source_url else None
    record: dict[str, object] = {
        "corroboration_id": _stable_id(
            "candidate-corroboration",
            {
                "candidate_id": candidate_id,
                "parent_observation_id": parent_observation_id,
                "attempted_at": attempted_at,
                "outcome": outcome,
                "source_url": source_url or "",
            },
        ),
        "candidate_id": candidate_id,
        "parent_observation_id": parent_observation_id,
        "parent_raw_sha256": parent_raw_sha256,
        "attempted_at": attempted_at,
        "outcome": outcome,
        "selector_reason": OFFICIAL_LINK_SELECTOR_REASON,
        "source_url": source_url,
        "source_hostname": source_hostname,
        "source_title": source_title,
        "raw_sha256": raw_sha256,
        "byte_size": byte_size,
        "snapshot_storage_bytes": snapshot_storage_bytes,
        "snapshot_relative_path": snapshot_relative_path,
        "error": error,
        "operation_scope": CORROBORATION_OPERATION_SCOPE,
    }
    return record


def _required_candidate_id(value: object) -> str:
    """校验只读补证详情只能按服务器候选 ID 查询，拒绝 URL、路径和任意文件名。"""
    if not isinstance(value, str) or not value or len(value) > 120:
        raise ValueError("candidate_id必须是有效的服务器候选 ID")
    if any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in value):
        raise ValueError("candidate_id必须是有效的服务器候选 ID")
    return value


def _read_verified_corroboration_snapshot(
    workspace_dir: Path,
    record: dict[str, Any],
) -> dict[str, object]:
    """按服务器 ID 读取并校验第二来源快照，不相信台账中的任意文件路径。

    输入为已保存的补证审计和服务器工作区，输出为哈希、长度与台账字段均已验证的快照 JSON。
    函数从 ``corroboration_id`` 重算唯一路径，拒绝符号链接、路径不一致、Base64/哈希异常和
    字段不一致；只读失败即停止，绝不尝试修复、覆盖或删除原候选、补证快照或正式证据链。
    """
    corroboration_id = record.get("corroboration_id")
    if not isinstance(corroboration_id, str):
        raise ValueError("候选第二来源补证审计缺少服务器 ID")
    path = candidate_corroboration_snapshot_path(workspace_dir, corroboration_id)
    expected_relative_path = str(path.relative_to(workspace_dir))
    if record.get("snapshot_relative_path") != expected_relative_path:
        raise ValueError("候选第二来源补证快照路径与服务器规则不一致，拒绝读取")
    if path.parent.is_symlink() or path.is_symlink() or not path.is_file():
        raise ValueError("候选第二来源补证快照文件不可安全读取")
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("候选第二来源补证快照不是有效 JSON") from exc
    if not isinstance(snapshot, dict):
        raise ValueError("候选第二来源补证快照必须是对象")
    for field_name in (
        "corroboration_id",
        "candidate_id",
        "parent_observation_id",
        "source_url",
        "source_title",
        "collected_at",
        "raw_sha256",
        "byte_size",
        "selector_reason",
        "operation_scope",
    ):
        expected = record.get("attempted_at") if field_name == "collected_at" else record.get(field_name)
        if snapshot.get(field_name) != expected:
            raise ValueError(f"候选第二来源补证快照与审计字段不一致：{field_name}")
    encoded_raw_content = snapshot.get("raw_content_base64")
    if not isinstance(encoded_raw_content, str):
        raise ValueError("候选第二来源补证快照缺少原始字节")
    try:
        raw_content = base64.b64decode(encoded_raw_content, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("候选第二来源补证快照原始字节编码无效") from exc
    if hashlib.sha256(raw_content).hexdigest() != record.get("raw_sha256"):
        raise ValueError("候选第二来源补证快照哈希校验失败")
    if len(raw_content) != record.get("byte_size"):
        raise ValueError("候选第二来源补证快照字节长度校验失败")
    if not isinstance(snapshot.get("visible_text"), str):
        raise ValueError("候选第二来源补证快照缺少可展示正文")
    return snapshot


def get_candidate_official_link_corroboration(
    workspace_dir: Path,
    *,
    candidate_id: str,
) -> dict[str, object]:
    """读取某候选最新成功的第二来源快照，供人工查看而非确认企业事实。

    输入只允许服务器已有候选 ID，输出为已验证补证审计及限制长度的可见正文。该接口不接受
    URL、页面文本、用户 ID、业务决定或证据字段，不重新抓网页、不生成草稿、不确认官网或
    中国/徐州关系，也不创建 evidence/lead；原始 Base64 和工作区路径只保留在服务器。
    """
    normalized_candidate_id = _required_candidate_id(candidate_id)
    ledger = load_ledger(workspace_dir)
    attempts = _corroboration_attempts(ledger)
    successful = [
        attempt
        for attempt in attempts
        if isinstance(attempt, dict)
        and attempt.get("candidate_id") == normalized_candidate_id
        and attempt.get("outcome") == "official_site_link_candidate"
    ]
    if not successful:
        raise ValueError("该候选尚无可查看的第二来源补证快照")
    successful.sort(key=lambda item: str(item.get("attempted_at", "")), reverse=True)
    record = successful[0]
    snapshot = _read_verified_corroboration_snapshot(workspace_dir, record)
    visible_text = snapshot["visible_text"]
    if not isinstance(visible_text, str):
        raise RuntimeError("候选第二来源补证快照正文格式异常")
    return {
        "corroboration": {
            "corroboration_id": record.get("corroboration_id"),
            "candidate_id": record.get("candidate_id"),
            "parent_observation_id": record.get("parent_observation_id"),
            "attempted_at": record.get("attempted_at"),
            "outcome": record.get("outcome"),
            "selector_reason": record.get("selector_reason"),
            "source_url": record.get("source_url"),
            "source_title": record.get("source_title"),
            "raw_sha256": record.get("raw_sha256"),
            "byte_size": record.get("byte_size"),
            "operation_scope": record.get("operation_scope"),
        },
        "snapshot": {
            "source_url": snapshot.get("source_url"),
            "source_title": snapshot.get("source_title"),
            "content_type": snapshot.get("content_type"),
            "collected_at": snapshot.get("collected_at"),
            "raw_sha256": snapshot.get("raw_sha256"),
            "byte_size": snapshot.get("byte_size"),
            "visible_text": visible_text[:MAX_RETURN_VISIBLE_TEXT_CHARS],
            "visible_text_truncated": len(visible_text) > MAX_RETURN_VISIBLE_TEXT_CHARS,
            "raw_content_available": True,
            "raw_content_not_returned_reason": "原始字节保留在受限快照中；请以正文、来源链接、采集时间和哈希人工核验。",
        },
        "caveat": "该页面只因父候选快照中的明确官网锚链接被低频抓取；它仍不是官网归属、中国业务、徐州关联、融资事实或招商意愿的自动确认。",
        "operation_scope": "server_candidate_official_link_corroboration_readonly_only",
    }


def run_candidate_official_link_corroboration(workspace_dir: Path) -> dict[str, object]:
    """从候选快照明确官网链接低频追加第二来源待人工核验材料。

    输入为服务器工作区，输出为本轮成功、无安全链接、robots 拒绝、抓取失败和容量跳过
    的审计摘要。函数只考虑需要中国业务补证、未被人工标为无效、且尚未处理当前父快照的
    候选；官网锚链接必须跨域，随后仍要通过 robots、DNS/公网、跳转、MIME、大小和哈希
    校验。输出不是企业事实，也不会创建 evidence、lead、评分、预警或业务推进。若无公开
    官网链接或站点拒绝访问，将保留明确原因供人工补证而不是转向任意搜索或绕过限制。

    ===== 付费升级（暂不启用；仅供后续工程师预算）=====
    若需要高覆盖、可批量核验“企业官网归属、工商登记、中国实际经营地与徐州关联”，应采购
    合规企业信息数据服务并签订数据处理/可追溯来源协议；常见企业信息 API 为按查询量或
    年度订阅计费，预算通常约 1 万–10 万元/年，须以供应商报价、许可范围和政务合规要求为
    准。不得以伪造 TLS、验证码绕过、无头浏览器隐身或未授权账号替代该方案。
    """
    started_at = _utc_now()
    ledger = load_ledger(workspace_dir)
    protected_before = _protected_business_snapshot(ledger)
    candidates, observations = _candidate_observation_lists(ledger)
    attempts = _corroboration_attempts(ledger)
    invalid_ids = invalid_discovery_candidate_ids(ledger)
    latest_observations = _latest_observations_by_candidate(observations)
    already_processed = {
        (attempt.get("candidate_id"), attempt.get("parent_observation_id"))
        for attempt in attempts
        if isinstance(attempt, dict)
    }
    storage_bytes_before = _existing_storage_bytes(attempts)
    storage_bytes_after = storage_bytes_before
    new_records: list[dict[str, object]] = []
    saved_snapshot_paths: list[Path] = []
    success_count = 0
    no_link_count = 0
    robots_denied_count = 0
    failed_count = 0
    quota_skipped_count = 0

    eligible: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for candidate in candidates:
        candidate_id = candidate.get("candidate_id")
        if (
            not isinstance(candidate_id, str)
            or candidate_id in invalid_ids
            or candidate.get("requires_china_business_verification") is not True
        ):
            continue
        parent = latest_observations.get(candidate_id)
        if parent is None or parent.get("state") != "pending_evidence_verification":
            continue
        parent_observation_id = parent.get("observation_id")
        if not isinstance(parent_observation_id, str) or (candidate_id, parent_observation_id) in already_processed:
            continue
        eligible.append((candidate, parent))
    eligible.sort(key=lambda pair: str(pair[1].get("collected_at", "")), reverse=True)

    for candidate, parent_observation in eligible[:MAX_OFFICIAL_LINK_CORROBORATIONS_PER_RUN]:
        candidate_id = candidate["candidate_id"]
        if not isinstance(candidate_id, str):
            continue
        attempted_at = _utc_now()
        try:
            snapshot = _read_verified_observation_snapshot(workspace_dir, parent_observation)
            parent_url = snapshot.get("source_url")
            encoded_raw_content = snapshot.get("raw_content_base64")
            if not isinstance(parent_url, str) or not isinstance(encoded_raw_content, str):
                raise ValueError("父候选快照缺少来源或原始字节")
            raw_content = base64.b64decode(encoded_raw_content, validate=True)
            official_links = _candidate_official_links(parent_url, raw_content)
            if not official_links:
                new_records.append(
                    _attempt_record(
                        candidate_id=candidate_id,
                        parent_observation=parent_observation,
                        attempted_at=attempted_at,
                        outcome="no_independent_official_site_link",
                    )
                )
                no_link_count += 1
                continue
            official_url = official_links[0]
            robots = check_robots_access(official_url)
            if not robots.get("allowed"):
                new_records.append(
                    _attempt_record(
                        candidate_id=candidate_id,
                        parent_observation=parent_observation,
                        attempted_at=attempted_at,
                        outcome="robots_disallow",
                        source_url=official_url,
                        error=str(robots.get("reason", "robots_denied"))[:MAX_CORROBORATION_ERROR_LENGTH],
                    )
                )
                robots_denied_count += 1
                continue
            fetched = fetch_public_source_for_observation(official_url)
            source_url = fetched.get("source_url")
            source_title = fetched.get("source_title")
            visible_text = fetched.get("visible_text")
            fetched_raw_content = fetched.get("raw_content")
            raw_sha256 = fetched.get("raw_sha256")
            content_type = fetched.get("content_type")
            byte_size = fetched.get("byte_size")
            if not (
                isinstance(source_url, str)
                and isinstance(source_title, str)
                and isinstance(visible_text, str)
                and isinstance(fetched_raw_content, bytes)
                and isinstance(raw_sha256, str)
                and isinstance(content_type, str)
                and isinstance(byte_size, int)
                and not isinstance(byte_size, bool)
                and byte_size == len(fetched_raw_content)
                and hashlib.sha256(fetched_raw_content).hexdigest() == raw_sha256
            ):
                raise RuntimeError("第二来源公开页面响应格式或哈希异常")
            if (
                len(attempts) + len(new_records) >= MAX_CORROBORATION_SNAPSHOT_RECORDS
                or storage_bytes_after >= MAX_CORROBORATION_SNAPSHOT_STORAGE_BYTES
            ):
                new_records.append(
                    _attempt_record(
                        candidate_id=candidate_id,
                        parent_observation=parent_observation,
                        attempted_at=attempted_at,
                        outcome="snapshot_quota_reached",
                        source_url=source_url,
                    )
                )
                quota_skipped_count += 1
                continue
            record = _attempt_record(
                candidate_id=candidate_id,
                parent_observation=parent_observation,
                attempted_at=attempted_at,
                outcome="official_site_link_candidate",
                source_url=source_url,
                source_title=source_title,
                raw_sha256=raw_sha256,
                byte_size=byte_size,
            )
            corroboration_id = record["corroboration_id"]
            if not isinstance(corroboration_id, str):
                raise RuntimeError("第二来源补证审计 ID 格式异常")
            snapshot_payload: dict[str, object] = {
                "corroboration_id": corroboration_id,
                "candidate_id": candidate_id,
                "parent_observation_id": parent_observation["observation_id"],
                "source_url": source_url,
                "source_title": source_title,
                "content_type": content_type,
                "collected_at": attempted_at,
                "raw_sha256": raw_sha256,
                "byte_size": byte_size,
                "visible_text": visible_text,
                "raw_content_base64": base64.b64encode(fetched_raw_content).decode("ascii"),
                "selector_reason": OFFICIAL_LINK_SELECTOR_REASON,
                "operation_scope": CORROBORATION_OPERATION_SCOPE,
            }
            serialized_size = _serialized_snapshot_size(snapshot_payload)
            if storage_bytes_after + serialized_size > MAX_CORROBORATION_SNAPSHOT_STORAGE_BYTES:
                new_records.append(
                    _attempt_record(
                        candidate_id=candidate_id,
                        parent_observation=parent_observation,
                        attempted_at=attempted_at,
                        outcome="snapshot_quota_reached",
                        source_url=source_url,
                    )
                )
                quota_skipped_count += 1
                continue
            snapshot_path = save_candidate_corroboration_snapshot(
                workspace_dir,
                corroboration_id=corroboration_id,
                payload=snapshot_payload,
            )
            saved_snapshot_paths.append(snapshot_path)
            record["snapshot_storage_bytes"] = serialized_size
            record["snapshot_relative_path"] = str(snapshot_path.relative_to(workspace_dir))
            new_records.append(record)
            storage_bytes_after += serialized_size
            success_count += 1
        except (OSError, RuntimeError, ValueError, UnicodeError) as exc:
            new_records.append(
                _attempt_record(
                    candidate_id=candidate_id,
                    parent_observation=parent_observation,
                    attempted_at=attempted_at,
                    outcome="corroboration_failed",
                    error=str(exc)[:MAX_CORROBORATION_ERROR_LENGTH],
                )
            )
            failed_count += 1

    _assert_protected_business_records_unchanged(protected_before, ledger)
    if new_records:
        attempts.extend(new_records)
        _assert_protected_business_records_unchanged(protected_before, ledger)
        try:
            saved_path = save_ledger(workspace_dir, ledger)
        except (OSError, RuntimeError, ValueError):
            for snapshot_path in saved_snapshot_paths:
                if snapshot_path.exists() and not snapshot_path.is_symlink():
                    snapshot_path.unlink()
            raise
    else:
        saved_path = ledger_path(workspace_dir)
    run = {
        "started_at": started_at,
        "completed_at": _utc_now(),
        "eligible_candidate_count": len(eligible),
        "attempted_candidate_count": len(new_records),
        "official_site_link_candidate_count": success_count,
        "no_independent_official_site_link_count": no_link_count,
        "robots_denied_count": robots_denied_count,
        "failed_count": failed_count,
        "snapshot_quota_skipped_count": quota_skipped_count,
        "snapshot_storage_bytes_before": storage_bytes_before,
        "snapshot_storage_bytes_after": storage_bytes_after,
        "operation_scope": CORROBORATION_OPERATION_SCOPE,
        "status": "partial_failure" if failed_count else "completed",
    }
    return {"run": run, "attempts": copy.deepcopy(new_records), "ledger_path": str(saved_path)}
