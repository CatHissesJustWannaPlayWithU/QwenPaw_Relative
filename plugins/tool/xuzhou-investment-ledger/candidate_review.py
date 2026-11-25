"""徐州招商发现候选的只追加人工业务判断与安全展示服务。"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .adaptive_learning import adaptive_learning_mode_from_ledger, source_learning_priority_for_url
from .storage import load_ledger, save_ledger


PENDING_CANDIDATE_REVIEW_STATE = "pending_user_review"
# ``waive_xuzhou`` is deliberately *not* a direct-evidence decision.  It is an
# audited human scope decision: the reviewer confirms that the business is in
# China and records the enterprise origin, while deciding that an explicit
# XuZhou field is not required for this particular high-value material.  The
# subsequent evidence confirmation still has to re-fetch and verify the page.
CANDIDATE_REVIEW_DECISIONS = frozenset({"need_evidence", "observe", "invalid", "waive_xuzhou"})
CANDIDATE_REVIEW_OPERATION_SCOPE = "authenticated_discovery_candidate_business_review_only"
MAX_CANDIDATE_LIST_LIMIT = 100
_ENTERPRISE_ORIGIN_KINDS = frozenset({"registered_location", "headquarters", "operation_site", "unknown"})


def _utc_now() -> str:
    """返回服务器当前 UTC 时间，供候选判断审计而非页面事实使用。

    输入为空，输出为带时区的 ISO-8601 时间。时间由服务器生成，客户端不能伪造；它仅标识
    人工业务判断发生的时刻，不会替换候选的发现时间、页面采集时间或正式证据日期。
    """
    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, payload: dict[str, object]) -> str:
    """根据服务器生成的审计载荷创建稳定短 ID，不接受客户端提供的 ID。

    输入是固定前缀和本函数构造的候选复核字段，输出为可追溯审计 ID。它不读取或修改台账，
    也不把 URL、页面内容或原始证据暴露给调用方。
    """
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _required_candidate_id(value: object) -> str:
    """校验只能引用服务器生成的发现候选 ID，阻止路径或 URL 注入。

    输入是 API 路径或内部读取传入的候选 ID，输出为受限格式的 ID；它拒绝空白、URL、文件
    路径和任意字符。校验不联网、不读取网页，也不会改变候选、快照或证据。
    """
    if not isinstance(value, str) or not value or len(value) > 120:
        raise ValueError("candidate_id必须是有效的服务器候选 ID")
    if any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in value):
        raise ValueError("candidate_id必须是有效的服务器候选 ID")
    return value


def _required_text(value: object, field_name: str, *, maximum: int) -> str:
    """校验复核备注和认证用户 ID 的长度，避免业务审计被空值或超长输入污染。

    输入是待校验值、字段名和固定上限，输出为去首尾空白后的文本。该函数不把文本解释为
    网页事实、不写入 URL、快照或证据；真正保存只会发生在通过完整性校验之后。
    """
    if not isinstance(value, str):
        raise ValueError(f"{field_name}必须是文本")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{field_name}必须是1到{maximum}个字符")
    return normalized


def _normalized_enterprise_origin(value: object) -> dict[str, str]:
    """Validate the auditable, human-reviewed enterprise-origin annotation.

    The annotation is intentionally separate from page evidence.  It records
    what the authenticated reviewer checked (for example, ``北京`` and the
    basis used), rather than silently inferring an organisation's location from
    a media domain.  It never changes the candidate, snapshot, or evidence.
    """
    if not isinstance(value, dict):
        raise ValueError("选择无需徐州字段时，必须填写企业来源地和人工核验依据")
    origin = _required_text(value.get("origin"), "企业来源地", maximum=100)
    origin_kind = value.get("origin_kind")
    if origin_kind not in _ENTERPRISE_ORIGIN_KINDS:
        raise ValueError("企业来源地类型必须是注册地、总部、经营地或未知")
    basis = _required_text(value.get("evidence_basis"), "中国业务人工核验依据", maximum=2_000)
    if value.get("china_business_confirmed") is not True:
        raise ValueError("无需徐州字段前，必须由人工确认该主体具有中国业务依据")
    return {
        "origin": origin,
        "origin_kind": str(origin_kind),
        "evidence_basis": basis,
        "origin_status": "human_reviewed_not_formal_evidence",
        "china_business_confirmed": "human_reviewed",
    }


def _candidate_reviews(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    """取得台账中的候选业务判断审计列表，并拒绝异常结构。

    输入是已通过 HMAC 校验的台账，输出为只追加审计列表的内存引用。它不创建候选、快照、
    线索或证据；若字段类型异常则停止，绝不猜测或覆盖历史业务记录。
    """
    reviews = ledger.get("candidate_business_reviews")
    if not isinstance(reviews, list):
        raise ValueError("招商线索台账中的 candidate_business_reviews 必须是列表")
    return reviews


def _discovery_candidates(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    """读取服务器发现候选列表，拒绝把任意客户端对象当作候选。

    输入是已验证台账，输出为其 discovery_candidates 列表。函数不联网、不改变候选状态，
    更不允许调用方传入或覆盖候选来源 URL、摘要、发现时间及其后续原始快照。
    """
    candidates = ledger.get("discovery_candidates")
    if not isinstance(candidates, list):
        raise ValueError("招商线索台账中的 discovery_candidates 必须是列表")
    if not all(isinstance(candidate, dict) for candidate in candidates):
        raise ValueError("招商线索台账中的 discovery_candidates 必须只包含对象")
    return candidates


def _latest_reviews_by_candidate(ledger: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """按追加顺序取得每个候选的最新业务判断，不改写历史审计。

    输入是服务器台账，输出以 candidate_id 为键的最后一条合法审计记录。无审计时调用方
    应显示“等待用户复核”；本函数不把“观察”或“待补证”推断为企业事实，也不影响证据链。
    """
    latest: dict[str, dict[str, Any]] = {}
    for review in _candidate_reviews(ledger):
        if not isinstance(review, dict):
            raise ValueError("招商候选业务判断审计必须是对象")
        candidate_id = review.get("candidate_id")
        state = review.get("new_review_state")
        if not isinstance(candidate_id, str) or state not in CANDIDATE_REVIEW_DECISIONS:
            raise ValueError("招商候选业务判断审计字段异常")
        latest[candidate_id] = review
    return latest


def candidate_geographic_scope(
    candidate: dict[str, Any], review: dict[str, Any] | None,
) -> dict[str, object]:
    """Return an explicit China/XuZhou scope view without claiming a fact.

    Older records stored one combined ``requires_china_business_verification``
    marker.  They remain visible as ``legacy_scope_unresolved`` rather than
    being silently classified or waived.  Newer title/index matches are merely
    discovery signals; a human waiver is an auditable operational decision and
    still does not make a formal evidence record.
    """
    assessment = candidate.get("eligibility_assessment")
    assessment = assessment if isinstance(assessment, dict) else {}
    china_terms = assessment.get("china_context_terms")
    xuzhou_terms = assessment.get("xuzhou_context_terms")
    has_china_term = isinstance(china_terms, list) and bool(china_terms)
    has_xuzhou_term = isinstance(xuzhou_terms, list) and bool(xuzhou_terms)
    combined_legacy_gate = bool(
        candidate.get("requires_china_business_verification") is True
        or assessment.get("requires_china_business_verification") is True
    )
    waiver = (
        review.get("geographic_scope_review")
        if isinstance(review, dict) and review.get("new_review_state") == "waive_xuzhou"
        else None
    )
    if isinstance(waiver, dict) and waiver.get("china_business_confirmed") == "human_reviewed":
        origin = waiver.get("enterprise_origin")
        return {
            "scope_status": "xuzhou_waived_by_human_pending_server_recapture",
            "can_request_server_recapture": True,
            "china_business_status": "human_reviewed",
            "xuzhou_association_status": "not_required_by_human",
            "geographic_priority": "low_without_xuzhou",
            "business_value_status": "human_high_value_review",
            "enterprise_origin": copy.deepcopy(origin) if isinstance(origin, dict) else None,
            "scope_note": "人工确认中国业务并决定本条不以徐州字段为入证前提；仍须服务器重新抓取并核验页面。",
        }
    if combined_legacy_gate:
        return {
            "scope_status": "legacy_scope_unresolved",
            "can_request_server_recapture": False,
            "china_business_status": "needs_human_review",
            "xuzhou_association_status": "needs_human_review",
            "geographic_priority": "needs_human_sorting",
            "business_value_status": "unreviewed",
            "enterprise_origin": None,
            "scope_note": "历史记录只保存了合并的中国/徐州门槛，系统不会替人推断；请人工补充判断或选择无需徐州字段。",
        }
    if has_xuzhou_term:
        xuzhou_status = "explicit_in_discovery_index"
        priority = "high_with_xuzhou"
    else:
        xuzhou_status = "not_explicit_in_discovery_index"
        priority = "low_without_xuzhou"
    return {
        "scope_status": "discovery_index_scope_available",
        "can_request_server_recapture": True,
        "china_business_status": "explicit_in_discovery_index" if has_china_term else "not_explicit_in_discovery_index",
        "xuzhou_association_status": xuzhou_status,
        "geographic_priority": priority,
        "business_value_status": "unreviewed",
        "enterprise_origin": None,
        "scope_note": "这是标题/摘要层面的发现信号，不是正式企业事实；是否有业务价值仍由人工决定。",
    }


def candidate_recapture_scope_blocker(ledger: dict[str, Any], *, candidate_id: str) -> str | None:
    """Return the explicit preflight reason that may block a server recapture."""
    candidate = next(
        (item for item in _discovery_candidates(ledger) if item.get("candidate_id") == candidate_id),
        None,
    )
    if candidate is None:
        raise ValueError("证据草稿引用的候选不存在")
    review = _latest_reviews_by_candidate(ledger).get(candidate_id)
    scope = candidate_geographic_scope(candidate, review)
    if scope.get("can_request_server_recapture") is not True:
        return "候选的中国业务与徐州关联仍是合并的历史待确认项；请人工作地域判断，或在确认中国业务和企业来源地后选择“无需徐州字段”。"
    return None


def _latest_corroborations_by_candidate(ledger: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """取得每个候选最新的第二来源补证审计，只读且不把链接当作事实确认。

    输入为已通过 HMAC 校验的台账，输出以候选 ID 为键的最后一条补证尝试。记录中的“官网
    链接候选”仅表示父页面明确给出了一个跨域官网锚链接并被受控抓取，仍需人工确认其归属
    及中国/徐州语境；本函数不联网、不读快照正文、不创建 evidence 或改变任何业务状态。
    """
    attempts = ledger.get("candidate_corroboration_attempts")
    if not isinstance(attempts, list):
        raise ValueError("招商线索台账中的 candidate_corroboration_attempts 必须是列表")
    latest: dict[str, dict[str, Any]] = {}
    for attempt in attempts:
        if not isinstance(attempt, dict):
            raise ValueError("招商候选第二来源补证审计必须是对象")
        candidate_id = attempt.get("candidate_id")
        outcome = attempt.get("outcome")
        if not isinstance(candidate_id, str) or not isinstance(outcome, str):
            raise ValueError("招商候选第二来源补证审计字段异常")
        latest[candidate_id] = attempt
    return latest


def invalid_discovery_candidate_ids(ledger: dict[str, Any]) -> set[str]:
    """返回人工标记无效、应退出未来自动观察预算的候选 ID 集合。

    输入是已验证台账，输出为服务器审计推导出的 ID 集合。仅“无效”停止后续自动访问；
    “待补证”和“观察”仍保留原始候选和历史快照。函数不删除任何记录、不改写 evidence，
    也不会将无效业务判断表述为企业风险或事实。
    """
    return {
        candidate_id
        for candidate_id, review in _latest_reviews_by_candidate(ledger).items()
        if review.get("new_review_state") == "invalid"
    }


def _public_candidate(
    candidate: dict[str, Any],
    review: dict[str, Any] | None,
    corroboration: dict[str, Any] | None,
    learning_priority: dict[str, object] | None = None,
) -> dict[str, object]:
    """生成可安全显示的发现候选摘要，不暴露服务器内部路径或原始字节。

    输入是服务器保存的候选和其最新业务判断，输出保留来源链接、标题、摘要、产业、时间和
    受限状态；不会返回本地快照路径、Base64 页面、完整性签名或任何可编辑证据字段。候选
    摘要始终只是待核验信息，不会由展示层升级为招商线索、评分、预警或关系事实。
    """
    geographic_scope = candidate_geographic_scope(candidate, review)
    china_verification_required = geographic_scope.get("can_request_server_recapture") is not True
    result: dict[str, object] = {
        "candidate_id": candidate.get("candidate_id"),
        "source_title": candidate.get("source_title"),
        "source_url": candidate.get("source_url"),
        "search_snippet": candidate.get("search_snippet"),
        "sector": candidate.get("sector"),
        "source_type": candidate.get("source_type"),
        "source_level": candidate.get("source_level"),
        "state": candidate.get("state"),
        "discovered_at": candidate.get("discovered_at"),
        "latest_observation_id": candidate.get("last_observation_id"),
        # A failed controlled recapture is operational context, not an
        # enterprise conclusion.  Showing its bounded reason prevents a
        # business user from mistaking “found a link” for “saved/verified
        # material”, and does not expose a filesystem path or page bytes.
        "capture_attempts": candidate.get("capture_attempts"),
        "last_capture_error": candidate.get("last_capture_error"),
        "last_captured_at": candidate.get("last_captured_at"),
        "requires_china_business_verification": china_verification_required,
        "geographic_scope": geographic_scope,
        "business_review_state": (
            review.get("new_review_state") if isinstance(review, dict) else PENDING_CANDIDATE_REVIEW_STATE
        ),
        "business_reviewed_at": review.get("reviewed_at") if isinstance(review, dict) else None,
        "business_review_note": review.get("review_note") if isinstance(review, dict) else None,
        # 第二来源仅是“页面明确官网链接”后的待人工核验材料；浏览器需要看到其 URL 和失败
        # 原因来决定补证动作，但不能获得快照本地路径、Base64 原文或把它显示成已确认事实。
        "latest_corroboration_outcome": corroboration.get("outcome") if isinstance(corroboration, dict) else None,
        "latest_corroboration_url": corroboration.get("source_url") if isinstance(corroboration, dict) else None,
        "latest_corroboration_title": corroboration.get("source_title") if isinstance(corroboration, dict) else None,
        "latest_corroboration_at": corroboration.get("attempted_at") if isinstance(corroboration, dict) else None,
        "latest_corroboration_error": corroboration.get("error") if isinstance(corroboration, dict) else None,
        # 学习优先级只影响队列展示顺序。它来自已有证据、人工跟进和结果反馈的聚合，
        # 绝不替代候选页面、人工判断或企业事实，也不修改候选原始字段。
        "learning_priority": copy.deepcopy(learning_priority) if learning_priority is not None else None,
        "operation_scope": "server_discovery_candidate_readonly_summary",
    }
    return copy.deepcopy(result)


def list_discovery_candidates(
    workspace_dir: Path,
    *,
    sector: str | None = None,
    review_state: str | None = None,
    limit: int = MAX_CANDIDATE_LIST_LIMIT,
) -> dict[str, object]:
    """列出刚发现但尚未必有原始快照的候选链接，供人工了解完整发现队列。

    输入是服务器工作区、可选产业/业务状态和有限数量，输出是只读候选摘要与最新判断。
    它不访问网页、不触发候选观察、不创建 evidence 或 lead；因此用户打开工作台就能看到
    手动发现结果，同时不会让页面展示行为改变来源 URL、摘要、时间、快照或证据链。
    """
    if sector is not None and (not isinstance(sector, str) or not sector.strip() or len(sector) > 100):
        raise ValueError("sector必须是1到100个字符")
    if review_state is not None and review_state not in {
        PENDING_CANDIDATE_REVIEW_STATE,
        *CANDIDATE_REVIEW_DECISIONS,
    }:
        raise ValueError("review_state不是允许的候选业务状态")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_CANDIDATE_LIST_LIMIT:
        raise ValueError(f"limit必须是1到{MAX_CANDIDATE_LIST_LIMIT}之间的整数")

    ledger = load_ledger(workspace_dir)
    latest_reviews = _latest_reviews_by_candidate(ledger)
    latest_corroborations = _latest_corroborations_by_candidate(ledger)
    items = []
    for candidate in _discovery_candidates(ledger):
        learning_priority = source_learning_priority_for_url(ledger, candidate.get("source_url"))
        items.append(
            _public_candidate(
                candidate,
                latest_reviews.get(str(candidate.get("candidate_id"))),
                latest_corroborations.get(str(candidate.get("candidate_id"))),
                learning_priority=learning_priority,
            )
        )
    if sector is not None:
        normalized_sector = sector.strip()
        items = [item for item in items if item.get("sector") == normalized_sector]
    if review_state is not None:
        items = [item for item in items if item.get("business_review_state") == review_state]
    if adaptive_learning_mode_from_ledger(ledger) in {"adaptive_ranking", "adaptive_ranking_and_recipe_order"}:
        # 先按发现时间倒序，再稳定地按学习优先级排序。这样无需把任意 ISO 时间字符串
        # 强行转为整数，带时区或小数秒的历史记录也不会使整个只读队列加载失败。
        items.sort(key=lambda item: str(item.get("discovered_at") or ""), reverse=True)
        items.sort(
            key=lambda item: (
                int((item.get("learning_priority") or {}).get("rank", 2))
                if isinstance(item.get("learning_priority"), dict)
                else 2
            )
        )
    else:
        items.sort(key=lambda item: str(item.get("discovered_at") or ""), reverse=True)
    return {
        "count": len(items),
        "items": items[:limit],
        "operation_scope": "server_discovery_candidate_readonly_summary",
    }


def review_discovery_candidate(
    workspace_dir: Path,
    *,
    candidate_id: str,
    decision: str,
    review_note: str,
    authenticated_user_id: str,
    enterprise_origin: dict[str, object] | None = None,
) -> dict[str, object]:
    """由普通登录用户追加发现候选的待补证、观察或无效判断。

    输入只能是服务器候选 ID、受控决定、备注和认证层提供的用户 ID；输出是追加审计和
    当前候选摘要。函数绝不接受来源 URL、页面摘要、采集时间、快照、证据、评分或客户端
    用户 ID，并在保存前后逐字校验候选、观察、线索和证据均未改变；因此业务复核不能改写
    原始证据链。只有“无效”会使该候选退出未来自动观察，历史材料仍完整保留。
    """
    normalized_candidate_id = _required_candidate_id(candidate_id)
    if decision not in CANDIDATE_REVIEW_DECISIONS:
        raise ValueError("decision必须是待补证、观察、无需徐州字段或无效之一")
    normalized_note = _required_text(review_note, "review_note", maximum=2_000)
    normalized_user_id = _required_text(authenticated_user_id, "authenticated_user_id", maximum=100)
    ledger = load_ledger(workspace_dir)
    candidates = _discovery_candidates(ledger)
    candidate = next(
        (item for item in candidates if item.get("candidate_id") == normalized_candidate_id),
        None,
    )
    if candidate is None:
        raise ValueError("未找到对应的发现候选")
    normalized_origin = (
        _normalized_enterprise_origin(enterprise_origin)
        if decision == "waive_xuzhou"
        else None
    )
    if decision != "waive_xuzhou" and enterprise_origin is not None:
        raise ValueError("企业来源地仅能随“无需徐州字段”这一人工地域判断提交")
    protected_before = {
        field_name: json.dumps(ledger.get(field_name), ensure_ascii=False, sort_keys=True)
        for field_name in (
            "discovery_runs",
            "discovery_candidates",
            "candidate_observations",
            "candidate_scope_exclusions",
            "leads",
            "evidence",
            "score_snapshots",
            "outcome_feedback",
        )
    }
    latest_review = _latest_reviews_by_candidate(ledger).get(normalized_candidate_id)
    old_state = (
        str(latest_review.get("new_review_state"))
        if isinstance(latest_review, dict)
        else PENDING_CANDIDATE_REVIEW_STATE
    )
    reviewed_at = _utc_now()
    audit = {
        "review_id": _stable_id(
            "discovery-candidate-business-review",
            {
                "candidate_id": normalized_candidate_id,
                "old_review_state": old_state,
                "new_review_state": decision,
                "reviewed_at": reviewed_at,
                "reviewed_by_user_id": normalized_user_id,
            },
        ),
        "candidate_id": normalized_candidate_id,
        "old_review_state": old_state,
        "new_review_state": decision,
        "review_note": normalized_note,
        "reviewed_at": reviewed_at,
        "reviewed_by_user_id": normalized_user_id,
        "operation_scope": CANDIDATE_REVIEW_OPERATION_SCOPE,
    }
    if normalized_origin is not None:
        audit["geographic_scope_review"] = {
            "decision": "xuzhou_not_required_for_this_material",
            "china_business_confirmed": "human_reviewed",
            "enterprise_origin": normalized_origin,
            "review_boundary": "does_not_replace_page_evidence_or_server_recapture",
        }
    _candidate_reviews(ledger).append(audit)
    protected_after = {
        field_name: json.dumps(ledger.get(field_name), ensure_ascii=False, sort_keys=True)
        for field_name in protected_before
    }
    if protected_after != protected_before:
        raise RuntimeError("候选业务复核不得修改候选、快照、线索、证据或评分记录")
    saved_path = save_ledger(workspace_dir, ledger)
    return {
        "candidate": _public_candidate(
            candidate,
            audit,
            _latest_corroborations_by_candidate(ledger).get(normalized_candidate_id),
        ),
        "review": copy.deepcopy(audit),
        "ledger_path": str(saved_path),
        "operation_scope": CANDIDATE_REVIEW_OPERATION_SCOPE,
    }
