"""面向业务人员的可解释、可撤回的徐州招商自适应学习。

学习的对象是“下一批应优先由机器看哪些已获授权的来源”和“工作台如何排序”，不是企业
事实本身。它只从已经保存的人工材料、服务器证据、人工跟进审批、候选业务判断和已录入
结果反馈中统计来源表现；不会从网页内容改写 Skill、添加域名、扩大关键词、自动入证、
自动联系企业或取代业务人员的最终招商决定。
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .source_recipe_governance import (
    apply_source_recipe_learning_proposal_under_authorized_policy,
    generate_source_recipe_learning_proposal,
)
from .storage import ledger_path, load_ledger, save_ledger


ADAPTIVE_LEARNING_OPERATION_SCOPE = "authenticated_team_member_bounded_adaptive_learning_policy"
ADAPTIVE_LEARNING_SERVICE_SCOPE = "scheduled_bounded_adaptive_learning_only"
DEFAULT_MODE = "adaptive_ranking"
ADAPTIVE_LEARNING_MODES = frozenset(
    {
        "manual_advice",
        "adaptive_ranking",
        "adaptive_ranking_and_recipe_order",
    }
)
MODE_LABELS = {
    "manual_advice": "只给建议，不自动改变队列顺序",
    "adaptive_ranking": "自动调整业务队列优先级",
    "adaptive_ranking_and_recipe_order": "自动调整队列，并重排固定五条来源配方",
}
MODE_BOUNDARIES = {
    "manual_advice": "机器只展示来源表现与建议；所有排序和来源配方保持人工当前状态。",
    "adaptive_ranking": "机器可用已记录结果自动排序业务队列；不会新增来源、网页访问范围或正式证据。",
    "adaptive_ranking_and_recipe_order": "机器可自动重排系统固定的五条来源配方；不会新增域名、关键词、产业范围、Skill或正式证据。",
}
MAX_ADAPTIVE_AUDITS = 1_000
SOURCE_SIGNAL_MIN_SAMPLE = 3
MAX_HUMAN_MATERIAL_OUTCOME_FEEDBACK_PER_LEAD = 100
HUMAN_MATERIAL_OUTCOME_ALIASES = {
    "落地": "landed",
    "landed": "landed",
    "推进": "positive_progress",
    "positive_progress": "positive_progress",
    "暂缓": "deferred",
    "观察": "deferred",
    "deferred": "deferred",
    "未转化": "not_converted",
    "not_converted": "not_converted",
    "无效": "invalidated",
    "invalidated": "invalidated",
}


def _utc_now() -> str:
    """生成统一的服务器审计时间，不使用客户端时钟。"""
    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, payload: dict[str, object]) -> str:
    """用受控业务字段生成稳定短标识，浏览器不能指定策略或审计 ID。"""
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}-{hashlib.sha256(serialized.encode('utf-8')).hexdigest()[:16]}"


def _required_text(value: object, field_name: str, *, maximum: int) -> str:
    """校验策略说明与认证身份，拒绝空白、超长和控制字符。"""
    if not isinstance(value, str) or not (normalized := value.strip()):
        raise ValueError(f"{field_name}不能为空")
    if len(normalized) > maximum:
        raise ValueError(f"{field_name}不能超过{maximum}个字符")
    if any(ord(character) < 0x20 and character not in {"\n", "\t"} for character in normalized):
        raise ValueError(f"{field_name}不能包含控制字符")
    return normalized


def _default_policy() -> dict[str, object]:
    """返回安全默认策略：自动排序，但不自动扩大来源或重排监控配方。"""
    return {
        "policy_id": "adaptive-learning-default-v1",
        "mode": DEFAULT_MODE,
        "mode_label": MODE_LABELS[DEFAULT_MODE],
        "changed_at": None,
        "changed_by_user_id": "system-default",
        "change_reason": "默认启用基于历史结果的业务队列排序；固定来源配方仍由人工决定。",
        "policy_status": "default_not_yet_explicitly_reconfigured",
        "operation_scope": ADAPTIVE_LEARNING_OPERATION_SCOPE,
    }


def _policy(ledger: dict[str, Any]) -> dict[str, object]:
    """读取已审计策略或安全默认值；异常结构拒绝悄悄降级为任意自动模式。"""
    raw = ledger.get("adaptive_learning_policy")
    if raw is None:
        return _default_policy()
    if not isinstance(raw, dict):
        raise ValueError("招商线索台账中的 adaptive_learning_policy 必须是对象")
    mode = raw.get("mode")
    if mode not in ADAPTIVE_LEARNING_MODES:
        raise ValueError("自适应学习模式不受支持")
    policy_id = _required_text(raw.get("policy_id"), "policy_id", maximum=120)
    changed_by = _required_text(raw.get("changed_by_user_id"), "changed_by_user_id", maximum=100)
    change_reason = _required_text(raw.get("change_reason"), "change_reason", maximum=2_000)
    changed_at = raw.get("changed_at")
    if changed_at is not None and (not isinstance(changed_at, str) or not changed_at):
        raise ValueError("自适应学习策略时间格式异常")
    return {
        "policy_id": policy_id,
        "mode": mode,
        "mode_label": MODE_LABELS[mode],
        "changed_at": changed_at,
        "changed_by_user_id": changed_by,
        "change_reason": change_reason,
        "policy_status": raw.get("policy_status", "explicitly_configured"),
        "operation_scope": ADAPTIVE_LEARNING_OPERATION_SCOPE,
    }


def _domain(url: object) -> str | None:
    """提取公开来源域名，仅用于来源统计与队列排序。"""
    if not isinstance(url, str) or not url:
        return None
    try:
        hostname = urlparse(url).hostname
    except ValueError:
        return None
    return hostname.casefold().rstrip(".") if hostname else None


def _signal_template(domain: str) -> dict[str, object]:
    """创建一个来源学习统计桶；其中任何数字都不是企业事实或来源评级。"""
    return {
        "domain": domain,
        "server_captured_evidence_count": 0,
        "human_attested_material_count": 0,
        "followup_approved_count": 0,
        "positive_progress_count": 0,
        "landed_count": 0,
        "not_converted_count": 0,
        "invalidated_count": 0,
        "candidate_invalid_count": 0,
    }


def _latest_outcome_feedback_by_lead(ledger: dict[str, Any]) -> dict[str, dict[str, object]]:
    """只读取得每条线索最新结果反馈，并保留其来源归属范围。

    常规服务器评分快照结果可以关联该线索的服务器材料。人工材料结果则必须只归因于
    提交时记录的人工材料来源，不能因为同一线索碰巧还有其他页面，就把一次人工判断
    复制记到所有域名上。
    """
    latest: dict[str, tuple[str, dict[str, object]]] = {}
    for item in ledger.get("outcome_feedback", []):
        if not isinstance(item, dict):
            continue
        lead_id = item.get("lead_id")
        outcome = item.get("outcome")
        recorded_at = item.get("recorded_at")
        if not isinstance(lead_id, str) or not isinstance(outcome, str) or not isinstance(recorded_at, str):
            continue
        previous = latest.get(lead_id)
        if previous is None or recorded_at >= previous[0]:
            latest[lead_id] = (recorded_at, copy.deepcopy(item))
    return {lead_id: item[1] for lead_id, item in latest.items()}


def _approved_lead_ids(ledger: dict[str, Any]) -> set[str]:
    """只读读取已完成招商跟进审批的线索集合。"""
    return {
        str(item.get("lead_id"))
        for item in ledger.get("lead_followup_approval_audit", [])
        if isinstance(item, dict)
        and item.get("decision") == "approved_for_followup"
        and isinstance(item.get("lead_id"), str)
    }


def _latest_candidate_review_by_candidate(ledger: dict[str, Any]) -> dict[str, str]:
    """取发现候选的末次人工判断；候选本身仍不是企业事实。"""
    result: dict[str, tuple[str, str]] = {}
    for item in ledger.get("candidate_business_reviews", []):
        if not isinstance(item, dict):
            continue
        candidate_id = item.get("candidate_id")
        state = item.get("new_review_state")
        reviewed_at = item.get("reviewed_at")
        if not isinstance(candidate_id, str) or not isinstance(state, str) or not isinstance(reviewed_at, str):
            continue
        previous = result.get(candidate_id)
        if previous is None or reviewed_at >= previous[0]:
            result[candidate_id] = (reviewed_at, state)
    return {candidate_id: item[1] for candidate_id, item in result.items()}


def _priority_from_signal(signal: dict[str, object]) -> dict[str, object]:
    """以有限、可解释的规则得到来源研究优先级，不评判任何企业。"""
    evidence_count = int(signal["server_captured_evidence_count"]) + int(signal["human_attested_material_count"])
    judgement_count = (
        int(signal["followup_approved_count"])
        + int(signal["positive_progress_count"])
        + int(signal["landed_count"])
        + int(signal["not_converted_count"])
        + int(signal["invalidated_count"])
        + int(signal["candidate_invalid_count"])
    )
    sample_count = evidence_count + judgement_count
    value_score = (
        int(signal["server_captured_evidence_count"]) * 2
        + int(signal["human_attested_material_count"])
        + int(signal["followup_approved_count"]) * 3
        + int(signal["positive_progress_count"]) * 2
        + int(signal["landed_count"]) * 5
        - int(signal["not_converted_count"]) * 2
        - int(signal["invalidated_count"]) * 3
        - int(signal["candidate_invalid_count"])
    )
    if sample_count < SOURCE_SIGNAL_MIN_SAMPLE:
        priority = "reference_only"
        label = "样本不足，仅供参考"
        reason = f"当前只有 {sample_count} 个可用记录，机器不会据此自动压低或放大来源。"
        rank = 2
    elif value_score >= 8:
        priority = "learned_high"
        label = "学习后高优先"
        reason = "该来源的已保存材料和已记录人工结果相对更有助于后续研判。"
        rank = 0
    elif value_score <= -3:
        priority = "learned_low"
        label = "学习后低优先"
        reason = "近期记录中无效或未转化较多；机器会降低工作台排序，不会删除来源或材料。"
        rank = 3
    else:
        priority = "learned_normal"
        label = "学习后常规优先"
        reason = "已有一定记录，但暂未形成足以明显升降优先级的稳定差异。"
        rank = 1
    return {
        "priority": priority,
        "label": label,
        "reason": reason,
        "rank": rank,
        "sample_count": sample_count,
        "value_score": value_score,
    }


def source_learning_snapshot_from_ledger(ledger: dict[str, Any]) -> dict[str, object]:
    """从受审计的已有记录生成来源表现快照，不写台账、不联网、不重新分类事实。"""
    signals: dict[str, dict[str, object]] = {}
    latest_outcomes = _latest_outcome_feedback_by_lead(ledger)
    approved_leads = _approved_lead_ids(ledger)
    human_evidence_ids_by_lead: dict[str, set[str]] = {}
    for item in ledger.get("evidence", []):
        if (
            isinstance(item, dict)
            and item.get("evidence_origin") == "human_attested_material"
            and isinstance(item.get("lead_id"), str)
            and isinstance(item.get("evidence_id"), str)
        ):
            human_evidence_ids_by_lead.setdefault(str(item["lead_id"]), set()).add(
                str(item["evidence_id"])
            )
    for evidence in ledger.get("evidence", []):
        if not isinstance(evidence, dict):
            continue
        domain = _domain(evidence.get("source_url"))
        lead_id = evidence.get("lead_id")
        if domain is None or not isinstance(lead_id, str):
            continue
        signal = signals.setdefault(domain, _signal_template(domain))
        if evidence.get("evidence_origin") == "human_attested_material":
            signal["human_attested_material_count"] = int(signal["human_attested_material_count"]) + 1
        else:
            signal["server_captured_evidence_count"] = int(signal["server_captured_evidence_count"]) + 1
        if lead_id in approved_leads:
            signal["followup_approved_count"] = int(signal["followup_approved_count"]) + 1
        feedback = latest_outcomes.get(lead_id)
        outcome = feedback.get("outcome") if isinstance(feedback, dict) else None
        if isinstance(feedback, dict) and feedback.get("feedback_kind") == "human_material_source_learning_only":
            recorded_ids = feedback.get("source_evidence_ids")
            target_ids = (
                {item for item in recorded_ids if isinstance(item, str)}
                if isinstance(recorded_ids, list)
                else human_evidence_ids_by_lead.get(lead_id, set())
            )
            # 旧版人工反馈没有 ``source_evidence_ids``；兼容时也只归因于该线索的人工
            # 材料，绝不让人工判断无意间影响同线索的服务器来源。
            if (
                evidence.get("evidence_origin") != "human_attested_material"
                or str(evidence.get("evidence_id")) not in target_ids
            ):
                outcome = None
        if outcome == "positive_progress":
            signal["positive_progress_count"] = int(signal["positive_progress_count"]) + 1
        elif outcome == "landed":
            signal["landed_count"] = int(signal["landed_count"]) + 1
        elif outcome == "not_converted":
            signal["not_converted_count"] = int(signal["not_converted_count"]) + 1
        elif outcome == "invalidated":
            signal["invalidated_count"] = int(signal["invalidated_count"]) + 1

    latest_candidate_reviews = _latest_candidate_review_by_candidate(ledger)
    for candidate in ledger.get("discovery_candidates", []):
        if not isinstance(candidate, dict):
            continue
        candidate_id = candidate.get("candidate_id")
        domain = _domain(candidate.get("source_url"))
        if domain is None or not isinstance(candidate_id, str):
            continue
        if latest_candidate_reviews.get(candidate_id) != "invalid":
            continue
        signal = signals.setdefault(domain, _signal_template(domain))
        signal["candidate_invalid_count"] = int(signal["candidate_invalid_count"]) + 1

    records: list[dict[str, object]] = []
    for signal in signals.values():
        priority = _priority_from_signal(signal)
        records.append({**signal, **priority})
    records.sort(key=lambda item: (int(item["rank"]), -int(item["sample_count"]), str(item["domain"])))

    candidate_domains = [
        domain
        for domain in (_domain(item.get("source_url")) for item in ledger.get("discovery_candidates", []) if isinstance(item, dict))
        if domain is not None
    ]
    total_candidates = len(candidate_domains)
    domain_counts: dict[str, int] = {}
    for domain in candidate_domains:
        domain_counts[domain] = domain_counts.get(domain, 0) + 1
    top_domain, top_count = max(domain_counts.items(), key=lambda item: item[1]) if domain_counts else (None, 0)
    top_share = (top_count / total_candidates) if total_candidates else 0.0
    return {
        "source_signals": records,
        "source_signal_count": len(records),
        "minimum_sample_count": SOURCE_SIGNAL_MIN_SAMPLE,
        "source_diversity": {
            "discovery_candidate_count": total_candidates,
            "distinct_source_domain_count": len(domain_counts),
            "top_source_domain": top_domain,
            "top_source_candidate_count": top_count,
            "top_source_share": round(top_share, 4),
            "status": (
                "concentrated_needs_more_human_selected_sources"
                if total_candidates >= 5 and top_share >= 0.7
                else "balanced_or_small_sample"
            ),
            "message": (
                "近期候选高度集中于同一来源；请由业务人员补充或启用其他已授权公开域名，机器不会自行新增来源。"
                if total_candidates >= 5 and top_share >= 0.7
                else "当前来源集中度没有达到提醒阈值，或样本仍较少。"
            ),
        },
        "learning_boundary": "学习只用于来源研究优先级和固定配方排序；不自动确认企业事实、修改证据、生成招商结论或对外联系。",
    }


def source_learning_priority_for_url(ledger: dict[str, Any], source_url: object) -> dict[str, object]:
    """为工作台候选返回来源学习排序信息；未见来源维持中性优先级。"""
    domain = _domain(source_url)
    snapshot = source_learning_snapshot_from_ledger(ledger)
    signal = next(
        (
            item
            for item in snapshot["source_signals"]
            if isinstance(item, dict) and item.get("domain") == domain
        ),
        None,
    )
    if isinstance(signal, dict):
        return {
            "priority": signal["priority"],
            "label": signal["label"],
            "reason": signal["reason"],
            "rank": signal["rank"],
            "sample_count": signal["sample_count"],
        }
    return {
        "priority": "unseen_source",
        "label": "新来源，等待样本",
        "reason": "当前尚无足够历史记录；机器不会因未知来源自动排除。",
        "rank": 2,
        "sample_count": 0,
    }


def get_adaptive_learning_status(workspace_dir: Path) -> dict[str, object]:
    """读取当前策略、来源学习解释和边界；不执行学习、不联网、不更改台账。"""
    ledger = load_ledger(workspace_dir)
    policy = _policy(ledger)
    snapshot = source_learning_snapshot_from_ledger(ledger)
    return {
        "policy": policy,
        **snapshot,
        "automatic_actions": {
            "queue_sorting": policy["mode"] in {"adaptive_ranking", "adaptive_ranking_and_recipe_order"},
            "fixed_recipe_ordering": policy["mode"] == "adaptive_ranking_and_recipe_order",
            "new_source_domains": False,
            "new_search_keywords": False,
            "formal_evidence_entry": False,
            "external_contact": False,
        },
        "policy_boundary": MODE_BOUNDARIES[str(policy["mode"])],
        "ledger_path": str(ledger_path(workspace_dir)),
    }


def adaptive_learning_mode_from_ledger(ledger: dict[str, Any]) -> str:
    """返回当前已验证台账的学习模式，供工作台排序复用而不重复读写台账。"""
    return str(_policy(ledger)["mode"])


def _policy_audits(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    """取得自适应策略的只追加审计列表；旧台账在受控写入时补齐为空。"""
    audits = ledger.setdefault("adaptive_learning_policy_audit", [])
    if not isinstance(audits, list) or not all(isinstance(item, dict) for item in audits):
        raise ValueError("招商线索台账中的 adaptive_learning_policy_audit 必须是对象列表")
    return audits


def _human_material_outcome_protected_snapshot(ledger: dict[str, Any]) -> dict[str, str]:
    """保存人工材料结果反馈前保护所有事实与配置字段。

    人工结果只能追加到 ``outcome_feedback``；它可以被来源优先级学习读取，但不能修改
    线索、证据、来源目录、自动化策略或历史反馈。整本台账签名元数据由 ``save_ledger``
    在受控保存中更新，因此不在比较范围中。
    """

    mutable_fields = {"outcome_feedback", "ledger_integrity", "ledger_revision", "updated_at"}
    return {
        key: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for key, value in ledger.items()
        if key not in mutable_fields
    }


def record_human_material_source_outcome(
    workspace_dir: Path,
    *,
    lead_id: str,
    outcome: str,
    note: str,
    authenticated_user_id: str,
) -> dict[str, object]:
    """记录人工材料线索的真实业务结果，只供来源学习而不影响机器评分。

    这条通道专门解决“业务人员有合法材料，但服务器无法重新抓取”的情况。它要求目标
    线索至少有一条 ``human_attested_material``，只接收受控结果枚举和说明，认证身份、
    时间与反馈 ID 均由服务器生成。它不会创建评分快照、改变招商状态、自动改权重、把
    人工材料升级成服务器事实或对外联系；来源学习只会据此调整队列优先级。
    """

    normalized_lead_id = _required_text(lead_id, "lead_id", maximum=100)
    normalized_outcome = HUMAN_MATERIAL_OUTCOME_ALIASES.get(
        _required_text(outcome, "outcome", maximum=100).casefold()
    )
    if normalized_outcome is None:
        accepted = "、".join(sorted(set(HUMAN_MATERIAL_OUTCOME_ALIASES.values())))
        raise ValueError(f"outcome不合法，可选值为：{accepted}")
    normalized_note = _required_text(note, "note", maximum=2_000)
    normalized_user_id = _required_text(
        authenticated_user_id,
        "authenticated_user_id",
        maximum=100,
    )
    ledger = load_ledger(workspace_dir)
    leads = ledger.get("leads")
    evidence = ledger.get("evidence")
    feedback_records = ledger.get("outcome_feedback")
    if not all(isinstance(value, list) for value in (leads, evidence, feedback_records)):
        raise ValueError("招商线索台账中的线索、材料或结果反馈字段必须是列表")
    if not any(
        isinstance(item, dict) and item.get("lead_id") == normalized_lead_id
        for item in leads
    ):
        raise ValueError("未找到对应的招商线索")
    human_material_evidence = [
        item
        for item in evidence
        if isinstance(item, dict)
        and item.get("lead_id") == normalized_lead_id
        and item.get("evidence_origin") == "human_attested_material"
        and isinstance(item.get("evidence_id"), str)
    ]
    if not human_material_evidence:
        raise ValueError("该线索没有人工授权材料；请使用常规评分快照结果反馈流程")
    human_feedback_count = sum(
        1
        for item in feedback_records
        if isinstance(item, dict)
        and item.get("lead_id") == normalized_lead_id
        and item.get("feedback_kind") == "human_material_source_learning_only"
    )
    if human_feedback_count >= MAX_HUMAN_MATERIAL_OUTCOME_FEEDBACK_PER_LEAD:
        raise RuntimeError("该线索的人工材料结果反馈已达到安全上限")

    protected_before = _human_material_outcome_protected_snapshot(ledger)
    recorded_at = _utc_now()
    source_evidence_ids = sorted(str(item["evidence_id"]) for item in human_material_evidence)
    source_domains = sorted(
        {
            domain
            for domain in (_domain(item.get("source_url")) for item in human_material_evidence)
            if domain is not None
        }
    )
    feedback = {
        "feedback_id": _stable_id(
            "human-material-source-outcome",
            {
                "lead_id": normalized_lead_id,
                "outcome": normalized_outcome,
                "note": normalized_note,
                "recorded_at": recorded_at,
                "recorded_by_user_id": normalized_user_id,
            },
        ),
        "lead_id": normalized_lead_id,
        "outcome": normalized_outcome,
        "note": normalized_note,
        "recorded_at": recorded_at,
        "recorded_by_user_id": normalized_user_id,
        "score_snapshot_id": None,
        "feedback_kind": "human_material_source_learning_only",
        "source_evidence_ids": source_evidence_ids,
        "source_domains": source_domains,
        "operation_scope": "authenticated_team_member_human_material_source_learning_feedback_only",
    }
    feedback_records.append(feedback)
    if _human_material_outcome_protected_snapshot(ledger) != protected_before:
        raise RuntimeError("人工材料结果反馈不得修改线索、证据、来源目录或学习策略")
    saved_path = save_ledger(workspace_dir, ledger)  # type: ignore[arg-type]
    return {
        "feedback": copy.deepcopy(feedback),
        "source_learning": source_learning_snapshot_from_ledger(ledger),
        "ledger_path": str(saved_path),
        "message": "结果已记录为来源学习样本；它不会影响机器评分规则，也不会把人工材料改写为服务器抓取证据。",
    }


def _policy_protected_snapshot(ledger: dict[str, Any]) -> dict[str, str]:
    """策略修改只允许改策略与自身审计，保护候选、证据、来源目录和全部业务记录。"""
    mutable_fields = {
        "adaptive_learning_policy",
        "adaptive_learning_policy_audit",
        "ledger_integrity",
        "ledger_revision",
        "updated_at",
    }
    return {
        key: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for key, value in ledger.items()
        if key not in mutable_fields
    }


def set_adaptive_learning_policy(
    workspace_dir: Path,
    *,
    mode: str,
    change_reason: str,
    authenticated_user_id: str,
) -> dict[str, object]:
    """让任一已授权业务成员选择自动化边界，所有改动保留审计并可随时改回。"""
    if mode not in ADAPTIVE_LEARNING_MODES:
        raise ValueError("mode必须是只给建议、自动排序或自动排序加固定配方重排之一")
    normalized_reason = _required_text(change_reason, "change_reason", maximum=2_000)
    normalized_user_id = _required_text(authenticated_user_id, "authenticated_user_id", maximum=100)
    ledger = load_ledger(workspace_dir)
    before = _policy_protected_snapshot(ledger)
    old_policy = _policy(ledger)
    changed_at = _utc_now()
    policy_id = _stable_id(
        "adaptive-learning-policy",
        {
            "mode": mode,
            "changed_at": changed_at,
            "changed_by_user_id": normalized_user_id,
        },
    )
    policy = {
        "policy_id": policy_id,
        "mode": mode,
        "mode_label": MODE_LABELS[mode],
        "changed_at": changed_at,
        "changed_by_user_id": normalized_user_id,
        "change_reason": normalized_reason,
        "policy_status": "explicitly_configured",
        "operation_scope": ADAPTIVE_LEARNING_OPERATION_SCOPE,
    }
    audits = _policy_audits(ledger)
    if len(audits) >= MAX_ADAPTIVE_AUDITS:
        raise RuntimeError("自适应学习策略变更审计已达到安全上限")
    audit = {
        "audit_id": _stable_id(
            "adaptive-learning-policy-audit",
            {"policy_id": policy_id, "changed_at": changed_at},
        ),
        "old_policy_id": old_policy["policy_id"],
        "old_mode": old_policy["mode"],
        "new_policy_id": policy_id,
        "new_mode": mode,
        "changed_at": changed_at,
        "changed_by_user_id": normalized_user_id,
        "change_reason": normalized_reason,
        "operation_scope": ADAPTIVE_LEARNING_OPERATION_SCOPE,
    }
    ledger["adaptive_learning_policy"] = policy
    audits.append(audit)
    if _policy_protected_snapshot(ledger) != before:
        raise RuntimeError("自适应学习策略不得修改候选、证据、来源目录或其他业务记录")
    saved_path = save_ledger(workspace_dir, ledger)  # type: ignore[arg-type]
    return {
        "policy": copy.deepcopy(policy),
        "audit": copy.deepcopy(audit),
        "ledger_path": str(saved_path),
        "policy_boundary": MODE_BOUNDARIES[mode],
    }


def run_adaptive_learning_cycle(workspace_dir: Path) -> dict[str, object]:
    """执行受限的定时来源学习后续步骤，不读取网页、不触及证据或招商结论。"""
    ledger = load_ledger(workspace_dir)
    policy = _policy(ledger)
    learning_snapshot = source_learning_snapshot_from_ledger(ledger)
    if policy["mode"] != "adaptive_ranking_and_recipe_order":
        return {
            "status": "queue_learning_active_recipe_order_requires_user_policy",
            "policy": policy,
            "source_learning": learning_snapshot,
            "operation_scope": ADAPTIVE_LEARNING_SERVICE_SCOPE,
        }
    proposal_result = generate_source_recipe_learning_proposal(workspace_dir)
    proposal = proposal_result.get("proposal")
    if not isinstance(proposal, dict) or not isinstance(proposal.get("proposal_id"), str):
        return {
            "status": "no_fixed_recipe_order_change_applied",
            "policy": policy,
            "source_recipe_learning": proposal_result,
            "source_learning": learning_snapshot,
            "operation_scope": ADAPTIVE_LEARNING_SERVICE_SCOPE,
        }
    applied = apply_source_recipe_learning_proposal_under_authorized_policy(
        workspace_dir,
        proposal_id=proposal["proposal_id"],
        policy_id=str(policy["policy_id"]),
        policy_authorized_by_user_id=str(policy["changed_by_user_id"]),
    )
    return {
        "status": "fixed_recipe_order_processed_under_user_policy",
        "policy": policy,
        "source_recipe_learning": proposal_result,
        "automatic_recipe_application": applied,
        "source_learning": learning_snapshot,
        "operation_scope": ADAPTIVE_LEARNING_SERVICE_SCOPE,
    }
