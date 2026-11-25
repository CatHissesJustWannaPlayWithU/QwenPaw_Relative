"""徐州招商中公开协同投资信号的受限草稿与人工复核服务。

本模块只从已通过完整性校验的正式公开证据中提取“联合投资、共同设立基金、寻找出资方”
等组织级文本信号，帮助招商人员优先核验可能存在产业资本协同窗口的主体。它不建立个人
资金池、不处理自然人的财务能力或适当性、不撮合交易，也不把中间人背书、政府背景、
多方参与或公开文字推断成资金承诺、投资意愿或招商结果。

草稿与复核审计完全独立于 ``evidence``：需要查看事实时必须沿 ``evidence_id`` 回看
原始来源。任何人工操作只能追加业务判断，不能改写来源 URL、原始摘录、采集时间、哈希、
线索状态或评分依据。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core import SUPPORTED_CLAIM_TYPE_TEXT_SUPPORTS
from .storage import ledger_path, load_ledger, save_ledger


CO_INVESTMENT_DRAFT_STATE = "pending_user_review"
CO_INVESTMENT_DRAFT_REVIEW_STATES = frozenset(
    {"ready_for_contact_validation", "need_evidence", "observe", "invalid"}
)
CO_INVESTMENT_DRAFT_REVIEW_STATE_ALIASES = {
    "可核验对接": "ready_for_contact_validation",
    "可继续核验": "ready_for_contact_validation",
    "待补证": "need_evidence",
    "观察": "observe",
    "无效": "invalid",
}
# 仅处理服务器原文支持的正式分类中的公开资本协同文本；普通新闻、搜索摘要、候选观察和
# 调用方自行声称的融资/合作标签都不能进入本模块。
_ELIGIBLE_CLAIM_TYPES = frozenset({"financing", "fund_raising", "fund_investment", "cooperation"})
_SIGNAL_TERMS: dict[str, tuple[str, ...]] = {
    "co_investment": ("联合投资", "共同投资", "协同投资", "联合出资", "跟投"),
    "fund_formation": ("共同设立基金", "共同发起基金", "产业基金", "基金募资", "LP招募"),
    "capital_partner_search": ("寻找出资方", "寻求投资者", "引入战略投资者", "寻找战略投资者"),
}
_SENTENCE_PATTERN = re.compile(r"[^。！？!?；;]{1,600}[。！？!?；;]?")
_VALID_DRAFT_ID_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")
CO_INVESTMENT_DRAFT_EXTRACTOR_VERSION = "public-org-co-investment-signal-v1"
CO_INVESTMENT_DRAFT_OPERATION_SCOPE = "signed_primary_evidence_org_level_co_investment_signal_draft_only"
CO_INVESTMENT_DRAFT_REVIEW_OPERATION_SCOPE = "authenticated_user_append_only_co_investment_signal_review_only"
MAX_CO_INVESTMENT_DRAFTS_PER_RUN = 100
MAX_CO_INVESTMENT_DRAFT_RECORDS = 2_000
MAX_CO_INVESTMENT_DRAFT_LIST_LIMIT = 200


def _utc_now() -> str:
    """返回服务器 UTC 时间，避免采信模型、浏览器或客户端自称的复核时间。"""

    return datetime.now(timezone.utc).isoformat()


def _has_server_supported_claim_type(evidence: dict[str, Any]) -> bool:
    """限制协同投资草稿只读取有固定原文词支持的分类证据。

    输入为既有已签名证据，输出为是否可按其分类进入协同投资词表抽取。未分类、历史未核验
    或未知支持状态都必须返回 ``False``，即使原记录带有融资、基金或合作标签也不能生成
    组织级待办。函数不改写证据、线索、评分或人工状态。
    """
    return evidence.get("claim_type_text_support") in SUPPORTED_CLAIM_TYPE_TEXT_SUPPORTS


def _stable_id(prefix: str, payload: dict[str, object]) -> str:
    """以固定服务器字段生成草稿或审计 ID，调用方不能指定或覆盖标识。"""

    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}-{hashlib.sha256(serialized.encode('utf-8')).hexdigest()[:16]}"


def _stable_json(value: object) -> str:
    """稳定序列化台账字段，用于保存前检测任何越权改写。"""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _required_text(value: object, field_name: str, *, maximum: int) -> str:
    """校验有限业务文本，拒绝空白、超长或非字符串输入且不写台账。"""

    if not isinstance(value, str) or not (normalized := value.strip()):
        raise ValueError(f"{field_name}不能为空")
    if len(normalized) > maximum:
        raise ValueError(f"{field_name}不能超过{maximum}个字符")
    return normalized


def _required_server_draft_id(value: object) -> str:
    """校验仅能使用服务端生成的草稿 ID，拒绝 URL、路径与任意客户端对象。"""

    draft_id = _required_text(value, "co_investment_draft_id", maximum=120)
    if any(character not in _VALID_DRAFT_ID_CHARACTERS for character in draft_id):
        raise ValueError("co_investment_draft_id必须是有效的服务器草稿 ID")
    return draft_id


def _co_investment_drafts(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    """取得协同投资信号草稿列表；结构异常时失败关闭，绝不重置历史记录。"""

    drafts = ledger.get("co_investment_signal_drafts")
    if not isinstance(drafts, list):
        raise ValueError("招商线索台账中的 co_investment_signal_drafts 必须是列表")
    return drafts


def _co_investment_draft_reviews(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    """取得只追加复核审计；缺失或异常均不能被解释成已经完成复核。"""

    reviews = ledger.get("co_investment_signal_draft_reviews")
    if not isinstance(reviews, list):
        raise ValueError("招商线索台账中的 co_investment_signal_draft_reviews 必须是列表")
    return reviews


def _protected_records_snapshot(ledger: dict[str, Any], *, writable_field: str) -> str:
    """快照本次操作禁止改变的整本台账字段，保护原始证据链和其他业务审计。

    输入为已验证台账和本次唯一允许追加的列表名，输出为其余字段的稳定快照。自动抽取
    只能追加草稿，人工复核只能追加复核审计；因此任一流程均不能借保存改写 ``evidence``、
    来源 URL、摘录、采集时间、评分、线索、图谱或其他草稿。该函数不联网、不做权限判断。
    """

    allowed = {"co_investment_signal_drafts", "co_investment_signal_draft_reviews"}
    if writable_field not in allowed:
        raise ValueError("协同投资信号操作不允许写入该台账字段")
    return _stable_json(
        {field_name: value for field_name, value in ledger.items() if field_name != writable_field}
    )


def _assert_protected_records_unchanged(
    before: str,
    ledger: dict[str, Any],
    *,
    writable_field: str,
) -> None:
    """保存前逐字确认本模块只追加获准列表，防止人工复核污染证据链。"""

    if _protected_records_snapshot(ledger, writable_field=writable_field) != before:
        raise RuntimeError("协同投资信号操作不得修改原始证据链或其他业务数据")


def _normalized_text(value: object) -> str:
    """归一公开摘录用于固定词匹配，不返回或写入新的事实文本。"""

    return re.sub(r"\s+", "", value) if isinstance(value, str) else ""


def _signal_match(sentence: str) -> tuple[str, list[str]] | None:
    """从单句原始摘录识别一个受控组织级协同资本信号，不推测人数、金额或意愿。

    输入为已签名 evidence 的一个短句；输出为固定信号类别和命中词，未满足条件时返回
    ``None``。函数只做字面匹配，不处理姓名、联系方式、资产、投资适当性或任何金融交易，
    也不读写台账或网络。
    """

    normalized = _normalized_text(sentence)
    for signal_type, terms in _SIGNAL_TERMS.items():
        matched = [term for term in terms if term in normalized]
        if matched:
            return signal_type, matched
    return None


def _review_state_for_draft(
    draft_id: str,
    reviews: list[dict[str, Any]],
) -> str:
    """由只追加审计派生草稿当前状态，异常记录不会被猜测为有效结论。"""

    state = CO_INVESTMENT_DRAFT_STATE
    for review in reviews:
        if review.get("co_investment_draft_id") != draft_id:
            continue
        new_state = review.get("new_state")
        if isinstance(new_state, str) and new_state in CO_INVESTMENT_DRAFT_REVIEW_STATES:
            state = new_state
    return state


def _public_draft(draft: dict[str, Any], reviews: list[dict[str, Any]]) -> dict[str, object]:
    """输出最小可审查草稿视图及审计历史，不暴露私人财务或快照文件信息。

    输入是已签名草稿及其只追加人工审计，输出只保留组织、证据回链、命中原句、来源卡片
    字段和复核历史。它不能被用于认定个人出资、资金承诺或投资资格，更不会修改 evidence、
    来源 URL、原始摘录、采集时间、评分或线索状态。
    """

    fields = (
        "co_investment_draft_id",
        "lead_id",
        "evidence_id",
        "entity_name",
        "sector",
        "signal_type",
        "matched_terms",
        "matched_excerpt",
        "source_url",
        "source_published_at",
        "source_level",
        "xuzhou_association_tags",
        "extractor_version",
        "created_at",
        "state",
        "operation_scope",
    )
    for field_name in fields:
        if field_name not in draft:
            raise ValueError(f"协同投资信号草稿缺少字段：{field_name}")
    draft_id = draft["co_investment_draft_id"]
    if not isinstance(draft_id, str):
        raise ValueError("协同投资信号草稿 ID 格式异常")
    history: list[dict[str, object]] = []
    for review in reviews:
        required_review_fields = (
            "review_id",
            "co_investment_draft_id",
            "reviewed_at",
            "reviewed_by_user_id",
            "old_state",
            "new_state",
            "review_note",
            "operation_scope",
        )
        if any(field_name not in review for field_name in required_review_fields):
            raise ValueError("协同投资信号草稿复核审计字段异常")
        history.append({field_name: review[field_name] for field_name in required_review_fields})
    history.sort(key=lambda item: str(item["reviewed_at"]), reverse=True)
    result = {field_name: draft[field_name] for field_name in fields}
    result["review_state"] = _review_state_for_draft(draft_id, reviews)
    result["review_history"] = history[:50]
    result["compliance_caveat"] = (
        "这是组织级公开协同资本信号，不代表任何个人资产、出资承诺、金融产品适当性或投资意愿；"
        "如需进一步沟通，必须由招商人员通过合规公开渠道人工核验主体和授权联系人。"
    )
    return result


def generate_co_investment_signal_drafts_from_signed_evidence(workspace_dir: Path) -> dict[str, object]:
    """从已签名一手证据确定性生成组织级协同投资信号草稿。

    输入仅为当前工作区；输出为本轮新增草稿、跳过计数和台账路径。函数只读取
    ``source_level=primary`` 且资本/合作类别的已有 evidence，在目标组织名与命中信号同句
    时追加待复核草稿。它不联网，不接受用户传入的人员、金额、资金方、联系人或意愿，
    不创建线索/图谱/评分/预警，不自动推进招商，也不会修改任何原始证据字段。
    """

    started_at = _utc_now()
    ledger = load_ledger(workspace_dir)
    protected_before = _protected_records_snapshot(
        ledger, writable_field="co_investment_signal_drafts"
    )
    drafts = _co_investment_drafts(ledger)
    evidence_records = ledger.get("evidence")
    leads = ledger.get("leads")
    if not isinstance(evidence_records, list) or not isinstance(leads, list):
        raise ValueError("招商线索台账中的线索或正式证据列表格式异常")
    leads_by_id = {
        lead_id: lead
        for lead in leads
        if isinstance(lead, dict)
        and isinstance((lead_id := lead.get("lead_id")), str)
        and lead_id
    }
    existing_draft_ids = {
        draft_id
        for draft in drafts
        if isinstance(draft, dict)
        and isinstance((draft_id := draft.get("co_investment_draft_id")), str)
        and draft_id
    }
    generated: list[dict[str, object]] = []
    skipped_duplicates = 0
    skipped_invalid_evidence = 0
    skipped_quota = 0
    for evidence in evidence_records:
        if not isinstance(evidence, dict):
            skipped_invalid_evidence += 1
            continue
        if (
            evidence.get("source_level") != "primary"
            or not _has_server_supported_claim_type(evidence)
            or evidence.get("claim_type") not in _ELIGIBLE_CLAIM_TYPES
        ):
            continue
        evidence_id = evidence.get("evidence_id")
        lead_id = evidence.get("lead_id")
        excerpt = evidence.get("excerpt")
        source_url = evidence.get("source_url")
        source_published_at = evidence.get("source_published_at")
        lead = leads_by_id.get(lead_id) if isinstance(lead_id, str) else None
        entity_name = lead.get("entity_name") if isinstance(lead, dict) else None
        sector = lead.get("sector") if isinstance(lead, dict) else None
        if not all(
            isinstance(value, str) and value.strip()
            for value in (evidence_id, lead_id, excerpt, source_url, source_published_at, entity_name, sector)
        ):
            skipped_invalid_evidence += 1
            continue
        tags = evidence.get("xuzhou_association_tags")
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            skipped_invalid_evidence += 1
            continue
        normalized_entity_name = _normalized_text(entity_name)
        if not normalized_entity_name:
            skipped_invalid_evidence += 1
            continue
        for sentence_match in _SENTENCE_PATTERN.finditer(excerpt):
            sentence = sentence_match.group(0).strip()
            if not sentence:
                continue
            # 目标组织必须与资本信号同句出现，避免文章另一段的泛泛行业评论被错绑到线索。
            if normalized_entity_name not in _normalized_text(sentence):
                continue
            signal = _signal_match(sentence)
            if signal is None:
                continue
            signal_type, matched_terms = signal
            draft_id = _stable_id(
                "co-investment-draft",
                {
                    "lead_id": lead_id,
                    "evidence_id": evidence_id,
                    "signal_type": signal_type,
                    "matched_excerpt": sentence,
                    "extractor_version": CO_INVESTMENT_DRAFT_EXTRACTOR_VERSION,
                },
            )
            if draft_id in existing_draft_ids:
                skipped_duplicates += 1
                continue
            if (
                len(drafts) + len(generated) >= MAX_CO_INVESTMENT_DRAFT_RECORDS
                or len(generated) >= MAX_CO_INVESTMENT_DRAFTS_PER_RUN
            ):
                skipped_quota += 1
                continue
            generated.append(
                {
                    "co_investment_draft_id": draft_id,
                    "lead_id": lead_id,
                    "evidence_id": evidence_id,
                    "entity_name": entity_name,
                    "sector": sector,
                    "signal_type": signal_type,
                    "matched_terms": matched_terms,
                    "matched_excerpt": sentence,
                    "source_url": source_url,
                    "source_published_at": source_published_at,
                    "source_level": "primary",
                    "xuzhou_association_tags": list(tags),
                    "extractor_version": CO_INVESTMENT_DRAFT_EXTRACTOR_VERSION,
                    "created_at": _utc_now(),
                    "state": CO_INVESTMENT_DRAFT_STATE,
                    "operation_scope": CO_INVESTMENT_DRAFT_OPERATION_SCOPE,
                }
            )
            existing_draft_ids.add(draft_id)
    _assert_protected_records_unchanged(
        protected_before, ledger, writable_field="co_investment_signal_drafts"
    )
    if generated:
        drafts.extend(generated)
        _assert_protected_records_unchanged(
            protected_before, ledger, writable_field="co_investment_signal_drafts"
        )
        saved_path = save_ledger(workspace_dir, ledger)
    else:
        saved_path = ledger_path(workspace_dir)
    return {
        "run": {
            "started_at": started_at,
            "completed_at": _utc_now(),
            "generated_draft_count": len(generated),
            "skipped_duplicate_count": skipped_duplicates,
            "skipped_invalid_evidence_count": skipped_invalid_evidence,
            "skipped_quota_count": skipped_quota,
            "draft_record_quota": MAX_CO_INVESTMENT_DRAFT_RECORDS,
            "status": "completed",
            "operation_scope": CO_INVESTMENT_DRAFT_OPERATION_SCOPE,
        },
        "co_investment_signal_drafts": [_public_draft(draft, []) for draft in generated],
        "ledger_path": str(saved_path),
    }


def list_co_investment_signal_drafts(
    workspace_dir: Path,
    *,
    lead_id: str | None = None,
    review_state: str | None = None,
    limit: int = 50,
) -> dict[str, object]:
    """只读列出组织级协同投资信号草稿和人工审计，不访问页面或改变业务数据。

    输入为当前工作区、可选既有线索 ID/复核状态及有限条数；输出为带 evidence 回链的草稿
    视图。认证和工作区归属由 API 层校验；本函数不接收人员、资金、URL、摘录、用户 ID 或
    写入字段，不能生成资金池、联系任何主体、自动推进线索或修改原始证据链。
    """

    normalized_lead_id = (
        _required_text(lead_id, "lead_id", maximum=100) if lead_id is not None else None
    )
    normalized_review_state = (
        _normalize_review_state(review_state) if review_state is not None else None
    )
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_CO_INVESTMENT_DRAFT_LIST_LIMIT:
        raise ValueError(f"limit必须在1到{MAX_CO_INVESTMENT_DRAFT_LIST_LIMIT}之间")
    ledger = load_ledger(workspace_dir)
    drafts = _co_investment_drafts(ledger)
    reviews = _co_investment_draft_reviews(ledger)
    reviews_by_draft: dict[str, list[dict[str, Any]]] = {}
    for review in reviews:
        if isinstance(review, dict) and isinstance(review.get("co_investment_draft_id"), str):
            reviews_by_draft.setdefault(review["co_investment_draft_id"], []).append(review)
    result: list[dict[str, object]] = []
    for draft in sorted(
        (item for item in drafts if isinstance(item, dict)),
        key=lambda item: str(item.get("created_at", "")),
        reverse=True,
    ):
        if normalized_lead_id is not None and draft.get("lead_id") != normalized_lead_id:
            continue
        public_draft = _public_draft(
            draft,
            reviews_by_draft.get(str(draft.get("co_investment_draft_id")), []),
        )
        if normalized_review_state is not None and public_draft["review_state"] != normalized_review_state:
            continue
        result.append(public_draft)
        if len(result) >= limit:
            break
    return {
        "co_investment_signal_drafts": result,
        "count": len(result),
        "operation_scope": "authenticated_user_readonly_org_level_co_investment_signal_drafts",
        "ledger_path": str(ledger_path(workspace_dir)),
    }


def _normalize_review_state(value: object) -> str:
    """归一人工复核结论，拒绝把“对接”扩展为自动联系或金融交易指令。"""

    raw_state = _required_text(value, "decision", maximum=100).casefold()
    normalized_state = CO_INVESTMENT_DRAFT_REVIEW_STATE_ALIASES.get(raw_state, raw_state)
    if normalized_state not in CO_INVESTMENT_DRAFT_REVIEW_STATES:
        raise ValueError("decision只能是可核验对接、待补证、观察或无效")
    return normalized_state


def review_co_investment_signal_draft(
    workspace_dir: Path,
    *,
    co_investment_draft_id: str,
    decision: str,
    review_note: str,
    authenticated_user_id: str,
) -> dict[str, object]:
    """普通登录用户追加协同投资信号业务判断，身份只能来自服务器认证层。

    输入为服务器草稿 ID、受控结论、复核备注及认证中间件提供的用户 ID；输出为新增审计和
    当前草稿视图。普通用户无需管理员角色，但客户端不得传入人员、金额、资金承诺、联系
    方式、证据、URL 或操作者 ID。该操作只表示后续可人工核验公开主体与授权联系人，不会
    自动联系、募资、撮合、推进线索或改写草稿/原始证据链。
    """

    normalized_draft_id = _required_server_draft_id(co_investment_draft_id)
    normalized_decision = _normalize_review_state(decision)
    normalized_note = _required_text(review_note, "review_note", maximum=2_000)
    normalized_user_id = _required_text(
        authenticated_user_id, "authenticated_user_id", maximum=100
    )
    ledger = load_ledger(workspace_dir)
    protected_before = _protected_records_snapshot(
        ledger, writable_field="co_investment_signal_draft_reviews"
    )
    drafts = _co_investment_drafts(ledger)
    reviews = _co_investment_draft_reviews(ledger)
    draft = next(
        (
            item
            for item in drafts
            if isinstance(item, dict)
            and item.get("co_investment_draft_id") == normalized_draft_id
        ),
        None,
    )
    if draft is None:
        raise ValueError("未找到协同投资信号草稿")
    if draft.get("state") != CO_INVESTMENT_DRAFT_STATE:
        raise ValueError("该协同投资信号草稿不是等待用户复核状态")
    prior_reviews = [
        review
        for review in reviews
        if isinstance(review, dict)
        and review.get("co_investment_draft_id") == normalized_draft_id
    ]
    old_state = _review_state_for_draft(normalized_draft_id, prior_reviews)
    reviewed_at = _utc_now()
    review = {
        "review_id": _stable_id(
            "co-investment-draft-review",
            {
                "co_investment_draft_id": normalized_draft_id,
                "reviewed_at": reviewed_at,
                "authenticated_user_id": normalized_user_id,
                "new_state": normalized_decision,
            },
        ),
        "co_investment_draft_id": normalized_draft_id,
        "reviewed_at": reviewed_at,
        "reviewed_by_user_id": normalized_user_id,
        "old_state": old_state,
        "new_state": normalized_decision,
        "review_note": normalized_note,
        "operation_scope": CO_INVESTMENT_DRAFT_REVIEW_OPERATION_SCOPE,
    }
    reviews.append(review)
    _assert_protected_records_unchanged(
        protected_before, ledger, writable_field="co_investment_signal_draft_reviews"
    )
    saved_path = save_ledger(workspace_dir, ledger)
    return {
        "co_investment_signal_draft": _public_draft(draft, [*prior_reviews, review]),
        "review": review,
        "ledger_path": str(saved_path),
    }
