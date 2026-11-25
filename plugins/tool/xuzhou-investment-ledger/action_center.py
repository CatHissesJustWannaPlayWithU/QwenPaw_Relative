"""徐州招商智能体的只读人工行动中心。

本模块将已存在的企业风险红色预警、关系图谱待办、来源时效和多来源事件对齐提示集中为可轮询
的人工队列，并为单条线索生成招商准备卡。它不联网、不发送外部消息、不自动推进线索，
更不会将“多个来源”或“来源较早”写成企业事实冲突或无效结论。
"""

from __future__ import annotations

import copy
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .co_investment_signal import list_co_investment_signal_drafts
from .core import SUPPORTED_CLAIM_TYPE_TEXT_SUPPORTS, has_open_enterprise_risk_alert
from .relationship_graph import get_relationship_review_queue
from .relationship_draft import list_relationship_drafts
from .storage import ledger_path, load_ledger


# 来源公开日期超过此固定天数只会提示人工判断时效性；融资历程、历史合作等旧事件可能仍
# 然具有业务价值，因此绝不能据此自动失效、降分或撤销线索。
EVIDENCE_FRESHNESS_REVIEW_DAYS = 180
DEFAULT_ACTION_CENTER_LIMIT = 50
MAX_ACTION_CENTER_LIMIT = 200
MAX_ALIGNMENT_SOURCE_CARDS = 10


def _utc_now() -> str:
    """返回行动中心生成时的服务器 UTC 时间，不接受模型或前端提供的时间。"""
    return datetime.now(timezone.utc).isoformat()


def _required_identifier(value: object, field_name: str) -> str:
    """校验只读查询所用的服务器 ID，拒绝空白、路径符号和无界输入。"""
    if not isinstance(value, str) or not (identifier := value.strip()) or len(identifier) > 120:
        raise ValueError(f"{field_name}必须是有效的服务器 ID")
    if any(character.isspace() or character in "/\\?&#" for character in identifier):
        raise ValueError(f"{field_name}必须是有效的服务器 ID")
    return identifier


def _validate_limit(value: object) -> int:
    """限制行动中心只读展示数量，防止调用方发起无界历史扫描。

    输入为请求展示上限，输出为 1–200 内的受控整数。该限制只截断本次返回的行动卡片，
    不删除、隐藏写入或改变红色预警、关系边、证据、评分和人工复核；函数不做权限判断、
    不联网也不读写台账。
    """
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("limit必须是1到200之间的整数")
    if not 1 <= value <= MAX_ACTION_CENTER_LIMIT:
        raise ValueError("limit必须是1到200之间的整数")
    return value


def _source_domain(source_url: object) -> str | None:
    """从已保存来源 URL 提取域名，仅用于人工对齐提示而非真实性判断。"""
    if not isinstance(source_url, str):
        return None
    hostname = urlparse(source_url).hostname
    return hostname.casefold().rstrip(".") if hostname else None


def _source_date(value: object) -> date | None:
    """解析证据已保存的公开日期；异常日期只标为无法评估而不擅自修复。"""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _ledger_records(ledger: dict[str, Any], field_name: str) -> list[dict[str, Any]]:
    """读取行动中心所需台账列表，结构异常时失败关闭而不猜测或改写历史数据。"""
    records = ledger.get(field_name)
    if not isinstance(records, list):
        raise ValueError(f"招商线索台账中的 {field_name} 必须是列表")
    return [record for record in records if isinstance(record, dict)]


def _source_card(evidence: dict[str, Any]) -> dict[str, object]:
    """输出行动中心所需的最小证据卡片，不暴露私有快照路径、原始字节或完整性密钥。

    输入是一条既有正式证据，输出其 ID、来源链接、公开日期、来源等级、事实类别、质量
    和已保存摘录。卡片只重述台账事实，绝不从摘录推断企业意愿或关系真实性；函数不接收
    操作者身份、不联网、不修改 URL、摘录、采集时间、哈希、评分或人工状态。
    """
    fields = (
        "evidence_id",
        "source_url",
        "source_title",
        "source_published_at",
        "source_level",
        "claim_type",
        "evidence_quality",
        "excerpt",
    )
    if any(field_name not in evidence for field_name in fields):
        raise ValueError("行动中心证据缺少来源卡片字段")
    if not all(
        isinstance(evidence[field_name], str)
        for field_name in (*fields[:6], "excerpt")
    ):
        raise ValueError("行动中心证据来源卡片字段格式异常")
    if not isinstance(evidence["evidence_quality"], int) or isinstance(
        evidence["evidence_quality"], bool
    ):
        raise ValueError("行动中心证据质量字段格式异常")
    card = {field_name: copy.deepcopy(evidence[field_name]) for field_name in fields}
    # 历史证据没有新版分类元数据时明确标为未核验，而非假定旧 ``claim_type`` 已被原文支持。
    # 这只影响行动中心怎样解释分类，不改动已签名的旧证据或其原始摘录。
    card.update(
        {
            "claim_type_requested": copy.deepcopy(evidence.get("claim_type_requested")),
            "claim_type_matched_terms": copy.deepcopy(evidence.get("claim_type_matched_terms", [])),
            "claim_type_text_support": copy.deepcopy(
                evidence.get("claim_type_text_support", "legacy_unverified")
            ),
            "claim_type_classification_method": copy.deepcopy(
                evidence.get("claim_type_classification_method")
            ),
        }
    )
    return card


