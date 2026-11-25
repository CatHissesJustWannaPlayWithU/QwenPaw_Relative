"""从已签名公开证据生成关键人员关系待复核草稿。

本模块只读取台账中已通过 HMAC 完整性校验的 ``evidence`` 摘录，以固定、可审查的规则
识别“法人、创始人、实控人、股东、董事、高管”和同句明确的徐州关联表述。输出始终是
独立草稿，不会联网、不会使用模型补全、不会创建图谱节点或边，也绝不把个人姓名解释为
个人投资能力、联系方式、资产或招商意愿。任何关系登记仍须由普通登录用户经现有图谱
接口重新校验同一条原始证据。
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .storage import ledger_path, load_ledger, save_ledger


# 单轮自动任务仅处理有限数量的明确公开角色，避免一条异常摘录或历史积压放大为大量待办。
MAX_RELATIONSHIP_DRAFTS_PER_RUN = 100
MAX_RELATIONSHIP_DRAFT_RECORDS = 5_000
MAX_RELATIONSHIP_DRAFT_LIST_LIMIT = 200
MAX_MATCHED_EXCERPT_CHARS = 1_200
RELATIONSHIP_DRAFT_EXTRACTOR_VERSION = "deterministic-signed-evidence-key-person-v1"
RELATIONSHIP_DRAFT_STATE = "pending_user_review"
RELATIONSHIP_DRAFT_OPERATION_SCOPE = (
    "automatic_signed_evidence_key_person_relationship_draft_pending_human_review_only"
)
RELATIONSHIP_DRAFT_REVIEW_OPERATION_SCOPE = (
    "authenticated_user_key_person_relationship_draft_business_review_only"
)
RELATIONSHIP_DRAFT_REVIEW_STATES = frozenset(
    {"ready_for_graph_registration", "need_evidence", "observe", "invalid"}
)
RELATIONSHIP_DRAFT_REVIEW_STATE_ALIASES = {
    "可登记图谱": "ready_for_graph_registration",
    "可进入图谱登记": "ready_for_graph_registration",
    "待补证": "need_evidence",
    "观察": "observe",
    "无效": "invalid",
}
_VALID_SERVER_ID_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")
_SENTENCE_PATTERN = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?", re.MULTILINE)

# 角色类型严格与 relationship_graph.py 的受控枚举相同。别名仅用于在原文中命中，草稿中
# 一律保存规范类型；不能根据“创办”“管理”等模糊动词额外推断法律或经营角色。
_ROLE_TERMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("legal_representative", ("法定代表人", "法定代表")),
    ("founder", ("联合创始人", "创始人")),
    ("actual_controller", ("实际控制人", "实控人")),
    ("shareholder", ("股东", "持股")),
    ("director", ("董事长", "董事")),
    ("senior_executive", ("高级管理人员", "财务负责人", "副总经理", "总经理")),
)
_ROLE_TYPE_BY_TERM = {
    term: relationship_type for relationship_type, terms in _ROLE_TERMS for term in terms
}
_ROLE_PATTERN_TEXT = "|".join(
    re.escape(term)
    for _relationship_type, terms in _ROLE_TERMS
    for term in sorted(terms, key=len, reverse=True)
)
# 两类句式均要求姓名与明确角色词紧邻：既支持“法定代表人为张三”，也支持“张三担任法定
# 代表人”。不用模糊 NER，宁可漏掉复杂句式，也不把机构名称、活动描述或模型猜测当成人员。
_ROLE_FIRST_PATTERN = re.compile(
    rf"(?P<role>{_ROLE_PATTERN_TEXT})(?:人员)?\s*(?:为|是|：|:|由)?\s*(?P<person>[\u4e00-\u9fff]{{2,4}})(?:先生|女士)?"
)
_PERSON_FIRST_PATTERN = re.compile(
    rf"(?P<person>[\u4e00-\u9fff]{{2,4}})(?:先生|女士)?\s*(?:系|为|担任|任职为|作为|出任)?\s*(?P<role>{_ROLE_PATTERN_TEXT})"
)
_XUZHOU_ASSOCIATION_TERMS = ("徐州籍", "祖籍徐州", "徐州人", "出生于徐州", "成长于徐州", "徐州关联")
# 这些常见连接词或泛称若被正则误吞，必须拒绝，避免把“包括张三”“该公司”等误作姓名。
_INVALID_PERSON_CANDIDATES = frozenset(
    {
        "包括",
        "其中",
        "公司",
        "企业",
        "机构",
        "团队",
        "基金",
        "本公司",
        "该公司",
        "负责人",
        "法定代表",
    }
)


def _utc_now() -> str:
    """返回服务器生成的 UTC 时间，草稿与复核均不接受页面或客户端时间。"""

    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, payload: dict[str, object]) -> str:
    """按已签名证据、受控角色与姓名生成稳定 ID，重复任务不会覆盖或重复追加草稿。"""

    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}-{hashlib.sha256(serialized.encode('utf-8')).hexdigest()[:16]}"


def _stable_json(value: object) -> str:
    """确定性序列化台账字段，供写入前逐字发现任何越权修改。"""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _required_text(value: object, field_name: str, *, maximum: int) -> str:
    """校验服务器草稿 ID、复核结论、备注和认证身份的有限文本，不接收来源事实。"""

    if not isinstance(value, str) or not (normalized := value.strip()):
        raise ValueError(f"{field_name}不能为空")
    if len(normalized) > maximum:
        raise ValueError(f"{field_name}不能超过{maximum}个字符")
    return normalized


def _required_server_draft_id(value: object) -> str:
    """仅接受服务器生成的关系草稿 ID，拒绝 URL、路径或客户端草稿对象进入复核。"""

    draft_id = _required_text(value, "relationship_draft_id", maximum=120)
    if any(character not in _VALID_SERVER_ID_CHARACTERS for character in draft_id):
        raise ValueError("relationship_draft_id必须是有效的服务器关系草稿 ID")
    return draft_id


def _relationship_drafts(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    """读取独立关键人员草稿列表；结构异常即失败关闭，不重建或覆盖历史草稿。"""

    drafts = ledger.get("relationship_drafts")
    if not isinstance(drafts, list):
        raise ValueError("招商线索台账中的 relationship_drafts 必须是列表")
    return drafts


def _relationship_draft_reviews(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    """读取关系草稿的只追加复核审计；异常时停止，避免伪造或覆盖责任记录。"""

    reviews = ledger.get("relationship_draft_reviews")
    if not isinstance(reviews, list):
        raise ValueError("招商线索台账中的 relationship_draft_reviews 必须是列表")
    return reviews


def _protected_records_snapshot(
    ledger: dict[str, Any], *, writable_field: str
) -> dict[str, str]:
    """保存本操作绝不能改写的整本台账快照，仅排除其唯一允许追加的列表。

    输入为已完成完整性验证的内存台账和本函数允许追加的一个字段；输出为其他所有字段的
    稳定序列化快照。自动抽取只能追加 ``relationship_drafts``，人工判断只能追加
    ``relationship_draft_reviews``；因此来源 URL、原始摘录、采集时间、证据 HMAC、评分、
    线索、图谱、来源规则及任何既有审计一旦被意外触碰都会被拒绝保存。
    """

    if writable_field not in {"relationship_drafts", "relationship_draft_reviews"}:
        raise ValueError("关系草稿操作范围不合法")
    return {
        field_name: _stable_json(value)
        for field_name, value in ledger.items()
        if field_name != writable_field
    }


def _assert_protected_records_unchanged(
    before: dict[str, str], ledger: dict[str, Any]
) -> None:
    """拒绝草稿生成或复核改写其允许列表之外的任何台账字段。"""

    current = {field_name: _stable_json(ledger.get(field_name)) for field_name in before}
    if current != before:
        raise RuntimeError("关键人员关系草稿操作不得修改原始证据链或既有业务记录")


def _normalized_text(value: object) -> str:
    """规范化已签名摘录用于字符串校验，不做模型补全、分词推断或事实改写。"""

    return "".join(value.split()) if isinstance(value, str) else ""


def _valid_person_name(value: str) -> bool:
    """保守筛除明显不是姓名的正则候选，无法确认时宁可不生成草稿。"""

    return (
        2 <= len(value) <= 4
        and value not in _INVALID_PERSON_CANDIDATES
        and not any(value.startswith(prefix) for prefix in ("包括", "其中", "该", "本", "公司", "企业"))
    )


def _sentence_role_matches(sentence: str) -> list[dict[str, str]]:
    """在单句原始摘录中提取明确“姓名 + 角色”组合，输出仅为待人工确认候选。

    输入是已签名证据中的一整句文本；输出为规范关系类型、原始角色词和姓名的去重列表。
    规则不跨句拼接、不从标题/搜索摘要补全、不接受人工提供的人名，故不能把模糊共现或
    网络传言变成关系事实。该函数纯读取字符串，不写台账或修改证据链。
    """

    matches: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for pattern in (_ROLE_FIRST_PATTERN, _PERSON_FIRST_PATTERN):
        for match in pattern.finditer(sentence):
            person = match.group("person")
            role_term = match.group("role")
            relationship_type = _ROLE_TYPE_BY_TERM.get(role_term)
            if relationship_type is None or not _valid_person_name(person):
                continue
            key = (person, relationship_type, role_term)
            if key in seen:
                continue
            seen.add(key)
            matches.append(
                {
                    "person_display_name": person,
                    "relationship_type": relationship_type,
                    "matched_role_term": role_term,
                }
            )
    return matches


def _xuzhou_association_term(sentence: str, *, person_display_name: str) -> str | None:
    """只在姓名紧邻受控徐州措辞时返回关联词，否则保持未知。

    输入为已签名摘录的一句原文和已抽取的人名；输出为原文中的一个徐州关联词或 ``None``。
    这不会推断籍贯、住址或成长经历，更不会复制这些敏感细节；调用者仅据此提供宽泛的
    ``xuzhou_related`` 草稿候选，人工登记图谱时仍须由图谱核心再次验证原始证据。
    """

    normalized_sentence = _normalized_text(sentence)
    for term in _XUZHOU_ASSOCIATION_TERMS:
        # “张三为徐州籍创业者”或“徐州籍张三”才可作为该人员的宽泛关联候选；同句出现
        # 另一名徐州人士时不应给当前人员贴标签，信息不足一律保留 unknown。
        if re.search(
            rf"{re.escape(person_display_name)}.{{0,8}}{re.escape(term)}|"
            rf"{re.escape(term)}.{{0,8}}{re.escape(person_display_name)}",
            normalized_sentence,
        ):
            return term
    return None


def _lead_by_id(ledger: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """建立既有招商线索索引；线索结构异常时不猜测组织名称或继续抽取。"""

    leads = ledger.get("leads")
    if not isinstance(leads, list):
        raise ValueError("招商线索台账中的 leads 必须是列表")
    return {
        lead_id: lead
        for lead in leads
        if isinstance(lead, dict)
        and isinstance((lead_id := lead.get("lead_id")), str)
        and lead_id
    }


def _public_draft(
    draft: dict[str, Any],
    reviews: list[dict[str, Any]],
    graph_registration_audits: list[dict[str, Any]],
) -> dict[str, object]:
    """返回可人工审查的草稿和追加复核历史，不暴露快照路径或任何私人信息。

    输入是已签名台账中的草稿和同草稿只追加审计；输出是固定字段的公开业务视图。视图仅
    引用 ``evidence_id`` 并显示其已保存摘录中的匹配句，不能由读取接口改写原始 URL、
    采集时间、摘录、评分或线索状态，也不包含个人联系方式、资产或投资意愿判断。
    """

    fields = (
        "relationship_draft_id",
        "lead_id",
        "evidence_id",
        "organization_name",
        "person_display_name",
        "relationship_type",
        "person_locality_candidate",
        "matched_role_term",
        "matched_xuzhou_term",
        "matched_excerpt",
        "extractor_version",
        "created_at",
        "state",
        "operation_scope",
    )
    for field_name in fields:
        if field_name not in draft:
            raise ValueError(f"关键人员关系草稿缺少字段：{field_name}")
    history = [
        {
            field_name: review[field_name]
            for field_name in (
                "review_id",
                "relationship_draft_id",
                "reviewed_at",
                "reviewed_by_user_id",
                "old_state",
                "new_state",
                "review_note",
                "operation_scope",
            )
        }
        for review in reviews
    ]
    history.sort(key=lambda item: str(item["reviewed_at"]), reverse=True)
    result = {field_name: draft[field_name] for field_name in fields}
    result["review_state"] = str(history[0]["new_state"]) if history else RELATIONSHIP_DRAFT_STATE
    result["review_history"] = history[:50]
    registrations = [
        {
            "registered_at": audit["operated_at"],
            "registered_by_user_id": audit["operated_by_user_id"],
            "operation_scope": audit["operation_scope"],
        }
        for audit in graph_registration_audits
        if audit.get("action") == "approved_relationship_draft_registered_to_graph"
        and audit.get("target_kind") == "relationship_draft"
        and audit.get("target_id") == draft["relationship_draft_id"]
        and isinstance(audit.get("operated_at"), str)
        and isinstance(audit.get("operated_by_user_id"), str)
        and isinstance(audit.get("operation_scope"), str)
    ]
    registrations.sort(key=lambda item: str(item["registered_at"]), reverse=True)
    result["graph_registration_state"] = (
        "registered_pending_relationship_review" if registrations else "not_registered"
    )
    result["graph_registration_history"] = registrations[:10]
    result["graph_registration_caveat"] = (
        "本草稿只有经普通登录用户明确执行“登记到图谱”后才会创建待复核关系边；登记仍会"
        "再次校验同一条已签名证据，关系边初始不是有效关系，不产生个人招商结论。"
    )
    return result


def generate_relationship_drafts_from_signed_evidence(workspace_dir: Path) -> dict[str, object]:
    """从已签名正式证据追加关键人员关系草稿，不联网、不自动建图、不改变招商状态。

    输入是平台隔离工作区；输出为本轮新草稿、重复/结构异常/配额跳过数量和台账路径。函数
    只接受每条 evidence 所属既有 lead，且要求该 lead 的组织名称、人员姓名和角色词同时
    出现在同一句原始摘录中；徐州关联也必须同句明示。保存前后逐字核验整本台账（排除唯一
    可追加的草稿列表），确保不改 URL、原始摘录、采集时间、HMAC、评分、线索、图谱或来源
    配置。输出草稿始终等待用户复核，不能创建图谱边、推进线索、标红或推断个人意愿。
    """

    started_at = _utc_now()
    ledger = load_ledger(workspace_dir)
    protected_before = _protected_records_snapshot(ledger, writable_field="relationship_drafts")
    drafts = _relationship_drafts(ledger)
    leads_by_id = _lead_by_id(ledger)
    evidence_records = ledger.get("evidence")
    if not isinstance(evidence_records, list):
        raise ValueError("招商线索台账中的 evidence 必须是列表")
    existing_draft_ids = {
        draft.get("relationship_draft_id")
        for draft in drafts
        if isinstance(draft, dict) and isinstance(draft.get("relationship_draft_id"), str)
    }
    generated: list[dict[str, object]] = []
    skipped_duplicates = 0
    skipped_invalid_evidence = 0
    skipped_quota = 0

    for evidence in evidence_records:
        if not isinstance(evidence, dict):
            skipped_invalid_evidence += 1
            continue
        evidence_id = evidence.get("evidence_id")
        lead_id = evidence.get("lead_id")
        excerpt = evidence.get("excerpt")
        lead = leads_by_id.get(lead_id) if isinstance(lead_id, str) else None
        organization_name = lead.get("entity_name") if isinstance(lead, dict) else None
        if not all(isinstance(value, str) and value.strip() for value in (evidence_id, lead_id, excerpt, organization_name)):
            skipped_invalid_evidence += 1
            continue
        normalized_organization = _normalized_text(organization_name)
        if not normalized_organization:
            skipped_invalid_evidence += 1
            continue
        for sentence_match in _SENTENCE_PATTERN.finditer(excerpt):
            sentence = sentence_match.group(0).strip()
            if not sentence or len(sentence) > MAX_MATCHED_EXCERPT_CHARS:
                continue
            # 关系登记核心要求双方名称同现；草稿阶段采用同一严格前提，避免产生无法登记的
            # 人员候选，也避免只凭文章其他段落的组织名把人名强行关联到目标线索。
            if normalized_organization not in _normalized_text(sentence):
                continue
            for role_match in _sentence_role_matches(sentence):
                person_display_name = role_match["person_display_name"]
                relationship_type = role_match["relationship_type"]
                draft_id = _stable_id(
                    "relationship-draft",
                    {
                        "lead_id": lead_id,
                        "evidence_id": evidence_id,
                        "organization_name": normalized_organization,
                        "person_display_name": person_display_name,
                        "relationship_type": relationship_type,
                        "extractor_version": RELATIONSHIP_DRAFT_EXTRACTOR_VERSION,
                    },
                )
                if draft_id in existing_draft_ids:
                    skipped_duplicates += 1
                    continue
                if len(drafts) + len(generated) >= MAX_RELATIONSHIP_DRAFT_RECORDS or len(generated) >= MAX_RELATIONSHIP_DRAFTS_PER_RUN:
                    skipped_quota += 1
                    continue
                xuzhou_term = _xuzhou_association_term(
                    sentence, person_display_name=person_display_name
                )
                generated.append(
                    {
                        "relationship_draft_id": draft_id,
                        "lead_id": lead_id,
                        "evidence_id": evidence_id,
                        "organization_name": organization_name,
                        "person_display_name": person_display_name,
                        "relationship_type": relationship_type,
                        "person_locality_candidate": "xuzhou_related" if xuzhou_term else "unknown",
                        "matched_role_term": role_match["matched_role_term"],
                        "matched_xuzhou_term": xuzhou_term,
                        "matched_excerpt": sentence,
                        "extractor_version": RELATIONSHIP_DRAFT_EXTRACTOR_VERSION,
                        "created_at": _utc_now(),
                        "state": RELATIONSHIP_DRAFT_STATE,
                        "operation_scope": RELATIONSHIP_DRAFT_OPERATION_SCOPE,
                    }
                )
                existing_draft_ids.add(draft_id)

    _assert_protected_records_unchanged(protected_before, ledger)
    if generated:
        drafts.extend(generated)
        _assert_protected_records_unchanged(protected_before, ledger)
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
            "draft_record_quota": MAX_RELATIONSHIP_DRAFT_RECORDS,
            "status": "completed",
            "operation_scope": RELATIONSHIP_DRAFT_OPERATION_SCOPE,
        },
        "drafts": [_public_draft(draft, [], []) for draft in generated],
        "ledger_path": str(saved_path),
    }


def list_relationship_drafts(
    workspace_dir: Path,
    *,
    lead_id: str | None = None,
    review_state: str | None = None,
    limit: int = 50,
) -> dict[str, object]:
    """只读列出关键人员关系草稿及其追加复核历史，不访问网页或改变业务数据。

    输入是当前工作区、可选既有线索 ID、可选派生复核状态和有限条数；输出是按最新生成
    时间倒序的草稿视图。函数不接收人员资料、URL、摘录、用户 ID 或写入字段，不能用于
    创建关系、更新个人标签或修改原始证据链；认证与工作区归属由 API 层负责。
    """

    normalized_lead_id = (
        _required_text(lead_id, "lead_id", maximum=100) if lead_id is not None else None
    )
    normalized_review_state = (
        _normalize_review_state(review_state) if review_state is not None else None
    )
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_RELATIONSHIP_DRAFT_LIST_LIMIT:
        raise ValueError(f"limit必须在1到{MAX_RELATIONSHIP_DRAFT_LIST_LIMIT}之间")
    ledger = load_ledger(workspace_dir)
    drafts = _relationship_drafts(ledger)
    reviews = _relationship_draft_reviews(ledger)
    graph_audit = ledger.get("relationship_graph_audit")
    if not isinstance(graph_audit, list):
        raise ValueError("招商线索台账中的 relationship_graph_audit 必须是列表")
    reviews_by_draft: dict[str, list[dict[str, Any]]] = {}
    for review in reviews:
        if isinstance(review, dict) and isinstance(review.get("relationship_draft_id"), str):
            reviews_by_draft.setdefault(review["relationship_draft_id"], []).append(review)
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
            reviews_by_draft.get(str(draft.get("relationship_draft_id")), []),
            [item for item in graph_audit if isinstance(item, dict)],
        )
        if normalized_review_state is not None and public_draft["review_state"] != normalized_review_state:
            continue
        result.append(public_draft)
        if len(result) >= limit:
            break
    return {
        "relationship_drafts": result,
        "count": len(result),
        "operation_scope": "authenticated_user_readonly_key_person_relationship_drafts",
        "ledger_path": str(ledger_path(workspace_dir)),
    }


def _normalize_review_state(value: object) -> str:
    """将中文或英文关系草稿复核结论归一为受控状态，不接受自动建图指令。"""

    raw_state = _required_text(value, "decision", maximum=100).casefold()
    normalized_state = RELATIONSHIP_DRAFT_REVIEW_STATE_ALIASES.get(raw_state, raw_state)
    if normalized_state not in RELATIONSHIP_DRAFT_REVIEW_STATES:
        raise ValueError("decision只能是可登记图谱、待补证、观察或无效")
    return normalized_state


def review_relationship_draft(
    workspace_dir: Path,
    *,
    relationship_draft_id: str,
    decision: str,
    review_note: str,
    authenticated_user_id: str,
) -> dict[str, object]:
    """由普通登录用户为关系草稿追加业务判断，身份只能来自服务器认证层。

    输入是服务器草稿 ID、受控结论、复核备注和认证中间件提供的用户 ID；输出为新增审计及
    当前草稿视图。普通用户不需要管理员角色；HTTP 层不允许客户端提交用户 ID。复核不能
    修改草稿字段、人员/组织名称、角色、证据 ID、URL、原始摘录、采集时间、评分或线索。
    “可登记图谱”只表示人工允许下一步通过既有图谱接口再次核验，不会自动创建节点或边。
    """

    normalized_draft_id = _required_server_draft_id(relationship_draft_id)
    normalized_decision = _normalize_review_state(decision)
    normalized_note = _required_text(review_note, "review_note", maximum=2_000)
    normalized_user_id = _required_text(
        authenticated_user_id, "authenticated_user_id", maximum=100
    )
    ledger = load_ledger(workspace_dir)
    protected_before = _protected_records_snapshot(
        ledger, writable_field="relationship_draft_reviews"
    )
    drafts = _relationship_drafts(ledger)
    reviews = _relationship_draft_reviews(ledger)
    draft = next(
        (
            item
            for item in drafts
            if isinstance(item, dict) and item.get("relationship_draft_id") == normalized_draft_id
        ),
        None,
    )
    if draft is None:
        raise ValueError("未找到关键人员关系草稿")
    if draft.get("state") != RELATIONSHIP_DRAFT_STATE:
        raise ValueError("该关系草稿不是等待用户复核状态")
    prior_reviews = [
        review
        for review in reviews
        if isinstance(review, dict) and review.get("relationship_draft_id") == normalized_draft_id
    ]
    prior_reviews.sort(key=lambda item: str(item.get("reviewed_at", "")), reverse=True)
    old_state = str(prior_reviews[0]["new_state"]) if prior_reviews else RELATIONSHIP_DRAFT_STATE
    reviewed_at = _utc_now()
    review = {
        "review_id": _stable_id(
            "relationship-draft-review",
            {
                "relationship_draft_id": normalized_draft_id,
                "reviewed_at": reviewed_at,
                "authenticated_user_id": normalized_user_id,
                "new_state": normalized_decision,
            },
        ),
        "relationship_draft_id": normalized_draft_id,
        "reviewed_at": reviewed_at,
        "reviewed_by_user_id": normalized_user_id,
        "old_state": old_state,
        "new_state": normalized_decision,
        "review_note": normalized_note,
        "operation_scope": RELATIONSHIP_DRAFT_REVIEW_OPERATION_SCOPE,
    }
    reviews.append(review)
    _assert_protected_records_unchanged(protected_before, ledger)
    saved_path = save_ledger(workspace_dir, ledger)
    return {
        "relationship_draft": _public_draft(draft, [*prior_reviews, review], []),
        "review": review,
        "ledger_path": str(saved_path),
    }
