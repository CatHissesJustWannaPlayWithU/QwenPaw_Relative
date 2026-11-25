"""正式证据后的人工招商跟进审批卡。

这个模块刻意把“事实能够入证”和“业务决定是否跟进”分成两件事。服务器只检查
已有正式证据、保留审计并提醒下一步；是否进入招商跟进池、由谁负责以及何时完成，
始终由已获授权的业务人员填写并确认。它不访问网页、不联系企业、不发送消息，也不把
审批记录写成企业投资意愿、签约或落地事实。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from .core import has_open_enterprise_risk_alert
from .storage import ledger_path, load_ledger, save_ledger


FOLLOWUP_APPROVAL_OPERATION_SCOPE = "authenticated_team_member_human_followup_approval_only"
FOLLOWUP_NEXT_ACTIONS = frozenset(
    {
        "official_information_check",
        "internal_industry_matching",
        "prepare_human_outreach",
        "assign_followup_research",
        "other_human_action",
    }
)
FOLLOWUP_NEXT_ACTION_LABELS = {
    "official_information_check": "核实企业公开官方信息",
    "internal_industry_matching": "完成内部产业与载体匹配研判",
    "prepare_human_outreach": "准备由业务人员通过公开渠道开展初步沟通",
    "assign_followup_research": "分派招商主管完成进一步调研",
    "other_human_action": "其他人工动作（以说明为准）",
}
REQUIRED_CHECKLIST_FIELDS: tuple[tuple[str, str], ...] = (
    ("identity_and_subject_checked", "已核对企业主体与材料所述对象一致"),
    ("formal_evidence_checked", "已逐项阅读并核对入账材料，并区分服务器抓取与人工提交材料"),
    ("industry_fit_checked", "已判断其与徐州目标产业或载体的匹配性"),
    ("geographic_basis_checked", "已核对徐州/中国业务依据，或写明无徐州字段仍纳入研判的理由"),
    ("key_claim_corroborated", "关键事项已由一手公开来源或独立公开来源交叉核对"),
    ("risk_and_conflict_checked", "已核对风险、冲突与时效，并明确当前没有未处理的阻断项"),
)


def _utc_now() -> str:
    """返回服务器生成的审计时间，不接受浏览器提供的时钟。"""
    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, payload: dict[str, object]) -> str:
    """以服务器确定的审批字段生成稳定审计 ID，客户端无法指定。"""
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}-{hashlib.sha256(serialized.encode('utf-8')).hexdigest()[:16]}"


def _stable_json(value: object) -> str:
    """稳定序列化，用于保存前后的不可变证据保护。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _required_text(value: object, field_name: str, *, maximum: int) -> str:
    """验证有限业务说明；这些说明是人工判断，不是来源事实。"""
    if not isinstance(value, str) or not (normalized := value.strip()):
        raise ValueError(f"{field_name}不能为空")
    if len(normalized) > maximum:
        raise ValueError(f"{field_name}不能超过{maximum}个字符")
    if any(ord(character) < 0x20 and character not in {"\n", "\t"} for character in normalized):
        raise ValueError(f"{field_name}不能包含控制字符")
    return normalized


