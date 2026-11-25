"""徐州招商证据化关系图谱的最小数据服务。

本模块只登记已经由台账证据支持的组织节点和关系边，服务于后续人工分析徐州本地
企业、园区、高校、基金与目标企业的关系。它不联网、不自动推断关系、不生成招商
结论，也不负责前端可视化或批量采集。
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .core import has_open_enterprise_risk_alert
from .storage import ledger_path, load_ledger, save_ledger


RELATIONSHIP_NODE_KINDS = frozenset(
    {
        "enterprise",
        "fund",
        "startup_team",
        "university",
        "park",
        "government_org",
        "research_institute",
        "industry_association",
        # 关键人员节点只保存公开披露的姓名及其与组织的证据化角色关系。它不是个人招商
        # 线索，禁止承载联系方式、住址、资产、募资能力或其他与本项目无关的个人信息。
        "person",
        "other",
    }
)
RELATIONSHIP_NODE_KIND_ALIASES = {
    "企业": "enterprise",
    "基金": "fund",
    # 创业团队可作为尚未公司化的早期主体登记；登记仍必须引用正式证据，不能根据名称、
    # 搜索摘要或模型猜测自动生成节点，更不会改写该证据的 URL、摘录或采集时间。
    "创业团队": "startup_team",
    "团队": "startup_team",
    "高校": "university",
    "大学": "university",
    "园区": "park",
    "政府机构": "government_org",
    "科研院所": "research_institute",
    "行业协会": "industry_association",
    "关键人员": "person",
    "个人": "person",
    "自然人": "person",
    "其他": "other",
}
RELATIONSHIP_LOCALITIES = frozenset(
    {"xuzhou_local", "xuzhou_related", "target_or_external", "unknown"}
)
RELATIONSHIP_LOCALITY_ALIASES = {
    "徐州本地": "xuzhou_local",
    "徐州": "xuzhou_local",
    # 对公开关键人员只登记可由证据支持的宽泛徐州关联，不把籍贯、住址或成长地等可
    # 识别细节复制进图谱字段；详细事实仍只能沿 evidence_id 回看原始公开摘录。
    "徐州关联": "xuzhou_related",
    "乡土关联": "xuzhou_related",
    "目标或外地": "target_or_external",
    "外地": "target_or_external",
    "未知": "unknown",
}
RELATIONSHIP_TYPES = frozenset(
    {
        "upstream_supplier",
        "downstream_customer",
        "industry_chain",
        "strategic_cooperation",
        "investment",
        "research_collaboration",
        "park_service",
        "business_customer",
        "legal_representative",
        "founder",
        "actual_controller",
        "shareholder",
        "director",
        "senior_executive",
    }
)
RELATIONSHIP_TYPE_ALIASES = {
    "上游供应": "upstream_supplier",
    "下游客户": "downstream_customer",
    "产业链": "industry_chain",
    "战略合作": "strategic_cooperation",
    "投资": "investment",
    "产学研合作": "research_collaboration",
    "园区服务": "park_service",
    "业务客户": "business_customer",
    "法定代表人": "legal_representative",
    "创始人": "founder",
    "实际控制人": "actual_controller",
    "股东": "shareholder",
    "董事": "director",
    "高级管理人员": "senior_executive",
    "高管": "senior_executive",
}
# 关键人员关系仅描述公开披露的组织角色，绝不表达个人资产、投资能力、联系方式或
# “愿意投资”的推断。其方向固定为 ``person -> organization``，使读者能明确看到
# 是哪位公开关键人员与哪一组织之间的已证据化角色，而不是把人员当作可招揽的资金池。
KEY_PERSON_RELATIONSHIP_TYPES = frozenset(
    {
        "legal_representative",
        "founder",
        "actual_controller",
        "shareholder",
        "director",
        "senior_executive",
    }
)
# 这三个类型才具有本模块可直接展示的产业链语义；其他关系边仍会被完整返回，但不能
# 被渲染成上下游。该分类只解释人工登记的受控 relationship_type，不从企业简介、模型
# 摘要或节点名称反推真实交易链路。
INDUSTRY_CHAIN_RELATIONSHIP_TYPES = frozenset(
    {"upstream_supplier", "downstream_customer", "industry_chain"}
)
_RELATIONSHIP_ORIENTATIONS: dict[str, dict[str, str]] = {
    "upstream_supplier": {
        "relationship_category": "industry_chain",
        "business_flow_direction": "from_node_to_to_node",
        "from_node_role": "upstream_supplier",
        "to_node_role": "downstream_recipient",
        "direction_basis": "仅按人工登记的上游供应关系类型展示，不额外推断产品、金额或持续合作。",
    },
    "downstream_customer": {
        "relationship_category": "industry_chain",
        "business_flow_direction": "to_node_to_from_node",
        "from_node_role": "downstream_customer",
        "to_node_role": "upstream_provider",
        "direction_basis": "仅按人工登记的下游客户关系类型展示，不额外推断采购金额、订单状态或持续合作。",
    },
    "industry_chain": {
        "relationship_category": "industry_chain",
        "business_flow_direction": "not_declared",
        "from_node_role": "industry_chain_participant",
        "to_node_role": "industry_chain_participant",
        "direction_basis": "原始登记只确认产业链关联，未登记上下游方向；不得由本模块补写方向。",
    },
}
RELATIONSHIP_EVIDENCE_TERMS: dict[str, tuple[str, ...]] = {
    "upstream_supplier": ("供应", "上游", "供应商", "供货"),
    "downstream_customer": ("下游", "客户", "采购", "订单", "供货"),
    "industry_chain": ("产业链", "上游", "下游", "供应", "配套"),
    "strategic_cooperation": ("合作", "签署", "协议", "联合"),
    "investment": ("投资", "领投", "出资", "被投"),
    "research_collaboration": ("产学研", "联合研发", "合作研究", "共建"),
    "park_service": ("园区", "入驻", "载体", "服务", "落地"),
    "business_customer": ("客户", "采购", "订单", "供应", "销售"),
    # 每个角色都有独立关键词，避免“张三出席活动”之类只提到姓名的摘录被误登记为
    # 法定代表人、实控人或股东等具有法律/商业含义的关系。
    "legal_representative": ("法定代表人", "法定代表"),
    "founder": ("创始人", "联合创始人"),
    "actual_controller": ("实际控制人", "实控人"),
    "shareholder": ("股东", "持股"),
    "director": ("董事",),
    "senior_executive": ("高级管理人员", "总经理", "副总经理", "财务负责人"),
}
RELATIONSHIP_EDGE_REVIEW_STATES = frozenset(
    {"pending_user_review", "valid", "need_evidence", "invalid"}
)
MANUAL_RELATIONSHIP_EDGE_REVIEW_STATES = frozenset({"valid", "need_evidence", "invalid"})
RELATIONSHIP_EDGE_REVIEW_STATE_ALIASES = {
    "等待用户复核": "pending_user_review",
    "有效": "valid",
    "待补证": "need_evidence",
    "无效": "invalid",
}
# 全局人工待办只展示有限数量的关系边，避免长期图谱被无界读出。调用方可以减小 limit，
# 不能扩大至此上限以上；完整图谱仍由专用只读工具按需查看。
DEFAULT_RELATIONSHIP_REVIEW_QUEUE_LIMIT = 50
MAX_RELATIONSHIP_REVIEW_QUEUE_LIMIT = 200
# 图谱登记只能新增自己的三类字段；这些既有字段在写入前后必须逐字相同，确保任何
# 人工关系判断均不能借本模块修改原始证据、线索、采集记录、评分或来源目录。
_PROTECTED_LEDGER_FIELDS = (
    "leads",
    "evidence",
    "scoring_rule_sets",
    "score_snapshots",
    "outcome_feedback",
    "discovery_runs",
    "discovery_candidates",
    "candidate_observations",
    "source_health",
    "monitoring_runs",
    "source_feed_runs",
    "trusted_sources",
    "trusted_source_audit",
    # 来源配方学习配置、建议与审批不是图谱业务的可写范围；把它们纳入快照可防止未来
    # 图谱改动意外影响自动发现顺序或审批审计。它们也不能成为关系证据或招商结论。
    "source_recipe_rule_sets",
    "source_recipe_learning_proposals",
    "source_recipe_learning_reviews",
    # 自动关键人员草稿与其人工判断独立于图谱登记。图谱操作只能读取既有正式 evidence，
    # 绝不能覆盖草稿、冒充复核结论或把待确认内容自动升级成节点/关系边。
    "relationship_drafts",
    "relationship_draft_reviews",
    # 除补证登记函数本身外，其他关系图谱操作也不得触及既有补证历史。
    "relationship_graph_edge_evidence_supplements",
)
_EDGE_IMMUTABLE_FIELDS = (
    "edge_id",
    "from_node_id",
    "to_node_id",
    "relationship_type",
    "evidence_ids",
    "registration_reason",
    "created_at",
    "created_by_user_id",
    "operation_scope",
)


def _utc_now() -> str:
    """返回服务器生成的 UTC 审计时间，不信任调用方提供的客户端时钟。"""
    return datetime.now(timezone.utc).isoformat()


def _required_text(value: object, field_name: str, *, max_length: int) -> str:
    """校验图谱业务文本并去除首尾空白，拒绝空值、非文本和异常长内容。"""
    if not isinstance(value, str) or not (text := value.strip()):
        raise ValueError(f"{field_name}不能为空")
    if len(text) > max_length:
        raise ValueError(f"{field_name}不能超过{max_length}个字符")
    return text


def _canonical(
    value: object,
    field_name: str,
    allowed_values: frozenset[str],
    aliases: dict[str, str],
) -> str:
    """将中英文图谱枚举归一化为受控值，避免关系类型无法稳定筛选。"""
    raw_value = _required_text(value, field_name, max_length=100).casefold()
    normalized_value = aliases.get(raw_value, raw_value)
    if normalized_value not in allowed_values:
        accepted = "、".join(sorted(allowed_values))
        raise ValueError(f"{field_name}不合法，可选值为：{accepted}")
    return normalized_value


def _stable_id(prefix: str, payload: dict[str, object]) -> str:
    """按稳定业务字段生成服务器端图谱 ID，用于去重而不接收客户端指定 ID。"""
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}-{hashlib.sha256(serialized.encode('utf-8')).hexdigest()[:16]}"


def _normalize_evidence_ids(value: object, *, field_name: str) -> list[str]:
    """校验并去重既有证据 ID 引用，不接受页面正文、URL 或客户端证据对象。"""
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field_name}必须是非空列表")
    if len(value) > 20:
        raise ValueError(f"{field_name}最多包含20条证据")
    evidence_ids: list[str] = []
    for raw_evidence_id in value:
        evidence_id = _required_text(raw_evidence_id, field_name, max_length=100)
        if evidence_id not in evidence_ids:
            evidence_ids.append(evidence_id)
    return evidence_ids


def _snapshot_protected_fields(
    ledger: dict[str, Any],
    *,
    allow_evidence_supplement_append: bool = False,
) -> dict[str, str]:
    """序列化受保护台账数据，供写入后强制确认其完全未被关系操作改写。

    默认输出包含原始证据链、其他业务记录及既有关系补证历史的快照。只有内部的补证
    追加函数可以显式跳过补证列表自身，并在该函数中额外校验它只是末尾新增一条记录；
    任何调用都不能跳过 ``evidence``、线索、评分、来源目录或采集历史的防篡改校验。
    """
    return {
        field_name: json.dumps(ledger[field_name], ensure_ascii=False, sort_keys=True)
        for field_name in _PROTECTED_LEDGER_FIELDS
        if not (
            allow_evidence_supplement_append
            and field_name == "relationship_graph_edge_evidence_supplements"
        )
    }


def _assert_protected_fields_unchanged(before: dict[str, str], ledger: dict[str, Any]) -> None:
    """拒绝任何触及原始证据链或其他既有业务记录的图谱写入副作用。"""
    for field_name, expected_value in before.items():
        current_value = json.dumps(ledger[field_name], ensure_ascii=False, sort_keys=True)
        if current_value != expected_value:
            raise RuntimeError("关系图谱登记不得修改原始证据链或既有业务记录")


def _graph_lists(
    ledger: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """读取已兼容补全的图谱列表，结构异常时拒绝猜测、重建或覆盖历史数据。"""
    nodes = ledger.get("relationship_graph_nodes")
    edges = ledger.get("relationship_graph_edges")
    audit = ledger.get("relationship_graph_audit")
    if not isinstance(nodes, list) or not isinstance(edges, list) or not isinstance(audit, list):
        raise ValueError("招商线索台账中的关系图谱字段必须是列表")
    return nodes, edges, audit


def _edge_evidence_supplements(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    """读取关系边补证追加列表，结构异常时拒绝猜测、重建或覆盖历史记录。"""
    supplements = ledger.get("relationship_graph_edge_evidence_supplements")
    if not isinstance(supplements, list):
        raise ValueError("招商线索台账中的关系边补证字段必须是列表")
    return supplements


def _evidence_by_id(ledger: dict[str, Any], evidence_ids: list[str]) -> dict[str, dict[str, Any]]:
    """确认所有引用均为台账既有证据，禁止图谱操作凭空构造或替换证据。"""
    evidence_records = ledger.get("evidence")
    if not isinstance(evidence_records, list):
        raise ValueError("招商线索台账中的 evidence 必须是列表")
    evidence_by_id = {
        record.get("evidence_id"): record
        for record in evidence_records
        if isinstance(record, dict) and isinstance(record.get("evidence_id"), str)
    }
    missing_ids = [evidence_id for evidence_id in evidence_ids if evidence_id not in evidence_by_id]
    if missing_ids:
        raise ValueError(f"未找到既有证据：{'、'.join(missing_ids)}")
    return {evidence_id: evidence_by_id[evidence_id] for evidence_id in evidence_ids}


def _normalized_evidence_text(value: object) -> str:
    """规范化证据摘录用于精确包含校验，不进行模型推断或事实补全。"""
    return " ".join(value.split()).casefold() if isinstance(value, str) else ""


def _evidence_supports_node_name(evidence: dict[str, Any], display_name: str) -> bool:
    """确认一条既有证据摘录明确出现节点名称，避免任意证据被借作节点背书。"""
    return _normalized_evidence_text(display_name) in _normalized_evidence_text(
        evidence.get("excerpt")
    )


def _assert_relation_evidence_semantics(
    evidence_by_id: dict[str, dict[str, Any]],
    *,
    from_display_name: str,
    to_display_name: str,
    relationship_type: str,
) -> None:
    """确认至少一条关系证据摘录同时出现两端名称和关系类型关键词。

    输入是已验证存在的证据记录、两个节点名称和受控关系类型；没有任何证据摘录同时
    包含双方名称及该类型的固定关键词时直接拒绝。该校验不使用模型猜测“可能有关”，
    只防止把仅属于目标企业但未提及本地组织或关系的证据误连成图谱事实；即使通过，
    关系仍须保持人工复核状态，不能自动成为招商结论。
    """
    from_name = _normalized_evidence_text(from_display_name)
    to_name = _normalized_evidence_text(to_display_name)
    terms = RELATIONSHIP_EVIDENCE_TERMS[relationship_type]
    if not from_name or not to_name:
        raise ValueError("图谱关系节点名称不能为空")
    for evidence in evidence_by_id.values():
        excerpt = _normalized_evidence_text(evidence.get("excerpt"))
        if from_name in excerpt and to_name in excerpt and any(term in excerpt for term in terms):
            return
    raise ValueError("关系边必须至少引用一条同时包含双方名称和关系关键词的原始证据摘录")


def _assert_public_person_node_constraints(
    *,
    node_kind: str,
    locality: str,
    linked_lead_id: str | None,
) -> None:
    """限制公开关键人员节点的业务边界，避免把个人信息或个人招商对象写入台账。

    输入是已归一化的节点类型、宽泛本地关联分类与可选招商线索 ID；无返回值，违反
    边界时抛出异常。只有 ``person`` 节点受本规则约束：人员不能绑定为招商线索，也不
    能使用“徐州本地”这种组织所在地语义，只能在有公开证据时使用“徐州关联”或保留
    为外地/未知。节点接口的固定字段和 ``extra=forbid`` 已阻止单独写入联系方式、住址、
    资产或募资能力；本函数进一步阻止把个人伪装成待招商企业。它不读取或修改原始证据，
    URL、摘录、采集时间、评分依据均仍由既有台账不可变地保存。
    """
    if node_kind != "person":
        return
    if linked_lead_id is not None:
        raise ValueError("公开关键人员节点不能绑定为个人招商线索")
    if locality == "xuzhou_local":
        raise ValueError("公开关键人员请使用“徐州关联”而非“徐州本地”")


def _assert_public_person_xuzhou_association_evidence(
    evidence_by_id: dict[str, dict[str, Any]],
    *,
    display_name: str,
    locality: str,
) -> None:
    """要求“徐州关联”的公开关键人员有同一原始摘录中的明确关联措辞。

    输入是已确认存在的证据记录、人员姓名和已归一化本地关联分类；非“徐州关联”人员
    直接返回，其他情况必须有一条原始摘录同时出现姓名与受控徐州关联措辞，否则拒绝。
    这避免用户仅凭印象给个人贴乡土标签，也不把籍贯、出生地或住址复制到独立字段中。
    函数只读取不可变摘录进行字符串校验，不联网、不推断成长经历、不接收用户身份，且
    绝不修改 evidence、来源 URL、采集时间或任何评分/复核记录。
    """
    if locality != "xuzhou_related":
        return
    normalized_name = _normalized_evidence_text(display_name)
    xuzhou_terms = ("徐州籍", "祖籍徐州", "徐州人", "出生于徐州", "成长于徐州", "徐州关联")
    if any(
        normalized_name in _normalized_evidence_text(evidence.get("excerpt"))
        and any(term in _normalized_evidence_text(evidence.get("excerpt")) for term in xuzhou_terms)
        for evidence in evidence_by_id.values()
    ):
        return
    raise ValueError("公开关键人员标记“徐州关联”必须引用同时明确姓名和徐州关联的原始证据摘录")


def _assert_key_person_relationship_endpoints(
    *,
    from_node: dict[str, Any],
    to_node: dict[str, Any],
    relationship_type: str,
) -> None:
    """校验公开关键人员角色边的端点与方向，禁止把个人当资金对象或推断私密关系。

    输入为两个已登记节点及已归一化关系类型；普通产业关系直接返回，关键人员类型则
    必须是 ``person -> 非 person 组织``，且组织端必须已关联一条企业/基金/团队招商
    线索。这样“法人、股东、高管等纳入对象”的含义被限定为研判目标组织的公开决策
    关系，而不是向个人募资、汇集个人资金或建立个人招商线索。函数只验证既有节点
    字段，不接收客户端用户 ID、不进行管理员判断、不写入节点/边/证据，更不能改写
    原始来源 URL、摘录、采集时间和评分依据。
    """
    if relationship_type not in KEY_PERSON_RELATIONSHIP_TYPES:
        return
    from_kind = from_node.get("node_kind")
    to_kind = to_node.get("node_kind")
    if from_kind != "person" or not isinstance(to_kind, str) or to_kind == "person":
        raise ValueError("关键人员关系必须从公开关键人员节点指向已登记组织节点")
    if from_node.get("linked_lead_id") is not None:
        raise ValueError("公开关键人员节点不能关联个人招商线索")
    if not isinstance(to_node.get("linked_lead_id"), str):
        raise ValueError("关键人员关系的组织端必须关联既有招商线索")


def _append_audit(
    audit: list[dict[str, Any]],
    *,
    action: str,
    target_kind: str,
    target_id: str,
    authenticated_user_id: str,
    operated_at: str,
    operation_scope: str,
) -> None:
    """追加图谱登记审计，记录认证主体和操作范围但绝不包含可改写的证据正文。"""
    audit.append(
        {
            "audit_id": _stable_id(
                "relationship-graph-audit",
                {
                    "action": action,
                    "target_kind": target_kind,
                    "target_id": target_id,
                    "operated_at": operated_at,
                    "authenticated_user_id": authenticated_user_id,
                },
            ),
            "action": action,
            "target_kind": target_kind,
            "target_id": target_id,
            "operated_at": operated_at,
            "operated_by_user_id": authenticated_user_id,
            "operation_scope": operation_scope,
        }
    )


def _edge_immutable_snapshot(edge: dict[str, Any]) -> str:
    """提取关系边不得被人工复核改写的字段，供保存前后逐字比对。"""
    return json.dumps(
        {field_name: edge.get(field_name) for field_name in _EDGE_IMMUTABLE_FIELDS},
        ensure_ascii=False,
        sort_keys=True,
    )


def _ensure_edge_review_fields(edge: dict[str, Any]) -> None:
    """为旧版已登记关系补齐安全默认复核状态，不推断有效性也不接触证据引用。

    旧记录在本功能上线前没有复核字段时，唯一安全默认值是 ``pending_user_review``；
    函数只补充当前状态和空历史列表，绝不因为历史关系存在而自动标为有效、失效或
    待补证，也不会改变关系边的端点、类型、证据 ID 或登记信息。
    """
    if "review_state" not in edge:
        edge["review_state"] = "pending_user_review"
    if "review_history" not in edge:
        edge["review_history"] = []
    if (
        not isinstance(edge["review_state"], str)
        or edge["review_state"] not in RELATIONSHIP_EDGE_REVIEW_STATES
    ):
        raise ValueError("关系边的 review_state 不合法")
    if not isinstance(edge["review_history"], list):
        raise ValueError("关系边的 review_history 必须是列表")


def _edge_review_state_for_read(edge: dict[str, Any]) -> str:
    """读取关系边当前复核状态，旧记录缺字段时仅在内存中视为等待用户复核。

    输入是一条已存关系边，输出为受控状态值。为兼容本功能上线前的历史边，缺少
    ``review_state`` 时返回安全默认 ``pending_user_review``，但绝不写回台账、不创建
    审计，也不把关系自动判为有效；字段存在却类型或取值异常则报错，不猜测其业务意义。
    """
    review_state = edge.get("review_state", "pending_user_review")
    if not isinstance(review_state, str) or review_state not in RELATIONSHIP_EDGE_REVIEW_STATES:
        raise ValueError("关系边的 review_state 不合法")
    return review_state


def _edge_for_read(edge: dict[str, Any]) -> dict[str, Any]:
    """返回带兼容默认状态的关系边副本，确保只读查询不会修改历史记录。"""
    result = copy.deepcopy(edge)
    result["review_state"] = _edge_review_state_for_read(edge)
    if "review_history" not in result:
        result["review_history"] = []
    elif not isinstance(result["review_history"], list):
        raise ValueError("关系边的 review_history 必须是列表")
    return result


def _supplement_evidence_ids_for_edge(
    supplements: list[dict[str, Any]],
    *,
    edge_id: str,
) -> set[str]:
    """读取一条关系边已追加的证据 ID，遇到异常历史结构时拒绝忽略或猜测。"""
    evidence_ids: set[str] = set()
    for supplement in supplements:
        if not isinstance(supplement, dict):
            raise ValueError("关系边补证记录必须是对象")
        if supplement.get("edge_id") != edge_id:
            continue
        raw_evidence_ids = supplement.get("evidence_ids")
        if not isinstance(raw_evidence_ids, list) or not all(
            isinstance(item, str) and item for item in raw_evidence_ids
        ):
            raise ValueError("关系边补证记录的 evidence_ids 必须是非空字符串列表")
        evidence_ids.update(raw_evidence_ids)
    return evidence_ids


def _relationship_orientation_for_read(relationship_type: object) -> dict[str, str]:
    """只读解释已登记关系的产业链或公开关键人员角色语义，不推断新事实。

    输入为关系边已保存的 ``relationship_type``，输出为前端或报告可直接采用的方向、两端
    角色与解释依据。三个受控产业链类型可展示上下游语义；六类关键人员关系仅展示
    ``公开人员 -> 组织`` 的已登记角色，绝不延伸为个人投资能力或意愿。其余合法类型
    明确标为非产业链，避免把投资、合作或园区服务误画成供应链。该函数无身份参数、
    无写入行为，因此既不改变人工权限，也绝不改写证据链。
    """
    if not isinstance(relationship_type, str) or relationship_type not in RELATIONSHIP_TYPES:
        raise ValueError("关系边的 relationship_type 不合法")
    if relationship_type in _RELATIONSHIP_ORIENTATIONS:
        return copy.deepcopy(_RELATIONSHIP_ORIENTATIONS[relationship_type])
    if relationship_type in KEY_PERSON_RELATIONSHIP_TYPES:
        return {
            "relationship_category": "public_key_person_role",
            "business_flow_direction": "not_applicable",
            "from_node_role": "public_key_person",
            "to_node_role": "organization_subject",
            "direction_basis": "仅展示原始公开摘录已明确披露的人员组织角色，不推断个人资产、投资意愿或控制范围。",
        }
    return {
        "relationship_category": "other_evidence_backed_relationship",
        "business_flow_direction": "not_applicable",
        "from_node_role": "not_inferred",
        "to_node_role": "not_inferred",
        "direction_basis": "该关系类型不具有受控上下游语义，图谱不推断产业链方向。",
    }


def _edge_evidence_ids_for_analysis(
    edge: dict[str, Any],
    supplements: list[dict[str, Any]],
) -> tuple[list[str], list[str], list[str]]:
    """汇总一条边的原始及追加证据 ID，严格保留两者边界而不修改任何记录。

    输入是一条关系边和整个补证追加列表；输出依次为去重后的全部 ID、原始边 ID、追加
    ID。读取时会校验历史结构和 ID，不会把补证写回原始 ``evidence_ids``，也不会新增
    证据、变更来源 URL、摘录、采集时间或人工复核。函数不接收操作者身份，因其完全
    只读；实际补证权限仍由专门写入函数从服务器认证身份判定。
    """
    edge_id = _required_text(edge.get("edge_id"), "edge_id", max_length=100)
    original_ids = _normalize_evidence_ids(edge.get("evidence_ids"), field_name="evidence_ids")
    supplement_ids = sorted(
        _supplement_evidence_ids_for_edge(supplements, edge_id=edge_id) - set(original_ids)
    )
    return sorted(set(original_ids) | set(supplement_ids)), original_ids, supplement_ids


def _evidence_support_summary(evidence_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """计算证据引用覆盖度，而非捏造企业之间的真实关系强度。

    输入为已经存在且被关系边引用的证据记录，输出为引用数、来源 URL/域名数、来源等级
    与质量字段的只读统计。``level`` 仅描述台账中有多少相互独立的证据引用，绝不表示
    采购规模、合作深度、投资意愿或关系真实性；这些业务结论仍须普通用户核对原始证据
    后作出。函数不处理权限、不写文件、不改动任何原始证据字段。
    """
    source_urls = {
        record.get("source_url").strip()
        for record in evidence_by_id.values()
        if isinstance(record.get("source_url"), str) and record["source_url"].strip()
    }
    source_domains = {
        parsed.netloc.casefold()
        for source_url in source_urls
        if (parsed := urlparse(source_url)).netloc
    }
    source_level_counts: dict[str, int] = {}
    evidence_quality_values: list[int] = []
    for record in evidence_by_id.values():
        source_level = record.get("source_level")
        if isinstance(source_level, str) and source_level:
            source_level_counts[source_level] = source_level_counts.get(source_level, 0) + 1
        evidence_quality = record.get("evidence_quality")
        if isinstance(evidence_quality, int) and not isinstance(evidence_quality, bool):
            evidence_quality_values.append(evidence_quality)
    if len(source_domains) >= 2:
        level = "multiple_source_domains"
    elif len(evidence_by_id) >= 2:
        level = "multiple_evidence_references"
    else:
        level = "single_evidence_reference"
    return {
        "level": level,
        "evidence_reference_count": len(evidence_by_id),
        "distinct_source_url_count": len(source_urls),
        "distinct_source_domain_count": len(source_domains),
        "source_level_counts": dict(sorted(source_level_counts.items())),
        "evidence_quality_values": sorted(evidence_quality_values),
        "business_relationship_strength_inferred": False,
        "caveat": "仅表示已登记证据引用覆盖度，不等同于真实关系强度、交易规模或落地意愿。",
    }


def _node_summary_for_analysis(
    node_by_id: dict[str, dict[str, Any]],
    *,
    node_id: object,
) -> dict[str, str]:
    """返回边端点的最小已登记节点信息，缺失或异常时拒绝猜测节点事实。

    输入为当前只读图谱范围内的节点索引和边端点 ID，输出为 ID、名称、类型及本地属性。
    节点必须确实存在且字段属于已登记枚举，否则说明台账完整性无法确认并直接报错；这
    比默认为“徐州本地”或“外地”更安全。函数无写入、无权限绕过，绝不接触节点所引证据。
    """
    normalized_node_id = _required_text(node_id, "关系边端点 node_id", max_length=100)
    node = node_by_id.get(normalized_node_id)
    if node is None:
        raise ValueError("关系边端点节点不存在，无法生成图谱研判")
    display_name = _required_text(node.get("display_name"), "节点 display_name", max_length=300)
    node_kind = node.get("node_kind")
    locality = node.get("locality")
    if not isinstance(node_kind, str) or node_kind not in RELATIONSHIP_NODE_KINDS:
        raise ValueError("关系边端点的 node_kind 不合法")
    if not isinstance(locality, str) or locality not in RELATIONSHIP_LOCALITIES:
        raise ValueError("关系边端点的 locality 不合法")
    return {
        "node_id": normalized_node_id,
        "display_name": display_name,
        "node_kind": node_kind,
        "locality": locality,
    }


def _pending_evidence_reason(review_state: str) -> str | None:
    """把受控人工复核状态转成待补证提示，不把提示误当作新的证据或自动结论。

    输入为关系边当前复核状态，输出为仅供人工待办使用的提示文本或 ``None``。等待用户
    复核和人工标记待补证会被列入待办；有效和无效状态不再伪造“待补证”事实。该函数
    不接收客户端用户 ID、不改变复核状态，也绝不修改关系原始证据和补证历史。
    """
    if review_state == "pending_user_review":
        return "该关系尚未由普通登录用户完成复核，不能作为已确认产业链事实。"
    if review_state == "need_evidence":
        return "该关系已被人工标记为待补证，应先核验新增原始来源再作业务判断。"
    return None


def _edge_analysis_for_read(
    ledger: dict[str, Any],
    edge: dict[str, Any],
    *,
    node_by_id: dict[str, dict[str, Any]],
    supplements: list[dict[str, Any]],
) -> dict[str, Any]:
    """为一条已登记关系边生成只读的方向、覆盖度与待补证研判卡片。

    输入是已加载台账、关系边、节点索引和追加补证历史；输出只引用既有 ID 和已登记
    枚举，包含方向展示、原始/追加证据数量及人工待办原因。没有任何模型推断、抓取或
    权限放宽：该函数既不认定关系有效，也不接受操作者身份，更不会写入边、证据、来源
    URL、原始摘录、采集时间、评分或人工复核。
    """
    edge_id = _required_text(edge.get("edge_id"), "edge_id", max_length=100)
    relationship_type = edge.get("relationship_type")
    orientation = _relationship_orientation_for_read(relationship_type)
    all_evidence_ids, original_evidence_ids, supplement_evidence_ids = _edge_evidence_ids_for_analysis(
        edge,
        supplements,
    )
    evidence_by_id = _evidence_by_id(ledger, all_evidence_ids)
    review_state = _edge_review_state_for_read(edge)
    return {
        "edge_id": edge_id,
        "relationship_type": relationship_type,
        **orientation,
        "from_node": _node_summary_for_analysis(
            node_by_id,
            node_id=edge.get("from_node_id"),
        ),
        "to_node": _node_summary_for_analysis(
            node_by_id,
            node_id=edge.get("to_node_id"),
        ),
        "review_state": review_state,
        "pending_evidence": review_state in {"pending_user_review", "need_evidence"},
        "pending_evidence_reason": _pending_evidence_reason(review_state),
        "evidence_support": {
            "original_edge_evidence_ids": original_evidence_ids,
            "supplement_evidence_ids": supplement_evidence_ids,
            **_evidence_support_summary(evidence_by_id),
        },
    }


def _industry_chain_analysis_for_read(
    ledger: dict[str, Any],
    *,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    supplements: list[dict[str, Any]],
) -> dict[str, Any]:
    """汇总当前子图的证据化产业链关系，供人工审查而不生成招商结论。

    输入是已按线索和状态筛选后的节点、边、补证列表；输出为每条边的只读研判卡片、
    上下游边数、徐州—外地连接数和待补证清单。所有判断完全来自人工已登记的关系类型、
    节点本地属性、复核状态及既有证据引用；不使用模型补全，不自动标红、推进、复核或
    改写原始证据链。调用该函数无需管理员权限，因为它不执行任何业务写入。
    """
    node_by_id: dict[str, dict[str, Any]] = {}
    for node in nodes:
        node_id = _required_text(node.get("node_id"), "node_id", max_length=100)
        if node_id in node_by_id:
            raise ValueError("关系图谱中存在重复 node_id，无法安全生成研判")
        node_by_id[node_id] = node
    edge_analysis = [
        _edge_analysis_for_read(
            ledger,
            edge,
            node_by_id=node_by_id,
            supplements=supplements,
        )
        for edge in edges
    ]
    relationship_type_counts: dict[str, int] = {}
    industry_chain_edges = [
        item
        for item in edge_analysis
        if item["relationship_type"] in INDUSTRY_CHAIN_RELATIONSHIP_TYPES
    ]
    for item in edge_analysis:
        relationship_type = item["relationship_type"]
        relationship_type_counts[relationship_type] = (
            relationship_type_counts.get(relationship_type, 0) + 1
        )
    xuzhou_external_edge_count = sum(
        {
            item["from_node"]["locality"],
            item["to_node"]["locality"],
        }
        == {"xuzhou_local", "target_or_external"}
        for item in industry_chain_edges
    )
    pending_evidence_edges = [
        {
            "edge_id": item["edge_id"],
            "relationship_type": item["relationship_type"],
            "review_state": item["review_state"],
            "reason": item["pending_evidence_reason"],
        }
        for item in edge_analysis
        if item["pending_evidence"]
    ]
    industry_chain_edge_analysis = [
        item for item in edge_analysis if item["relationship_type"] in INDUSTRY_CHAIN_RELATIONSHIP_TYPES
    ]
    industry_chain_pending_evidence_edges = [
        item for item in pending_evidence_edges
        if item["relationship_type"] in INDUSTRY_CHAIN_RELATIONSHIP_TYPES
    ]
    industry_chain_relationship_type_counts = {
        relationship_type: count
        for relationship_type, count in relationship_type_counts.items()
        if relationship_type in INDUSTRY_CHAIN_RELATIONSHIP_TYPES
    }
    return {
        "analysis_scope": "registered_evidence_backed_relationships_only",
        "business_relationship_strength_inferred": False,
        "caveat": "上下游方向只来自人工登记的受控关系类型；证据覆盖度不能替代交易、合作或落地意愿判断。",
        "summary": {
            "edge_count": len(edge_analysis),
            "evidence_backed_industry_chain_edge_count": len(industry_chain_edges),
            "xuzhou_external_industry_chain_edge_count": xuzhou_external_edge_count,
            "pending_evidence_edge_count": len(pending_evidence_edges),
            "relationship_type_counts": dict(sorted(relationship_type_counts.items())),
        },
        "edge_analysis": edge_analysis,
        "pending_evidence_edges": pending_evidence_edges,
        # 旧版 ``edge_analysis`` 保留整图边以兼容既有读取方；新的两项严格只保留三类
        # 产业链边，标准研判报告据此避免把人员角色、投资或园区服务误写成上下游关系。
        "industry_chain_edge_analysis": industry_chain_edge_analysis,
        "industry_chain_summary": {
            "edge_count": len(industry_chain_edge_analysis),
            "evidence_backed_industry_chain_edge_count": len(industry_chain_edge_analysis),
            "xuzhou_external_industry_chain_edge_count": xuzhou_external_edge_count,
            "pending_evidence_edge_count": len(industry_chain_pending_evidence_edges),
            "relationship_type_counts": dict(sorted(industry_chain_relationship_type_counts.items())),
        },
        "industry_chain_pending_evidence_edges": industry_chain_pending_evidence_edges,
    }


def _key_person_relationship_analysis_for_read(
    industry_chain_analysis: dict[str, Any],
) -> dict[str, Any]:
    """从同一份只读图谱分析中投影公开关键人员—组织关系，不创建个人招商结论。

    输入是本次查询已生成的边研判卡片；输出只保留法定代表人、创始人、实控人、股东、
    董事和高管六类受控关系的人员/组织摘要、复核状态及现有证据 ID。函数不重新解析
    网页、不接受人员联系方式、住址、资产、募资能力或客户端身份，也不会把公开角色
    推断为个人投资意愿、徐州落地承诺或个人资金来源。所有姓名和“徐州关联”均须回到
    已登记的原始证据核验；该函数严格只读，不能改写证据 URL、摘录、采集时间、评分
    依据、线索状态、关系边或审计记录。
    """
    raw_edge_analysis = industry_chain_analysis.get("edge_analysis")
    if not isinstance(raw_edge_analysis, list):
        raise ValueError("关系图谱缺少关键人员研判所需的边列表")

    role_cards: list[dict[str, Any]] = []
    relationship_type_counts: dict[str, int] = {}
    public_person_ids: set[str] = set()
    xuzhou_related_person_ids: set[str] = set()
    pending_review_edges: list[dict[str, Any]] = []
    for raw_edge in raw_edge_analysis:
        if not isinstance(raw_edge, dict):
            raise ValueError("关系图谱关键人员边研判必须是对象")
        relationship_type = raw_edge.get("relationship_type")
        if relationship_type not in KEY_PERSON_RELATIONSHIP_TYPES:
            continue
        person = raw_edge.get("from_node")
        organization = raw_edge.get("to_node")
        evidence_support = raw_edge.get("evidence_support")
        edge_id = raw_edge.get("edge_id")
        review_state = raw_edge.get("review_state")
        pending_evidence = raw_edge.get("pending_evidence")
        if (
            not isinstance(person, dict)
            or person.get("node_kind") != "person"
            or not isinstance(organization, dict)
            or organization.get("node_kind") == "person"
            or not isinstance(evidence_support, dict)
            or not all(isinstance(value, str) and value for value in (edge_id, review_state))
            or not isinstance(pending_evidence, bool)
        ):
            raise ValueError("关键人员关系边缺少受控端点、复核状态或证据支持")
        person_id = _required_text(person.get("node_id"), "公开关键人员 node_id", max_length=100)
        public_person_ids.add(person_id)
        if person.get("locality") == "xuzhou_related":
            xuzhou_related_person_ids.add(person_id)
        relationship_type_counts[relationship_type] = (
            relationship_type_counts.get(relationship_type, 0) + 1
        )
        role_cards.append(
            {
                "edge_id": edge_id,
                "relationship_type": relationship_type,
                "person": copy.deepcopy(person),
                "organization": copy.deepcopy(organization),
                "review_state": review_state,
                "pending_evidence": pending_evidence,
                "pending_evidence_reason": copy.deepcopy(raw_edge.get("pending_evidence_reason")),
                "evidence_support": copy.deepcopy(evidence_support),
            }
        )
        if pending_evidence:
            pending_review_edges.append(
                {
                    "edge_id": edge_id,
                    "relationship_type": relationship_type,
                    "review_state": review_state,
                    "reason": copy.deepcopy(raw_edge.get("pending_evidence_reason")),
                }
            )

    return {
        "analysis_scope": "publicly_disclosed_key_person_roles_only",
        "person_financial_or_contact_data_collected": False,
        "personal_investment_or_landing_intent_inferred": False,
        "caveat": (
            "仅展示可沿 evidence_id 追溯的公开人员组织角色；徐州关联不等于籍贯、住址或"
            "个人投资意愿，任何招商沟通与合规判断仍须人工完成。"
        ),
        "summary": {
            "evidence_backed_key_person_relationship_edge_count": len(role_cards),
            "distinct_public_key_person_count": len(public_person_ids),
            "xuzhou_related_public_key_person_count": len(xuzhou_related_person_ids),
            "pending_review_key_person_relationship_edge_count": len(pending_review_edges),
            "relationship_type_counts": dict(sorted(relationship_type_counts.items())),
        },
        "role_cards": role_cards,
        "pending_review_edges": pending_review_edges,
    }


def register_relationship_node(
    workspace_dir: Path,
    *,
    display_name: str,
    node_kind: str,
    locality: str,
    linked_lead_id: str | None,
    evidence_ids: list[str],
    registration_reason: str,
    authenticated_user_id: str,
) -> dict[str, Any]:
    """由已认证普通用户登记一个由既有证据支撑的图谱节点。

    输入为名称、节点类型、本地属性、可选既有线索、既有证据 ID、人工登记理由和仅可
    来自认证层的操作者 ID；输出为新增或已有节点及台账路径。所有用户均可进行这一
    业务登记，不检查管理员角色；HTTP 层必须从服务器 ``Principal`` 提取身份，模型和
    前端不能提交 ``authenticated_user_id``。节点只引用证据 ID，写入前后强制校验原始
    证据、来源 URL、摘录、采集时间、评分与其他台账业务记录不变。
    """
    normalized_name = _required_text(display_name, "display_name", max_length=300)
    normalized_kind = _canonical(
        node_kind,
        "node_kind",
        RELATIONSHIP_NODE_KINDS,
        RELATIONSHIP_NODE_KIND_ALIASES,
    )
    normalized_locality = _canonical(
        locality,
        "locality",
        RELATIONSHIP_LOCALITIES,
        RELATIONSHIP_LOCALITY_ALIASES,
    )
    normalized_linked_lead_id = (
        _required_text(linked_lead_id, "linked_lead_id", max_length=100)
        if linked_lead_id is not None
        else None
    )
    _assert_public_person_node_constraints(
        node_kind=normalized_kind,
        locality=normalized_locality,
        linked_lead_id=normalized_linked_lead_id,
    )
    normalized_evidence_ids = _normalize_evidence_ids(evidence_ids, field_name="evidence_ids")
    normalized_reason = _required_text(registration_reason, "registration_reason", max_length=2_000)
    normalized_user_id = _required_text(
        authenticated_user_id,
        "authenticated_user_id",
        max_length=100,
    )

    ledger = load_ledger(workspace_dir)
    protected_before = _snapshot_protected_fields(ledger)
    nodes, _, audit = _graph_lists(ledger)
    evidence_by_id = _evidence_by_id(ledger, normalized_evidence_ids)
    if not any(
        _evidence_supports_node_name(evidence, normalized_name)
        for evidence in evidence_by_id.values()
    ):
        raise ValueError("图谱节点必须至少引用一条明确出现该节点名称的原始证据摘录")
    if normalized_kind == "person":
        _assert_public_person_xuzhou_association_evidence(
            evidence_by_id,
            display_name=normalized_name,
            locality=normalized_locality,
        )
    if normalized_linked_lead_id is not None:
        matching_lead = next(
            (
                lead
                for lead in ledger["leads"]
                if isinstance(lead, dict) and lead.get("lead_id") == normalized_linked_lead_id
            ),
            None,
        )
        if matching_lead is None:
            raise ValueError("linked_lead_id未对应既有招商线索")
        if not any(
            evidence.get("lead_id") == normalized_linked_lead_id
            for evidence in evidence_by_id.values()
        ):
            raise ValueError("关联招商线索的图谱节点必须至少引用该线索的一条既有证据")

    existing_node = next(
        (
            node
            for node in nodes
            if isinstance(node, dict)
            and node.get("display_name", "").casefold() == normalized_name.casefold()
            and node.get("node_kind") == normalized_kind
            and node.get("linked_lead_id") == normalized_linked_lead_id
        ),
        None,
    )
    if existing_node is not None:
        return {
            "created": False,
            "message": "相同名称、类型和关联线索的图谱节点已经登记，未覆盖既有记录",
            "node": copy.deepcopy(existing_node),
            "ledger_path": str(ledger_path(workspace_dir)),
        }

    created_at = _utc_now()
    node_id = _stable_id(
        "relationship-node",
        {
            "display_name": normalized_name.casefold(),
            "node_kind": normalized_kind,
            "linked_lead_id": normalized_linked_lead_id,
        },
    )
    node = {
        "node_id": node_id,
        "display_name": normalized_name,
        "node_kind": normalized_kind,
        "locality": normalized_locality,
        "linked_lead_id": normalized_linked_lead_id,
        "evidence_ids": normalized_evidence_ids,
        "registration_reason": normalized_reason,
        "created_at": created_at,
        "created_by_user_id": normalized_user_id,
        "operation_scope": "evidence_backed_relationship_graph_registration_only",
    }
    nodes.append(node)
    _append_audit(
        audit,
        action="relationship_node_registered",
        target_kind="node",
        target_id=node_id,
        authenticated_user_id=normalized_user_id,
        operated_at=created_at,
        operation_scope="evidence_backed_relationship_graph_registration_only",
    )
    _assert_protected_fields_unchanged(protected_before, ledger)
    saved_path = save_ledger(workspace_dir, ledger)
    return {"created": True, "node": copy.deepcopy(node), "ledger_path": str(saved_path)}


def register_relationship_edge(
    workspace_dir: Path,
    *,
    from_node_id: str,
    to_node_id: str,
    relationship_type: str,
    evidence_ids: list[str],
    registration_reason: str,
    authenticated_user_id: str,
) -> dict[str, Any]:
    """由已认证普通用户登记一条由既有证据支持的目标线索关系边。

    输入是两个服务器生成的节点 ID、受控关系类型、既有证据 ID、登记理由和认证层提供
    的操作者 ID；输出为新增或已有关系边。普通登录用户无需管理员角色，但至少一个
    端点必须关联目标线索，且关系边至少一条证据必须属于该目标线索，防止把没有招商
    事实依据的本地组织关系伪装成目标关系。函数只追加边和审计，写入前后校验所有原始
    证据字段及既有业务记录完全不变。
    """
    normalized_from_node_id = _required_text(from_node_id, "from_node_id", max_length=100)
    normalized_to_node_id = _required_text(to_node_id, "to_node_id", max_length=100)
    if normalized_from_node_id == normalized_to_node_id:
        raise ValueError("关系边不能连接同一个图谱节点")
    normalized_relationship_type = _canonical(
        relationship_type,
        "relationship_type",
        RELATIONSHIP_TYPES,
        RELATIONSHIP_TYPE_ALIASES,
    )
    normalized_evidence_ids = _normalize_evidence_ids(evidence_ids, field_name="evidence_ids")
    normalized_reason = _required_text(registration_reason, "registration_reason", max_length=2_000)
    normalized_user_id = _required_text(
        authenticated_user_id,
        "authenticated_user_id",
        max_length=100,
    )

    ledger = load_ledger(workspace_dir)
    protected_before = _snapshot_protected_fields(ledger)
    nodes, edges, audit = _graph_lists(ledger)
    nodes_by_id = {
        node.get("node_id"): node
        for node in nodes
        if isinstance(node, dict) and isinstance(node.get("node_id"), str)
    }
    from_node = nodes_by_id.get(normalized_from_node_id)
    to_node = nodes_by_id.get(normalized_to_node_id)
    if from_node is None or to_node is None:
        raise ValueError("from_node_id和to_node_id必须都是已登记的图谱节点")
    _assert_key_person_relationship_endpoints(
        from_node=from_node,
        to_node=to_node,
        relationship_type=normalized_relationship_type,
    )
    evidence_by_id = _evidence_by_id(ledger, normalized_evidence_ids)
    linked_lead_ids = {
        node.get("linked_lead_id")
        for node in (from_node, to_node)
        if isinstance(node.get("linked_lead_id"), str)
    }
    if not linked_lead_ids:
        raise ValueError("关系边至少一个端点必须关联既有招商线索")
    if not any(
        evidence.get("lead_id") in linked_lead_ids for evidence in evidence_by_id.values()
    ):
        raise ValueError("关系边必须至少引用关联目标线索的一条既有证据")
    from_display_name = from_node.get("display_name")
    to_display_name = to_node.get("display_name")
    if not isinstance(from_display_name, str) or not isinstance(to_display_name, str):
        raise ValueError("关系边引用的图谱节点缺少名称")
    _assert_relation_evidence_semantics(
        evidence_by_id,
        from_display_name=from_display_name,
        to_display_name=to_display_name,
        relationship_type=normalized_relationship_type,
    )

    existing_edge = next(
        (
            edge
            for edge in edges
            if isinstance(edge, dict)
            and edge.get("from_node_id") == normalized_from_node_id
            and edge.get("to_node_id") == normalized_to_node_id
            and edge.get("relationship_type") == normalized_relationship_type
            and edge.get("evidence_ids") == normalized_evidence_ids
        ),
        None,
    )
    if existing_edge is not None:
        return {
            "created": False,
            "message": "相同节点、关系类型和证据引用的关系边已经登记，未覆盖既有记录",
            "edge": copy.deepcopy(existing_edge),
            "ledger_path": str(ledger_path(workspace_dir)),
        }

    created_at = _utc_now()
    edge_id = _stable_id(
        "relationship-edge",
        {
            "from_node_id": normalized_from_node_id,
            "to_node_id": normalized_to_node_id,
            "relationship_type": normalized_relationship_type,
            "evidence_ids": normalized_evidence_ids,
        },
    )
    edge = {
        "edge_id": edge_id,
        "from_node_id": normalized_from_node_id,
        "to_node_id": normalized_to_node_id,
        "relationship_type": normalized_relationship_type,
        "evidence_ids": normalized_evidence_ids,
        "registration_reason": normalized_reason,
        "created_at": created_at,
        "created_by_user_id": normalized_user_id,
        "operation_scope": "evidence_backed_relationship_graph_registration_only",
        # 新关系只是已有证据支持的待核验业务判断；它不会因登记而自动成为有效关系。
        "review_state": "pending_user_review",
        "review_history": [],
    }
    edges.append(edge)
    _append_audit(
        audit,
        action="relationship_edge_registered",
        target_kind="edge",
        target_id=edge_id,
        authenticated_user_id=normalized_user_id,
        operated_at=created_at,
        operation_scope="evidence_backed_relationship_graph_registration_only",
    )
    _assert_protected_fields_unchanged(protected_before, ledger)
    saved_path = save_ledger(workspace_dir, ledger)
    return {"created": True, "edge": copy.deepcopy(edge), "ledger_path": str(saved_path)}


def register_approved_relationship_draft_to_graph(
    workspace_dir: Path,
    *,
    relationship_draft_id: str,
    authenticated_user_id: str,
) -> dict[str, Any]:
    """将已由普通用户确认的关键人员草稿一次性登记为待复核图谱节点与关系边。

    输入仅为服务器生成的关系草稿 ID 和认证层提供的操作者 ID；输出为本次使用的人员节点、
    组织节点、待复核关系边及只追加图谱审计。函数不接受姓名、组织、角色、徐州标签、证据
    ID、URL、摘录或客户端用户 ID：所有事实只能从已签名草稿引用的既有 ``evidence`` 读取。
    只有最近一次草稿业务判断为 ``ready_for_graph_registration`` 才能调用，且原始摘录会再次
    校验人员、组织和角色同现。普通用户无需管理员角色；操作只创建/复用图谱对象，关系边
    初始仍为等待用户复核，绝不推进招商线索、推断个人意愿或改写证据 URL、摘录、采集时间、
    HMAC、评分、草稿和复核历史。
    """

    normalized_draft_id = _required_text(
        relationship_draft_id, "relationship_draft_id", max_length=120
    )
    normalized_user_id = _required_text(
        authenticated_user_id, "authenticated_user_id", max_length=100
    )
    ledger = load_ledger(workspace_dir)
    protected_before = _snapshot_protected_fields(ledger)
    drafts = ledger.get("relationship_drafts")
    draft_reviews = ledger.get("relationship_draft_reviews")
    if not isinstance(drafts, list) or not isinstance(draft_reviews, list):
        raise ValueError("招商线索台账中的关键人员关系草稿字段必须是列表")
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
    if draft.get("state") != "pending_user_review":
        raise ValueError("该关系草稿不是等待用户复核状态")
    latest_review = next(
        (
            item
            for item in reversed(draft_reviews)
            if isinstance(item, dict)
            and item.get("relationship_draft_id") == normalized_draft_id
            and isinstance(item.get("new_state"), str)
        ),
        None,
    )
    if latest_review is None or latest_review.get("new_state") != "ready_for_graph_registration":
        raise ValueError("关系草稿须先由普通登录用户标记为可登记图谱")

    nodes, edges, audit = _graph_lists(ledger)
    if any(
        isinstance(item, dict)
        and item.get("action") == "approved_relationship_draft_registered_to_graph"
        and item.get("target_kind") == "relationship_draft"
        and item.get("target_id") == normalized_draft_id
        for item in audit
    ):
        raise ValueError("该关系草稿已经登记到图谱，不能重复执行")

    lead_id = _required_text(draft.get("lead_id"), "草稿 lead_id", max_length=100)
    evidence_id = _required_text(draft.get("evidence_id"), "草稿 evidence_id", max_length=100)
    organization_name = _required_text(
        draft.get("organization_name"), "草稿 organization_name", max_length=300
    )
    person_display_name = _required_text(
        draft.get("person_display_name"), "草稿 person_display_name", max_length=300
    )
    relationship_type = _canonical(
        draft.get("relationship_type"),
        "草稿 relationship_type",
        KEY_PERSON_RELATIONSHIP_TYPES,
        RELATIONSHIP_TYPE_ALIASES,
    )
    person_locality = _canonical(
        draft.get("person_locality_candidate"),
        "草稿 person_locality_candidate",
        RELATIONSHIP_LOCALITIES,
        RELATIONSHIP_LOCALITY_ALIASES,
    )
    _assert_public_person_node_constraints(
        node_kind="person", locality=person_locality, linked_lead_id=None
    )
    evidence_by_id = _evidence_by_id(ledger, [evidence_id])
    evidence = evidence_by_id[evidence_id]
    if evidence.get("lead_id") != lead_id:
        raise ValueError("关系草稿引用的正式证据不属于其招商线索")
    if not _evidence_supports_node_name(evidence, organization_name) or not _evidence_supports_node_name(
        evidence, person_display_name
    ):
        raise ValueError("关系草稿引用的原始证据未明确同时出现组织或人员名称")
    _assert_public_person_xuzhou_association_evidence(
        evidence_by_id,
        display_name=person_display_name,
        locality=person_locality,
    )
    _assert_relation_evidence_semantics(
        evidence_by_id,
        from_display_name=person_display_name,
        to_display_name=organization_name,
        relationship_type=relationship_type,
    )
    lead = next(
        (
            item
            for item in ledger["leads"]
            if isinstance(item, dict) and item.get("lead_id") == lead_id
        ),
        None,
    )
    if lead is None or lead.get("entity_name") != organization_name:
        raise ValueError("关系草稿关联的招商线索与组织名称不一致")
    organization_kind = _canonical(
        lead.get("entity_kind"),
        "招商线索 entity_kind",
        RELATIONSHIP_NODE_KINDS,
        RELATIONSHIP_NODE_KIND_ALIASES,
    )
    if organization_kind == "person":
        raise ValueError("关键人员关系草稿的组织端不能是个人")

    created_at = _utc_now()
    fixed_reason = (
        f"已由认证用户确认的关键人员关系草稿 {normalized_draft_id}；仅引用该草稿绑定的"
        "既有正式证据登记待复核图谱关系。"
    )
    organization_node = next(
        (
            node
            for node in nodes
            if isinstance(node, dict)
            and node.get("display_name", "").casefold() == organization_name.casefold()
            and node.get("node_kind") == organization_kind
            and node.get("linked_lead_id") == lead_id
        ),
        None,
    )
    organization_node_created = organization_node is None
    if organization_node is None:
        organization_node = {
            "node_id": _stable_id(
                "relationship-node",
                {
                    "display_name": organization_name.casefold(),
                    "node_kind": organization_kind,
                    "linked_lead_id": lead_id,
                },
            ),
            "display_name": organization_name,
            "node_kind": organization_kind,
            "locality": "target_or_external",
            "linked_lead_id": lead_id,
            "evidence_ids": [evidence_id],
            "registration_reason": fixed_reason,
            "created_at": created_at,
            "created_by_user_id": normalized_user_id,
            "operation_scope": "approved_key_person_relationship_draft_graph_registration_only",
        }
        nodes.append(organization_node)
        _append_audit(
            audit,
            action="relationship_node_registered_from_approved_draft",
            target_kind="node",
            target_id=str(organization_node["node_id"]),
            authenticated_user_id=normalized_user_id,
            operated_at=created_at,
            operation_scope="approved_key_person_relationship_draft_graph_registration_only",
        )

    person_node = next(
        (
            node
            for node in nodes
            if isinstance(node, dict)
            and node.get("display_name", "").casefold() == person_display_name.casefold()
            and node.get("node_kind") == "person"
            and node.get("linked_lead_id") is None
        ),
        None,
    )
    person_node_created = person_node is None
    if person_node is None:
        person_node = {
            "node_id": _stable_id(
                "relationship-node",
                {
                    "display_name": person_display_name.casefold(),
                    "node_kind": "person",
                    "linked_lead_id": None,
                },
            ),
            "display_name": person_display_name,
            "node_kind": "person",
            "locality": person_locality,
            "linked_lead_id": None,
            "evidence_ids": [evidence_id],
            "registration_reason": fixed_reason,
            "created_at": created_at,
            "created_by_user_id": normalized_user_id,
            "operation_scope": "approved_key_person_relationship_draft_graph_registration_only",
        }
        nodes.append(person_node)
        _append_audit(
            audit,
            action="relationship_node_registered_from_approved_draft",
            target_kind="node",
            target_id=str(person_node["node_id"]),
            authenticated_user_id=normalized_user_id,
            operated_at=created_at,
            operation_scope="approved_key_person_relationship_draft_graph_registration_only",
        )

    person_node_id = _required_text(person_node.get("node_id"), "人员节点 node_id", max_length=100)
    organization_node_id = _required_text(
        organization_node.get("node_id"), "组织节点 node_id", max_length=100
    )
    edge = next(
        (
            item
            for item in edges
            if isinstance(item, dict)
            and item.get("from_node_id") == person_node_id
            and item.get("to_node_id") == organization_node_id
            and item.get("relationship_type") == relationship_type
            and item.get("evidence_ids") == [evidence_id]
        ),
        None,
    )
    edge_created = edge is None
    if edge is None:
        edge = {
            "edge_id": _stable_id(
                "relationship-edge",
                {
                    "from_node_id": person_node_id,
                    "to_node_id": organization_node_id,
                    "relationship_type": relationship_type,
                    "evidence_ids": [evidence_id],
                },
            ),
            "from_node_id": person_node_id,
            "to_node_id": organization_node_id,
            "relationship_type": relationship_type,
            "evidence_ids": [evidence_id],
            "registration_reason": fixed_reason,
            "created_at": created_at,
            "created_by_user_id": normalized_user_id,
            "operation_scope": "approved_key_person_relationship_draft_graph_registration_only",
            "review_state": "pending_user_review",
            "review_history": [],
        }
        edges.append(edge)
        _append_audit(
            audit,
            action="relationship_edge_registered_from_approved_draft",
            target_kind="edge",
            target_id=str(edge["edge_id"]),
            authenticated_user_id=normalized_user_id,
            operated_at=created_at,
            operation_scope="approved_key_person_relationship_draft_graph_registration_only",
        )
    _append_audit(
        audit,
        action="approved_relationship_draft_registered_to_graph",
        target_kind="relationship_draft",
        target_id=normalized_draft_id,
        authenticated_user_id=normalized_user_id,
        operated_at=created_at,
        operation_scope="approved_key_person_relationship_draft_graph_registration_only",
    )
    _assert_protected_fields_unchanged(protected_before, ledger)
    saved_path = save_ledger(workspace_dir, ledger)
    return {
        "organization_node": copy.deepcopy(organization_node),
        "organization_node_created": organization_node_created,
        "person_node": copy.deepcopy(person_node),
        "person_node_created": person_node_created,
        "edge": copy.deepcopy(edge),
        "edge_created": edge_created,
        "relationship_draft_id": normalized_draft_id,
        "operation_scope": "approved_key_person_relationship_draft_graph_registration_only",
        "ledger_path": str(saved_path),
    }


def review_relationship_edge(
    workspace_dir: Path,
    *,
    edge_id: str,
    decision: str,
    review_note: str,
    reviewer_user_id: str,
) -> dict[str, Any]:
    """由已认证普通用户复核图谱关系边，仅追加业务状态和审计历史。

    输入是服务器已生成的关系边 ID、受控结论（有效、待补证或无效）、备注以及只能由
    认证层取得的操作者 ID；输出为复核后的关系边与台账路径。普通登录用户无需管理员
    角色。复核不接受任何节点、关系类型、证据 ID、URL、摘录、采集时间或评分字段；
    保存前后会校验台账非图谱数据、全部节点、其他关系边、目标边的不可变字段及既有
    审计均未变化，因此只能写入当前状态、追加复核历史和一条新审计。
    """
    normalized_edge_id = _required_text(edge_id, "edge_id", max_length=100)
    normalized_decision = _canonical(
        decision,
        "decision",
        MANUAL_RELATIONSHIP_EDGE_REVIEW_STATES,
        RELATIONSHIP_EDGE_REVIEW_STATE_ALIASES,
    )
    normalized_note = _required_text(review_note, "review_note", max_length=2_000)
    normalized_reviewer_user_id = _required_text(
        reviewer_user_id,
        "reviewer_user_id",
        max_length=100,
    )

    ledger = load_ledger(workspace_dir)
    protected_before = _snapshot_protected_fields(ledger)
    nodes, edges, audit = _graph_lists(ledger)
    edge = next(
        (
            item
            for item in edges
            if isinstance(item, dict) and item.get("edge_id") == normalized_edge_id
        ),
        None,
    )
    if edge is None:
        raise ValueError("未找到对应的图谱关系边")
    _ensure_edge_review_fields(edge)
    nodes_before = json.dumps(nodes, ensure_ascii=False, sort_keys=True)
    other_edges_before = json.dumps(
        [item for item in edges if item is not edge],
        ensure_ascii=False,
        sort_keys=True,
    )
    immutable_edge_before = _edge_immutable_snapshot(edge)
    audit_before = json.dumps(audit, ensure_ascii=False, sort_keys=True)
    old_review_state = edge["review_state"]
    reviewed_at = _utc_now()
    edge["review_state"] = normalized_decision
    edge["review_history"].append(
        {
            "old_review_state": old_review_state,
            "new_review_state": normalized_decision,
            "review_note": normalized_note,
            "reviewed_at": reviewed_at,
            "reviewer_user_id": normalized_reviewer_user_id,
            "operation_scope": "relationship_graph_business_review_only",
        }
    )
    _append_audit(
        audit,
        action="relationship_edge_reviewed",
        target_kind="edge",
        target_id=normalized_edge_id,
        authenticated_user_id=normalized_reviewer_user_id,
        operated_at=reviewed_at,
        operation_scope="relationship_graph_business_review_only",
    )
    if (
        nodes_before != json.dumps(nodes, ensure_ascii=False, sort_keys=True)
        or other_edges_before
        != json.dumps([item for item in edges if item is not edge], ensure_ascii=False, sort_keys=True)
        or immutable_edge_before != _edge_immutable_snapshot(edge)
        or audit_before != json.dumps(audit[:-1], ensure_ascii=False, sort_keys=True)
    ):
        raise RuntimeError("图谱关系复核只能追加状态审计，不得修改节点、关系或原始证据链")
    _assert_protected_fields_unchanged(protected_before, ledger)
    saved_path = save_ledger(workspace_dir, ledger)
    return {"edge": copy.deepcopy(edge), "ledger_path": str(saved_path)}


def append_relationship_edge_evidence_supplement(
    workspace_dir: Path,
    *,
    edge_id: str,
    evidence_ids: list[str],
    supplement_note: str,
    authenticated_user_id: str,
) -> dict[str, Any]:
    """由已认证普通用户为既有关系边追加新的证据引用，不覆盖原边或历史补证。

    输入是服务器生成的关系边 ID、尚未被该边使用的既有证据 ID、补证理由和只能来自
    认证层的操作者 ID；输出为独立的补证记录与台账路径。普通登录用户无需管理员角色。
    补证必须至少引用一条关联目标线索的既有证据，不能重复原边或已有补证引用；函数
    只追加补证记录和审计，并严格比对原始证据、节点、关系边、既有补证、评分和其他
    台账历史，因此不会改写来源 URL、摘录、采集时间、哈希、关系类型或人工复核结果。
    """
    normalized_edge_id = _required_text(edge_id, "edge_id", max_length=100)
    normalized_evidence_ids = _normalize_evidence_ids(evidence_ids, field_name="evidence_ids")
    normalized_note = _required_text(supplement_note, "supplement_note", max_length=2_000)
    normalized_user_id = _required_text(
        authenticated_user_id,
        "authenticated_user_id",
        max_length=100,
    )

    ledger = load_ledger(workspace_dir)
    protected_before = _snapshot_protected_fields(
        ledger,
        allow_evidence_supplement_append=True,
    )
    nodes, edges, audit = _graph_lists(ledger)
    supplements = _edge_evidence_supplements(ledger)
    edge = next(
        (
            item
            for item in edges
            if isinstance(item, dict) and item.get("edge_id") == normalized_edge_id
        ),
        None,
    )
    if edge is None:
        raise ValueError("未找到对应的图谱关系边")
    nodes_by_id = {
        node.get("node_id"): node
        for node in nodes
        if isinstance(node, dict) and isinstance(node.get("node_id"), str)
    }
    from_node = nodes_by_id.get(edge.get("from_node_id"))
    to_node = nodes_by_id.get(edge.get("to_node_id"))
    if from_node is None or to_node is None:
        raise ValueError("关系边引用的图谱节点不存在，不能追加补证")
    linked_lead_ids = {
        node.get("linked_lead_id")
        for node in (from_node, to_node)
        if isinstance(node.get("linked_lead_id"), str)
    }
    if not linked_lead_ids:
        raise ValueError("关系边至少一个端点必须关联既有招商线索")
    evidence_by_id = _evidence_by_id(ledger, normalized_evidence_ids)
    if not any(
        evidence.get("lead_id") in linked_lead_ids for evidence in evidence_by_id.values()
    ):
        raise ValueError("关系边补证必须至少引用关联目标线索的一条既有证据")
    from_display_name = from_node.get("display_name")
    to_display_name = to_node.get("display_name")
    relationship_type = edge.get("relationship_type")
    if (
        not isinstance(from_display_name, str)
        or not isinstance(to_display_name, str)
        or not isinstance(relationship_type, str)
        or relationship_type not in RELATIONSHIP_EVIDENCE_TERMS
    ):
        raise ValueError("关系边缺少可核验的节点名称或关系类型")
    _assert_relation_evidence_semantics(
        evidence_by_id,
        from_display_name=from_display_name,
        to_display_name=to_display_name,
        relationship_type=relationship_type,
    )
    original_evidence_ids = edge.get("evidence_ids")
    if not isinstance(original_evidence_ids, list) or not all(
        isinstance(item, str) and item for item in original_evidence_ids
    ):
        raise ValueError("关系边的原始 evidence_ids 必须是非空字符串列表")
    used_evidence_ids = set(original_evidence_ids) | _supplement_evidence_ids_for_edge(
        supplements,
        edge_id=normalized_edge_id,
    )
    repeated_evidence_ids = [
        evidence_id for evidence_id in normalized_evidence_ids if evidence_id in used_evidence_ids
    ]
    if repeated_evidence_ids:
        raise ValueError(
            f"补证不能重复引用关系边原始或既有补证：{'、'.join(repeated_evidence_ids)}"
        )

    nodes_before = json.dumps(nodes, ensure_ascii=False, sort_keys=True)
    edges_before = json.dumps(edges, ensure_ascii=False, sort_keys=True)
    supplements_before = json.dumps(supplements, ensure_ascii=False, sort_keys=True)
    audit_before = json.dumps(audit, ensure_ascii=False, sort_keys=True)
    supplemented_at = _utc_now()
    supplement_id = _stable_id(
        "relationship-edge-supplement",
        {
            "edge_id": normalized_edge_id,
            "evidence_ids": normalized_evidence_ids,
            "supplemented_at": supplemented_at,
            "authenticated_user_id": normalized_user_id,
        },
    )
    supplement = {
        "supplement_id": supplement_id,
        "edge_id": normalized_edge_id,
        "evidence_ids": normalized_evidence_ids,
        "supplement_note": normalized_note,
        "supplemented_at": supplemented_at,
        "supplemented_by_user_id": normalized_user_id,
        "operation_scope": "relationship_graph_evidence_supplement_append_only",
    }
    supplements.append(supplement)
    _append_audit(
        audit,
        action="relationship_edge_evidence_supplemented",
        target_kind="edge_evidence_supplement",
        target_id=supplement_id,
        authenticated_user_id=normalized_user_id,
        operated_at=supplemented_at,
        operation_scope="relationship_graph_evidence_supplement_append_only",
    )
    if (
        nodes_before != json.dumps(nodes, ensure_ascii=False, sort_keys=True)
        or edges_before != json.dumps(edges, ensure_ascii=False, sort_keys=True)
        or supplements_before != json.dumps(supplements[:-1], ensure_ascii=False, sort_keys=True)
        or audit_before != json.dumps(audit[:-1], ensure_ascii=False, sort_keys=True)
    ):
        raise RuntimeError("关系边补证只能追加引用和审计，不得修改既有图谱或原始证据链")
    _assert_protected_fields_unchanged(protected_before, ledger)
    saved_path = save_ledger(workspace_dir, ledger)
    return {
        "supplement": copy.deepcopy(supplement),
        "ledger_path": str(saved_path),
    }


def get_relationship_graph(
    workspace_dir: Path,
    *,
    lead_id: str | None = None,
    review_state: str | None = None,
) -> dict[str, Any]:
    """只读返回证据化关系子图、产业链与公开关键人员研判卡片，不推断业务事实。

    输入可选既有 ``lead_id`` 和关系边 ``review_state``；先确定整图或该线索一跳范围，
    再统计该范围内等待复核、有效、待补证和无效边的数量，最后可按状态筛选关系边。
    输出保留节点、关系边、审计和证据 ID 引用，并额外按已登记关系类型展示上下游方向、
    公开关键人员角色、证据覆盖度和待补证原因；调用方仍须通过原台账读取事实正文。
    人员摘要仅能来自已登记公开证据，不包含联系方式、住址、资产或个人投资能力。函数
    不联网、不自动推断关系或真实强度、不写文件，也不改变证据、评分、复核、预警或
    来源配置。
    """
    normalized_lead_id = (
        _required_text(lead_id, "lead_id", max_length=100) if lead_id is not None else None
    )
    normalized_review_state = (
        _canonical(
            review_state,
            "review_state",
            RELATIONSHIP_EDGE_REVIEW_STATES,
            RELATIONSHIP_EDGE_REVIEW_STATE_ALIASES,
        )
        if review_state is not None
        else None
    )
    ledger = load_ledger(workspace_dir)
    nodes, edges, audit = _graph_lists(ledger)
    supplements = _edge_evidence_supplements(ledger)
    if normalized_lead_id is not None and not any(
        isinstance(lead, dict) and lead.get("lead_id") == normalized_lead_id
        for lead in ledger["leads"]
    ):
        raise ValueError("未找到对应的招商线索")

    selected_nodes = [node for node in nodes if isinstance(node, dict)]
    scoped_edges = [edge for edge in edges if isinstance(edge, dict)]
    root_node_ids: set[str] = set()
    if normalized_lead_id is not None:
        root_node_ids = {
            node["node_id"]
            for node in selected_nodes
            if node.get("linked_lead_id") == normalized_lead_id and isinstance(node.get("node_id"), str)
        }
        scoped_edges = [
            edge
            for edge in scoped_edges
            if edge.get("from_node_id") in root_node_ids or edge.get("to_node_id") in root_node_ids
        ]
    review_state_summary = {state: 0 for state in sorted(RELATIONSHIP_EDGE_REVIEW_STATES)}
    for edge in scoped_edges:
        review_state_summary[_edge_review_state_for_read(edge)] += 1
    selected_edges = [
        edge
        for edge in scoped_edges
        if normalized_review_state is None
        or _edge_review_state_for_read(edge) == normalized_review_state
    ]
    if normalized_lead_id is not None:
        selected_node_ids = root_node_ids | {
            endpoint_id
            for edge in selected_edges
            for endpoint_id in (edge.get("from_node_id"), edge.get("to_node_id"))
            if isinstance(endpoint_id, str)
        }
        selected_nodes = [
            node for node in selected_nodes if node.get("node_id") in selected_node_ids
        ]
    elif normalized_review_state is not None:
        selected_node_ids = {
            endpoint_id
            for edge in selected_edges
            for endpoint_id in (edge.get("from_node_id"), edge.get("to_node_id"))
            if isinstance(endpoint_id, str)
        }
        selected_nodes = [
            node for node in selected_nodes if node.get("node_id") in selected_node_ids
        ]

    selected_edge_ids = {
        edge.get("edge_id") for edge in selected_edges if isinstance(edge.get("edge_id"), str)
    }
    selected_supplements = [
        supplement
        for supplement in supplements
        if isinstance(supplement, dict) and supplement.get("edge_id") in selected_edge_ids
    ]
    selected_target_ids = {
        node.get("node_id") for node in selected_nodes if isinstance(node.get("node_id"), str)
    } | selected_edge_ids | {
        supplement.get("supplement_id")
        for supplement in selected_supplements
        if isinstance(supplement.get("supplement_id"), str)
    }
    selected_audit = [
        item
        for item in audit
        if isinstance(item, dict) and item.get("target_id") in selected_target_ids
    ]
    evidence_reference_ids = sorted(
        {
            evidence_id
            for item in [*selected_nodes, *selected_edges, *selected_supplements]
            for evidence_id in item.get("evidence_ids", [])
            if isinstance(evidence_id, str)
        }
    )
    industry_chain_analysis = _industry_chain_analysis_for_read(
        ledger,
        nodes=selected_nodes,
        edges=selected_edges,
        supplements=selected_supplements,
    )
    key_person_relationship_analysis = _key_person_relationship_analysis_for_read(
        industry_chain_analysis
    )
    return {
        "lead_id": normalized_lead_id,
        "nodes": copy.deepcopy(selected_nodes),
        # 对旧版关系边只在返回副本中补齐等待复核状态，查询过程不写台账也不造审计。
        "edges": [_edge_for_read(edge) for edge in selected_edges],
        "edge_evidence_supplements": copy.deepcopy(selected_supplements),
        "audit": copy.deepcopy(selected_audit),
        "evidence_reference_ids": evidence_reference_ids,
        "review_state_filter": normalized_review_state,
        "review_state_summary": review_state_summary,
        "industry_chain_analysis": industry_chain_analysis,
        "key_person_relationship_analysis": key_person_relationship_analysis,
        "node_count": len(selected_nodes),
        "edge_count": len(selected_edges),
        "read_only": True,
        "operation_scope": "evidence_backed_relationship_graph_read_only",
        "ledger_path": str(ledger_path(workspace_dir)),
    }


def _relationship_review_queue_limit(value: object) -> int:
    """校验只读关系待办队列的展示上限，防止模型或调用方发起无界历史读取。

    输入为调用方请求的整数上限，输出为 1 至固定最大值之间的安全值；该限制只影响本次
    响应中显示多少待办，不删除、隐藏写入或改变图谱关系。函数无权限判断和写入行为，
    不读取网络，也不会接触任何证据正文、来源 URL、采集时间或人工复核记录。
    """
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("limit必须是关系待办队列允许范围内的整数")
    if not 1 <= value <= MAX_RELATIONSHIP_REVIEW_QUEUE_LIMIT:
        raise ValueError(
            f"limit必须是1到{MAX_RELATIONSHIP_REVIEW_QUEUE_LIMIT}之间的整数"
        )
    return value


def _queue_linked_leads_for_edge(
    edge_analysis: dict[str, Any],
    *,
    nodes_by_id: dict[str, dict[str, Any]],
    leads_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """读取一条边两端已关联的招商线索摘要，缺少登记对象时拒绝猜测优先级。

    输入为图谱只读分析卡片、图谱节点索引和当前工作区线索索引；输出为去重后的关联线索
    ID、名称、产业、人工状态和已存在的红色预警标记。红色预警只来自已保存 lead 字段，
    绝不由本函数新增或升级。节点或线索缺失时直接报错，避免把未知关系误排为低优先级；
    函数无操作者参数、无权限放宽、不写入节点、线索、证据或审计。
    """
    linked_lead_ids: set[str] = set()
    for endpoint_key in ("from_node", "to_node"):
        endpoint = edge_analysis.get(endpoint_key)
        if not isinstance(endpoint, dict):
            raise ValueError("关系待办缺少图谱端点信息")
        node_id = _required_text(endpoint.get("node_id"), "关系边端点 node_id", max_length=100)
        node = nodes_by_id.get(node_id)
        if node is None:
            raise ValueError("关系待办端点节点不存在，无法安全关联招商线索")
        linked_lead_id = node.get("linked_lead_id")
        if linked_lead_id is not None:
            linked_lead_ids.add(_required_text(linked_lead_id, "linked_lead_id", max_length=100))

    lead_summaries: list[dict[str, Any]] = []
    for linked_lead_id in sorted(linked_lead_ids):
        lead = leads_by_id.get(linked_lead_id)
        if lead is None:
            raise ValueError("关系待办关联的招商线索不存在，无法安全生成优先级")
        entity_name = _required_text(lead.get("entity_name"), "线索 entity_name", max_length=300)
        sector = lead.get("sector")
        review_state = lead.get("review_state")
        if not isinstance(sector, str) or not isinstance(review_state, str):
            raise ValueError("关系待办关联线索缺少产业或人工状态")
        lead_summaries.append(
            {
                "lead_id": linked_lead_id,
                "entity_name": entity_name,
                "sector": sector,
                "review_state": review_state,
                "red_alert": has_open_enterprise_risk_alert(lead),
            }
        )
    return lead_summaries


def _relationship_review_priority(
    edge_analysis: dict[str, Any],
    *,
    linked_leads: list[dict[str, Any]],
) -> tuple[int, str, list[str]]:
    """按固定、可审查规则给未复核关系边排序，不计算招商价值或自动作出业务决定。

    输入为已登记关系边的只读分析卡片及关联线索摘要，输出为优先级数字、稳定标签和逐条
    规则原因。排序只使用已保存的红色预警、人工复核状态、受控产业链类型和已登记两端
    本地属性；不读取模型文本、评分权重、融资金额或网页内容。该函数不会标红、复核、
    推进、搁置或改写任何线索、关系、证据和审计，最终业务决定仍由普通登录用户完成。
    """
    review_state = edge_analysis.get("review_state")
    relationship_type = edge_analysis.get("relationship_type")
    from_node = edge_analysis.get("from_node")
    to_node = edge_analysis.get("to_node")
    if not isinstance(review_state, str) or review_state not in {
        "pending_user_review",
        "need_evidence",
    }:
        raise ValueError("关系待办只能包含等待复核或待补证状态")
    if not isinstance(relationship_type, str):
        raise ValueError("关系待办缺少 relationship_type")
    if not isinstance(from_node, dict) or not isinstance(to_node, dict):
        raise ValueError("关系待办缺少两端节点本地属性")
    localities = {from_node.get("locality"), to_node.get("locality")}
    is_xuzhou_external = localities == {"xuzhou_local", "target_or_external"}
    is_industry_chain = relationship_type in INDUSTRY_CHAIN_RELATIONSHIP_TYPES
    # ``linked_leads`` 是上一层已用 ``has_open_enterprise_risk_alert`` 过滤后的最小摘要，
    # 为避免把未携带风险字段的摘要再误判为旧版记录，这里只读取其已验证的布尔投影。
    has_linked_red_alert = any(lead.get("red_alert") is True for lead in linked_leads)

    if has_linked_red_alert:
        return (
            0,
            "P0_red_alert_relation_review",
            ["关联招商线索已有红色预警，关系边仍需人工复核。"],
        )
    if review_state == "need_evidence" and is_xuzhou_external and is_industry_chain:
        return (
            1,
            "P1_xuzhou_external_industry_chain_need_evidence",
            ["该边连接徐州本地与目标或外地节点，且属于已登记产业链类型。", "人工已标记待补证。"],
        )
    if review_state == "need_evidence":
        return (
            2,
            "P2_need_evidence",
            ["人工已标记待补证，应先补充或核验原始来源。"],
        )
    if is_xuzhou_external and is_industry_chain:
        return (
            3,
            "P3_xuzhou_external_industry_chain_pending_review",
            ["该边连接徐州本地与目标或外地节点，且属于已登记产业链类型。", "仍等待普通登录用户复核。"],
        )
    return (
        4,
        "P4_pending_user_review",
        ["该边仍等待普通登录用户复核。"],
    )


def _queue_evidence_reference_ids(edge_analysis: dict[str, Any]) -> list[str]:
    """提取关系边原始与追加证据 ID，供人工追溯而不暴露或修改原始采集内容。

    输入为图谱只读边分析卡片，输出为排序去重后的证据 ID。函数严格要求原始边和补证的
    引用均为非空字符串列表，异常即失败而不忽略；它不读取证据正文、不返回来源 URL，
    不创建补证或改写证据链，实际补证仍须经过服务器认证的业务写入接口。
    """
    evidence_support = edge_analysis.get("evidence_support")
    if not isinstance(evidence_support, dict):
        raise ValueError("关系待办缺少证据覆盖信息")
    original_ids = evidence_support.get("original_edge_evidence_ids")
    supplement_ids = evidence_support.get("supplement_evidence_ids")
    if not all(
        isinstance(ids, list) and all(isinstance(item, str) and item for item in ids)
        for ids in (original_ids, supplement_ids)
    ):
        raise ValueError("关系待办缺少可追溯证据 ID")
    return sorted(set(original_ids) | set(supplement_ids))


def get_relationship_review_queue(
    workspace_dir: Path,
    *,
    limit: int = DEFAULT_RELATIONSHIP_REVIEW_QUEUE_LIMIT,
) -> dict[str, Any]:
    """只读汇总当前工作区待人工复核的关系边，并用固定规则说明排序原因。

    输入为工作区和受限展示上限，输出仅包含等待用户复核或待补证的关系边、关联线索
    摘要、证据 ID、固定优先级和可执行人工核验原因。所有普通用户可查看自己当前工作区
    的业务待办；函数不接收客户端用户 ID，不检查管理员身份，也不跨工作区读取数据。
    它不联网、不自动复核、不自动标红、不推进线索、不调整权重，更不会修改原始证据、
    来源 URL、摘录、采集时间、关系边或审计记录。
    """
    normalized_limit = _relationship_review_queue_limit(limit)
    ledger = load_ledger(workspace_dir)
    lead_records = ledger.get("leads")
    if not isinstance(lead_records, list):
        raise ValueError("招商线索台账中的 leads 必须是列表")
    leads_by_id: dict[str, dict[str, Any]] = {}
    for lead in lead_records:
        if not isinstance(lead, dict):
            continue
        lead_id = lead.get("lead_id")
        if not isinstance(lead_id, str) or not lead_id:
            raise ValueError("招商线索缺少 lead_id，无法安全生成关系待办")
        if lead_id in leads_by_id:
            raise ValueError("招商线索存在重复 lead_id，无法安全生成关系待办")
        leads_by_id[lead_id] = lead

    graph = get_relationship_graph(workspace_dir)
    if graph.get("read_only") is not True:
        raise RuntimeError("关系图谱查询未处于只读模式，拒绝生成关系待办队列")
    raw_nodes = graph.get("nodes")
    raw_edges = graph.get("edges")
    analysis = graph.get("industry_chain_analysis")
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list) or not isinstance(analysis, dict):
        raise ValueError("关系图谱缺少待办队列所需字段")
    raw_edge_analysis = analysis.get("edge_analysis")
    if not isinstance(raw_edge_analysis, list):
        raise ValueError("关系图谱缺少边研判列表")
    nodes_by_id: dict[str, dict[str, Any]] = {}
    for node in raw_nodes:
        if not isinstance(node, dict):
            raise ValueError("关系图谱节点必须是对象")
        node_id = _required_text(node.get("node_id"), "node_id", max_length=100)
        if node_id in nodes_by_id:
            raise ValueError("关系图谱存在重复 node_id，无法安全生成关系待办")
        nodes_by_id[node_id] = node
    edge_created_at_by_id: dict[str, str] = {}
    for edge in raw_edges:
        if not isinstance(edge, dict):
            raise ValueError("关系图谱边必须是对象")
        edge_id = _required_text(edge.get("edge_id"), "edge_id", max_length=100)
        created_at = edge.get("created_at")
        if not isinstance(created_at, str) or not created_at:
            raise ValueError("关系图谱边缺少 created_at，无法安全排序待办")
        if edge_id in edge_created_at_by_id:
            raise ValueError("关系图谱存在重复 edge_id，无法安全生成关系待办")
        edge_created_at_by_id[edge_id] = created_at

    queued_items: list[dict[str, Any]] = []
    for edge_analysis in raw_edge_analysis:
        if not isinstance(edge_analysis, dict):
            raise ValueError("关系图谱边研判必须是对象")
        if edge_analysis.get("review_state") not in {"pending_user_review", "need_evidence"}:
            continue
        edge_id = _required_text(edge_analysis.get("edge_id"), "edge_id", max_length=100)
        registered_at = edge_created_at_by_id.get(edge_id)
        if registered_at is None:
            raise ValueError("关系待办边未找到原始图谱登记时间")
        linked_leads = _queue_linked_leads_for_edge(
            edge_analysis,
            nodes_by_id=nodes_by_id,
            leads_by_id=leads_by_id,
        )
        priority_rank, priority, priority_reasons = _relationship_review_priority(
            edge_analysis,
            linked_leads=linked_leads,
        )
        queued_items.append(
            {
                "edge_id": edge_id,
                "registered_at": registered_at,
                "priority_rank": priority_rank,
                "priority": priority,
                "priority_reasons": priority_reasons,
                "relationship_type": copy.deepcopy(edge_analysis.get("relationship_type")),
                "relationship_category": copy.deepcopy(edge_analysis.get("relationship_category")),
                "business_flow_direction": copy.deepcopy(edge_analysis.get("business_flow_direction")),
                "from_node": copy.deepcopy(edge_analysis.get("from_node")),
                "to_node": copy.deepcopy(edge_analysis.get("to_node")),
                "linked_leads": linked_leads,
                "review_state": copy.deepcopy(edge_analysis.get("review_state")),
                "pending_evidence_reason": copy.deepcopy(edge_analysis.get("pending_evidence_reason")),
                "evidence_reference_ids": _queue_evidence_reference_ids(edge_analysis),
                "evidence_support": copy.deepcopy(edge_analysis.get("evidence_support")),
                "requires_human_action": True,
            }
        )
    queued_items.sort(
        key=lambda item: (item["priority_rank"], item["registered_at"], item["edge_id"])
    )
    priority_counts = Counter(item["priority"] for item in queued_items)
    review_state_counts = Counter(item["review_state"] for item in queued_items)
    xuzhou_external_industry_chain_count = sum(
        item["priority"] in {
            "P1_xuzhou_external_industry_chain_need_evidence",
            "P3_xuzhou_external_industry_chain_pending_review",
        }
        for item in queued_items
    )
    return {
        "queue_type": "xuzhou_relationship_manual_review_queue_readonly_v1",
        "priority_rules": [
            {
                "priority_rank": 0,
                "priority": "P0_red_alert_relation_review",
                "rule": "关联招商线索已有红色预警且关系边仍待人工处理。",
            },
            {
                "priority_rank": 1,
                "priority": "P1_xuzhou_external_industry_chain_need_evidence",
                "rule": "徐州—外地已登记产业链边被人工标记为待补证。",
            },
            {
                "priority_rank": 2,
                "priority": "P2_need_evidence",
                "rule": "其他被人工标记为待补证的关系边。",
            },
            {
                "priority_rank": 3,
                "priority": "P3_xuzhou_external_industry_chain_pending_review",
                "rule": "徐州—外地已登记产业链边仍等待普通登录用户复核。",
            },
            {
                "priority_rank": 4,
                "priority": "P4_pending_user_review",
                "rule": "其他仍等待普通登录用户复核的关系边。",
            },
        ],
        "summary": {
            "queued_item_count": len(queued_items),
            "priority_counts": dict(sorted(priority_counts.items())),
            "review_state_counts": dict(sorted(review_state_counts.items())),
            "xuzhou_external_industry_chain_queue_item_count": xuzhou_external_industry_chain_count,
        },
        "items": copy.deepcopy(queued_items[:normalized_limit]),
        "shown_item_count": min(len(queued_items), normalized_limit),
        "truncated": len(queued_items) > normalized_limit,
        "read_only": True,
        "operation_scope": "relationship_graph_fixed_rule_manual_review_queue_read_only",
        "ledger_path": str(ledger_path(workspace_dir)),
    }