def _has_server_supported_claim_type(evidence: dict[str, Any]) -> bool:
    """判断一条证据的归档类别是否有服务器固定原文词支持。

    输入是已保存证据，输出为是否可把其 ``claim_type`` 用作行动中心分组或动作触发。只有
    新版 ``matched_fixed_terms`` 或服务端覆盖得到的 ``server_reclassified_risk`` 可以通过；
    旧记录、无词匹配记录和任何未知状态都只能供人工查看原文，不能据此生成融资、合作或
    风险动作。函数纯读取、不改证据、评分、线索或人工状态。
    """
    return evidence.get("claim_type_text_support") in SUPPORTED_CLAIM_TYPE_TEXT_SUPPORTS


def _lead_summary(lead: dict[str, Any]) -> dict[str, object]:
    """输出待办所需的最小线索摘要，不自动变化企业风险预警或人工业务状态。

    输入是一条已验证台账线索，输出只读摘要。新版风险字段必须来自 core.py 保存的正式
    证据判断；缺少字段的历史线索按“无结构化风险卡”展示，绝不把旧版机会预警猜成企业
    风险。函数不联网、不写台账、不修改证据、状态或评分。
    """
    fields = ("lead_id", "entity_name", "sector", "review_state")
    if not all(isinstance(lead.get(field_name), str) and lead[field_name] for field_name in fields):
        raise ValueError("行动中心线索缺少标准摘要字段")
    return {
        **{field_name: copy.deepcopy(lead[field_name]) for field_name in fields},
        "red_alert": has_open_enterprise_risk_alert(lead),
        "alert_reason": copy.deepcopy(lead.get("alert_reason")),
        "risk_alert_level": copy.deepcopy(lead.get("risk_alert_level")),
        "risk_alert_type": copy.deepcopy(lead.get("risk_alert_type")),
        "risk_alert_status": copy.deepcopy(lead.get("risk_alert_status")),
        "risk_alert_evidence_ids": copy.deepcopy(lead.get("risk_alert_evidence_ids", [])),
    }


