"""徐州招商评分规则的版本化业务逻辑。"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .core import SUPPORTED_CLAIM_TYPE_TEXT_SUPPORTS
from .storage import ledger_path, load_ledger, save_ledger


TARGET_SECTORS = frozenset({"embodied_intelligence", "semiconductor", "biomedicine"})
FINAL_SUCCESS_OUTCOMES = frozenset({"landed"})
FINAL_FAILURE_OUTCOMES = frozenset({"not_converted", "invalidated"})
OUTCOME_ALIASES = {
    "落地": "landed",
    "landed": "landed",
    "推进": "positive_progress",
    "positive_progress": "positive_progress",
    "观察": "deferred",
    "deferred": "deferred",
    "未转化": "not_converted",
    "not_converted": "not_converted",
    "无效": "invalidated",
    "invalidated": "invalidated",
}
MAX_SCORE_SNAPSHOTS_PER_LEAD = 100
MAX_OUTCOME_FEEDBACK_PER_LEAD = 100
MIN_FINAL_FEEDBACK_FOR_SUGGESTIONS = 10
MIN_GROUP_SIZE_FOR_SUGGESTIONS = 3
WEIGHT_SUGGESTION_STEP = 5
WEIGHT_SUGGESTION_EFFECT_THRESHOLD = 0.25
OUTCOME_LEARNING_CYCLE_SCOPE = "scheduled_outcome_learning_cycle"
OUTCOME_LEARNING_SERVICE_USER_ID = "system-outcome-learning-v1"

_LANDING_CLAIM_TYPES = frozenset({"financing", "fund_raising", "cooperation", "expansion"})
_RISK_TERMS: dict[str, tuple[str, ...]] = {
    "direct_competitor": ("直接竞品", "已投资竞品", "投资了竞品", "竞争对手已投资"),
    "stage_or_scale_mismatch": ("阶段不匹配", "规模不匹配", "单笔金额不匹配", "投资阶段不匹配"),
    "fund_lifecycle_or_lack_of_recent_cases": (
        "基金生命周期末期",
        "基金临近到期",
        "缺少近期投资案例",
        "无近期投资案例",
    ),
}


def _utc_now() -> str:
    """返回规则版本与审计记录统一使用的 UTC 时间字符串。"""
    return datetime.now(timezone.utc).isoformat()


def _has_server_supported_claim_type(evidence: dict[str, Any]) -> bool:
    """判断证据分类是否已被服务器固定原文词支持，供自动评分安全筛选。

    输入是一条已签名证据，输出为是否允许其 ``claim_type`` 参与自动评分。旧版记录、未分类
    记录和未知状态一律返回 ``False``；即使其中保存了融资、徐州关联或合作标签，也只能由
    人工查看原始来源后再处理。函数不重新分类、不写评分或证据，避免结果学习把主观标签
    反馈为“有效规律”。
    """
    # 业务人员可登记有权使用的人工材料，但该材料不是由服务器重新抓取、按固定规则
    # 完成文本分类的页面。它可以进入人工招商研判和来源学习，不能被自动评分当作
    # 机器已验证的企业事实。
    return (
        evidence.get("evidence_origin") != "human_attested_material"
        and evidence.get("claim_type_text_support") in SUPPORTED_CLAIM_TYPE_TEXT_SUPPORTS
    )


def _required_text(value: object, field_name: str, *, max_length: int) -> str:
    """校验规则字段中的必填文本，拒绝空白、过长或非字符串输入。"""
    if not isinstance(value, str) or not (text := value.strip()):
        raise ValueError(f"{field_name}不能为空")
    if len(text) > max_length:
        raise ValueError(f"{field_name}不能超过{max_length}个字符")
    return text


def _validate_dimensions(value: object) -> list[dict[str, object]]:
    """校验正向评分维度，确保启用维度权重之和恰为 100。

    输入是客户端或后续规则建议提供的维度列表，输出是去除无效空白后的独立副本。
    该函数只验证业务规则结构，不读取或修改线索和证据；权重必须由已登录用户通过
    后端接口提交，避免模型、网页内容或自动学习过程直接篡改规则。
    """
    if not isinstance(value, list) or not value:
        raise ValueError("dimensions必须是非空列表")

    dimensions: list[dict[str, object]] = []
    seen_keys: set[str] = set()
    enabled_weight_total = 0
    for index, raw_dimension in enumerate(value):
        if not isinstance(raw_dimension, dict):
            raise ValueError(f"dimensions[{index}]必须是对象")
        key = _required_text(raw_dimension.get("key"), f"dimensions[{index}].key", max_length=100)
        if key in seen_keys:
            raise ValueError(f"dimensions中的key重复：{key}")
        seen_keys.add(key)
        label = _required_text(raw_dimension.get("label"), f"dimensions[{index}].label", max_length=100)
        description = _required_text(
            raw_dimension.get("description"),
            f"dimensions[{index}].description",
            max_length=1_000,
        )
        weight = raw_dimension.get("weight")
        enabled = raw_dimension.get("enabled")
        if not isinstance(weight, int) or isinstance(weight, bool) or not 0 <= weight <= 100:
            raise ValueError(f"dimensions[{index}].weight必须是0到100之间的整数")
        if not isinstance(enabled, bool):
            raise ValueError(f"dimensions[{index}].enabled必须是布尔值")
        if enabled:
            enabled_weight_total += weight
        elif weight != 0:
            raise ValueError(f"dimensions[{index}]未启用时weight必须为0")
        dimensions.append(
            {
                "key": key,
                "label": label,
                "weight": weight,
                "description": description,
                "enabled": enabled,
            }
        )

    if enabled_weight_total != 100:
        raise ValueError("已启用评分维度的weight之和必须为100")
    return dimensions


def _validate_penalty_rules(value: object) -> list[dict[str, object]]:
    """校验降权规则，确保每项具名且分值不会意外变成加分。

    输入是降权规则列表，输出是规范化副本。降权规则与正向维度独立保存，以免直接
    竞品、弱证据或过期信号被高分掩盖；本函数不处理任何证据正文或线索状态。
    """
    if not isinstance(value, list):
        raise ValueError("penalty_rules必须是列表")

    penalty_rules: list[dict[str, object]] = []
    seen_keys: set[str] = set()
    for index, raw_rule in enumerate(value):
        if not isinstance(raw_rule, dict):
            raise ValueError(f"penalty_rules[{index}]必须是对象")
        key = _required_text(raw_rule.get("key"), f"penalty_rules[{index}].key", max_length=100)
        if key in seen_keys:
            raise ValueError(f"penalty_rules中的key重复：{key}")
        seen_keys.add(key)
        label = _required_text(raw_rule.get("label"), f"penalty_rules[{index}].label", max_length=100)
        description = _required_text(
            raw_rule.get("description"),
            f"penalty_rules[{index}].description",
            max_length=1_000,
        )
        score_delta = raw_rule.get("score_delta")
        enabled = raw_rule.get("enabled")
        if (
            not isinstance(score_delta, int)
            or isinstance(score_delta, bool)
            or not -100 <= score_delta <= 0
        ):
            raise ValueError(f"penalty_rules[{index}].score_delta必须是-100到0之间的整数")
        if not isinstance(enabled, bool):
            raise ValueError(f"penalty_rules[{index}].enabled必须是布尔值")
        if enabled and score_delta == 0:
            raise ValueError(f"penalty_rules[{index}]启用时score_delta必须小于0")
        penalty_rules.append(
            {
                "key": key,
                "label": label,
                "score_delta": score_delta,
                "description": description,
                "enabled": enabled,
            }
        )
    return penalty_rules


def get_active_scoring_rule_set(workspace_dir: Path) -> dict[str, Any]:
    """读取当前智能体工作区唯一生效的招商评分规则版本。

    输入是工作区路径，输出是规则集的深拷贝，调用方无法通过返回对象篡改内存台账。
    所有登录用户都可读取业务规则；函数不需要管理员权限，也不读取或修改线索证据。
    多个或零个生效版本均被视为数据异常并报错，避免系统悄悄使用不确定的权重。
    """
    ledger = load_ledger(workspace_dir)
    active_rule_sets = [
        rule_set
        for rule_set in ledger["scoring_rule_sets"]
        if isinstance(rule_set, dict) and rule_set.get("status") == "active"
    ]
    if len(active_rule_sets) != 1:
        raise RuntimeError("招商评分规则必须且只能有一个生效版本")
    return copy.deepcopy(active_rule_sets[0])


def create_scoring_rule_set(
    workspace_dir: Path,
    *,
    dimensions: list[dict[str, object]],
    penalty_rules: list[dict[str, object]],
    change_reason: str,
    authenticated_user_id: str,
    expected_active_rule_set_id: str | None = None,
    expected_active_rule_set_version: int | None = None,
) -> dict[str, Any]:
    """由已认证用户创建新的评分规则版本，并保留旧版本供回溯。

    输入是完整维度、降权规则、变更原因和仅由服务器认证层提供的用户 ID；输出是新
    版本及台账路径。普通登录用户可执行该业务操作，无需管理员角色，但调用方绝不能
    把浏览器、模型或网页传来的用户 ID 作为 ``authenticated_user_id``。自动学习可额外
    提供服务器读取到的当前规则 ID 与版本，保存前必须再次匹配以拒绝并发下的旧建议；
    该参数不由 HTTP 请求暴露。函数先校验权重和降权边界，再将旧生效版本标为已替代并
    追加新版本；线索、原始证据、评分快照和结果反馈均保持不变。
    """
    normalized_dimensions = _validate_dimensions(dimensions)
    normalized_penalty_rules = _validate_penalty_rules(penalty_rules)
    normalized_change_reason = _required_text(change_reason, "change_reason", max_length=2_000)
    normalized_user_id = _required_text(
        authenticated_user_id,
        "authenticated_user_id",
        max_length=100,
    )
    if (expected_active_rule_set_id is None) != (expected_active_rule_set_version is None):
        raise ValueError("自动规则更新必须同时提供期望规则 ID 和版本")
    if expected_active_rule_set_id is not None:
        normalized_expected_rule_id = _required_text(
            expected_active_rule_set_id,
            "expected_active_rule_set_id",
            max_length=100,
        )
        if (
            not isinstance(expected_active_rule_set_version, int)
            or isinstance(expected_active_rule_set_version, bool)
            or expected_active_rule_set_version < 1
        ):
            raise ValueError("expected_active_rule_set_version必须是正整数")
    else:
        normalized_expected_rule_id = None

    ledger = load_ledger(workspace_dir)
    evidence_snapshot = json.dumps(ledger["evidence"], ensure_ascii=False, sort_keys=True)
    snapshots_snapshot = json.dumps(ledger["score_snapshots"], ensure_ascii=False, sort_keys=True)
    feedback_snapshot = json.dumps(ledger["outcome_feedback"], ensure_ascii=False, sort_keys=True)
    rule_sets: list[dict[str, Any]] = ledger["scoring_rule_sets"]
    active_rule_sets = [rule_set for rule_set in rule_sets if rule_set.get("status") == "active"]
    if len(active_rule_sets) != 1:
        raise RuntimeError("招商评分规则必须且只能有一个生效版本")
    if normalized_expected_rule_id is not None and (
        active_rule_sets[0].get("rule_set_id") != normalized_expected_rule_id
        or active_rule_sets[0].get("version") != expected_active_rule_set_version
    ):
        raise RuntimeError("自动学习所依据的当前评分规则已变化，拒绝写入过期建议")

    versions = [rule_set.get("version") for rule_set in rule_sets]
    if any(not isinstance(version, int) or isinstance(version, bool) or version < 1 for version in versions):
        raise ValueError("招商评分规则版本必须是正整数")
    next_version = max(versions) + 1
    created_at = _utc_now()
    previous_rule_set = active_rule_sets[0]
    previous_rule_set["status"] = "superseded"
    new_rule_set = {
        "rule_set_id": f"xuzhou-scoring-rules-v{next_version}",
        "version": next_version,
        "status": "active",
        "dimensions": normalized_dimensions,
        "penalty_rules": normalized_penalty_rules,
        "created_at": created_at,
        "created_by_user_id": normalized_user_id,
        "change_reason": normalized_change_reason,
    }
    rule_sets.append(new_rule_set)

    if (
        evidence_snapshot != json.dumps(ledger["evidence"], ensure_ascii=False, sort_keys=True)
        or snapshots_snapshot != json.dumps(ledger["score_snapshots"], ensure_ascii=False, sort_keys=True)
        or feedback_snapshot != json.dumps(ledger["outcome_feedback"], ensure_ascii=False, sort_keys=True)
    ):
        raise RuntimeError("创建评分规则版本不得修改线索、证据或历史评分反馈")

    saved_path = save_ledger(workspace_dir, ledger)
    return {"rule_set": new_rule_set, "ledger_path": str(saved_path)}


def _stable_id(prefix: str, payload: dict[str, object]) -> str:
    """依据服务器已验证的业务字段生成稳定 ID，不使用客户端指定主键。

    输入是受控载荷，输出为短 SHA-256 标识，用于评分快照去重和反馈审计关联。函数不联网、
    不写台账、不修改线索或证据；调用者必须先完成 lead、规则和证据归属验证。
    """
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}-{hashlib.sha256(serialized.encode('utf-8')).hexdigest()[:16]}"


def _required_identifier(value: object, field_name: str) -> str:
    """校验服务器传入的线索、快照或认证用户标识，拒绝空值和异常长度。

    输入为字符串标识及字段名，输出为去除空白后的标识。该函数只做格式边界校验，不把
    前端或模型传入的 ID 视为可信身份；身份可信性由 HTTP 路由的认证中间件保证。
    """
    return _required_text(value, field_name, max_length=100)


def _parse_source_date(value: object) -> date | None:
    """解析已签名证据中的来源日期，格式异常时保守返回空而不猜测发布时间。"""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _source_strength(evidence: dict[str, Any], *, today: date) -> int:
    """按来源等级、证据质量与发布日期计算 0–100 的保守证据强度。

    输入是一条已签名证据和服务器当天日期，输出为仅用于评分解释的整数强度。来源等级、
    质量或日期缺失时降低分数而不补造事实；本函数只读证据、不写快照、不改变状态。
    """
    if evidence.get("evidence_origin") == "human_attested_material":
        return 0
    quality = evidence.get("evidence_quality")
    if not isinstance(quality, int) or isinstance(quality, bool) or not 1 <= quality <= 5:
        return 0
    level_factor = {"primary": 1.0, "secondary": 0.75, "lead_only": 0.4}.get(
        evidence.get("source_level"),
        0.0,
    )
    source_date = _parse_source_date(evidence.get("source_published_at"))
    if source_date is None:
        recency_factor = 0.25
    else:
        age_days = max(0, (today - source_date).days)
        recency_factor = 1.0 if age_days <= 90 else 0.75 if age_days <= 180 else 0.5 if age_days <= 365 else 0.25
    return round((quality / 5) * level_factor * recency_factor * 100)


def _evidence_text(evidence: dict[str, Any]) -> str:
    """拼接已签名标题和摘录供明确风险词匹配，不从文本推导未出现的事实。"""
    if evidence.get("evidence_origin") == "human_attested_material":
        return ""
    return "\n".join(
        value.casefold()
        for value in (evidence.get("source_title"), evidence.get("excerpt"))
        if isinstance(value, str)
    )


def _score_component(
    *,
    rule_key: str,
    raw_score: int,
    weighted_score: int,
    reason: str,
    evidence_ids: list[str],
) -> dict[str, object]:
    """构造只引用既有证据 ID 的可解释评分组件，不复制或改写原始证据字段。"""
    return {
        "rule_key": rule_key,
        "raw_score": raw_score,
        "weighted_score": weighted_score,
        "reason": reason,
        "evidence_ids": sorted(set(evidence_ids)),
    }


def _matched_evidence_ids(
    evidence_records: list[dict[str, Any]],
    predicate: Any,
) -> list[str]:
    """返回满足固定评分条件的有效证据 ID，不读取网页或接受模型新证据。"""
    return sorted(
        evidence["evidence_id"]
        for evidence in evidence_records
        if predicate(evidence) and isinstance(evidence.get("evidence_id"), str)
    )


def _dimension_components(
    lead: dict[str, Any],
    evidence_records: list[dict[str, Any]],
    dimensions: list[dict[str, Any]],
    *,
    today: date,
) -> list[dict[str, object]]:
    """按固定证据规则生成正向维度组件，不以模型猜测补足企业意愿。

    输入为目标线索、其已签名证据、当前规则维度和服务器日期，输出为每个启用维度的
    原始分、加权分、理由及引用证据 ID。它只读取已有台账；无明确证据则给低分或零分，
    不能创建证据、改变线索状态或修改业务权重。
    """
    components: list[dict[str, object]] = []
    for dimension in dimensions:
        if dimension.get("enabled") is not True:
            continue
        key = dimension.get("key")
        weight = dimension.get("weight")
        if not isinstance(key, str) or not isinstance(weight, int):
            raise ValueError("生效评分维度格式异常")
        matching: list[dict[str, Any]] = []
        raw_score = 0
        reason = "当前规则未定义该维度的可验证评分逻辑，保守计为 0 分。"
        if key == "landing_intent":
            matching = [
                evidence
                for evidence in evidence_records
                if _has_server_supported_claim_type(evidence)
                and evidence.get("claim_type") in _LANDING_CLAIM_TYPES
            ]
            raw_score = max((_source_strength(evidence, today=today) for evidence in matching), default=0)
            reason = "仅按融资、募资、合作或扩张类已签名证据的来源强度计分。"
        elif key == "xuzhou_association":
            matching = [
                evidence
                for evidence in evidence_records
                if _has_server_supported_claim_type(evidence)
                and evidence.get("claim_type") == "xuzhou_connection"
            ]
            raw_score = max((_source_strength(evidence, today=today) for evidence in matching), default=0)
            reason = "仅按服务器原文支持的徐州关联分类证据计分；调用方关联标签不单独自动加分。"
        elif key == "industry_fit":
            matching = [
                evidence
                for evidence in evidence_records
                if _has_server_supported_claim_type(evidence)
                and evidence.get("claim_type") == "industry_focus"
            ]
            if lead.get("sector") in TARGET_SECTORS:
                raw_score = 100 if matching else 60
            reason = "目标产业线有基础匹配；行业方向证据可将该维度提升至满分。"
        elif key == "evidence_quality_recency":
            matching = [
                evidence
                for evidence in evidence_records
                if evidence.get("evidence_origin") != "human_attested_material"
            ]
            strengths = sorted(
                (_source_strength(evidence, today=today) for evidence in matching),
                reverse=True,
            )[:3]
            raw_score = round(sum(strengths) / len(strengths)) if strengths else 0
            reason = "取至多三条最强已签名证据的来源等级、质量与时效均值。"
        evidence_ids = _matched_evidence_ids(matching, lambda _evidence: True)
        components.append(
            _score_component(
                rule_key=key,
                raw_score=raw_score,
                weighted_score=round(raw_score * weight / 100),
                reason=reason,
                evidence_ids=evidence_ids,
            )
        )
    return components


def _penalty_components(
    lead: dict[str, Any],
    evidence_records: list[dict[str, Any]],
    penalty_rules: list[dict[str, Any]],
    *,
    today: date,
) -> list[dict[str, object]]:
    """按明确风险词、弱证据和过期日期生成降权组件，不把沉默解释为风险事实。

    输入为线索、已签名证据、当前降权规则与服务器日期，输出为触发的负分组件。直接竞品、
    阶段不匹配和基金生命周期只在标题或原始摘录出现固定术语时触发；函数不写台账、不改
    证据、不自动作废或推进线索。
    """
    components: list[dict[str, object]] = []
    scoring_evidence = [
        evidence
        for evidence in evidence_records
        if evidence.get("evidence_origin") != "human_attested_material"
    ]
    evidence_text = {
        evidence["evidence_id"]: _evidence_text(evidence)
        for evidence in scoring_evidence
        if isinstance(evidence.get("evidence_id"), str)
    }
    for rule in penalty_rules:
        if rule.get("enabled") is not True:
            continue
        key = rule.get("key")
        score_delta = rule.get("score_delta")
        if not isinstance(key, str) or not isinstance(score_delta, int):
            raise ValueError("生效降权规则格式异常")
        matching_ids: list[str] = []
        reason = ""
        if key in _RISK_TERMS:
            matching_ids = sorted(
                evidence_id
                for evidence_id, text in evidence_text.items()
                if any(term.casefold() in text for term in _RISK_TERMS[key])
            )
            if key == "fund_lifecycle_or_lack_of_recent_cases" and lead.get("entity_kind") != "fund":
                matching_ids = []
            reason = "仅在已签名标题或摘录出现固定风险术语时降权。"
        elif key == "weak_or_conflicting_evidence":
            matching_ids = _matched_evidence_ids(
                scoring_evidence,
                lambda evidence: _source_strength(evidence, today=today) < 45,
            )
            if not any(_source_strength(evidence, today=today) >= 45 for evidence in scoring_evidence):
                reason = "没有达到最低强度阈值的可追溯证据，按弱证据规则降权。"
            else:
                matching_ids = []
        elif key == "signal_stale":
            signal_dates = [
                _parse_source_date(evidence.get("source_published_at"))
                for evidence in evidence_records
                if _has_server_supported_claim_type(evidence)
                and evidence.get("claim_type") in _LANDING_CLAIM_TYPES
            ]
            latest_signal_date = max((item for item in signal_dates if item is not None), default=None)
            if latest_signal_date is not None and (today - latest_signal_date).days > 180:
                matching_ids = _matched_evidence_ids(
                    scoring_evidence,
                    lambda evidence: _has_server_supported_claim_type(evidence)
                    and evidence.get("claim_type") in _LANDING_CLAIM_TYPES,
                )
                reason = "最近一条可验证的融资、合作或扩张信号已超过 180 天。"
        if matching_ids:
            components.append(
                _score_component(
                    rule_key=key,
                    raw_score=-100,
                    weighted_score=score_delta,
                    reason=reason,
                    evidence_ids=matching_ids,
                )
            )
    return components


def _protected_scoring_snapshot(ledger: dict[str, Any]) -> dict[str, str]:
    """序列化评分与反馈操作绝不能改写的业务数据，供保存前后逐字校验。"""
    protected_fields = ("leads", "evidence", "scoring_rule_sets")
    snapshot: dict[str, str] = {}
    for field_name in protected_fields:
        value = ledger.get(field_name)
        if not isinstance(value, list):
            raise ValueError(f"招商线索台账中的 {field_name} 必须是列表")
        snapshot[field_name] = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return snapshot


def _assert_protected_scoring_records_unchanged(before: dict[str, str], ledger: dict[str, Any]) -> None:
    """确保评分或反馈只追加各自审计，不得改变线索、证据或规则版本。"""
    if _protected_scoring_snapshot(ledger) != before:
        raise RuntimeError("评分和结果反馈不得修改线索、原始证据或评分规则")


def create_score_snapshot(
    workspace_dir: Path,
    *,
    lead_id: str,
    authenticated_user_id: str,
) -> dict[str, Any]:
    """由已登录用户按当前规则为一条线索创建可追溯评分快照。

    输入是线索 ID 和仅由服务器认证层提供的用户 ID，输出是新建或已存在的评分结果及台账
    路径。函数只读取已签名证据，以固定规则生成组件并绑定当前规则版本、全部引用证据 ID
    与操作人；普通用户可以触发，不要求管理员。它不接受客户端总分、组件、规则版本或
    证据正文，且绝不改变线索状态、人工复核、原始证据、评分规则或招商推进。
    """
    normalized_lead_id = _required_identifier(lead_id, "lead_id")
    normalized_user_id = _required_identifier(authenticated_user_id, "authenticated_user_id")
    ledger = load_ledger(workspace_dir)
    protected_before = _protected_scoring_snapshot(ledger)
    leads = ledger.get("leads")
    evidence_records = ledger.get("evidence")
    score_snapshots = ledger.get("score_snapshots")
    rule_sets = ledger.get("scoring_rule_sets")
    if not all(isinstance(value, list) for value in (leads, evidence_records, score_snapshots, rule_sets)):
        raise ValueError("招商线索台账评分字段必须是列表")
    lead = next((item for item in leads if item.get("lead_id") == normalized_lead_id), None)
    if not isinstance(lead, dict):
        raise ValueError("未找到对应的招商线索")
    active_rule_sets = [item for item in rule_sets if item.get("status") == "active"]
    if len(active_rule_sets) != 1:
        raise RuntimeError("招商评分规则必须且只能有一个生效版本")
    active_rule_set = active_rule_sets[0]
    dimensions = active_rule_set.get("dimensions")
    penalty_rules = active_rule_set.get("penalty_rules")
    rule_set_id = active_rule_set.get("rule_set_id")
    rule_set_version = active_rule_set.get("version")
    if (
        not isinstance(dimensions, list)
        or not isinstance(penalty_rules, list)
        or not isinstance(rule_set_id, str)
        or not isinstance(rule_set_version, int)
    ):
        raise ValueError("当前招商评分规则格式异常")
    lead_evidence = [
        evidence
        for evidence in evidence_records
        if isinstance(evidence, dict) and evidence.get("lead_id") == normalized_lead_id
    ]
    # 评分仅消费服务器可复现的页面证据。人工材料仍会保留在线索中，供人阅读、审批和
    # 来源学习，但不能让自动总分、风险词或后续权重学习误认为服务器已经核验过其内容。
    scoring_evidence = [
        evidence
        for evidence in lead_evidence
        if evidence.get("evidence_origin") != "human_attested_material"
    ]
    evidence_ids = _matched_evidence_ids(scoring_evidence, lambda _evidence: True)
    snapshot_id = _stable_id(
        "score-snapshot",
        {
            "lead_id": normalized_lead_id,
            "rule_set_id": rule_set_id,
            "rule_set_version": rule_set_version,
            "evidence_ids": evidence_ids,
        },
    )
    existing_snapshot = next(
        (item for item in score_snapshots if item.get("score_snapshot_id") == snapshot_id),
        None,
    )
    if isinstance(existing_snapshot, dict):
        return {
            "created": False,
            "message": "当前规则与证据基础已经生成评分，未重复写入快照",
            "score_snapshot": copy.deepcopy(existing_snapshot),
            "ledger_path": str(ledger_path(workspace_dir)),
        }
    lead_snapshot_count = sum(
        1 for item in score_snapshots if isinstance(item, dict) and item.get("lead_id") == normalized_lead_id
    )
    if lead_snapshot_count >= MAX_SCORE_SNAPSHOTS_PER_LEAD:
        raise RuntimeError("该线索的评分快照数量已达到安全上限")
    today = datetime.now(timezone.utc).date()
    components = _dimension_components(lead, scoring_evidence, dimensions, today=today)
    components.extend(_penalty_components(lead, scoring_evidence, penalty_rules, today=today))
    total_score = max(0, min(100, sum(int(component["weighted_score"]) for component in components)))
    score_snapshot = {
        "score_snapshot_id": snapshot_id,
        "lead_id": normalized_lead_id,
        "rule_set_id": rule_set_id,
        "rule_set_version": rule_set_version,
        "total_score": total_score,
        "components": components,
        "scored_at": _utc_now(),
        "scored_by_user_id": normalized_user_id,
        "evidence_ids": evidence_ids,
        "server_captured_evidence_count": len(scoring_evidence),
        "human_attested_material_excluded_count": len(lead_evidence) - len(scoring_evidence),
        "operation_scope": "authenticated_user_evidence_based_score_snapshot_only",
    }
    score_snapshots.append(score_snapshot)
    _assert_protected_scoring_records_unchanged(protected_before, ledger)
    saved_path = save_ledger(workspace_dir, ledger)
    return {"created": True, "score_snapshot": score_snapshot, "ledger_path": str(saved_path)}


def record_outcome_feedback(
    workspace_dir: Path,
    *,
    lead_id: str,
    score_snapshot_id: str,
    outcome: str,
    note: str,
    authenticated_user_id: str,
) -> dict[str, Any]:
    """由已登录用户为既有评分快照追加真实招商结果反馈。

    输入是线索、其评分 ID、受控结果枚举、备注和服务器认证身份；输出为只追加的反馈审计。
    函数验证快照确属该线索，禁止客户端提交用户 ID、分数或证据；普通用户无需管理员角色。
    它不改权重、Skill、线索状态、原始证据或历史快照。学习模块会先把这些已确认反馈汇总
    为建议；仅在固定范围的服务器定时学习周期被明确启用后，才可能自动创建下一版规则。
    """
    normalized_lead_id = _required_identifier(lead_id, "lead_id")
    normalized_snapshot_id = _required_identifier(score_snapshot_id, "score_snapshot_id")
    normalized_outcome = OUTCOME_ALIASES.get(_required_text(outcome, "outcome", max_length=100).casefold())
    if normalized_outcome is None:
        accepted = "、".join(sorted(set(OUTCOME_ALIASES.values())))
        raise ValueError(f"outcome不合法，可选值为：{accepted}")
    normalized_note = _required_text(note, "note", max_length=2_000)
    normalized_user_id = _required_identifier(authenticated_user_id, "authenticated_user_id")
    ledger = load_ledger(workspace_dir)
    protected_before = _protected_scoring_snapshot(ledger)
    leads = ledger.get("leads")
    score_snapshots = ledger.get("score_snapshots")
    feedback_records = ledger.get("outcome_feedback")
    if not all(isinstance(value, list) for value in (leads, score_snapshots, feedback_records)):
        raise ValueError("招商线索台账反馈字段必须是列表")
    if not any(isinstance(lead, dict) and lead.get("lead_id") == normalized_lead_id for lead in leads):
        raise ValueError("未找到对应的招商线索")
    score_snapshot = next(
        (
            item
            for item in score_snapshots
            if isinstance(item, dict)
            and item.get("score_snapshot_id") == normalized_snapshot_id
            and item.get("lead_id") == normalized_lead_id
        ),
        None,
    )
    if score_snapshot is None:
        raise ValueError("评分快照不存在，或不属于该招商线索")
    lead_feedback_count = sum(
        1 for item in feedback_records if isinstance(item, dict) and item.get("lead_id") == normalized_lead_id
    )
    if lead_feedback_count >= MAX_OUTCOME_FEEDBACK_PER_LEAD:
        raise RuntimeError("该线索的结果反馈数量已达到安全上限")
    recorded_at = _utc_now()
    feedback = {
        "feedback_id": _stable_id(
            "outcome-feedback",
            {
                "lead_id": normalized_lead_id,
                "score_snapshot_id": normalized_snapshot_id,
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
        "score_snapshot_id": normalized_snapshot_id,
        "operation_scope": "authenticated_user_append_outcome_feedback_only",
    }
    feedback_records.append(feedback)
    _assert_protected_scoring_records_unchanged(protected_before, ledger)
    saved_path = save_ledger(workspace_dir, ledger)
    return {"feedback": feedback, "ledger_path": str(saved_path)}


def get_scoring_adjustment_suggestions(workspace_dir: Path) -> dict[str, object]:
    """根据已确认的最终招商结果生成只读权重调整建议，不自动学习或修改规则。

    输入为当前工作区，输出为样本量、每个维度的高低分组转化率和建议方向。仅使用绑定评分
    快照的最终结果：``landed`` 为成功，``not_converted`` 与 ``invalidated`` 为失败；少于
    10 条最终反馈或任一比较组少于 3 条时只报告样本不足。只统计当前生效规则版本生成的
    快照，避免不同权重口径相互污染。函数不联网、不写台账、不改权重、Skill、线索、复核
    或证据；固定范围的服务器学习周期可读取本结果创建下一规则版本，但模型与前端不能。
    """
    ledger = load_ledger(workspace_dir)
    rule_set = get_active_scoring_rule_set(workspace_dir)
    score_snapshots = ledger.get("score_snapshots")
    feedback_records = ledger.get("outcome_feedback")
    if not isinstance(score_snapshots, list) or not isinstance(feedback_records, list):
        raise ValueError("招商线索台账中的评分反馈字段必须是列表")
    snapshots_by_id = {
        snapshot.get("score_snapshot_id"): snapshot
        for snapshot in score_snapshots
        if (
            isinstance(snapshot, dict)
            and isinstance(snapshot.get("score_snapshot_id"), str)
            and snapshot.get("rule_set_id") == rule_set.get("rule_set_id")
            and snapshot.get("rule_set_version") == rule_set.get("version")
            # 新版快照显式记录服务器证据数量。没有机器可复现证据的线索可继续由
            # 人工研判和来源学习使用，但不能作为自动评分权重调整的训练样本。
            and (
                not isinstance(snapshot.get("server_captured_evidence_count"), int)
                or snapshot.get("server_captured_evidence_count") > 0
            )
        )
    }
    latest_final_feedback_by_snapshot: dict[str, dict[str, Any]] = {}
    for feedback in feedback_records:
        if not isinstance(feedback, dict):
            continue
        snapshot_id = feedback.get("score_snapshot_id")
        outcome = feedback.get("outcome")
        if (
            not isinstance(snapshot_id, str)
            or snapshot_id not in snapshots_by_id
            or outcome not in FINAL_SUCCESS_OUTCOMES | FINAL_FAILURE_OUTCOMES
        ):
            continue
        previous = latest_final_feedback_by_snapshot.get(snapshot_id)
        if previous is None or str(feedback.get("recorded_at", "")) > str(previous.get("recorded_at", "")):
            latest_final_feedback_by_snapshot[snapshot_id] = feedback
    final_feedback = list(latest_final_feedback_by_snapshot.values())
    if len(final_feedback) < MIN_FINAL_FEEDBACK_FOR_SUGGESTIONS:
        return {
            "ready": False,
            "final_feedback_count": len(final_feedback),
            "minimum_final_feedback_count": MIN_FINAL_FEEDBACK_FOR_SUGGESTIONS,
            "suggestions": [],
            "reason": "最终结果反馈样本不足，只保留统计，不生成权重调整建议。",
        }
    dimensions = rule_set.get("dimensions")
    if not isinstance(dimensions, list):
        raise ValueError("当前招商评分规则维度格式异常")
    suggestions: list[dict[str, object]] = []
    for dimension in dimensions:
        if not isinstance(dimension, dict) or dimension.get("enabled") is not True:
            continue
        dimension_key = dimension.get("key")
        if not isinstance(dimension_key, str):
            continue
        high_outcomes: list[bool] = []
        low_outcomes: list[bool] = []
        for feedback in final_feedback:
            snapshot = snapshots_by_id[feedback["score_snapshot_id"]]
            components = snapshot.get("components")
            if not isinstance(components, list):
                continue
            component = next(
                (
                    item
                    for item in components
                    if isinstance(item, dict) and item.get("rule_key") == dimension_key
                ),
                None,
            )
            if not isinstance(component, dict) or not isinstance(component.get("raw_score"), int):
                continue
            target = high_outcomes if component["raw_score"] >= 60 else low_outcomes
            target.append(feedback.get("outcome") in FINAL_SUCCESS_OUTCOMES)
        high_count = len(high_outcomes)
        low_count = len(low_outcomes)
        if high_count < MIN_GROUP_SIZE_FOR_SUGGESTIONS or low_count < MIN_GROUP_SIZE_FOR_SUGGESTIONS:
            suggestions.append(
                {
                    "dimension_key": dimension_key,
                    "recommendation": "observe",
                    "reason": "高低分组任一侧最终反馈不足 3 条，不调整权重。",
                    "high_group_count": high_count,
                    "low_group_count": low_count,
                }
            )
            continue
        high_success_rate = sum(high_outcomes) / high_count
        low_success_rate = sum(low_outcomes) / low_count
        success_rate_gap = high_success_rate - low_success_rate
        recommendation = "observe"
        suggested_weight_delta = 0
        if success_rate_gap >= WEIGHT_SUGGESTION_EFFECT_THRESHOLD:
            recommendation = "increase"
            suggested_weight_delta = WEIGHT_SUGGESTION_STEP
        elif success_rate_gap <= -WEIGHT_SUGGESTION_EFFECT_THRESHOLD:
            recommendation = "decrease"
            suggested_weight_delta = -WEIGHT_SUGGESTION_STEP
        suggestions.append(
            {
                "dimension_key": dimension_key,
                "recommendation": recommendation,
                "suggested_weight_delta": suggested_weight_delta,
                "high_group_count": high_count,
                "low_group_count": low_count,
                "high_group_landed_rate": round(high_success_rate, 4),
                "low_group_landed_rate": round(low_success_rate, 4),
                "landed_rate_gap": round(success_rate_gap, 4),
                "reason": "建议仅供普通登录用户创建新版规则时人工审查，系统不会自动改权重。",
            }
        )
    return {
        "ready": True,
        "final_feedback_count": len(final_feedback),
        "minimum_final_feedback_count": MIN_FINAL_FEEDBACK_FOR_SUGGESTIONS,
        "active_rule_set_id": rule_set.get("rule_set_id"),
        "active_rule_set_version": rule_set.get("version"),
        "suggestions": suggestions,
        "operation_scope": "readonly_outcome_feedback_based_weight_suggestions_only",
    }


def _select_automatic_weight_adjustment(
    rule_set: dict[str, Any],
    suggestions: list[dict[str, object]],
) -> dict[str, object] | None:
    """从充分样本建议中选择单个可逆权重调整，并保持启用权重总和为 100。

    输入是当前生效规则和只读学习建议，输出为目标维度、平衡维度、调整后的完整维度列表及
    审计原因；没有明确增减建议时返回空。每次周期最多调整一个维度 5 个百分点，增分必从
    另一启用维度等额扣除、降分必等额补到另一维度，避免自动学习改变总分语义。函数不写
    台账、不接收模型权重、不修改证据或线索；实际保存仍由受控规则版本函数完成。
    """
    dimensions = rule_set.get("dimensions")
    if not isinstance(dimensions, list):
        raise ValueError("当前招商评分规则维度格式异常")
    normalized_dimensions = copy.deepcopy(dimensions)
    dimensions_by_key = {
        dimension.get("key"): dimension
        for dimension in normalized_dimensions
        if isinstance(dimension, dict) and isinstance(dimension.get("key"), str)
    }
    actionable = [
        suggestion
        for suggestion in suggestions
        if suggestion.get("recommendation") in {"increase", "decrease"}
        and isinstance(suggestion.get("dimension_key"), str)
        and suggestion["dimension_key"] in dimensions_by_key
    ]
    if not actionable:
        return None
    actionable.sort(
        key=lambda suggestion: (
            -abs(float(suggestion.get("landed_rate_gap", 0))),
            str(suggestion["dimension_key"]),
        )
    )
    selected = actionable[0]
    target_key = selected["dimension_key"]
    target = dimensions_by_key[target_key]
    if target.get("enabled") is not True or not isinstance(target.get("weight"), int):
        return None
    increase = selected.get("recommendation") == "increase"
    positive_keys = {
        suggestion.get("dimension_key")
        for suggestion in actionable
        if suggestion.get("recommendation") == "increase"
    }
    candidates = [
        dimension
        for key, dimension in dimensions_by_key.items()
        if key != target_key
        and dimension.get("enabled") is True
        and isinstance(dimension.get("weight"), int)
        and (not increase or key not in positive_keys)
        and (not increase or dimension["weight"] >= WEIGHT_SUGGESTION_STEP)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda dimension: (-int(dimension["weight"]), str(dimension["key"])))
    balancing_dimension = candidates[0]
    if increase:
        target["weight"] += WEIGHT_SUGGESTION_STEP
        balancing_dimension["weight"] -= WEIGHT_SUGGESTION_STEP
    else:
        if target["weight"] < WEIGHT_SUGGESTION_STEP:
            return None
        target["weight"] -= WEIGHT_SUGGESTION_STEP
        balancing_dimension["weight"] += WEIGHT_SUGGESTION_STEP
    return {
        "target_dimension_key": target_key,
        "balancing_dimension_key": balancing_dimension["key"],
        "recommendation": selected["recommendation"],
        "suggested_weight_delta": WEIGHT_SUGGESTION_STEP if increase else -WEIGHT_SUGGESTION_STEP,
        "dimensions": normalized_dimensions,
        "reason": (
            f"自动学习基于当前规则版本的最终反馈：{target_key} "
            f"{selected['recommendation']} {WEIGHT_SUGGESTION_STEP}，"
            f"与 {balancing_dimension['key']} 等额平衡；总权重保持 100。"
        ),
    }


def run_outcome_learning_cycle(workspace_dir: Path) -> dict[str, object]:
    """执行一次固定范围的结果反馈学习周期，必要时自动创建一版新的生效评分规则。

    输入只有服务器工作区，输出为未执行原因或新规则版本及所用建议。函数先限制样本仅来自
    当前规则版本，再要求最少 10 条最终反馈和每组 3 条样本；满足条件时只调整一个维度 5
    个百分点，并以另一维度等额平衡。系统操作人是写死的服务器服务身份，不能由 Cron、
    模型或前端传入。它绝不改写 evidence、来源快照、线索状态、人工复核、反馈或既有规则；
    只通过版本化规则函数追加新版本，普通用户仍可用人工规则版本覆盖后续权重。
    """
    suggestion_result = get_scoring_adjustment_suggestions(workspace_dir)
    if suggestion_result.get("ready") is not True:
        return {
            "applied": False,
            "reason": suggestion_result.get("reason", "学习样本不足，未创建新规则版本。"),
            "suggestion_result": suggestion_result,
        }
    active_rule_set = get_active_scoring_rule_set(workspace_dir)
    if (
        active_rule_set.get("rule_set_id") != suggestion_result.get("active_rule_set_id")
        or active_rule_set.get("version") != suggestion_result.get("active_rule_set_version")
    ):
        raise RuntimeError("学习建议对应的规则版本已变化，请在下一周期重新计算")
    suggestions = suggestion_result.get("suggestions")
    if not isinstance(suggestions, list):
        raise ValueError("学习建议格式异常")
    adjustment = _select_automatic_weight_adjustment(active_rule_set, suggestions)
    if adjustment is None:
        return {
            "applied": False,
            "reason": "当前规则版本没有达到自动调整阈值的单维度建议。",
            "suggestion_result": suggestion_result,
        }
    dimensions = adjustment.get("dimensions")
    if not isinstance(dimensions, list):
        raise RuntimeError("自动学习调整后的维度格式异常")
    result = create_scoring_rule_set(
        workspace_dir,
        dimensions=dimensions,
        penalty_rules=copy.deepcopy(active_rule_set["penalty_rules"]),
        change_reason=str(adjustment["reason"]),
        authenticated_user_id=OUTCOME_LEARNING_SERVICE_USER_ID,
        expected_active_rule_set_id=str(active_rule_set["rule_set_id"]),
        expected_active_rule_set_version=int(active_rule_set["version"]),
    )
    return {
        "applied": True,
        "operation_scope": "scheduled_outcome_feedback_single_dimension_rule_versioning_only",
        "adjustment": adjustment,
        "suggestion_result": suggestion_result,
        **result,
    }