def _approval_audits(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    """读取只追加招商跟进审批审计；结构异常时停止而不猜测历史。"""
    audits = ledger.get("lead_followup_approval_audit")
    if not isinstance(audits, list):
        raise ValueError("招商线索台账中的 lead_followup_approval_audit 必须是列表")
    if not all(isinstance(item, dict) for item in audits):
        raise ValueError("招商跟进审批审计必须只包含对象")
    return audits


def _find_lead(ledger: dict[str, Any], lead_id: object) -> dict[str, Any]:
    """只接受既有服务器线索 ID，拒绝由审批表单临时创建招商对象。"""
    normalized_lead_id = _required_text(lead_id, "lead_id", maximum=100)
    lead = next(
        (
            item
            for item in ledger.get("leads", [])
            if isinstance(item, dict) and item.get("lead_id") == normalized_lead_id
        ),
        None,
    )
    if lead is None:
        raise ValueError("未找到对应的招商线索")
    return lead


def _formal_evidence_for_lead(ledger: dict[str, Any], lead: dict[str, Any]) -> list[dict[str, Any]]:
    """确认审批卡只引用当前线索已经存在的正式证据，绝不从表单接收 URL 或摘录。"""
    evidence_ids = lead.get("evidence_ids")
    if not isinstance(evidence_ids, list) or not all(isinstance(value, str) and value for value in evidence_ids):
        raise ValueError("招商线索的正式证据引用格式异常")
    evidence_by_id = {
        item.get("evidence_id"): item
        for item in ledger.get("evidence", [])
        if isinstance(item, dict) and isinstance(item.get("evidence_id"), str)
    }
    selected: list[dict[str, Any]] = []
    for evidence_id in evidence_ids:
        evidence = evidence_by_id.get(evidence_id)
        if not isinstance(evidence, dict):
            raise RuntimeError("招商线索引用的正式证据不存在，不能进入招商跟进审批")
        selected.append(evidence)
    return selected


def _latest_approval(ledger: dict[str, Any], *, lead_id: str) -> dict[str, Any] | None:
    """读取最新追加审批，不覆盖历史决定或负责人安排。"""
    matched = [
        item
        for item in _approval_audits(ledger)
        if item.get("lead_id") == lead_id and item.get("decision") == "approved_for_followup"
    ]
    matched.sort(key=lambda item: str(item.get("approved_at", "")), reverse=True)
    return matched[0] if matched else None


def _public_approval(audit: dict[str, Any]) -> dict[str, object]:
    """返回团队内部可读审批记录，不暴露存储路径、原始网页或不可变签名。"""
    fields = (
        "approval_id",
        "lead_id",
        "decision",
        "approved_at",
        "approved_by_user_id",
        "old_review_state",
        "new_review_state",
        "evidence_ids",
        "checklist",
        "business_value_reason",
        "next_action",
        "next_action_detail",
        "owner_name",
        "target_date",
        "review_note",
        "operation_scope",
    )
    for field_name in fields:
        if field_name not in audit:
            raise ValueError(f"招商跟进审批审计缺少字段：{field_name}")
    return {field_name: audit[field_name] for field_name in fields}


def _protected_snapshot(ledger: dict[str, Any], lead: dict[str, Any]) -> dict[str, str]:
    """审批只可追加自身审计并更新该线索的业务状态，其他记录一律逐字保护。"""
    protected_fields = (
        "evidence",
        "discovery_runs",
        "discovery_candidates",
        "candidate_observations",
        "candidate_corroboration_attempts",
        "candidate_scope_exclusions",
        "candidate_business_reviews",
        "evidence_drafts",
        "evidence_draft_confirmation_audit",
        "evidence_draft_business_review_audit",
        "score_snapshots",
        "outcome_feedback",
    )
    snapshot = {field_name: _stable_json(ledger.get(field_name)) for field_name in protected_fields}
    snapshot["lead_evidence_ids"] = _stable_json(lead.get("evidence_ids"))
    return snapshot


def _assert_protected_unchanged(
    before: dict[str, str], ledger: dict[str, Any], lead: dict[str, Any]
) -> None:
    """防止招商审批夹带修改来源、快照、评分或正式证据关联。"""
    after = _protected_snapshot(ledger, lead)
    if after != before:
        raise RuntimeError("招商跟进审批不得修改正式证据、候选材料、评分或证据关联")


def _normalize_target_date(value: object) -> str:
    """只保存近一年内的人工计划日期，不把它误当企业计划日期。"""
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = date.fromisoformat(value.strip())
        except ValueError as exc:
            raise ValueError("下一步完成日期必须是 YYYY-MM-DD") from exc
    else:
        raise ValueError("下一步完成日期必须是 YYYY-MM-DD")
    today = date.today()
    if parsed < today:
        raise ValueError("下一步完成日期不能早于今天")
    if (parsed - today).days > 366:
        raise ValueError("下一步完成日期不能超过未来366天")
    return parsed.isoformat()


def _machine_checks(lead: dict[str, Any], evidence: list[dict[str, Any]]) -> list[dict[str, object]]:
    """生成只读机器检查结果；它们不是对企业的自动招商结论。"""
    has_open_risk = has_open_enterprise_risk_alert(lead)
    human_material_count = sum(
        1 for item in evidence if item.get("evidence_origin") == "human_attested_material"
    )
    server_captured_count = len(evidence) - human_material_count
    material_detail = (
        f"当前线索已关联 {len(evidence)} 条入账材料：服务器抓取 {server_captured_count} 条，"
        f"业务人员实名提交 {human_material_count} 条。"
    )
    if human_material_count:
        material_detail += (
            "人工提交材料可支持内部招商研判，但不等于服务器已重新抓取；"
            "审批人必须完成主体、关键事项和独立来源交叉核对。"
        )
    else:
        material_detail += "没有入账材料时，不能进入招商跟进审批。"
    return [
        {
            "key": "formal_evidence",
            "title": "机器：确认可追溯入账材料是否已存在",
            "passed": bool(evidence),
            "detail": material_detail,
        },
        {
            "key": "risk_alert",
            "title": "机器：提示已记录的企业风险",
            "passed": not has_open_risk,
            "detail": "当前没有开放的结构化风险预警。" if not has_open_risk else "存在已记录风险预警；业务人员必须在审批卡中核对并写明风险处理判断，系统不会自行忽略。",
        },
        {
            "key": "audit",
            "title": "机器：记录审批人与后续责任",
            "passed": True,
            "detail": "系统只记录认证用户、时间、理由、负责人和下一步；不会自动联系企业、发送外部消息或承诺政策。",
        },
    ]


def _current_card(ledger: dict[str, Any], lead: dict[str, Any]) -> dict[str, object]:
    """构造业务人员可直接阅读的招商跟进审批卡。"""
    evidence = _formal_evidence_for_lead(ledger, lead)
    human_attested_material_count = sum(
        1 for item in evidence if item.get("evidence_origin") == "human_attested_material"
    )
    server_captured_evidence_count = len(evidence) - human_attested_material_count
    lead_id = str(lead["lead_id"])
    latest = _latest_approval(ledger, lead_id=lead_id)
    review_state = str(lead.get("review_state", "pending_user_review"))
    has_open_risk = has_open_enterprise_risk_alert(lead)

    if not evidence:
        state, state_label = "not_ready_no_formal_evidence", "尚不能进入招商判断：还没有任何可追溯入账材料"
    elif review_state == "invalid":
        state, state_label = "not_available_invalid", "当前已标为不纳入，不能直接审批跟进"
    elif latest is not None and review_state == "advance":
        state, state_label = "approved_for_followup", "已确认纳入招商跟进池"
    elif latest is not None:
        state, state_label = "approval_superseded", "原审批后已有新的业务处理，需重新判断"
    elif review_state == "advance":
        state, state_label = "legacy_advance_needs_checklist", "历史推进记录缺少新版审批卡，请补全人工判断"
    else:
        state, state_label = "ready_for_human_approval", "入账材料已具备，等待人工招商判断"

    can_approve = bool(evidence) and review_state != "invalid"
    blockers: list[str] = []
    if not evidence:
        blockers.append("尚无可追溯入账材料：可等待服务器重新核验，或由有权访问页面的业务人员提交人工材料。")
    if review_state == "invalid":
        blockers.append("该线索已被标记为不纳入：请先由团队重新作业务处理，不能直接绕过。")
    if has_open_risk:
        blockers.append("存在企业风险提示：审批人必须在“风险与冲突”勾选项中确认已处理，并在理由中说明。")

    return {
        "lead_id": lead_id,
        "entity_name": lead.get("entity_name"),
        "sector": lead.get("sector"),
        "state": state,
        "state_label": state_label,
        "can_approve": can_approve,
        # ``formal_evidence_count`` 是已有前端的兼容字段；新页面应优先使用下面三个
        # 更准确的名称，避免把人工实名材料误读成服务器抓取证据。
        "formal_evidence_count": len(evidence),
        "evidence_record_count": len(evidence),
        "server_captured_evidence_count": server_captured_evidence_count,
        "human_attested_material_count": human_attested_material_count,
        "human_material_requires_cross_check": human_attested_material_count > 0,
        "formal_evidence_ids": [item["evidence_id"] for item in evidence],
        "machine_checks": _machine_checks(lead, evidence),
        "human_required_items": [
            {"key": key, "label": label} for key, label in REQUIRED_CHECKLIST_FIELDS
        ],
        "next_action_options": [
            {"value": value, "label": FOLLOWUP_NEXT_ACTION_LABELS[value]}
            for value in sorted(FOLLOWUP_NEXT_ACTIONS)
        ],
        "blockers": blockers,
        "current_approval": _public_approval(latest) if latest is not None else None,
        "boundary": "审批通过只表示“可纳入招商跟进池”，不表示企业已同意来徐州、已签约、已投资或系统会自动对外联系。人工提交材料会保留提交人和来源方式，不能被表述为服务器已抓取。",
        "machine_role": "机器保留材料、区分服务器抓取与人工提交、提示风险并记录审计；机器不作招商拍板，也不会取得人工的登录信息。",
        "human_role": "被授权团队成员可直接提交有权使用的材料，并核对招商价值、关键事项和交叉来源，确定负责人和下一步；实际调研、联系、会谈与结果确认均由人完成。",
    }


def get_lead_followup_card(workspace_dir: Path, *, lead_id: str) -> dict[str, object]:
    """只读读取一条线索的招商跟进审批卡，不联网、不改变状态或创建待办。"""
    ledger = load_ledger(workspace_dir)
    lead = _find_lead(ledger, lead_id)
    return {"card": _current_card(ledger, lead), "ledger_path": str(ledger_path(workspace_dir))}


def approve_lead_for_followup(
    workspace_dir: Path,
    *,
    lead_id: str,
    checklist: dict[str, object],
    business_value_reason: str,
    next_action: str,
    next_action_detail: str,
    owner_name: str,
    target_date: object,
    review_note: str,
    authenticated_user_id: str,
) -> dict[str, object]:
    """由已授权团队成员完成完整招商判断后，追加“纳入跟进池”审批。

    它只允许基于当前台账中已有、可追溯的入账材料作人工业务决定：材料可以是服务器抓取
    的页面证据，也可以是明确标注提交人的人工授权材料；前端不能传 URL、证据、企业事实、
    评分或操作者 ID。人工材料仍必须通过既有清单完成关键事项与独立来源交叉核对。通过后
    仅把线索状态标为 ``advance``，并追加可审计的负责人和行动计划；不会自动发送钉钉、
    发起联系、拨打电话或把计划写成企业承诺。
    """
    if not isinstance(checklist, dict):
        raise ValueError("招商跟进检查项必须是对象")
    normalized_user_id = _required_text(authenticated_user_id, "authenticated_user_id", maximum=100)
    normalized_reason = _required_text(business_value_reason, "招商价值判断", maximum=2_000)
    normalized_next_action = _required_text(next_action, "下一步动作", maximum=100)
    if normalized_next_action not in FOLLOWUP_NEXT_ACTIONS:
        raise ValueError("下一步动作不受支持")
    normalized_action_detail = _required_text(next_action_detail, "下一步动作说明", maximum=2_000)
    normalized_owner = _required_text(owner_name, "负责人", maximum=100)
    normalized_target_date = _normalize_target_date(target_date)
    normalized_review_note = _required_text(review_note, "审批说明", maximum=2_000)
    normalized_checklist: dict[str, bool] = {}
    for key, label in REQUIRED_CHECKLIST_FIELDS:
        if checklist.get(key) is not True:
            raise ValueError(f"尚未确认检查项：{label}")
        normalized_checklist[key] = True

    ledger = load_ledger(workspace_dir)
    lead = _find_lead(ledger, lead_id)
    evidence = _formal_evidence_for_lead(ledger, lead)
    if not evidence:
        raise ValueError("尚无可追溯入账材料，不能确认纳入招商跟进池")
    if lead.get("review_state") == "invalid":
        raise ValueError("该线索已标为不纳入，不能直接确认招商跟进")

    protected_before = _protected_snapshot(ledger, lead)
    old_review_state = str(lead.get("review_state", "pending_user_review"))
    approved_at = _utc_now()
    evidence_ids = [str(item["evidence_id"]) for item in evidence]
    approval_id = _stable_id(
        "lead-followup-approval",
        {
            "lead_id": str(lead["lead_id"]),
            "approved_at": approved_at,
            "approved_by_user_id": normalized_user_id,
            "evidence_ids": evidence_ids,
        },
    )
    audit = {
        "approval_id": approval_id,
        "lead_id": str(lead["lead_id"]),
        "decision": "approved_for_followup",
        "approved_at": approved_at,
        "approved_by_user_id": normalized_user_id,
        "old_review_state": old_review_state,
        "new_review_state": "advance",
        "evidence_ids": evidence_ids,
        "checklist": normalized_checklist,
        "business_value_reason": normalized_reason,
        "next_action": normalized_next_action,
        "next_action_detail": normalized_action_detail,
        "owner_name": normalized_owner,
        "target_date": normalized_target_date,
        "review_note": normalized_review_note,
        "operation_scope": FOLLOWUP_APPROVAL_OPERATION_SCOPE,
    }
    _approval_audits(ledger).append(audit)
    lead["review_state"] = "advance"
    if has_open_enterprise_risk_alert(lead):
        # 风险原始证据和触发原因不删除；审批人勾选“风险与冲突已处理”后才关闭当前提醒。
        lead["risk_alert_status"] = "closed"
        lead["red_alert"] = False
    lead["updated_at"] = approved_at
    history = lead.setdefault("status_history", [])
    if not isinstance(history, list):
        raise RuntimeError("招商线索状态历史格式异常")
    history.append(
        {
            "state": "advance",
            "reason": "已完成招商跟进审批卡；不等于企业承诺、签约或自动对外联系。",
            "old_state": old_review_state,
            "new_state": "advance",
            "review_note": normalized_review_note,
            "reviewer_user_id": normalized_user_id,
            "operation_scope": FOLLOWUP_APPROVAL_OPERATION_SCOPE,
            "at": approved_at,
        }
    )
    _assert_protected_unchanged(protected_before, ledger, lead)
    saved_path = save_ledger(workspace_dir, ledger)
    return {
        "approval": _public_approval(audit),
        "lead": lead,
        "card": _current_card(ledger, lead),
        "ledger_path": str(saved_path),
    }