def _evidence_by_lead(
    evidence_records: list[dict[str, Any]],
    *,
    leads_by_id: dict[str, dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """按不可变 lead_id 分组正式证据，拒绝孤儿记录以防待办归属被错误猜测。

    输入是台账正式证据和已验证线索索引，输出按线索分组的证据副本引用。证据必须引用
    当前工作区真实线索，否则直接报错；函数不补写 lead_id、不移动证据、不改变完整性链，
    也不依据来源标题或模型文本猜测其归属。
    """
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for evidence in evidence_records:
        lead_id = _required_identifier(evidence.get("lead_id"), "evidence.lead_id")
        if lead_id not in leads_by_id:
            raise ValueError("行动中心发现证据关联了不存在的招商线索")
        grouped[lead_id].append(evidence)
    return grouped


def _red_alert_items(
    leads: list[dict[str, Any]],
    *,
    evidence_by_lead: dict[str, list[dict[str, Any]]],
) -> list[dict[str, object]]:
    """构造已有企业风险红色预警的人工复核卡片，不重新判断或创建任何预警。

    输入为已存线索及其证据索引，输出当前仍带 ``red_alert`` 且具有明确风险等级、类型和
    触发证据的最小线索卡片。融资、扩产、合作等旧版机会预警若没有新版风险字段，一律
    不展示为企业风险；无效线索不会进入待办。返回内容完全沿用 core.py 已保存字段，不会
    因本函数读取而标红、推进或改写证据链。
    """
    items: list[dict[str, object]] = []
    for lead in leads:
        if not has_open_enterprise_risk_alert(lead) or lead.get("review_state") == "invalid":
            continue
        lead_summary = _lead_summary(lead)
        lead_id = str(lead_summary["lead_id"])
        risk_level = lead_summary["risk_alert_level"]
        risk_type = lead_summary["risk_alert_type"]
        risk_status = lead_summary["risk_alert_status"]
        risk_evidence_ids = lead_summary["risk_alert_evidence_ids"]
        if (
            risk_level not in {"critical", "high"}
            or not isinstance(risk_type, str)
            or risk_status not in {"open", "need_evidence", "observe"}
            or not isinstance(risk_evidence_ids, list)
            or not all(isinstance(evidence_id, str) for evidence_id in risk_evidence_ids)
        ):
            continue
        cards = sorted(
            (
                _source_card(evidence)
                for evidence in evidence_by_lead.get(lead_id, [])
                if evidence.get("evidence_id") in risk_evidence_ids
            ),
            key=lambda card: (str(card["source_published_at"]), str(card["evidence_id"])),
            reverse=True,
        )
        items.append(
            {
                "item_kind": "red_alert_review",
                "action_priority_rank": 0,
                "lead": lead_summary,
                "action": "review_existing_red_alert",
                "risk": {
                    "level": risk_level,
                    "type": risk_type,
                    "status": risk_status,
                    "reason": lead_summary["alert_reason"],
                    "trigger_evidence_ids": sorted(set(risk_evidence_ids)),
                },
                "reason": "该线索有已签名正式证据支持的企业风险，需由普通登录用户决定推进、待补证、观察或无效。",
                "evidence_cards": cards[:MAX_ALIGNMENT_SOURCE_CARDS],
                "evidence_cards_truncated": len(cards) > MAX_ALIGNMENT_SOURCE_CARDS,
                "automatic_business_conclusion": "not_generated",
            }
        )
    return items


def _alignment_items(
    evidence_by_lead: dict[str, list[dict[str, Any]]],
    *,
    leads_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, object]]:
    """识别同线索、同事实类别、同公开日期的多来源记录，提示人工事件对齐而非声称冲突。

    输入为按线索分组的正式证据与线索索引，输出仅在两个以上不同来源域名出现时的待办。
    该规则不能判断来源是否矛盾、是否真实或是否描述同一事件；它只提醒人工对照原文、
    日期和链接。函数不合并、覆盖、删除或降权任何证据，不改线索或关系图谱，也不需要
    管理员身份。
    """
    items: list[dict[str, object]] = []
    for lead_id, records in evidence_by_lead.items():
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for evidence in records:
            if not _has_server_supported_claim_type(evidence):
                continue
            claim_type = evidence.get("claim_type")
            published_at = evidence.get("source_published_at")
            if isinstance(claim_type, str) and isinstance(published_at, str):
                grouped[(claim_type, published_at)].append(evidence)
        for (claim_type, published_at), grouped_records in grouped.items():
            domains = {
                domain
                for domain in (_source_domain(record.get("source_url")) for record in grouped_records)
                if domain is not None
            }
            if len(domains) < 2:
                continue
            cards = sorted(
                (_source_card(record) for record in grouped_records),
                key=lambda card: (str(card["source_url"]), str(card["evidence_id"])),
            )
            items.append(
                {
                    "item_kind": "multi_source_event_alignment",
                    "action_priority_rank": 2,
                    "lead": _lead_summary(leads_by_id[lead_id]),
                    "claim_type": claim_type,
                    "source_published_at": published_at,
                    "unique_source_domains": sorted(domains),
                    "action": "align_source_records_manually",
                    "reason": "同一线索、事实类别和公开日期存在多个来源域名；请人工核对是否指向同一具体事件，不能自动视为互相印证或冲突。",
                    "evidence_cards": cards[:MAX_ALIGNMENT_SOURCE_CARDS],
                    "evidence_cards_truncated": len(cards) > MAX_ALIGNMENT_SOURCE_CARDS,
                    "automatic_business_conclusion": "not_generated",
                }
            )
    return items


def _freshness_items(
    evidence_by_lead: dict[str, list[dict[str, Any]]],
    *,
    leads_by_id: dict[str, dict[str, Any]],
    evaluated_on: date,
) -> list[dict[str, object]]:
    """列出公开日期超过固定阈值的来源，提示人工判断是否仍适用于当前招商工作。

    输入为证据分组、线索索引和服务器日期，输出公开日期距今至少 180 天的来源卡片及
    天数。历史融资、合作和团队经历可能长期有效，因此“较早”绝不等于过期、失实或
    自动降权；无效日期也不被伪造成旧日期。函数不联网、不重新采集、不改变证据、评分、
    预警、关系或人工复核。
    """
    items: list[dict[str, object]] = []
    for lead_id, records in evidence_by_lead.items():
        for evidence in records:
            published_on = _source_date(evidence.get("source_published_at"))
            if published_on is None:
                continue
            age_days = (evaluated_on - published_on).days
            if age_days < EVIDENCE_FRESHNESS_REVIEW_DAYS:
                continue
            items.append(
                {
                    "item_kind": "source_freshness_review",
                    "action_priority_rank": 3,
                    "lead": _lead_summary(leads_by_id[lead_id]),
                    "source_published_at": published_on.isoformat(),
                    "age_days": age_days,
                    "freshness_review_threshold_days": EVIDENCE_FRESHNESS_REVIEW_DAYS,
                    "action": "confirm_current_relevance_manually",
                    "reason": "来源公开日期距离当前服务器日期达到固定复核阈值；请人工判断该历史事实是否仍适用于当前招商研判。",
                    "evidence_card": _source_card(evidence),
                    "automatic_business_conclusion": "not_generated",
                }
            )
    return items


def _industry_metric_draft_review_items(
    metric_drafts: list[dict[str, Any]],
    metric_draft_reviews: list[dict[str, Any]],
    evidence_records: list[dict[str, Any]],
    *,
    leads_by_id: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, object]], int]:
    """构造可回链正式证据的经营指标人工复核待办，绝不确认指标或改变线索。

    输入是指标草稿、只追加复核审计、正式证据及当前线索索引，输出为待复核行动卡片和
    因字段异常、复核完成或证据归属不一致而排除的数量。函数只接受仍为
    ``pending_user_review`` 且草稿的实体、产业、lead_id、evidence_id 均能与当前台账
    对齐的记录；金额、年份和原始指标词只作为待核验内容展示，不能被理解为已经确认的
    经营事实。函数不联网、不写审计、不调用指标确认接口，不改 evidence、来源 URL、
    原始摘录、采集时间、线索状态、评分或预警。
    """

    reviewed_draft_ids = {
        review.get("metric_draft_id")
        for review in metric_draft_reviews
        if isinstance(review.get("metric_draft_id"), str)
        and review.get("new_state") in {"confirmed", "need_evidence", "invalid"}
    }
    evidence_by_id: dict[str, dict[str, Any]] = {}
    for evidence in evidence_records:
        evidence_id = evidence.get("evidence_id")
        if not isinstance(evidence_id, str) or not evidence_id or evidence_id in evidence_by_id:
            raise ValueError("行动中心发现重复或异常 evidence_id，无法安全关联指标草稿")
        evidence_by_id[evidence_id] = evidence

    items: list[dict[str, object]] = []
    excluded_count = 0
    for draft in metric_drafts:
        draft_id = draft.get("metric_draft_id")
        if not isinstance(draft_id, str) or not draft_id:
            excluded_count += 1
            continue
        if draft.get("state") != "pending_user_review" or draft_id in reviewed_draft_ids:
            # 草稿确认状态由只追加审计派生；已处理草稿绝不重新排队或改写其原始字段。
            continue
        required_text_fields = (
            "lead_id",
            "evidence_id",
            "entity_name",
            "sector",
            "metric_kind",
            "metric_label",
            "source_metric_label",
            "statistic_scope",
            "original_value_text",
        )
        if any(
            not isinstance(draft.get(field_name), str) or not draft[field_name]
            for field_name in required_text_fields
        ):
            excluded_count += 1
            continue
        lead_id = draft["lead_id"]
        evidence_id = draft["evidence_id"]
        lead = leads_by_id.get(lead_id)
        evidence = evidence_by_id.get(evidence_id)
        value_yuan = draft.get("value_yuan")
        period_year = draft.get("period_year")
        if (
            lead is None
            or evidence is None
            or evidence.get("lead_id") != lead_id
            or draft["entity_name"] != lead.get("entity_name")
            or draft["sector"] != lead.get("sector")
            or not isinstance(value_yuan, int)
            or isinstance(value_yuan, bool)
            or value_yuan <= 0
            or not isinstance(period_year, int)
            or isinstance(period_year, bool)
            or not 2000 <= period_year <= 2100
        ):
            excluded_count += 1
            continue
        items.append(
            {
                "item_kind": "industry_metric_draft_manual_review",
                "action_priority_rank": 1,
                "lead": _lead_summary(lead),
                "action": "review_industry_metric_draft_manually",
                "reason": "该经营指标仅由已签名证据摘录自动提取，须普通登录用户核对原始来源、统计年份、范围、单位和数值后确认、待补证或作废。",
                "metric_draft": {
                    "metric_draft_id": draft_id,
                    "metric_kind": draft["metric_kind"],
                    "metric_label": draft["metric_label"],
                    "source_metric_label": draft["source_metric_label"],
                    "statistic_scope": draft["statistic_scope"],
                    "period_year": period_year,
                    "value_yuan": value_yuan,
                    "original_value_text": draft["original_value_text"],
                },
                # 来源卡片只取正式 evidence，不能相信草稿中复制的 URL 或日期来替换证据链。
                "evidence_card": _source_card(evidence),
                "automatic_business_conclusion": "not_generated",
            }
        )
    return items, excluded_count


