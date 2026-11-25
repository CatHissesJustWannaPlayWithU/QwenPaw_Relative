"""从已签名证据生成经营指标待复核草稿，并提供不可混加的并列统计。

本模块只处理已经进入 ``evidence`` 的公开证据摘录。它不联网、不读取候选页面、不会
把搜索摘要当成指标，也不会自动确认或推进招商线索。营业收入、产值和项目投资额在现实
业务中口径不同，报告只将三者按各自口径并列展示，严禁相加成一个所谓的“产业总额”。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .storage import ledger_path, load_ledger, save_ledger


# 自动提取只产生有限数量的待复核草稿，防止单条异常摘录在后台任务中放大为大量待办。
MAX_METRIC_DRAFTS_PER_RUN = 50
MAX_METRIC_DRAFT_RECORDS = 2_000
MAX_METRIC_REVIEW_NOTE_CHARS = 2_000
METRIC_EXTRACTOR_VERSION = "deterministic-signed-evidence-metric-v1"
METRIC_DRAFT_STATE = "pending_user_review"
METRIC_REVIEW_OPERATION_SCOPE = "authenticated_user_industry_metric_draft_review_only"
_VALID_ID_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")

# 三类指标必须保持独立。项目投资额的三个细项仍归到 project_investment，但保留原词，
# 让人工能区分总投资、计划投资和完成投资，避免误把不同项目阶段混为同一数。
METRIC_RULES: tuple[dict[str, str], ...] = (
    {
        "metric_kind": "revenue",
        "metric_label": "营业收入",
        "statistic_scope": "enterprise",
        "pattern": r"(?P<label>营业收入|销售收入)\s*(?:达|达到|为|约为|实现)?\s*(?P<value>\d{1,12}(?:[,.]\d{1,4})?)\s*(?P<unit>亿元|亿|万元|万|元)",
    },
    {
        "metric_kind": "output_value",
        "metric_label": "产值",
        "statistic_scope": "enterprise",
        "pattern": r"(?P<label>工业总产值|工业产值|产值)\s*(?:达|达到|为|约为|实现)?\s*(?P<value>\d{1,12}(?:[,.]\d{1,4})?)\s*(?P<unit>亿元|亿|万元|万|元)",
    },
    {
        "metric_kind": "project_investment",
        "metric_label": "项目投资额",
        "statistic_scope": "project",
        "pattern": r"(?P<label>项目总投资|总投资|计划投资|完成投资)\s*(?:达|达到|为|约为|完成)?\s*(?P<value>\d{1,12}(?:[,.]\d{1,4})?)\s*(?P<unit>亿元|亿|万元|万|元)",
    },
)
_SENTENCE_PATTERN = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?", re.MULTILINE)
_YEAR_PATTERN = re.compile(r"(?<!\d)(?P<year>20\d{2})年")
_UNIT_MULTIPLIERS = {
    "元": Decimal("1"),
    "万": Decimal("10000"),
    "万元": Decimal("10000"),
    "亿": Decimal("100000000"),
    "亿元": Decimal("100000000"),
}


def _utc_now() -> str:
    """返回服务器生成的 UTC 审计时间，不接受页面、模型或客户端提供的操作时间。"""

    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, payload: dict[str, object]) -> str:
    """根据已签名证据 ID、命中位置和抽取版本生成稳定 ID，避免重复任务重复入草稿。"""

    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}-{hashlib.sha256(serialized.encode('utf-8')).hexdigest()[:16]}"


def _stable_json(value: object) -> str:
    """把受保护字段确定性序列化，用于在保存前发现任何越权改写。"""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _required_text(value: object, field_name: str, *, maximum: int) -> str:
    """校验服务器草稿 ID、复核结论与认证用户 ID 的短文本，不接收任何来源事实字段。"""

    if not isinstance(value, str) or not (normalized := value.strip()):
        raise ValueError(f"{field_name}不能为空")
    if len(normalized) > maximum:
        raise ValueError(f"{field_name}不能超过{maximum}个字符")
    return normalized


def _required_metric_draft_id(value: object) -> str:
    """只接受服务器生成的指标草稿 ID，拒绝 URL、路径和任意对象进入复核流程。"""

    draft_id = _required_text(value, "metric_draft_id", maximum=120)
    if any(character not in _VALID_ID_CHARACTERS for character in draft_id):
        raise ValueError("metric_draft_id必须是有效的服务器指标草稿 ID")
    return draft_id


def _metric_drafts(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    """读取独立指标草稿列表；台账结构异常时失败关闭，不猜测或重建历史草稿。"""

    drafts = ledger.get("industry_metric_drafts")
    if not isinstance(drafts, list):
        raise ValueError("招商线索台账中的 industry_metric_drafts 必须是列表")
    return drafts


def _metric_draft_reviews(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    """读取指标复核追加审计；异常时停止，避免伪造或覆盖既有责任记录。"""

    reviews = ledger.get("industry_metric_draft_reviews")
    if not isinstance(reviews, list):
        raise ValueError("招商线索台账中的 industry_metric_draft_reviews 必须是列表")
    return reviews


def _confirmed_metrics(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    """读取人工确认后的经营指标列表；该列表仅能由受控草稿复核追加。"""

    metrics = ledger.get("industry_business_metrics")
    if not isinstance(metrics, list):
        raise ValueError("招商线索台账中的 industry_business_metrics 必须是列表")
    return metrics


def _protected_records_snapshot(
    ledger: dict[str, Any],
    *,
    include_metric_drafts: bool,
) -> dict[str, str]:
    """快照本模块绝不能改写的原始证据、线索和既有草稿内容。

    输入为已通过全台账 HMAC 验证的内存台账，输出为稳定序列化快照。自动提取只能追加
    ``industry_metric_drafts``，人工复核只能追加审计和确认指标；因此来源 URL、原始摘录、
    采集时间、证据签名、评分依据、线索状态和先前草稿都不能被指标功能覆盖。
    """

    protected_fields = (
        "leads",
        "evidence",
        "score_snapshots",
        "outcome_feedback",
        "discovery_candidates",
        "candidate_observations",
        "evidence_drafts",
    )
    if include_metric_drafts:
        # 人工复核只能追加审计及确认后的派生记录，已生成草稿本体也不得被覆盖。
        protected_fields = (*protected_fields, "industry_metric_drafts")
    snapshot: dict[str, str] = {}
    for field_name in protected_fields:
        value = ledger.get(field_name)
        if not isinstance(value, list):
            raise ValueError(f"招商线索台账中的 {field_name} 必须是列表")
        snapshot[field_name] = _stable_json(value)
    return snapshot


def _assert_protected_records_unchanged(
    before: dict[str, str],
    ledger: dict[str, Any],
    *,
    include_metric_drafts: bool,
) -> None:
    """保存前验证原始证据链和已有草稿逐字未变，拒绝指标功能越权写入。"""

    if _protected_records_snapshot(ledger, include_metric_drafts=include_metric_drafts) != before:
        raise RuntimeError("经营指标功能不得修改线索、原始证据、评分、候选或已有指标草稿")


def _value_yuan(value_text: str, unit: str) -> int | None:
    """将原句中带单位的数值精确换算为元；单位不明、小数异常或非正数一律不推测。"""

    multiplier = _UNIT_MULTIPLIERS.get(unit)
    if multiplier is None:
        return None
    try:
        decimal_value = Decimal(value_text.replace(",", "")) * multiplier
    except (InvalidOperation, ValueError):
        return None
    if not decimal_value.is_finite() or decimal_value <= 0 or decimal_value != decimal_value.to_integral_value():
        return None
    return int(decimal_value)


def _metric_candidates_from_evidence(
    evidence: dict[str, Any],
    lead: dict[str, Any],
) -> list[dict[str, object]]:
    """从一条已签名证据的原始摘录提取可复核的指标候选，不使用网页发布日期猜测统计期。

    输入是已经验签的 ``evidence`` 及其所属 ``lead``，输出为带原句、显式年份和精确数值
    的候选列表。只有同一原句同时含 20xx 年、受限指标词、数值和单位时才输出；函数不联网、
    不写台账、不创建线索，也不把候选当成已确认经营事实。
    """

    evidence_id = evidence.get("evidence_id")
    excerpt = evidence.get("excerpt")
    lead_id = lead.get("lead_id")
    entity_name = lead.get("entity_name")
    sector = lead.get("sector")
    source_url = evidence.get("source_url")
    source_published_at = evidence.get("source_published_at")
    if not all(
        isinstance(value, str) and value
        for value in (evidence_id, excerpt, lead_id, entity_name, sector, source_url, source_published_at)
    ):
        return []

    candidates: list[dict[str, object]] = []
    for sentence_number, raw_sentence in enumerate(_SENTENCE_PATTERN.findall(excerpt)):
        sentence = raw_sentence.strip()
        year_match = _YEAR_PATTERN.search(sentence)
        if year_match is None:
            # 禁止以文章来源日期或采集日期代替统计期间，避免把“今年营收”写成虚假年份。
            continue
        period_year = int(year_match.group("year"))
        for rule in METRIC_RULES:
            pattern = re.compile(rule["pattern"])
            for occurrence, match in enumerate(pattern.finditer(sentence)):
                value_yuan = _value_yuan(match.group("value"), match.group("unit"))
                if value_yuan is None:
                    continue
                original_value_text = f"{match.group('value')}{match.group('unit')}"
                metric_draft_id = _stable_id(
                    "industry-metric-draft",
                    {
                        "evidence_id": evidence_id,
                        "metric_kind": rule["metric_kind"],
                        "sentence_number": sentence_number,
                        "occurrence": occurrence,
                        "match_start": match.start(),
                        "extractor_version": METRIC_EXTRACTOR_VERSION,
                    },
                )
                candidates.append(
                    {
                        "metric_draft_id": metric_draft_id,
                        "evidence_id": evidence_id,
                        "lead_id": lead_id,
                        "entity_name": entity_name,
                        "sector": sector,
                        "metric_kind": rule["metric_kind"],
                        "metric_label": rule["metric_label"],
                        "source_metric_label": match.group("label"),
                        "statistic_scope": rule["statistic_scope"],
                        "period_year": period_year,
                        "value_yuan": value_yuan,
                        "original_value_text": original_value_text,
                        "matched_excerpt": sentence,
                        "source_url": source_url,
                        "source_published_at": source_published_at,
                        "extractor_version": METRIC_EXTRACTOR_VERSION,
                    }
                )
    return candidates


def generate_industry_metric_drafts(workspace_dir: Path) -> dict[str, object]:
    """从所有已签名正式证据中追加经营指标待复核草稿。

    输入是当前智能体的服务器隔离工作区，输出为生成数量、跳过原因和台账路径。函数只读取
    经过 ``load_ledger`` 完整性校验的正式证据，且只追加此前不存在的草稿；没有任何客户端
    输入、网络请求或模型推理。保存前逐字检查原始证据链、线索与已有草稿保持不变，生成的
    草稿始终为 ``pending_user_review``，不会自动确认、统计、评分、预警或推进线索。
    """

    ledger = load_ledger(workspace_dir)
    protected_before = _protected_records_snapshot(ledger, include_metric_drafts=False)
    drafts = _metric_drafts(ledger)
    existing_draft_ids = {
        item.get("metric_draft_id")
        for item in drafts
        if isinstance(item, dict) and isinstance(item.get("metric_draft_id"), str)
    }
    leads_by_id = {
        item.get("lead_id"): item
        for item in ledger["leads"]
        if isinstance(item, dict) and isinstance(item.get("lead_id"), str)
    }
    generated_drafts: list[dict[str, object]] = []
    skipped_invalid_evidence_count = 0
    skipped_duplicate_count = 0
    skipped_capacity_count = 0

    for evidence in ledger["evidence"]:
        if not isinstance(evidence, dict):
            skipped_invalid_evidence_count += 1
            continue
        # 人工提交材料可以支持内部招商研判，但其正文不是服务器重新抓取并完成的
        # 文本分类结果。经营数值草稿会进入后续统计面板，必须只来自服务器可复现的
        # 页面证据，避免把人工摘录自动放大成经营事实。
        if evidence.get("evidence_origin") == "human_attested_material":
            continue
        lead_id = evidence.get("lead_id")
        lead = leads_by_id.get(lead_id) if isinstance(lead_id, str) else None
        if not isinstance(lead, dict):
            skipped_invalid_evidence_count += 1
            continue
        candidates = _metric_candidates_from_evidence(evidence, lead)
        if not candidates:
            continue
        for candidate in candidates:
            draft_id = candidate["metric_draft_id"]
            if not isinstance(draft_id, str):
                skipped_invalid_evidence_count += 1
                continue
            if draft_id in existing_draft_ids:
                skipped_duplicate_count += 1
                continue
            if len(drafts) >= MAX_METRIC_DRAFT_RECORDS or len(generated_drafts) >= MAX_METRIC_DRAFTS_PER_RUN:
                skipped_capacity_count += 1
                continue
            draft = {
                **candidate,
                "state": METRIC_DRAFT_STATE,
                "created_at": _utc_now(),
                "operation_scope": "automatic_signed_evidence_metric_draft_only",
            }
            drafts.append(draft)
            generated_drafts.append(draft)
            existing_draft_ids.add(draft_id)

    _assert_protected_records_unchanged(
        protected_before,
        ledger,
        include_metric_drafts=False,
    )
    if generated_drafts:
        saved_path = save_ledger(workspace_dir, ledger)
    else:
        saved_path = ledger_path(workspace_dir)
    return {
        "run": {
            "status": "completed",
            "generated_draft_count": len(generated_drafts),
            "skipped_invalid_evidence_count": skipped_invalid_evidence_count,
            "skipped_duplicate_count": skipped_duplicate_count,
            "skipped_capacity_count": skipped_capacity_count,
            "operation_scope": "automatic_signed_evidence_metric_draft_only",
        },
        "metric_drafts": generated_drafts,
        "ledger_path": str(saved_path),
    }


def _find_metric_draft(ledger: dict[str, Any], metric_draft_id: str) -> dict[str, Any]:
    """精确读取已保存草稿；请求不能携带草稿正文来绕过已签名证据来源。"""

    draft = next(
        (
            item
            for item in _metric_drafts(ledger)
            if isinstance(item, dict) and item.get("metric_draft_id") == metric_draft_id
        ),
        None,
    )
    if draft is None:
        raise ValueError("未找到对应的经营指标草稿")
    return draft


def _draft_review_state(ledger: dict[str, Any], metric_draft_id: str) -> str:
    """从追加审计派生当前草稿状态，不修改草稿原始提取内容和证据引用。"""

    reviews = [
        item
        for item in _metric_draft_reviews(ledger)
        if isinstance(item, dict) and item.get("metric_draft_id") == metric_draft_id
    ]
    if not reviews:
        return METRIC_DRAFT_STATE
    latest = reviews[-1].get("new_state")
    return latest if isinstance(latest, str) else METRIC_DRAFT_STATE


def review_industry_metric_draft(
    workspace_dir: Path,
    *,
    metric_draft_id: str,
    decision: str,
    review_note: str,
    authenticated_user_id: str,
) -> dict[str, object]:
    """由普通登录用户确认、待补证或作废一条经营指标草稿，并追加不可覆盖审计。

    输入只应来自认证路由：服务器草稿 ID、受控决定、复核备注以及认证中间件得出的用户 ID；
    输出为新增审计和（仅确认时）指标记录。普通用户不要求管理员身份，但客户端绝不能提交
    指标数值、年份、实体、来源 URL、摘录、证据 ID 或操作者 ID。函数只从已签名草稿读取
    这些事实，且保存前逐字确认 ``evidence``、线索和草稿本体不变；确认仅使派生指标可供
    报告并列展示，绝不自动修改招商状态、评分、预警或原始证据链。
    """

    normalized_draft_id = _required_metric_draft_id(metric_draft_id)
    normalized_decision = _required_text(decision, "decision", maximum=100).casefold()
    decision_aliases = {"确认": "confirm", "待补证": "need_evidence", "无效": "invalid"}
    normalized_decision = decision_aliases.get(normalized_decision, normalized_decision)
    if normalized_decision not in {"confirm", "need_evidence", "invalid"}:
        raise ValueError("decision只能是确认、待补证或无效")
    normalized_note = _required_text(review_note, "review_note", maximum=MAX_METRIC_REVIEW_NOTE_CHARS)
    normalized_user_id = _required_text(authenticated_user_id, "authenticated_user_id", maximum=100)

    ledger = load_ledger(workspace_dir)
    draft = _find_metric_draft(ledger, normalized_draft_id)
    if _draft_review_state(ledger, normalized_draft_id) != METRIC_DRAFT_STATE:
        raise ValueError("该经营指标草稿已经完成业务复核，不能重复改写历史判断")
    protected_before = _protected_records_snapshot(ledger, include_metric_drafts=True)
    reviewed_at = _utc_now()
    new_state = {
        "confirm": "confirmed",
        "need_evidence": "need_evidence",
        "invalid": "invalid",
    }[normalized_decision]
    review = {
        "metric_draft_review_id": _stable_id(
            "industry-metric-review",
            {
                "metric_draft_id": normalized_draft_id,
                "decision": normalized_decision,
                "reviewed_at": reviewed_at,
                "reviewed_by_user_id": normalized_user_id,
            },
        ),
        "metric_draft_id": normalized_draft_id,
        "reviewed_at": reviewed_at,
        "reviewed_by_user_id": normalized_user_id,
        "old_state": METRIC_DRAFT_STATE,
        "new_state": new_state,
        "review_note": normalized_note,
        "operation_scope": METRIC_REVIEW_OPERATION_SCOPE,
    }
    _metric_draft_reviews(ledger).append(review)
    confirmed_metric: dict[str, object] | None = None
    if normalized_decision == "confirm":
        metrics = _confirmed_metrics(ledger)
        if any(
            isinstance(item, dict) and item.get("metric_draft_id") == normalized_draft_id
            for item in metrics
        ):
            raise RuntimeError("该经营指标草稿已经存在已确认指标，拒绝重复写入")
        confirmed_metric = {
            "metric_id": _stable_id(
                "industry-business-metric",
                {"metric_draft_id": normalized_draft_id, "review_id": review["metric_draft_review_id"]},
            ),
            "metric_draft_id": normalized_draft_id,
            "evidence_id": draft.get("evidence_id"),
            "lead_id": draft.get("lead_id"),
            "entity_name": draft.get("entity_name"),
            "sector": draft.get("sector"),
            "metric_kind": draft.get("metric_kind"),
            "metric_label": draft.get("metric_label"),
            "source_metric_label": draft.get("source_metric_label"),
            "statistic_scope": draft.get("statistic_scope"),
            "period_year": draft.get("period_year"),
            "value_yuan": draft.get("value_yuan"),
            "original_value_text": draft.get("original_value_text"),
            "confirmed_at": reviewed_at,
            "confirmed_by_user_id": normalized_user_id,
            "operation_scope": METRIC_REVIEW_OPERATION_SCOPE,
        }
        metrics.append(confirmed_metric)
    _assert_protected_records_unchanged(
        protected_before,
        ledger,
        include_metric_drafts=True,
    )
    saved_path = save_ledger(workspace_dir, ledger)
    return {
        "metric_draft_review": review,
        "confirmed_metric": confirmed_metric,
        "ledger_path": str(saved_path),
    }


def _public_metric(metric: dict[str, Any]) -> dict[str, object] | None:
    """从已确认记录投影只读报表字段，证据链接只以 evidence_id 可追溯而不被复制改写。"""

    required = (
        "metric_id",
        "evidence_id",
        "lead_id",
        "entity_name",
        "sector",
        "metric_kind",
        "metric_label",
        "statistic_scope",
        "period_year",
        "value_yuan",
        "original_value_text",
        "confirmed_at",
    )
    if any(field not in metric for field in required):
        return None
    text_fields = (
        "metric_id",
        "evidence_id",
        "lead_id",
        "entity_name",
        "sector",
        "metric_kind",
        "metric_label",
        "statistic_scope",
        "original_value_text",
        "confirmed_at",
    )
    # 即使整本台账已验签，报表仍不把异常字段隐式 str() 化为“可信展示”；结构不完整的
    # 派生记录会被忽略，原始证据和审计不受该只读投影影响。
    if any(not isinstance(metric[field], str) or not metric[field] for field in text_fields):
        return None
    if not isinstance(metric["period_year"], int) or isinstance(metric["period_year"], bool):
        return None
    if (
        not isinstance(metric["value_yuan"], int)
        or isinstance(metric["value_yuan"], bool)
        or metric["value_yuan"] <= 0
    ):
        return None
    return {field: metric[field] for field in required}


def get_industry_business_metrics_report(
    workspace_dir: Path,
    *,
    window_years: int = 5,
) -> dict[str, object]:
    """生成营业收入、产值和项目投资额的只读并列统计报告。

    输入是当前工作区和 1–10 年统计窗口，输出为按企业、产业、指标种类和统计范围分组的
    时序记录及同口径环比。该函数只读取人工确认的记录，不联网、不写台账、不接收金额或
    口径参数；尤其不把不同企业、不同项目或 revenue/output_value/project_investment 三类
    指标相加，避免“营业收入 + 产值 + 投资额”的错误混合统计。
    """

    if not isinstance(window_years, int) or isinstance(window_years, bool) or not 1 <= window_years <= 10:
        raise ValueError("window_years必须是1到10之间的整数")
    ledger = load_ledger(workspace_dir)
    current_year = datetime.now(timezone.utc).year
    minimum_year = current_year - window_years + 1
    public_metrics = [
        metric
        for item in _confirmed_metrics(ledger)
        if isinstance(item, dict)
        and (metric := _public_metric(item)) is not None
        and int(metric["period_year"]) >= minimum_year
        and int(metric["period_year"]) <= current_year
    ]
    groups: dict[tuple[str, str, str, str, str], list[dict[str, object]]] = {}
    for metric in public_metrics:
        key = (
            str(metric["sector"]),
            str(metric["entity_name"]),
            str(metric["metric_kind"]),
            str(metric["metric_label"]),
            str(metric["statistic_scope"]),
        )
        groups.setdefault(key, []).append(metric)

    series: list[dict[str, object]] = []
    for key, records in sorted(groups.items()):
        ordered_records = sorted(records, key=lambda item: (int(item["period_year"]), str(item["metric_id"])))
        records_with_change: list[dict[str, object]] = []
        previous: dict[str, object] | None = None
        for record in ordered_records:
            output_record = dict(record)
            if previous is not None:
                previous_value = int(previous["value_yuan"])
                current_value = int(record["value_yuan"])
                output_record["same_metric_absolute_change_yuan"] = current_value - previous_value
                output_record["same_metric_change_percent"] = (
                    None
                    if previous_value <= 0
                    else str(
                        ((Decimal(current_value - previous_value) / Decimal(previous_value)) * Decimal("100")).quantize(
                            Decimal("0.01")
                        )
                    )
                )
            else:
                output_record["same_metric_absolute_change_yuan"] = None
                output_record["same_metric_change_percent"] = None
            records_with_change.append(output_record)
            previous = record
        series.append(
            {
                "sector": key[0],
                "entity_name": key[1],
                "metric_kind": key[2],
                "metric_label": key[3],
                "statistic_scope": key[4],
                "records": records_with_change,
            }
        )

    pending_draft_count = sum(
        1
        for item in _metric_drafts(ledger)
        if isinstance(item, dict)
        and isinstance(item.get("metric_draft_id"), str)
        and _draft_review_state(ledger, item["metric_draft_id"]) == METRIC_DRAFT_STATE
    )
    return {
        "window": {"start_year": minimum_year, "end_year": current_year},
        "mixed_metric_rule": {
            "mode": "parallel_by_metric_kind_and_scope",
            "forbidden_aggregation": ["revenue", "output_value", "project_investment"],
            "reason": "营业收入、产值与项目投资额统计口径不同，只能并列比较，不能相加。",
        },
        "series": series,
        "confirmed_metric_count": len(public_metrics),
        "pending_user_review_draft_count": pending_draft_count,
        "ledger_path": str(ledger_path(workspace_dir)),
    }
