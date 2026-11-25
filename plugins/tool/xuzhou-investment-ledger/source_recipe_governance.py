"""徐州招商免费公开来源配方的学习建议与人工审批治理。

本模块只允许已签名正式证据、既有候选观察和已录入的招商结果反馈影响“固定配方的
执行顺序”。它不读取网页、不修改搜索词、URL、产业或细分方向，更不会让网页内容直接
写入 Skill。自动周期只能追加待审批建议；任一普通登录用户必须经认证接口明确批准，
才能创建下一版来源配方规则。
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .storage import (
    SOURCE_RECIPE_BASELINE_ORDER,
    ledger_path,
    load_ledger,
    save_ledger,
)


SOURCE_RECIPE_LEARNING_WINDOW_DAYS = 90
MIN_FORMAL_EVIDENCE_FOR_SOURCE_RECIPE_PROPOSAL = 3
MIN_RECIPES_WITH_EVIDENCE_FOR_SOURCE_RECIPE_PROPOSAL = 2
MAX_SOURCE_RECIPE_LEARNING_PROPOSALS = 500
FINAL_OUTCOMES = frozenset({"landed", "not_converted", "invalidated"})
REVIEW_DECISION_ALIASES = {
    "批准": "approved",
    "approved": "approved",
    "拒绝": "rejected",
    "rejected": "rejected",
}
AUTO_RECIPE_POLICY_SERVICE_USER_ID = "system-adaptive-learning-v1"


def _utc_now() -> str:
    """返回建议和审批审计统一使用的服务器 UTC 时间，不接受客户端时钟。"""
    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, payload: dict[str, object]) -> str:
    """根据受控业务载荷生成稳定短 ID，避免同一学习建议被重复追加。"""
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}-{hashlib.sha256(serialized.encode('utf-8')).hexdigest()[:16]}"


def _required_identifier(value: object, field_name: str) -> str:
    """校验服务器记录 ID 或认证用户 ID，拒绝 URL、空白和无界输入。"""
    if not isinstance(value, str) or not (identifier := value.strip()) or len(identifier) > 120:
        raise ValueError(f"{field_name}必须是有效的服务器标识")
    if any(character.isspace() or character in "/\\?&#" for character in identifier):
        raise ValueError(f"{field_name}必须是有效的服务器标识")
    return identifier


def _required_note(value: object) -> str:
    """校验普通用户审批备注，备注只解释业务判断，不能携带来源或证据写入字段。"""
    if not isinstance(value, str) or not (note := value.strip()) or len(note) > 2_000:
        raise ValueError("review_note必须是1到2000个字符")
    return note


def _known_recipe_ids() -> tuple[str, ...]:
    """读取发现器的固定配方标识，并拒绝源码配置与台账基线漂移。

    输入为空，输出发现器当前允许的完整配方顺序。导入被延后到函数执行时，避免发现器
    读取生效规则时产生模块初始化循环；若任一代码升级只改了一侧，函数失败关闭而不是
    猜测可删减的配方。它不访问网络、不读取业务记录或修改来源配置。
    """
    from .source_discovery import QUERY_RECIPES

    recipe_ids = tuple(recipe.get("catalog_id") for recipe in QUERY_RECIPES)
    if not all(isinstance(recipe_id, str) and recipe_id for recipe_id in recipe_ids):
        raise RuntimeError("固定来源配方配置缺少有效标识")
    if recipe_ids != SOURCE_RECIPE_BASELINE_ORDER:
        raise RuntimeError("来源配方基线与发现器配置不一致，拒绝应用学习顺序")
    return recipe_ids


def _validate_recipe_order(value: object) -> list[str]:
    """验证规则或建议只能完整重排固定配方，不得删改或注入搜索范围。

    输入是台账规则或学习建议中的配方标识列表，输出为经过精确集合校验的独立顺序列表。
    允许的唯一变化是顺序：五条配方都必须恰好出现一次，因而不能取消任何产业信号入口、
    添加 URL/关键词或将网页文本变成可执行查询。函数纯校验，不联网、不写台账，也不
    判断企业、基金或个人事实。
    """
    if not isinstance(value, list) or not value:
        raise ValueError("recipe_order必须是非空列表")
    normalized = [_required_identifier(item, "recipe_order项") for item in value]
    expected = _known_recipe_ids()
    if len(normalized) != len(expected) or set(normalized) != set(expected):
        raise ValueError("recipe_order必须完整且仅包含系统固定的五条来源配方")
    if len(set(normalized)) != len(normalized):
        raise ValueError("recipe_order不能包含重复配方")
    return normalized


def _rule_sets(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    """读取来源配方规则版本列表；结构异常时失败关闭而不补写历史配置。"""
    records = ledger.get("source_recipe_rule_sets")
    if not isinstance(records, list):
        raise ValueError("招商线索台账中的 source_recipe_rule_sets 必须是列表")
    if not all(isinstance(record, dict) for record in records):
        raise ValueError("来源配方规则版本必须是对象")
    return records


def _proposals(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    """读取只追加的来源学习建议列表，异常结构不会被自动修复或覆盖。"""
    records = ledger.get("source_recipe_learning_proposals")
    if not isinstance(records, list):
        raise ValueError("招商线索台账中的 source_recipe_learning_proposals 必须是列表")
    if not all(isinstance(record, dict) for record in records):
        raise ValueError("来源学习建议必须是对象")
    return records


def _reviews(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    """读取来源学习审批审计；审计只追加，不能被用来改写建议原文或证据。"""
    records = ledger.get("source_recipe_learning_reviews")
    if not isinstance(records, list):
        raise ValueError("招商线索台账中的 source_recipe_learning_reviews 必须是列表")
    if not all(isinstance(record, dict) for record in records):
        raise ValueError("来源学习审批审计必须是对象")
    return records


def _active_rule_set(ledger: dict[str, Any]) -> dict[str, Any]:
    """返回唯一生效的来源配方规则，并验证其版本与完整配方顺序。

    输入是已通过完整性校验的台账，输出为当前唯一 ``active`` 规则的深拷贝。规则状态
    或顺序异常时拒绝自动回退到任意旧版本，防止学习/审批故障悄悄改变监控范围；该函数
    只读，不接受用户身份、网页数据或关键词，也不修改台账。
    """
    active_records = [record for record in _rule_sets(ledger) if record.get("status") == "active"]
    if len(active_records) != 1:
        raise RuntimeError("来源配方规则必须恰有一版处于生效状态")
    active = active_records[0]
    rule_set_id = _required_identifier(active.get("rule_set_id"), "rule_set_id")
    version = active.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError("来源配方规则版本必须是正整数")
    return {
        **copy.deepcopy(active),
        "rule_set_id": rule_set_id,
        "version": version,
        "recipe_order": _validate_recipe_order(active.get("recipe_order")),
    }


def _protected_snapshot(ledger: dict[str, Any]) -> dict[str, str]:
    """快照来源学习允许变化字段之外的完整台账，防止审批越权触及业务事实。

    输入是完整台账，输出为稳定序列化快照。只有来源配方规则版本、学习建议、审批审计
    以及保存时自然变化的完整性元数据被排除；线索、原始证据、快照、来源目录、评分、
    结果反馈、关系图谱和监控审计均必须逐字保持不变。函数不保存、不联网，也不解释
    任一企业招商价值。
    """
    mutable_fields = {
        "source_recipe_rule_sets",
        "source_recipe_learning_proposals",
        "source_recipe_learning_reviews",
        "ledger_integrity",
        "ledger_revision",
        "updated_at",
    }
    return {
        field_name: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for field_name, value in ledger.items()
        if field_name not in mutable_fields
    }


def _assert_protected_snapshot_unchanged(before: dict[str, str], ledger: dict[str, Any]) -> None:
    """确认来源学习写入未改动任何受保护业务字段，否则拒绝保存。"""
    if _protected_snapshot(ledger) != before:
        raise RuntimeError("来源配方学习不得修改证据、线索、评分、反馈、目录或监控历史")


def _review_state_by_proposal(reviews: list[dict[str, Any]]) -> dict[str, str]:
    """从只追加审批审计推导每条建议状态，不在建议原文上覆盖审批结论。"""
    states: dict[str, str] = {}
    for review in reviews:
        proposal_id = review.get("proposal_id")
        decision = review.get("decision")
        if not isinstance(proposal_id, str) or decision not in {"approved", "rejected", "auto_applied"}:
            continue
        if proposal_id in states:
            raise RuntimeError("同一来源学习建议存在多条审批审计")
        states[proposal_id] = str(decision)
    return states


def apply_source_recipe_learning_proposal_under_authorized_policy(
    workspace_dir: Path,
    *,
    proposal_id: str,
    policy_id: str,
    policy_authorized_by_user_id: str,
) -> dict[str, object]:
    """在团队已明确开启自动排序时，自动应用既有的固定五配方建议。

    该函数只能由服务器内的自适应学习周期调用，且仅接受已经保存的 proposal ID 和
    已审计策略的标识。它仍不能新增域名、删除固定配方、修改关键词、产业范围、Skill、
    网页内容、候选、证据或招商状态；唯一的可变结果是把完整的五条既有配方换成另一种
    排序，并记录这是“用户曾授权的自动执行”而非某位业务人员逐条点击批准。
    """
    normalized_proposal_id = _required_identifier(proposal_id, "proposal_id")
    normalized_policy_id = _required_identifier(policy_id, "policy_id")
    normalized_authorizer = _required_identifier(
        policy_authorized_by_user_id,
        "policy_authorized_by_user_id",
    )
    ledger = load_ledger(workspace_dir)
    protected_before = _protected_snapshot(ledger)
    proposal = next(
        (item for item in _proposals(ledger) if item.get("proposal_id") == normalized_proposal_id),
        None,
    )
    if proposal is None:
        raise ValueError("未找到来源学习建议")
    review_states = _review_state_by_proposal(_reviews(ledger))
    if normalized_proposal_id in review_states:
        return {
            "status": "already_reviewed",
            "review_state": review_states[normalized_proposal_id],
            "operation_scope": "authorized_adaptive_fixed_recipe_order_only",
        }
    proposed_recipe_order = _validate_recipe_order(proposal.get("proposed_recipe_order"))
    active_rule_set = _active_rule_set(ledger)
    if (
        proposal.get("active_rule_set_id") != active_rule_set["rule_set_id"]
        or proposal.get("active_rule_set_version") != active_rule_set["version"]
    ):
        raise RuntimeError("来源学习建议基于过期规则版本，拒绝自动应用")

    reviewed_at = _utc_now()
    active_record = next(
        record
        for record in _rule_sets(ledger)
        if record.get("rule_set_id") == active_rule_set["rule_set_id"]
        and record.get("version") == active_rule_set["version"]
    )
    active_record["status"] = "superseded"
    next_version = max(
        int(record.get("version", 0))
        for record in _rule_sets(ledger)
        if isinstance(record.get("version"), int) and not isinstance(record.get("version"), bool)
    ) + 1
    new_rule_set = {
        "rule_set_id": _stable_id(
            "source-recipe-rule-set",
            {
                "version": next_version,
                "proposal_id": normalized_proposal_id,
                "policy_id": normalized_policy_id,
                "reviewed_at": reviewed_at,
            },
        ),
        "version": next_version,
        "status": "active",
        "recipe_order": proposed_recipe_order,
        "created_at": reviewed_at,
        "created_by_user_id": AUTO_RECIPE_POLICY_SERVICE_USER_ID,
        "change_reason": "由团队已授权的自适应学习策略自动调整固定来源配方顺序",
        "based_on_proposal_id": normalized_proposal_id,
        "authorized_policy_id": normalized_policy_id,
        "policy_authorized_by_user_id": normalized_authorizer,
    }
    _rule_sets(ledger).append(new_rule_set)
    review = {
        "review_id": _stable_id(
            "source-recipe-learning-auto-application",
            {
                "proposal_id": normalized_proposal_id,
                "policy_id": normalized_policy_id,
                "reviewed_at": reviewed_at,
            },
        ),
        "proposal_id": normalized_proposal_id,
        "decision": "auto_applied",
        "old_review_state": "pending_user_review",
        "new_review_state": "auto_applied",
        "review_note": "仅按团队已授权策略重排固定五条来源配方；未新增来源、关键词、产业范围或证据。",
        "reviewed_at": reviewed_at,
        "reviewed_by_user_id": AUTO_RECIPE_POLICY_SERVICE_USER_ID,
        "policy_id": normalized_policy_id,
        "policy_authorized_by_user_id": normalized_authorizer,
        "new_rule_set_id": new_rule_set["rule_set_id"],
        "operation_scope": "authorized_adaptive_fixed_recipe_order_only",
    }
    _reviews(ledger).append(review)
    _assert_protected_snapshot_unchanged(protected_before, ledger)
    saved_path = save_ledger(workspace_dir, ledger)
    return {
        "status": "auto_applied",
        "review": copy.deepcopy(review),
        "new_rule_set": copy.deepcopy(new_rule_set),
        "ledger_path": str(saved_path),
        "operation_scope": "authorized_adaptive_fixed_recipe_order_only",
    }


def _outcome_feedback_summary(ledger: dict[str, Any]) -> dict[str, int]:
    """汇总既有最终招商反馈数量，供建议审查参考而非直接调来源配方。"""
    feedback_records = ledger.get("outcome_feedback")
    if not isinstance(feedback_records, list):
        raise ValueError("招商线索台账中的 outcome_feedback 必须是列表")
    counts = {"final_feedback_count": 0, "landed_count": 0, "not_converted_count": 0, "invalidated_count": 0}
    for feedback in feedback_records:
        if not isinstance(feedback, dict):
            continue
        outcome = feedback.get("outcome")
        if outcome not in FINAL_OUTCOMES:
            continue
        counts["final_feedback_count"] += 1
        counts[f"{outcome}_count"] += 1
    return counts


def _aggregate_recipe_feedback(feedback: dict[str, Any]) -> dict[str, dict[str, int]]:
    """聚合三条产业线的只读有效性计数，拒绝把候选或观察当作正式证据。

    输入是既有来源覆盖报告中的只读有效性反馈，输出为每条固定配方的正式证据、候选、
    域名广度、抓取失败和高优先级待补证观察计数。只有报告明确标记的
    ``formal_evidence_count_in_window`` 才用于排序；候选和高优先级观察只供人工上下文查看，
    不会被升级为企业事实或自动招商信号。函数不读写台账、不触发发现或抓取。
    """
    recipe_assessments = feedback.get("recipe_assessments")
    if not isinstance(recipe_assessments, list):
        raise ValueError("来源有效性反馈缺少配方评估列表")
    known_recipe_ids = _known_recipe_ids()
    aggregate = {
        recipe_id: {
            "formal_evidence_count_in_window": 0,
            "candidate_count": 0,
            "distinct_candidate_source_domain_count": 0,
            "capture_failed_candidate_count": 0,
            # 此处只统计候选快照的查看优先级，绝不是企业风险红色预警数量；风险不会参与
            # 来源配方排序，避免负面报道意外改变自动发现策略。
            "high_priority_observation_count": 0,
        }
        for recipe_id in known_recipe_ids
    }
    for assessment in recipe_assessments:
        if not isinstance(assessment, dict):
            raise ValueError("来源有效性配方评估必须是对象")
        recipe_id = assessment.get("source_catalog_id")
        metrics = assessment.get("metrics")
        if recipe_id not in aggregate:
            continue
        if not isinstance(metrics, dict):
            raise ValueError("来源有效性配方评估缺少计数指标")
        for field_name in aggregate[recipe_id]:
            value = metrics.get(field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("来源有效性配方计数必须是非负整数")
            aggregate[recipe_id][field_name] += value
    return aggregate


def get_active_source_recipe_rule_set(workspace_dir: Path) -> dict[str, object]:
    """读取当前工作区生效的来源配方规则，供认证业务接口与发现器安全复用。"""
    ledger = load_ledger(workspace_dir)
    return {"rule_set": _active_rule_set(ledger), "ledger_path": str(ledger_path(workspace_dir))}


def get_active_source_recipe_order(workspace_dir: Path) -> list[str]:
    """返回生效规则的完整固定配方顺序，发现器只能据此排序而不能接受调用方顺序。"""
    active_rule_set = get_active_source_recipe_rule_set(workspace_dir)["rule_set"]
    if not isinstance(active_rule_set, dict):
        raise RuntimeError("来源配方规则读取结果异常")
    return _validate_recipe_order(active_rule_set.get("recipe_order"))


def generate_source_recipe_learning_proposal(workspace_dir: Path) -> dict[str, object]:
    """由固定后台学习周期生成一条待审批的来源配方顺序建议。

    输入只有当前智能体服务器工作区，输出为样本不足、无需重排、已存在或新增建议的状态。
    函数读取 90 天来源有效性反馈，并仅按已签名正式证据数量及候选域名广度重排固定五条
    配方；至少需要三条正式证据、覆盖两条配方才提出建议，避免用单次命中放大偏见。它
    也附带既有最终招商反馈计数供人工审查，但不从结果文字、网页正文或模型输出生成
    Skill 指令。自动过程只追加建议，不能批准建议、替换生效规则、修改搜索词、证据、
    线索、评分或人工复核。
    """
    from .source_monitoring import get_source_coverage_report

    ledger = load_ledger(workspace_dir)
    protected_before = _protected_snapshot(ledger)
    active_rule_set = _active_rule_set(ledger)
    coverage = get_source_coverage_report(
        workspace_dir,
        window_days=SOURCE_RECIPE_LEARNING_WINDOW_DAYS,
    )
    feedback = coverage.get("source_effectiveness_feedback")
    if not isinstance(feedback, dict):
        raise RuntimeError("来源覆盖报告缺少有效性反馈")
    aggregate = _aggregate_recipe_feedback(feedback)
    total_formal_evidence_count = sum(
        metrics["formal_evidence_count_in_window"] for metrics in aggregate.values()
    )
    recipes_with_evidence_count = sum(
        metrics["formal_evidence_count_in_window"] > 0 for metrics in aggregate.values()
    )
    if total_formal_evidence_count < MIN_FORMAL_EVIDENCE_FOR_SOURCE_RECIPE_PROPOSAL or (
        recipes_with_evidence_count < MIN_RECIPES_WITH_EVIDENCE_FOR_SOURCE_RECIPE_PROPOSAL
    ):
        return {
            "status": "insufficient_evidence_sample",
            "total_formal_evidence_count": total_formal_evidence_count,
            "recipes_with_evidence_count": recipes_with_evidence_count,
            "minimum_formal_evidence_count": MIN_FORMAL_EVIDENCE_FOR_SOURCE_RECIPE_PROPOSAL,
            "minimum_recipes_with_evidence_count": MIN_RECIPES_WITH_EVIDENCE_FOR_SOURCE_RECIPE_PROPOSAL,
            "operation_scope": "scheduled_source_recipe_learning_pending_human_approval_only",
        }

    baseline_index = {recipe_id: index for index, recipe_id in enumerate(_known_recipe_ids())}
    proposed_recipe_order = sorted(
        _known_recipe_ids(),
        key=lambda recipe_id: (
            -aggregate[recipe_id]["formal_evidence_count_in_window"],
            -aggregate[recipe_id]["distinct_candidate_source_domain_count"],
            aggregate[recipe_id]["capture_failed_candidate_count"],
            baseline_index[recipe_id],
        ),
    )
    current_recipe_order = _validate_recipe_order(active_rule_set.get("recipe_order"))
    if proposed_recipe_order == current_recipe_order:
        return {
            "status": "no_priority_change_recommended",
            "active_rule_set": active_rule_set,
            "aggregate_recipe_feedback": aggregate,
            "operation_scope": "scheduled_source_recipe_learning_pending_human_approval_only",
        }

    proposal_fingerprint = {
        "active_rule_set_id": active_rule_set["rule_set_id"],
        "active_rule_set_version": active_rule_set["version"],
        "window_days": SOURCE_RECIPE_LEARNING_WINDOW_DAYS,
        "proposed_recipe_order": proposed_recipe_order,
        "aggregate_recipe_feedback": aggregate,
    }
    proposal_id = _stable_id("source-recipe-learning-proposal", proposal_fingerprint)
    review_states = _review_state_by_proposal(_reviews(ledger))
    existing_proposal = next(
        (proposal for proposal in _proposals(ledger) if proposal.get("proposal_id") == proposal_id),
        None,
    )
    if existing_proposal is not None:
        return {
            "status": "already_recorded",
            "proposal": copy.deepcopy(existing_proposal),
            "proposal_review_state": review_states.get(proposal_id, "pending_user_review"),
            "operation_scope": "scheduled_source_recipe_learning_pending_human_approval_only",
        }
    proposals = _proposals(ledger)
    if len(proposals) >= MAX_SOURCE_RECIPE_LEARNING_PROPOSALS:
        raise RuntimeError("来源学习建议数量已达到安全上限，请先人工审查现有建议")
    proposal = {
        "proposal_id": proposal_id,
        "created_at": _utc_now(),
        "proposal_type": "evidence_backed_fixed_source_recipe_order_only",
        "active_rule_set_id": active_rule_set["rule_set_id"],
        "active_rule_set_version": active_rule_set["version"],
        "current_recipe_order": current_recipe_order,
        "proposed_recipe_order": proposed_recipe_order,
        "window_days": SOURCE_RECIPE_LEARNING_WINDOW_DAYS,
        "aggregate_recipe_feedback": aggregate,
        "outcome_feedback_summary": _outcome_feedback_summary(ledger),
        "skill_update_performed": False,
        "skill_update_reason": "网页与模型输入不得直接改写Skill；仅生成来源配方的待审批建议。",
        "operation_scope": "scheduled_source_recipe_learning_pending_human_approval_only",
    }
    proposals.append(proposal)
    _assert_protected_snapshot_unchanged(protected_before, ledger)
    saved_path = save_ledger(workspace_dir, ledger)
    return {
        "status": "created_pending_user_review",
        "proposal": copy.deepcopy(proposal),
        "proposal_review_state": "pending_user_review",
        "ledger_path": str(saved_path),
        "operation_scope": "scheduled_source_recipe_learning_pending_human_approval_only",
    }


def list_source_recipe_learning_proposals(workspace_dir: Path) -> dict[str, object]:
    """只读列出来源学习建议及其派生审批状态，不触发学习、抓取或配置变更。"""
    ledger = load_ledger(workspace_dir)
    review_states = _review_state_by_proposal(_reviews(ledger))
    proposals = []
    for proposal in _proposals(ledger):
        proposal_id = _required_identifier(proposal.get("proposal_id"), "proposal_id")
        proposals.append(
            {
                "proposal": copy.deepcopy(proposal),
                "review_state": review_states.get(proposal_id, "pending_user_review"),
            }
        )
    return {
        "proposals": proposals,
        "proposal_count": len(proposals),
        "operation_scope": "authenticated_source_recipe_learning_read_only",
        "ledger_path": str(ledger_path(workspace_dir)),
    }


def review_source_recipe_learning_proposal(
    workspace_dir: Path,
    *,
    proposal_id: str,
    decision: str,
    review_note: str,
    authenticated_user_id: str,
) -> dict[str, object]:
    """由已认证普通用户批准或拒绝一条不可覆盖的来源学习建议。

    输入是服务器建议 ID、受控批准/拒绝决定、备注和只能来自认证层的操作者 ID；输出为
    只追加的审批审计，以及批准时新增的来源配方规则版本。任何普通登录用户可执行这一
    业务判断，不检查管理员角色；HTTP 层必须拒绝客户端提交的 ``authenticated_user_id``。
    建议原文、候选、原始证据、评分与招商状态始终不变；批准只把完整固定五配方的顺序
    写入新规则版本，拒绝只追加审计，二者都不更新 Skill、搜索词、URL 或网页访问范围。
    """
    normalized_proposal_id = _required_identifier(proposal_id, "proposal_id")
    normalized_decision = REVIEW_DECISION_ALIASES.get(
        _required_identifier(decision, "decision").casefold()
    )
    if normalized_decision is None:
        raise ValueError("decision必须是批准或拒绝")
    normalized_note = _required_note(review_note)
    normalized_user_id = _required_identifier(authenticated_user_id, "authenticated_user_id")

    ledger = load_ledger(workspace_dir)
    protected_before = _protected_snapshot(ledger)
    proposals = _proposals(ledger)
    proposal = next(
        (item for item in proposals if item.get("proposal_id") == normalized_proposal_id),
        None,
    )
    if proposal is None:
        raise ValueError("未找到来源学习建议")
    review_states = _review_state_by_proposal(_reviews(ledger))
    if normalized_proposal_id in review_states:
        raise RuntimeError("该来源学习建议已经完成审批，不能重复处理")
    proposed_recipe_order = _validate_recipe_order(proposal.get("proposed_recipe_order"))
    active_rule_set = _active_rule_set(ledger)
    if normalized_decision == "approved" and (
        proposal.get("active_rule_set_id") != active_rule_set["rule_set_id"]
        or proposal.get("active_rule_set_version") != active_rule_set["version"]
    ):
        raise RuntimeError("来源学习建议基于过期规则版本，需重新生成后再审批")

    reviewed_at = _utc_now()
    new_rule_set: dict[str, object] | None = None
    if normalized_decision == "approved":
        active_record = next(
            record
            for record in _rule_sets(ledger)
            if record.get("rule_set_id") == active_rule_set["rule_set_id"]
            and record.get("version") == active_rule_set["version"]
        )
        active_record["status"] = "superseded"
        next_version = max(
            int(record.get("version", 0))
            for record in _rule_sets(ledger)
            if isinstance(record.get("version"), int)
            and not isinstance(record.get("version"), bool)
        ) + 1
        new_rule_set = {
            "rule_set_id": _stable_id(
                "source-recipe-rule-set",
                {
                    "version": next_version,
                    "proposal_id": normalized_proposal_id,
                    "reviewed_at": reviewed_at,
                },
            ),
            "version": next_version,
            "status": "active",
            "recipe_order": proposed_recipe_order,
            "created_at": reviewed_at,
            "created_by_user_id": normalized_user_id,
            "change_reason": normalized_note,
            "based_on_proposal_id": normalized_proposal_id,
        }
        _rule_sets(ledger).append(new_rule_set)

    review = {
        "review_id": _stable_id(
            "source-recipe-learning-review",
            {
                "proposal_id": normalized_proposal_id,
                "decision": normalized_decision,
                "reviewed_at": reviewed_at,
                "reviewed_by_user_id": normalized_user_id,
            },
        ),
        "proposal_id": normalized_proposal_id,
        "decision": normalized_decision,
        "old_review_state": "pending_user_review",
        "new_review_state": normalized_decision,
        "review_note": normalized_note,
        "reviewed_at": reviewed_at,
        "reviewed_by_user_id": normalized_user_id,
        "new_rule_set_id": new_rule_set["rule_set_id"] if new_rule_set is not None else None,
        "operation_scope": "authenticated_source_recipe_learning_business_review_only",
    }
    _reviews(ledger).append(review)
    _assert_protected_snapshot_unchanged(protected_before, ledger)
    saved_path = save_ledger(workspace_dir, ledger)
    return {
        "review": copy.deepcopy(review),
        "new_rule_set": copy.deepcopy(new_rule_set),
        "ledger_path": str(saved_path),
        "operation_scope": "authenticated_source_recipe_learning_business_review_only",
    }