def _relationship_draft_action_items(
    relationship_drafts: list[object],
    evidence_records: list[dict[str, Any]],
    *,
    leads_by_id: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, object]], int]:
    """把关键人员关系草稿投影为只读人工待办，不创建节点、边或个人招商结论。

    输入是只读草稿视图、既有正式证据和已验证线索索引；输出为仍待业务复核、待补证或已
    人工允许登记图谱的行动卡片，以及结构/归属异常的排除数量。草稿必须能回指同一线索的
    既有 ``evidence_id``，否则宁可排除；行动中心只提示用户调用认证草稿/图谱接口，绝不
    自动建图、改变草稿状态或改写 URL、原始摘录、采集时间、评分、线索和证据链。
    """

    evidence_by_id: dict[str, dict[str, Any]] = {}
    for evidence in evidence_records:
        evidence_id = evidence.get("evidence_id")
        if not isinstance(evidence_id, str) or not evidence_id or evidence_id in evidence_by_id:
            raise ValueError("行动中心发现重复或异常 evidence_id，无法安全关联关系草稿")
        evidence_by_id[evidence_id] = evidence
    items: list[dict[str, object]] = []
    excluded_count = 0
    for draft in relationship_drafts:
        if not isinstance(draft, dict):
            excluded_count += 1
            continue
        draft_id = draft.get("relationship_draft_id")
        lead_id = draft.get("lead_id")
        evidence_id = draft.get("evidence_id")
        review_state = draft.get("review_state")
        graph_registration_state = draft.get("graph_registration_state")
        if not all(isinstance(value, str) and value for value in (draft_id, lead_id, evidence_id, review_state)):
            excluded_count += 1
            continue
        if review_state in {"observe", "invalid"} or graph_registration_state == "registered_pending_relationship_review":
            continue
        lead = leads_by_id.get(lead_id)
        evidence = evidence_by_id.get(evidence_id)
        if lead is None or evidence is None or evidence.get("lead_id") != lead_id:
            excluded_count += 1
            continue
        required_fields = (
            "organization_name",
            "person_display_name",
            "relationship_type",
            "person_locality_candidate",
            "matched_role_term",
        )
        if any(not isinstance(draft.get(field_name), str) or not draft[field_name] for field_name in required_fields):
            excluded_count += 1
            continue
        if draft["organization_name"] != lead.get("entity_name"):
            excluded_count += 1
            continue
        if review_state == "ready_for_graph_registration":
            item_kind = "relationship_draft_graph_registration"
            action = "register_reviewed_key_person_relationship_to_graph"
            priority = 1
            reason = "该公开关键人员角色已由普通登录用户人工标为可登记图谱；仍须明确调用认证接口，服务器会再次核验已签名证据后才创建一条等待用户复核的关系边。"
        elif review_state == "need_evidence":
            item_kind = "relationship_draft_need_evidence"
            action = "collect_traceable_evidence_for_key_person_relationship"
            priority = 2
            reason = "该公开关键人员关系草稿被标为待补证；应另行采集并确认可追溯正式证据，不能修改现有草稿或把推测写成事实。"
        elif review_state == "pending_user_review":
            item_kind = "relationship_draft_manual_review"
            action = "review_key_person_relationship_draft_manually"
            priority = 1
            reason = "该姓名、组织和角色仅由已签名原始摘录按固定规则抽取；普通登录用户须先核对来源后决定可登记图谱、待补证、观察或无效。"
        else:
            excluded_count += 1
            continue
        items.append(
            {
                "item_kind": item_kind,
                "action_priority_rank": priority,
                "lead": _lead_summary(lead),
                "action": action,
                "reason": reason,
                "relationship_draft": {
                    field_name: draft[field_name]
                    for field_name in (
                        "relationship_draft_id",
                        "person_display_name",
                        "relationship_type",
                        "person_locality_candidate",
                        "matched_role_term",
                        "matched_xuzhou_term",
                        "review_state",
                        "graph_registration_state",
                    )
                },
                "evidence_card": _source_card(evidence),
                "automatic_business_conclusion": "not_generated",
            }
        )
    return items, excluded_count


def _co_investment_signal_draft_action_items(
    co_investment_drafts: list[object],
    evidence_records: list[dict[str, Any]],
    *,
    leads_by_id: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, object]], int]:
    """把组织级协同投资信号草稿投影为人工待办，不生成资金池或外部联络。

    输入是只读草稿视图、已签名正式证据和已验证线索索引；输出为待人工复核/补证/主体联系
    核验的行动卡片及因字段或证据归属异常被排除的数量。草稿必须能回链同一线索的一手
    evidence，且实体、产业、来源 URL 与证据一致；否则宁可排除。函数只提示人工后续通过
    合规公开渠道核验组织和授权联系人，不收集个人资产或联系方式，不募资、不撮合、不自动
    推进线索，也不会改 evidence、URL、摘录、采集时间、评分、线索或审计。
    """

    evidence_by_id: dict[str, dict[str, Any]] = {}
    for evidence in evidence_records:
        evidence_id = evidence.get("evidence_id")
        if not isinstance(evidence_id, str) or not evidence_id or evidence_id in evidence_by_id:
            raise ValueError("行动中心发现重复或异常 evidence_id，无法安全关联协同投资信号草稿")
        evidence_by_id[evidence_id] = evidence
    items: list[dict[str, object]] = []
    excluded_count = 0
    for draft in co_investment_drafts:
        if not isinstance(draft, dict):
            excluded_count += 1
            continue
        draft_id = draft.get("co_investment_draft_id")
        lead_id = draft.get("lead_id")
        evidence_id = draft.get("evidence_id")
        review_state = draft.get("review_state")
        if not all(
            isinstance(value, str) and value
            for value in (draft_id, lead_id, evidence_id, review_state)
        ):
            excluded_count += 1
            continue
        if review_state in {"observe", "invalid"}:
            continue
        lead = leads_by_id.get(lead_id)
        evidence = evidence_by_id.get(evidence_id)
        if lead is None or evidence is None or evidence.get("lead_id") != lead_id:
            excluded_count += 1
            continue
        required_fields = (
            "entity_name",
            "sector",
            "signal_type",
            "matched_excerpt",
            "source_url",
            "source_published_at",
            "source_level",
        )
        if any(
            not isinstance(draft.get(field_name), str) or not draft[field_name]
            for field_name in required_fields
        ):
            excluded_count += 1
            continue
        if (
            draft["entity_name"] != lead.get("entity_name")
            or draft["sector"] != lead.get("sector")
            or draft["source_level"] != "primary"
            or draft["source_url"] != evidence.get("source_url")
            or draft["source_published_at"] != evidence.get("source_published_at")
        ):
            excluded_count += 1
            continue
        matched_terms = draft.get("matched_terms")
        xuzhou_tags = draft.get("xuzhou_association_tags")
        if (
            not isinstance(matched_terms, list)
            or not matched_terms
            or not all(isinstance(term, str) and term for term in matched_terms)
            or not isinstance(xuzhou_tags, list)
            or not all(isinstance(tag, str) for tag in xuzhou_tags)
        ):
            excluded_count += 1
            continue
        if review_state == "ready_for_contact_validation":
            item_kind = "co_investment_signal_contact_validation"
            action = "validate_public_organization_contact_and_authorization_manually"
            priority = 1
            reason = "该组织级公开协同投资信号已被普通登录用户标为可继续核验；仅可通过合规公开渠道人工确认主体和授权联系人，不得据此认定个人资金、出资承诺或自动发起联系。"
        elif review_state == "need_evidence":
            item_kind = "co_investment_signal_need_evidence"
            action = "collect_traceable_primary_evidence_for_co_investment_signal"
            priority = 2
            reason = "该协同投资信号被标为待补证；应另行采集可追溯一手公开材料，不能把中间人背书、资金猜测或现有草稿写成事实。"
        elif review_state == "pending_user_review":
            item_kind = "co_investment_signal_manual_review"
            action = "review_org_level_co_investment_signal_manually"
            priority = 1
            reason = "该组织级信号仅由已签名一手证据按固定词表抽取；普通登录用户须核对原始来源后决定可核验对接、待补证、观察或无效。"
        else:
            excluded_count += 1
            continue
        items.append(
            {
                "item_kind": item_kind,
                "action_priority_rank": priority,
                "lead": _lead_summary(lead),
                "action": action,
                "reason": reason,
                "co_investment_signal_draft": {
                    "co_investment_draft_id": draft_id,
                    "signal_type": draft["signal_type"],
                    "matched_terms": list(matched_terms),
                    "matched_excerpt": draft["matched_excerpt"],
                    "review_state": review_state,
                    "xuzhou_association_tags": list(xuzhou_tags),
                },
                "evidence_card": _source_card(evidence),
                "automatic_business_conclusion": "not_generated",
            }
        )
    return items, excluded_count


def get_investment_action_center(
    workspace_dir: Path,
    *,
    limit: int = DEFAULT_ACTION_CENTER_LIMIT,
) -> dict[str, object]:
    """只读汇总红色预警、关系/草稿/指标待办、来源对齐与时效复核，供人工轮询而非自动推送。

    输入是当前工作区和受限展示上限，输出按固定优先级整理的行动卡片与完整分类计数。
    所有普通用户可查看自身当前工作区的待办；函数没有客户端用户 ID、不会跨工作区读取，
    更不调用短信、邮件、企业微信或其他外部推送服务。它不联网、不自动复核、不自动
    标红、推进、搁置、确认指标或评分，不修改原始证据 URL、摘录、采集时间、哈希、关系
    边、指标草稿或审计。
    """
    normalized_limit = _validate_limit(limit)
    ledger = load_ledger(workspace_dir)
    leads = _ledger_records(ledger, "leads")
    evidence_records = _ledger_records(ledger, "evidence")
    metric_drafts = _ledger_records(ledger, "industry_metric_drafts")
    metric_draft_reviews = _ledger_records(ledger, "industry_metric_draft_reviews")
    leads_by_id: dict[str, dict[str, Any]] = {}
    for lead in leads:
        lead_id = _required_identifier(lead.get("lead_id"), "lead_id")
        if lead_id in leads_by_id:
            raise ValueError("行动中心发现重复 lead_id，无法安全生成待办")
        leads_by_id[lead_id] = lead
    evidence_by_lead = _evidence_by_lead(evidence_records, leads_by_id=leads_by_id)
    relationship_queue = get_relationship_review_queue(
        workspace_dir,
        limit=MAX_ACTION_CENTER_LIMIT,
    )
    raw_relationship_items = relationship_queue.get("items")
    if not isinstance(raw_relationship_items, list):
        raise ValueError("关系待办队列缺少 items")
    relationship_items = [
        {
            "item_kind": "relationship_manual_review",
            "action_priority_rank": 1,
            "relationship_queue_priority": copy.deepcopy(item.get("priority")),
            "action": "review_relationship_edge_manually",
            "reason": "关系边已有固定规则待办排序；必须由普通登录用户在认证接口完成复核或补证。",
            "relationship_item": copy.deepcopy(item),
            "automatic_business_conclusion": "not_generated",
        }
        for item in raw_relationship_items
        if isinstance(item, dict)
    ]
    relationship_draft_result = list_relationship_drafts(
        workspace_dir, limit=MAX_ACTION_CENTER_LIMIT
    )
    raw_relationship_drafts = relationship_draft_result.get("relationship_drafts")
    if not isinstance(raw_relationship_drafts, list):
        raise ValueError("关键人员关系草稿只读视图缺少 relationship_drafts")
    relationship_draft_items, excluded_relationship_draft_count = _relationship_draft_action_items(
        raw_relationship_drafts,
        evidence_records,
        leads_by_id=leads_by_id,
    )
    co_investment_signal_draft_result = list_co_investment_signal_drafts(
        workspace_dir, limit=MAX_ACTION_CENTER_LIMIT
    )
    raw_co_investment_signal_drafts = co_investment_signal_draft_result.get(
        "co_investment_signal_drafts"
    )
    if not isinstance(raw_co_investment_signal_drafts, list):
        raise ValueError("协同投资信号草稿只读视图缺少 co_investment_signal_drafts")
    co_investment_signal_draft_items, excluded_co_investment_signal_draft_count = (
        _co_investment_signal_draft_action_items(
            raw_co_investment_signal_drafts,
            evidence_records,
            leads_by_id=leads_by_id,
        )
    )
    metric_review_items, excluded_metric_draft_count = _industry_metric_draft_review_items(
        metric_drafts,
        metric_draft_reviews,
        evidence_records,
        leads_by_id=leads_by_id,
    )
    evaluated_on = datetime.now(timezone.utc).date()
    action_items = [
        *_red_alert_items(leads, evidence_by_lead=evidence_by_lead),
        *relationship_items,
        *relationship_draft_items,
        *co_investment_signal_draft_items,
        *metric_review_items,
        *_alignment_items(evidence_by_lead, leads_by_id=leads_by_id),
        *_freshness_items(
            evidence_by_lead,
            leads_by_id=leads_by_id,
            evaluated_on=evaluated_on,
        ),
    ]
    action_items.sort(
        key=lambda item: (
            item["action_priority_rank"],
            str(item.get("lead", {}).get("lead_id", ""))
            if isinstance(item.get("lead"), dict)
            else str(item.get("relationship_item", {}).get("edge_id", "")),
            str(item.get("item_kind", "")),
        )
    )
    item_counts = Counter(
        item["item_kind"] for item in action_items if isinstance(item.get("item_kind"), str)
    )
    return {
        "center_type": "xuzhou_investment_action_center_readonly_v4",
        "generated_at": _utc_now(),
        "freshness_review_threshold_days": EVIDENCE_FRESHNESS_REVIEW_DAYS,
        "summary": {
            "action_item_count": len(action_items),
            "item_kind_counts": dict(sorted(item_counts.items())),
            "red_alert_lead_count": len(
                [item for item in action_items if item.get("item_kind") == "red_alert_review"]
            ),
            "relationship_queue_summary": copy.deepcopy(relationship_queue.get("summary")),
            "pending_key_person_relationship_draft_action_count": len(relationship_draft_items),
            "excluded_invalid_or_untraceable_key_person_relationship_draft_count": excluded_relationship_draft_count,
            "pending_co_investment_signal_draft_action_count": len(co_investment_signal_draft_items),
            "excluded_invalid_or_untraceable_co_investment_signal_draft_count": excluded_co_investment_signal_draft_count,
            "pending_industry_metric_draft_review_count": len(metric_review_items),
            "excluded_invalid_or_untraceable_metric_draft_count": excluded_metric_draft_count,
        },
        "items": copy.deepcopy(action_items[:normalized_limit]),
        "shown_item_count": min(len(action_items), normalized_limit),
        "truncated": len(action_items) > normalized_limit,
        "delivery_boundary": {
            "external_push_sent": False,
            "reason": "插件只生成当前工作区可轮询的人工待办；外部推送必须由经授权的部署层配置，并另行处理账号、密钥、收件人与频率。",
        },
        "automated_business_conclusion": {
            "status": "not_generated",
            "reason": "行动排序仅用于人工查看顺序，不等同企业事实、关系冲突、招商意愿或自动推进决定。",
        },
        "read_only": True,
        "operation_scope": "readonly_existing_ledger_action_center_relationship_metric_and_co_investment_signal_review_queue_only",
        "ledger_path": str(ledger_path(workspace_dir)),
    }


def get_outreach_preparation_brief(
    workspace_dir: Path,
    *,
    lead_id: str,
) -> dict[str, object]:
    """为单条线索生成固定规则的招商准备卡，所有行动均须人工核验与授权后执行。

    输入是当前工作区和服务器已存在的 lead_id，输出根据已保存事实类别、徐州关联标签、
    企业风险预警和关系待办形成准备动作及对应 evidence_id。动作只表示“应核验/应准备何种
    材料”，不代表可以联系企业、承诺政策、认定意愿或自动推进。函数不接收联系人、
    通讯地址、用户 ID 或写入字段，不联网、不发送消息、不改变线索、证据、评分、关系
    图谱和人工复核；普通登录用户仍须通过认证业务接口自行决定下一步。
    """
    normalized_lead_id = _required_identifier(lead_id, "lead_id")
    ledger = load_ledger(workspace_dir)
    leads = _ledger_records(ledger, "leads")
    evidence_records = _ledger_records(ledger, "evidence")
    lead = next((item for item in leads if item.get("lead_id") == normalized_lead_id), None)
    if lead is None:
        raise ValueError("未找到对应的招商线索")
    lead_summary = _lead_summary(lead)
    lead_evidence = [
        evidence for evidence in evidence_records if evidence.get("lead_id") == normalized_lead_id
    ]
    claim_type_evidence_ids: dict[str, list[str]] = defaultdict(list)
    xuzhou_connection_evidence_ids: list[str] = []
    for evidence in lead_evidence:
        evidence_id = _required_identifier(evidence.get("evidence_id"), "evidence_id")
        claim_type = evidence.get("claim_type")
        if isinstance(claim_type, str) and _has_server_supported_claim_type(evidence):
            claim_type_evidence_ids[claim_type].append(evidence_id)
        # 关联标签可能只是调用方的待核验提示，不能单独触发招商动作；只有服务器已从原文
        # 支持 ``xuzhou_connection`` 分类的证据才可进入徐州关联准备卡。
        if (
            _has_server_supported_claim_type(evidence)
            and claim_type == "xuzhou_connection"
        ):
            xuzhou_connection_evidence_ids.append(evidence_id)
    relationship_queue = get_relationship_review_queue(
        workspace_dir,
        limit=MAX_ACTION_CENTER_LIMIT,
    )
    raw_relationship_items = relationship_queue.get("items")
    if not isinstance(raw_relationship_items, list):
        raise ValueError("关系待办队列缺少 items")
    related_relationship_items: list[dict[str, object]] = []
    for item in raw_relationship_items:
        if not isinstance(item, dict):
            raise ValueError("关系待办队列包含异常项目")
        linked_leads = item.get("linked_leads")
        if not isinstance(linked_leads, list):
            raise ValueError("关系待办项目缺少 linked_leads")
        if any(
            isinstance(linked_lead, dict) and linked_lead.get("lead_id") == normalized_lead_id
            for linked_lead in linked_leads
        ):
            related_relationship_items.append(item)
    action_cards: list[dict[str, object]] = []
    risk_evidence_ids = lead_summary.get("risk_alert_evidence_ids")
    if (
        lead_summary["red_alert"] is True
        and lead_summary.get("risk_alert_level") in {"critical", "high"}
        and isinstance(risk_evidence_ids, list)
        and all(isinstance(evidence_id, str) for evidence_id in risk_evidence_ids)
    ):
        action_cards.append(
            {
                "action_key": "review_existing_red_alert",
                "instruction": "先核验企业风险的原始来源、严重程度和当前人工状态，再决定继续推进、待补证、观察或无效。",
                "evidence_ids": sorted(set(risk_evidence_ids)),
            }
        )
    if any(key in claim_type_evidence_ids for key in {"financing", "fund_raising"}):
        action_cards.append(
            {
                "action_key": "prepare_financing_or_fund_resource_question_list",
                "instruction": "基于原文核验融资或募资时间、资金用途、阶段与资源需求，再准备可供人工沟通的载体和产业资源问题清单。",
                "evidence_ids": sorted(
                    set(claim_type_evidence_ids.get("financing", []))
                    | set(claim_type_evidence_ids.get("fund_raising", []))
                ),
            }
        )
    if xuzhou_connection_evidence_ids:
        action_cards.append(
            {
                "action_key": "verify_xuzhou_connection_before_outreach",
                "instruction": "先核验祖籍、成长经历、业务、供应链、高校或园区关联的原始来源，不能把标签本身当作招商意愿。",
                "evidence_ids": sorted(set(xuzhou_connection_evidence_ids)),
            }
        )
    if any(key in claim_type_evidence_ids for key in {"cooperation", "expansion"}):
        action_cards.append(
            {
                "action_key": "verify_landing_or_cooperation_requirement",
                "instruction": "核验合作、扩张、选址或地方资源需求是否仍有效，再由人工判断是否准备产业载体和合作方资料。",
                "evidence_ids": sorted(
                    set(claim_type_evidence_ids.get("cooperation", []))
                    | set(claim_type_evidence_ids.get("expansion", []))
                ),
            }
        )
    if claim_type_evidence_ids.get("risk"):
        action_cards.append(
            {
                "action_key": "review_counter_signals_before_outreach",
                # ``risk`` 证据既可能已达到红色阈值，也可能因来源等级不足只作为一般反向
                # 信号；两者都必须先核验原文、当前影响和严重程度，不能靠准备卡自行判定。
                "instruction": "先核验风险类原始来源、当前影响和严重程度，再决定继续推进、待补证、观察或无效；不得把尚未澄清的企业风险带入后续沟通。",
                "evidence_ids": sorted(set(claim_type_evidence_ids["risk"])),
            }
        )
    if related_relationship_items:
        action_cards.append(
            {
                "action_key": "resolve_relationship_graph_review_items",
                "instruction": "先在认证关系图谱接口复核或补充关系边证据，再将其作为本地产业协同材料使用。",
                "relationship_edge_ids": sorted(
                    edge_id
                    for edge_id in (item.get("edge_id") for item in related_relationship_items)
                    if isinstance(edge_id, str)
                ),
            }
        )
    if not action_cards:
        action_cards.append(
            {
                "action_key": "collect_traceable_evidence_before_outreach",
                "instruction": "当前台账未见可支撑招商准备的特定事实类别；应先通过受控公开发现和原文核验补齐证据。",
                "evidence_ids": [],
            }
        )
    return {
        "brief_type": "xuzhou_outreach_preparation_readonly_v1",
        "generated_at": _utc_now(),
        "lead": lead_summary,
        "action_cards": action_cards,
        "related_relationship_review_item_count": len(related_relationship_items),
        "manual_authorization_boundary": {
            "external_contact_sent": False,
            "policy_or_incentive_committed": False,
            "reason": "本简报只准备人工核验材料，任何联系、邀约、政策承诺和线索状态变更均须另行由有权人员完成。",
        },
        "automated_business_conclusion": {
            "status": "not_generated",
            "reason": "固定动作卡只由已存证据类别和关系待办触发，不判断投资意愿或自动推进。",
        },
        "read_only": True,
        "operation_scope": "readonly_evidence_backed_outreach_preparation_only",
        "ledger_path": str(ledger_path(workspace_dir)),
    }
