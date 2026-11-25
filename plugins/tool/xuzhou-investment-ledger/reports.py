"""徐州招商线索的只读标准化研判报告与日常工作统计。

本模块只汇总现有台账中的线索、不可变证据、候选观察、评分快照和人工状态历史。它不
联网、不调用模型、不保存报告，也不把词表命中、同类证据数量或不同来源域名数量写成
企业事实；所有结论均明确为“已有记录的结构化展示”或“仍待补证”。
"""

from __future__ import annotations

import copy
import re
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .core import SUPPORTED_CLAIM_TYPE_TEXT_SUPPORTS, has_open_enterprise_risk_alert
from .relationship_graph import get_relationship_graph, get_relationship_review_queue
from .source_monitoring import get_source_coverage_report
from .storage import ledger_path, load_ledger


MAX_WORK_SUMMARY_WINDOW_DAYS = 90
# 报告保留完整 ``evidence_chain`` 供精确追溯；下方的章节卡片和时间线另设上限，避免
# 只读汇总因长期积累的证据无限放大响应。超出部分只标记截断，绝不删除或改写原始记录。
MAX_REPORT_EVIDENCE_CARDS_PER_SECTION = 50
MAX_REPORT_TIMELINE_RECORDS = 200
# 图谱本身可通过专用只读工具查看完整一跳子图；标准研判报告只保留有限关系卡片，避免
# 长期积累的图谱边使单条企业报告无界膨胀。截断只影响本次展示，绝不删除关系或证据。
MAX_REPORT_RELATIONSHIP_EDGE_CARDS = 100
# 细分方向报告会为每个固定细分词保留有限来源卡片；完整证据链仍由单条线索报告读取，
# 因此上限只限制单次统计展示，绝不删除或隐藏台账中的不可变证据记录。
MAX_SUBDIRECTION_EVIDENCE_CARDS = 20
SECTOR_LABELS = {
    "embodied_intelligence": "具身智能",
    "semiconductor": "半导体",
    "biomedicine": "生物医药",
    "other": "其他",
}
CLAIM_TYPE_LABELS = {
    "financing": "融资",
    "fund_raising": "基金募资",
    "xuzhou_connection": "徐州关联",
    "cooperation": "合作",
    "expansion": "扩张",
    "industry_focus": "产业方向",
    "fund_investment": "投资案例",
    "risk": "风险",
    "unclassified": "待人工分类",
}
REVIEW_STATE_LABELS = {
    "pending_user_review": "等待用户复核",
    "advance": "推进",
    "need_evidence": "待补证",
    "observe": "观察",
    "invalid": "无效",
}
# 标准研判报告只能展示已经由普通登录用户确认的派生经营指标。三类口径分别对应企业营收、
# 企业产值和项目投资，不能因为同属一条招商线索就合并计算“总产值”或其他混合总额。
CONFIRMED_METRIC_KIND_RULES = {
    "revenue": {"metric_label": "营业收入", "statistic_scope": "enterprise"},
    "output_value": {"metric_label": "产值", "statistic_scope": "enterprise"},
    "project_investment": {"metric_label": "项目投资额", "statistic_scope": "project"},
}
# 仅提取明确包含“元”或“人民币”的中文金额文本用于“人工核对口径”提示；它不识别美元、
# 估值、营收、项目总额或任何未明确币种的数值，更不会把不同金额自动判定为冲突。刻意不
# 接受“100万台”“30亿参数”等只带“万/亿”的表达，避免把数量或技术指标错误地标作金额。
# 正则的作用是让跨来源融资、募资、扩产与合作材料中的金额表达可被人工快速定位，原始
# 摘录仍是唯一事实依据。
_CNY_AMOUNT_TOKEN_PATTERN = re.compile(
    r"(?<![\d.])(?P<number>\d{1,8}(?:\.\d{1,4})?)\s*"
    r"(?P<unit>亿元人民币|亿人民币|亿元|万元人民币|万人民币|万元|人民币元|元)"
)
_CNY_AMOUNT_UNIT_TO_YUAN = {
    "亿元人民币": Decimal("100000000"),
    "亿人民币": Decimal("100000000"),
    "亿元": Decimal("100000000"),
    "万元人民币": Decimal("10000"),
    "万人民币": Decimal("10000"),
    "万元": Decimal("10000"),
    "人民币元": Decimal("1"),
    "元": Decimal("1"),
}


def _utc_now() -> str:
    """返回报告生成时的服务器 UTC 时间，不接受调用方指定生成时间。"""
    return datetime.now(timezone.utc).isoformat()


def _required_lead_id(value: object) -> str:
    """校验报告查询的服务器线索 ID，拒绝空白、URL 和无边界输入。

    输入是调用方提供的 lead_id，输出是长度受限的稳定台账键。该函数只验证查询参数，
    不读取网络、不写入报告或台账，也不会借 ID 推断企业身份、事实或业务优先级。
    """
    if not isinstance(value, str) or not (lead_id := value.strip()) or len(lead_id) > 120:
        raise ValueError("lead_id必须是有效的服务器线索 ID")
    if any(character.isspace() or character in "/\\?&#" for character in lead_id):
        raise ValueError("lead_id必须是有效的服务器线索 ID")
    return lead_id


def _validate_window_days(value: object) -> int:
    """限制统计窗口为 1–90 天，避免只读报表被用于无界历史扫描。"""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"window_days必须是1到{MAX_WORK_SUMMARY_WINDOW_DAYS}之间的整数")
    if not 1 <= value <= MAX_WORK_SUMMARY_WINDOW_DAYS:
        raise ValueError(f"window_days必须是1到{MAX_WORK_SUMMARY_WINDOW_DAYS}之间的整数")
    return value


def _ledger_list(ledger: dict[str, Any], field_name: str) -> list[dict[str, Any]]:
    """读取指定台账列表并在结构异常时显式失败，不猜测、修复或改写历史记录。"""
    value = ledger.get(field_name)
    if not isinstance(value, list):
        raise ValueError(f"招商线索台账中的 {field_name} 必须是列表")
    return [item for item in value if isinstance(item, dict)]


def _parse_utc_timestamp(value: object) -> datetime | None:
    """解析台账已有 UTC 时间；异常时间只是不计入时间窗口，绝不擅自修正记录。"""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _source_domain(source_url: object) -> str | None:
    """从已保存来源 URL 取得小写域名，供来源覆盖计数而非真实性判断使用。"""
    if not isinstance(source_url, str):
        return None
    hostname = urlparse(source_url).hostname
    return hostname.casefold().rstrip(".") if hostname else None


def _candidate_source_diversity(
    candidates: list[dict[str, Any]], *, window_started_at: datetime
) -> dict[str, object]:
    """Summarise discovery-domain concentration without deleting candidates.

    A dominant media domain is a workflow-quality warning, not a judgement on
    the source or any enterprise.  We require at least three in-window items
    before raising the 60% alert, so a single legitimate discovery does not
    create noise.  The result is read-only and deliberately keeps all counts.
    """
    in_window = [
        item
        for item in candidates
        if (_parse_utc_timestamp(item.get("discovered_at")) or datetime.min.replace(tzinfo=timezone.utc))
        >= window_started_at
    ]
    domains = [domain for domain in (_source_domain(item.get("source_url")) for item in in_window) if domain]
    counts = Counter(domains)
    total = len(domains)
    if not total:
        return {
            "window_candidate_count": 0,
            "unique_source_domain_count": 0,
            "dominant_domain": None,
            "dominant_share_percent": 0,
            "status": "no_recent_candidates",
            "warning": None,
            "domain_counts": {},
        }
    dominant_domain, dominant_count = counts.most_common(1)[0]
    share = round(dominant_count * 100 / total, 1)
    concentrated = total >= 3 and dominant_count / total > 0.60
    return {
        "window_candidate_count": total,
        "unique_source_domain_count": len(counts),
        "dominant_domain": dominant_domain,
        "dominant_share_percent": share,
        "status": "warning" if concentrated else "balanced_or_small_sample",
        "warning": (
            f"近窗口 {share}% 的待核验候选来自 {dominant_domain}；系统会保留全部候选，"
            "但后续调度应优先补充独立官方或其他媒体来源。"
            if concentrated
            else None
        ),
        "domain_counts": dict(sorted(counts.items())),
    }


def _evidence_for_lead(evidence_records: list[dict[str, Any]], lead_id: str) -> list[dict[str, Any]]:
    """按不可变 lead_id 关联证据，返回深拷贝以隔离报告调用方对内存对象的修改。"""
    return [
        copy.deepcopy(evidence)
        for evidence in evidence_records
        if evidence.get("lead_id") == lead_id
    ]


def _has_server_supported_claim_type(evidence: dict[str, Any]) -> bool:
    """判断证据类别是否有服务器原文词支持，限制报告章节和交叉核对的自动归类。

    输入是一条只读证据，输出为是否可用其 ``claim_type`` 填充融资、徐州关联、合作、行业
    或风险章节。未分类、旧版未核验和未知状态仍会保留在完整 evidence_chain 中供人工查看，
    但不会让报告自动声称该维度“已有证据”。函数不写入台账、不改变原始摘录或人工结论。
    """
    return evidence.get("claim_type_text_support") in SUPPORTED_CLAIM_TYPE_TEXT_SUPPORTS


def _report_evidence_card(evidence: dict[str, Any]) -> dict[str, object]:
    """把一条不可变证据转换为报告用的有限来源卡片，不复制快照路径或完整原始字节。

    输入是已经按线索筛选的正式证据，输出为证据 ID、来源、公开日期、采集时间、服务器
    分类及其原文支持状态、来源等级、质量、关联标签与已有摘录。卡片只重述台账已保存
    字段，不能从标题或摘录推断新事实；不返回本地快照路径、Base64 原文或签名细节。函数
    纯内存转换，不联网、不写入台账、不改写 evidence、线索、评分、红色预警或人工复核。
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
        "xuzhou_association_tags",
        "created_at",
    )
    if any(field_name not in evidence for field_name in fields):
        raise ValueError("证据记录缺少标准报告来源卡片字段")
    evidence_id = evidence["evidence_id"]
    claim_type = evidence["claim_type"]
    source_url = evidence["source_url"]
    if not isinstance(evidence_id, str) or not isinstance(claim_type, str) or not isinstance(source_url, str):
        raise ValueError("证据记录的标准报告来源卡片字段格式异常")
    return {
        "evidence_id": evidence_id,
        "claim_type": claim_type,
        "claim_type_label": CLAIM_TYPE_LABELS.get(claim_type, claim_type),
        # 缺少字段的历史证据不能被静默视为通过新版文本分类；状态仅提示阅读者如何审查，
        # 不修改 HMAC 已保护的旧记录，也不据此否定来源本身。
        "claim_type_requested": copy.deepcopy(evidence.get("claim_type_requested")),
        "claim_type_matched_terms": copy.deepcopy(evidence.get("claim_type_matched_terms", [])),
        "claim_type_text_support": copy.deepcopy(
            evidence.get("claim_type_text_support", "legacy_unverified")
        ),
        "claim_type_classification_method": copy.deepcopy(
            evidence.get("claim_type_classification_method")
        ),
        "source_url": source_url,
        "source_domain": _source_domain(source_url),
        "source_title": copy.deepcopy(evidence["source_title"]),
        "source_published_at": copy.deepcopy(evidence["source_published_at"]),
        "collected_at": copy.deepcopy(evidence["created_at"]),
        "source_level": copy.deepcopy(evidence["source_level"]),
        "evidence_quality": copy.deepcopy(evidence["evidence_quality"]),
        "xuzhou_association_tags": copy.deepcopy(evidence["xuzhou_association_tags"]),
        "excerpt": copy.deepcopy(evidence["excerpt"]),
    }


def _bounded_report_evidence_cards(
    evidence_records: list[dict[str, Any]],
    *,
    limit: int,
) -> dict[str, object]:
    """按来源公开日期倒序生成有上限的证据卡片，用于章节和交叉核验人工查看。

    输入是同一报告范围内的已存证据和固定上限，输出包含展示卡片、完整记录数和截断标记。
    排序只使用保存的发布日期、采集时间与证据 ID，不臆测事件顺序；截断不影响完整
    ``evidence_chain``。函数不读文件或网页、不修改证据、候选快照、线索或人工状态。
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("标准报告证据卡片上限必须是正整数")
    cards = [_report_evidence_card(evidence) for evidence in evidence_records]
    cards.sort(
        key=lambda card: (
            str(card["source_published_at"]),
            str(card["collected_at"]),
            str(card["evidence_id"]),
        ),
        reverse=True,
    )
    return {
        "records": cards[:limit],
        "recorded_evidence_count": len(cards),
        "truncated": len(cards) > limit,
    }


def _section_cross_source_validation(evidence_records: list[dict[str, Any]]) -> dict[str, object]:
    """汇总一个报告章节的来源覆盖，明确区分记录数量和人工事实对齐。

    输入是该章节命中的正式证据，输出为来源域名、来源等级、质量分布和待人工对齐提示。
    两个不同域名只能说明已记录多域名来源，不能证明它们描述同一融资、团队、技术或合作
    事件；无论状态为何函数都不自动验证、合并或改写证据。函数只读内存，不联网、不评分、
    不改变线索或人工业务决定。
    """
    domains = sorted(
        {
            domain
            for domain in (_source_domain(record.get("source_url")) for record in evidence_records)
            if domain is not None
        }
    )
    source_levels = sorted(
        {
            source_level
            for source_level in (record.get("source_level") for record in evidence_records)
            if isinstance(source_level, str)
        }
    )
    quality_scores = sorted(
        {
            quality
            for quality in (record.get("evidence_quality") for record in evidence_records)
            if isinstance(quality, int) and not isinstance(quality, bool)
        }
    )
    if not evidence_records:
        status = "no_traceable_evidence"
    elif len(domains) >= 2:
        status = "multiple_source_domains_pending_event_alignment"
    else:
        status = "single_or_unidentified_source_domain"
    requires_manual_event_alignment = len(domains) >= 2
    return {
        "status": status,
        "unique_source_domains": domains,
        "unique_source_domain_count": len(domains),
        "source_levels": source_levels,
        "evidence_quality_values": quality_scores,
        "requires_manual_event_alignment": requires_manual_event_alignment,
        "manual_event_alignment_fields": (
            ["source_published_at", "source_title", "excerpt", "source_url"]
            if requires_manual_event_alignment
            else []
        ),
        "caveat": (
            "当前章节已有多个不同域名记录；请人工核对日期、标题、原始摘录和链接是否指向同一具体事件。"
            if requires_manual_event_alignment
            else "来源覆盖仅反映当前台账记录，不自动构成交叉验证或业务事实判断。"
        ),
    }


def _evidence_timeline(evidence_records: list[dict[str, Any]]) -> dict[str, object]:
    """生成整条线索的证据时间线，展示已保存日期而不重建或推断企业历程。

    输入是同一线索的正式证据，输出为按来源发布日期和采集时间倒序的有限卡片列表。时间线
    用于人工查看融资、团队、技术等信息何时出现；它不合并同类事件、不从空白字段推测
    时间，也不等同企业真实发展史。函数不联网、不保存报告、不改写原始证据或业务状态。
    """
    timeline = _bounded_report_evidence_cards(
        evidence_records,
        limit=MAX_REPORT_TIMELINE_RECORDS,
    )
    return {
        "timeline_order": "source_published_at_then_collected_at_desc",
        "records": timeline["records"],
        "recorded_evidence_count": timeline["recorded_evidence_count"],
        "truncated": timeline["truncated"],
        "caveat": "时间线只按已保存日期排列；同类记录是否属于同一事件、是否完整，仍须人工核验原始证据。",
    }


def _section_from_evidence(
    *,
    section_key: str,
    title: str,
    instruction: str,
    evidence_records: list[dict[str, Any]],
    claim_types: set[str] | None = None,
    association_tags: set[str] | None = None,
    required_text_terms: tuple[str, ...] | None = None,
) -> dict[str, object]:
    """把服务器原文支持的分类证据整理为标准报告章节。

    输入是章节规则和同一线索的不可变证据副本，输出仅引用对应 evidence_id 与待补证
    提示。章节有记录时只说明“台账存在这些证据”；没有记录时明确表示“当前台账未收集
    到该维度证据”，而非断言企业没有融资、团队、技术或合作。函数不读取页面、不作
    语义推理、不写入 evidence、评分或人工状态。
    """
    matched_records: list[dict[str, Any]] = []
    for evidence in evidence_records:
        claim_matched = (
            _has_server_supported_claim_type(evidence)
            and claim_types is not None
            and evidence.get("claim_type") in claim_types
        )
        searchable_text = f"{evidence.get('source_title', '')}\n{evidence.get('excerpt', '')}".casefold()
        text_matched = required_text_terms is None or any(
            term.casefold() in searchable_text for term in required_text_terms
        )
        # 关联标签保留为人工阅读提示，不能单独把某篇材料归入报告章节。若它确有原文
        # 支持，应由服务器分类为 ``xuzhou_connection`` 后通过上方规则进入徐州关联章节。
        if claim_matched and text_matched:
            matched_records.append(evidence)
    evidence_ids = [record.get("evidence_id") for record in matched_records]
    if not all(isinstance(evidence_id, str) for evidence_id in evidence_ids):
        raise ValueError("证据记录缺少 evidence_id")
    evidence_cards = _bounded_report_evidence_cards(
        matched_records,
        limit=MAX_REPORT_EVIDENCE_CARDS_PER_SECTION,
    )
    return {
        "section_key": section_key,
        "title": title,
        "instruction": instruction,
        "status": "evidence_available" if matched_records else "needs_evidence",
        "evidence_ids": evidence_ids,
        "recorded_evidence_count": len(matched_records),
        "evidence_cards": evidence_cards["records"],
        "evidence_cards_truncated": evidence_cards["truncated"],
        "cross_source_validation": _section_cross_source_validation(matched_records),
        "caveat": (
            "以下仅为已存证据引用，仍须人工核对原文是否支持具体业务判断。"
            if matched_records
            else "当前台账尚无此维度的可追溯证据；这不代表相关事实不存在。"
        ),
    }


def _cross_source_coverage(evidence_records: list[dict[str, Any]]) -> list[dict[str, object]]:
    """按事实类别显示不同来源域名的覆盖程度，不将数量自动解释为事实交叉验证。

    输入是同一线索的已存证据，输出各 claim_type 的证据 ID、来源域名与来源等级分布。
    两个以上不同域名只标记为“多域名覆盖”，提示人工核对是否描述同一具体事件；它并不
    等同于机器确认事实。函数只读内存数据，不联网、不评估来源真假、不写入任何台账。
    """
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for evidence in evidence_records:
        claim_type = evidence.get("claim_type")
        if isinstance(claim_type, str) and _has_server_supported_claim_type(evidence):
            grouped[claim_type].append(evidence)
    checks: list[dict[str, object]] = []
    for claim_type in sorted(grouped):
        records = grouped[claim_type]
        domains = sorted(
            {
                domain
                for domain in (_source_domain(record.get("source_url")) for record in records)
                if domain is not None
            }
        )
        source_levels = sorted(
            {
                level
                for level in (record.get("source_level") for record in records)
                if isinstance(level, str)
            }
        )
        evidence_ids = [record.get("evidence_id") for record in records]
        if not all(isinstance(evidence_id, str) for evidence_id in evidence_ids):
            raise ValueError("证据记录缺少 evidence_id")
        has_multiple_domains = len(domains) >= 2
        alignment_records = _bounded_report_evidence_cards(
            records,
            limit=MAX_REPORT_EVIDENCE_CARDS_PER_SECTION,
        )
        checks.append(
            {
                "claim_type": claim_type,
                "claim_type_label": CLAIM_TYPE_LABELS.get(claim_type, claim_type),
                "evidence_ids": evidence_ids,
                "evidence_count": len(records),
                "unique_source_domains": domains,
                "unique_source_domain_count": len(domains),
                "source_levels": source_levels,
                "evidence_alignment_records": alignment_records["records"],
                "evidence_alignment_records_truncated": alignment_records["truncated"],
                "requires_manual_event_alignment": has_multiple_domains,
                "coverage_status": (
                    "multiple_independent_source_domains"
                    if has_multiple_domains
                    else "single_or_no_source_domain"
                ),
                "caveat": (
                    "同一事实类别已有多个不同域名记录；仍须人工核验是否指向同一具体事件及原文是否一致。"
                    if has_multiple_domains
                    else "当前只有一个或无法识别的来源域名，尚不足以形成跨来源覆盖提示。"
                ),
            }
        )
    return checks


def _cny_amount_tokens_for_cross_validation(
    evidence_records: list[dict[str, Any]],
) -> list[dict[str, object]]:
    """从已保存摘录中提取可人工回看的人民币金额文本，不把金额视为确认事实。

    输入是同一事实类别的不可变证据，输出为按“换算元”归并的金额文本及证据 ID。函数
    只处理明确标有元或人民币的表达，解析失败、其他币种、估值、比例和未标单位数字均
    忽略；相同或不同数值都只能提示人工核对是否属于同一轮、同一项目与同一统计口径，
    不能自动认定融资金额、冲突、真实性或招商意愿。函数不联网、不写入台账或改写摘录。
    """
    grouped: dict[str, dict[str, object]] = {}
    for evidence in evidence_records:
        evidence_id = evidence.get("evidence_id")
        excerpt = evidence.get("excerpt")
        if not isinstance(evidence_id, str) or not isinstance(excerpt, str):
            continue
        for match in _CNY_AMOUNT_TOKEN_PATTERN.finditer(excerpt):
            try:
                amount_yuan = Decimal(match.group("number")) * _CNY_AMOUNT_UNIT_TO_YUAN[
                    match.group("unit")
                ]
            except (InvalidOperation, KeyError):
                continue
            normalized_yuan = format(amount_yuan.normalize(), "f")
            entry = grouped.setdefault(
                normalized_yuan,
                {
                    "normalized_cny_yuan": normalized_yuan,
                    "observed_texts": [],
                    "evidence_ids": [],
                },
            )
            observed_text = match.group(0).strip()
            if observed_text not in entry["observed_texts"]:
                entry["observed_texts"].append(observed_text)
            if evidence_id not in entry["evidence_ids"]:
                entry["evidence_ids"].append(evidence_id)
    return [grouped[key] for key in sorted(grouped, key=lambda value: Decimal(value))]


def _cross_source_consistency_review(evidence_records: list[dict[str, Any]]) -> dict[str, object]:
    """生成按事实类别的确定性交叉核对清单，不把多来源或数字差异写成事实结论。

    输入是同一招商线索的正式证据，输出列出每类事实的来源域名、发布日期、金额文本、
    证据 ID 与人工核对原因。不同域名只能表示独立覆盖，金额文本不同也可能对应不同轮次、
    项目、税前税后或统计口径，因此结果仅标记“待人工对齐”，绝不判定“来源矛盾”或改变
    红色预警、分数、线索状态。函数不访问网页、不创建任务、不保存报告，原始证据链完整
    保持不变。
    """
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for evidence in evidence_records:
        claim_type = evidence.get("claim_type")
        if isinstance(claim_type, str) and _has_server_supported_claim_type(evidence):
            grouped[claim_type].append(evidence)
    checks: list[dict[str, object]] = []
    for claim_type in sorted(grouped):
        records = grouped[claim_type]
        evidence_ids = [record.get("evidence_id") for record in records]
        if not all(isinstance(evidence_id, str) for evidence_id in evidence_ids):
            raise ValueError("证据记录缺少 evidence_id")
        domains = sorted(
            {
                domain
                for domain in (_source_domain(record.get("source_url")) for record in records)
                if domain is not None
            }
        )
        publication_dates = sorted(
            {
                published_at
                for published_at in (record.get("source_published_at") for record in records)
                if isinstance(published_at, str) and published_at
            }
        )
        amount_tokens = _cny_amount_tokens_for_cross_validation(records)
        multiple_domains = len(domains) >= 2
        multiple_amounts = len(amount_tokens) >= 2
        if multiple_domains and multiple_amounts:
            status = "multiple_domains_with_distinct_numeric_tokens_needs_manual_alignment"
            review_reason = (
                "同一事实类别存在多个来源域名和不同人民币金额文本；请人工核对是否属于同一轮次、"
                "同一项目及同一统计口径，不能自动视为矛盾。"
            )
        elif multiple_domains:
            status = "multiple_domains_pending_event_alignment"
            review_reason = (
                "同一事实类别已有多个来源域名；请人工核对发布日期、标题、原始摘录和链接是否"
                "指向同一具体事件。"
            )
        elif multiple_amounts:
            status = "single_domain_with_distinct_numeric_tokens_needs_manual_alignment"
            review_reason = (
                "同一来源域名下记录到不同人民币金额文本；请人工核对是否属于不同轮次、不同项目"
                "或不同统计口径，不能自动视为矛盾。"
            )
        else:
            status = "single_or_no_domain_needs_more_independent_coverage"
            review_reason = "当前独立来源覆盖不足；不能据此完成交叉验证或作出招商事实结论。"
        checks.append(
            {
                "claim_type": claim_type,
                "claim_type_label": CLAIM_TYPE_LABELS.get(claim_type, claim_type),
                "evidence_ids": evidence_ids,
                "unique_source_domains": domains,
                "unique_source_domain_count": len(domains),
                "source_published_dates": publication_dates,
                "observed_cny_amount_tokens": amount_tokens,
                "requires_manual_event_alignment": multiple_domains or multiple_amounts,
                "status": status,
                "review_reason": review_reason,
            }
        )
    manual_review_claim_types = [
        str(check["claim_type"])
        for check in checks
        if check["requires_manual_event_alignment"] is True
    ]
    return {
        "review_type": "deterministic_existing_evidence_cross_source_alignment_only",
        "claim_type_checks": checks,
        "manual_review_claim_types": manual_review_claim_types,
        "manual_review_claim_type_count": len(manual_review_claim_types),
        "caveat": (
            "本清单只帮助人工定位多来源或金额口径待核对记录；不同来源、日期或金额均不自动"
            "等于同一事件、确认事实或来源矛盾。"
        ),
    }


def _unverified_observations_for_lead(
    evidence_records: list[dict[str, Any]],
    observations: list[dict[str, Any]],
) -> list[dict[str, object]]:
    """关联自动抓取候选的待补证观察，但绝不将词表命中混入正式证据链。

    输入是已存证据和候选观察记录；只有证据的服务器 discovery_candidate_id 与观察的
    candidate_id 一致时才关联。输出为不含快照本地路径或原始字节的观察元数据，并固定
    标注 pending_evidence_verification，提醒人工先查看原文。函数不读取快照文件、不
    更新候选或证据，也不把观察信号用于自动评分、预警或招商推进。
    """
    candidate_ids = {
        candidate_id
        for candidate_id in (record.get("discovery_candidate_id") for record in evidence_records)
        if isinstance(candidate_id, str)
    }
    result: list[dict[str, object]] = []
    for observation in observations:
        if observation.get("candidate_id") not in candidate_ids:
            continue
        fields = (
            "observation_id",
            "candidate_id",
            "source_url",
            "source_title",
            "collected_at",
            "raw_sha256",
            "byte_size",
            "signals",
            "review_priority",
            "state",
        )
        if any(field_name not in observation for field_name in fields):
            raise ValueError("候选观察记录缺少报告所需字段")
        result.append({field_name: copy.deepcopy(observation[field_name]) for field_name in fields})
    result.sort(key=lambda observation: str(observation["collected_at"]), reverse=True)
    return result


def _latest_score_snapshots(
    score_snapshots: list[dict[str, Any]], lead_id: str
) -> list[dict[str, object]]:
    """返回同一线索按时间倒序的历史评分快照，不重新计算或覆盖人工规则。"""
    snapshots = [
        copy.deepcopy(snapshot)
        for snapshot in score_snapshots
        if snapshot.get("lead_id") == lead_id
    ]
    snapshots.sort(key=lambda snapshot: str(snapshot.get("scored_at", "")), reverse=True)
    return snapshots


def _report_completeness(standardized_sections: list[dict[str, object]]) -> dict[str, object]:
    """汇总标准报告各章节的已有记录、待补证项与人工来源对齐工作。

    输入是本次报告已经按固定规则构建的章节，输出为可审查的完整性矩阵和人工后续动作。
    “完整性”只表示当前台账是否记录了对应维度的可追溯证据，不能被解释为企业资料完整、
    企业可信或招商可推进。函数不读取网络、不创建任务、不修改报告以外的数据，更不会
    自动填补团队、融资金额、技术细节或交叉验证结论。
    """
    matrix: list[dict[str, object]] = []
    needs_evidence_sections: list[str] = []
    manual_alignment_sections: list[str] = []
    recommended_human_followups: list[dict[str, object]] = []
    for section in standardized_sections:
        section_key = section.get("section_key")
        status = section.get("status")
        title = section.get("title")
        instruction = section.get("instruction")
        validation = section.get("cross_source_validation")
        if not all(isinstance(value, str) for value in (section_key, status, title, instruction)):
            raise ValueError("标准报告章节缺少完整性矩阵字段")
        if not isinstance(validation, dict):
            raise ValueError("标准报告章节缺少来源核验字段")
        requires_alignment = validation.get("requires_manual_event_alignment") is True
        matrix.append(
            {
                "section_key": section_key,
                "title": title,
                "evidence_status": status,
                "recorded_evidence_count": section.get("recorded_evidence_count"),
                "cross_source_validation_status": validation.get("status"),
                "requires_manual_event_alignment": requires_alignment,
            }
        )
        if status == "needs_evidence":
            needs_evidence_sections.append(section_key)
            recommended_human_followups.append(
                {
                    "section_key": section_key,
                    "action": "collect_traceable_evidence",
                    "instruction": instruction,
                }
            )
        elif requires_alignment:
            manual_alignment_sections.append(section_key)
            recommended_human_followups.append(
                {
                    "section_key": section_key,
                    "action": "align_existing_source_records",
                    "instruction": "人工核对多个来源是否指向同一具体事件，不得因来源数量自动确认事实。",
                }
            )
    return {
        "section_count": len(matrix),
        "evidence_available_section_count": sum(
            1 for item in matrix if item["evidence_status"] == "evidence_available"
        ),
        "needs_evidence_section_count": len(needs_evidence_sections),
        "needs_evidence_sections": needs_evidence_sections,
        "manual_event_alignment_section_count": len(manual_alignment_sections),
        "manual_event_alignment_sections": manual_alignment_sections,
        "matrix": matrix,
        "recommended_human_followups": recommended_human_followups,
        "caveat": "本矩阵只显示当前台账证据覆盖与人工核验待办，不生成企业研判结论、招商意愿判断或自动推进动作。",
    }


def _industry_chain_relationship_assessment(
    workspace_dir: Path,
    *,
    lead_id: str,
    evidence_chain: list[dict[str, Any]],
) -> dict[str, object]:
    """把当前线索的一跳证据化图谱整理为报告章节，不生成或修改任何关系事实。

    输入为工作区、服务器已验证的线索 ID 与该线索不可变正式证据；输出为有限的关系卡片、
    已登记上下游展示方向、关系复核状态、待补证清单及证据 ID 对齐结果。函数只调用图谱
    的只读查询，方向和覆盖度完全沿用已人工登记的受控关系类型与既有证据引用；它不接受
    客户端操作者身份、不联网、不新建关系、不自动确认有效性，也不会改写 evidence、URL、
    原始摘录、采集时间、评分、预警或任何人工复核记录。
    """
    graph = get_relationship_graph(workspace_dir, lead_id=lead_id)
    if graph.get("read_only") is not True:
        raise RuntimeError("关系图谱查询未处于只读模式，拒绝生成标准研判报告")
    analysis = graph.get("industry_chain_analysis")
    if not isinstance(analysis, dict):
        raise ValueError("关系图谱缺少产业链研判结果")
    # 新版图谱把产业链边与其他证据化关系分开投影；回退字段仅服务旧版历史读取，不能
    # 把法人、股东或高管角色误当作上下游关系写进“产业链”报告章节。
    raw_edge_analysis = analysis.get("industry_chain_edge_analysis", analysis.get("edge_analysis"))
    raw_summary = analysis.get("industry_chain_summary", analysis.get("summary"))
    raw_review_summary = graph.get("review_state_summary")
    if not isinstance(raw_edge_analysis, list) or not isinstance(raw_summary, dict):
        raise ValueError("关系图谱产业链研判结构异常")
    if not isinstance(raw_review_summary, dict):
        raise ValueError("关系图谱缺少复核状态汇总")

    edge_cards: list[dict[str, object]] = []
    edge_evidence_ids: set[str] = set()
    pending_evidence_edges: list[dict[str, object]] = []
    for raw_edge in raw_edge_analysis:
        if not isinstance(raw_edge, dict):
            raise ValueError("关系图谱产业链边研判必须是对象")
        evidence_support = raw_edge.get("evidence_support")
        edge_id = raw_edge.get("edge_id")
        relationship_type = raw_edge.get("relationship_type")
        review_state = raw_edge.get("review_state")
        pending_evidence = raw_edge.get("pending_evidence")
        if not isinstance(evidence_support, dict) or not all(
            isinstance(value, str) for value in (edge_id, relationship_type, review_state)
        ) or not isinstance(pending_evidence, bool):
            raise ValueError("关系图谱产业链边研判缺少标准报告字段")
        original_ids = evidence_support.get("original_edge_evidence_ids")
        supplement_ids = evidence_support.get("supplement_evidence_ids")
        if not all(
            isinstance(ids, list) and all(isinstance(item, str) and item for item in ids)
            for ids in (original_ids, supplement_ids)
        ):
            raise ValueError("关系图谱产业链边缺少可追溯证据 ID")
        edge_evidence_ids.update(original_ids)
        edge_evidence_ids.update(supplement_ids)
        card = {
            "edge_id": edge_id,
            "relationship_type": relationship_type,
            "relationship_category": copy.deepcopy(raw_edge.get("relationship_category")),
            "business_flow_direction": copy.deepcopy(raw_edge.get("business_flow_direction")),
            "from_node": copy.deepcopy(raw_edge.get("from_node")),
            "to_node": copy.deepcopy(raw_edge.get("to_node")),
            "review_state": review_state,
            "pending_evidence": pending_evidence,
            "pending_evidence_reason": copy.deepcopy(raw_edge.get("pending_evidence_reason")),
            "evidence_support": copy.deepcopy(evidence_support),
        }
        edge_cards.append(card)
        if pending_evidence:
            pending_evidence_edges.append(
                {
                    "edge_id": edge_id,
                    "relationship_type": relationship_type,
                    "review_state": review_state,
                    "reason": copy.deepcopy(raw_edge.get("pending_evidence_reason")),
                }
            )

    current_lead_evidence_ids = {
        evidence_id
        for evidence_id in (record.get("evidence_id") for record in evidence_chain)
        if isinstance(evidence_id, str)
    }
    sorted_edge_evidence_ids = sorted(edge_evidence_ids)
    non_current_lead_evidence_ids = sorted(edge_evidence_ids - current_lead_evidence_ids)
    shown_edge_cards = edge_cards[:MAX_REPORT_RELATIONSHIP_EDGE_CARDS]
    shown_pending_edges = pending_evidence_edges[:MAX_REPORT_RELATIONSHIP_EDGE_CARDS]
    has_relationships = bool(edge_cards)
    return {
        "section_key": "industry_chain_relationship_graph",
        "title": "产业链与本地关系图谱",
        "status": "evidence_available" if has_relationships else "needs_evidence",
        "instruction": "逐条核验关系边引用的原始来源、上下游登记方向、徐州本地属性和人工复核状态。",
        "relationship_graph_scope": graph.get("operation_scope"),
        "relationship_graph_summary": copy.deepcopy(raw_summary),
        "relationship_review_state_summary": copy.deepcopy(raw_review_summary),
        "recorded_edge_count": len(edge_cards),
        "edge_cards": shown_edge_cards,
        "edge_cards_truncated": len(edge_cards) > MAX_REPORT_RELATIONSHIP_EDGE_CARDS,
        "pending_evidence_edge_count": len(pending_evidence_edges),
        "pending_evidence_edges": shown_pending_edges,
        "pending_evidence_edges_truncated": (
            len(pending_evidence_edges) > MAX_REPORT_RELATIONSHIP_EDGE_CARDS
        ),
        "edge_evidence_reference_ids": sorted_edge_evidence_ids,
        "not_in_current_lead_evidence_chain_ids": non_current_lead_evidence_ids,
        "manual_review_checklist": (
            [
                "逐条打开关系边引用的原始来源，确认两端名称、关系关键词与登记类型一致。",
                "仅对已登记为上游供应或下游客户的边采用方向展示；“产业链”边未登记方向时不得自行补写。",
                "等待用户复核或待补证的边不得作为已确认合作、供应链或落地事实。",
            ]
            if has_relationships
            else [
                "当前台账尚未登记该线索的一跳证据化关系；应先基于可追溯原始来源人工登记，不得由报告推断。"
            ]
        ),
        "caveat": (
            "关系卡片只展示已登记且有证据引用的图谱记录；证据覆盖度不等同于真实交易强度、合作深度或招商意愿。"
            if has_relationships
            else "当前未登记关系不代表企业与徐州不存在供应链、合作或其他关联，只表示台账尚无可追溯图谱记录。"
        ),
    }


def _public_key_person_relationship_assessment(
    workspace_dir: Path,
    *,
    lead_id: str,
    evidence_chain: list[dict[str, Any]],
) -> dict[str, object]:
    """为标准研判报告只读投影公开关键人员—组织角色，不建立个人招商结论。

    输入为当前工作区、既有招商线索 ID 和该线索不可变证据链；输出为法定代表人、创始
    人、实控人、股东、董事和高管等已登记公开角色的有限卡片、复核状态及证据 ID。函数
    只调用关系图谱的只读查询，不收集或展示电话、住址、资产、个人募资能力或个人投资
    意愿；“徐州关联”仅是可回溯的宽泛证据标签，不能替代对籍贯、乡土情结或招商窗口的
    人工核验。它不联网、不写报告、不接受客户端身份，更不会改写 evidence、来源 URL、
    原始摘录、采集时间、评分、线索状态、关系边或审计记录。
    """
    graph = get_relationship_graph(workspace_dir, lead_id=lead_id)
    if graph.get("read_only") is not True:
        raise RuntimeError("关系图谱查询未处于只读模式，拒绝生成关键人员报告章节")
    analysis = graph.get("key_person_relationship_analysis")
    if not isinstance(analysis, dict):
        raise ValueError("关系图谱缺少公开关键人员研判结果")
    raw_role_cards = analysis.get("role_cards")
    raw_summary = analysis.get("summary")
    if not isinstance(raw_role_cards, list) or not isinstance(raw_summary, dict):
        raise ValueError("公开关键人员关系研判结构异常")

    role_cards: list[dict[str, object]] = []
    evidence_ids: set[str] = set()
    pending_review_edges: list[dict[str, object]] = []
    for raw_card in raw_role_cards:
        if not isinstance(raw_card, dict):
            raise ValueError("公开关键人员关系卡片必须是对象")
        edge_id = raw_card.get("edge_id")
        relationship_type = raw_card.get("relationship_type")
        person = raw_card.get("person")
        organization = raw_card.get("organization")
        evidence_support = raw_card.get("evidence_support")
        review_state = raw_card.get("review_state")
        pending_evidence = raw_card.get("pending_evidence")
        if (
            not all(isinstance(value, str) and value for value in (edge_id, relationship_type, review_state))
            or not isinstance(person, dict)
            or person.get("node_kind") != "person"
            or not isinstance(organization, dict)
            or organization.get("node_kind") == "person"
            or not isinstance(evidence_support, dict)
            or not isinstance(pending_evidence, bool)
        ):
            raise ValueError("公开关键人员关系卡片缺少受控字段")
        original_ids = evidence_support.get("original_edge_evidence_ids")
        supplement_ids = evidence_support.get("supplement_evidence_ids")
        if not all(
            isinstance(ids, list) and all(isinstance(item, str) and item for item in ids)
            for ids in (original_ids, supplement_ids)
        ):
            raise ValueError("公开关键人员关系缺少可追溯证据 ID")
        evidence_ids.update(original_ids)
        evidence_ids.update(supplement_ids)
        role_cards.append(copy.deepcopy(raw_card))
        if pending_evidence:
            pending_review_edges.append(
                {
                    "edge_id": edge_id,
                    "relationship_type": relationship_type,
                    "review_state": review_state,
                    "reason": copy.deepcopy(raw_card.get("pending_evidence_reason")),
                }
            )

    current_lead_evidence_ids = {
        evidence_id
        for evidence_id in (record.get("evidence_id") for record in evidence_chain)
        if isinstance(evidence_id, str)
    }
    sorted_evidence_ids = sorted(evidence_ids)
    shown_role_cards = role_cards[:MAX_REPORT_RELATIONSHIP_EDGE_CARDS]
    shown_pending_edges = pending_review_edges[:MAX_REPORT_RELATIONSHIP_EDGE_CARDS]
    has_relationships = bool(role_cards)
    return {
        "section_key": "public_key_person_relationships",
        "title": "公开关键人员与组织关系",
        "status": "evidence_available" if has_relationships else "needs_evidence",
        "instruction": "逐条核验公开摘录中的人员姓名、组织名称、角色关键词及人工复核状态；不得据此推断个人资产、资金来源或投资意愿。",
        "relationship_graph_scope": graph.get("operation_scope"),
        "key_person_relationship_summary": copy.deepcopy(raw_summary),
        "recorded_relationship_count": len(role_cards),
        "role_cards": shown_role_cards,
        "role_cards_truncated": len(role_cards) > MAX_REPORT_RELATIONSHIP_EDGE_CARDS,
        "pending_review_relationship_count": len(pending_review_edges),
        "pending_review_edges": shown_pending_edges,
        "pending_review_edges_truncated": (
            len(pending_review_edges) > MAX_REPORT_RELATIONSHIP_EDGE_CARDS
        ),
        "evidence_reference_ids": sorted_evidence_ids,
        "not_in_current_lead_evidence_chain_ids": sorted(
            evidence_ids - current_lead_evidence_ids
        ),
        "manual_review_checklist": (
            [
                "逐条打开原始来源，确认人员姓名、组织名称和登记角色在同一摘录中明确出现。",
                "“徐州关联”只能作为公开可追溯关联线索，不得补写籍贯、住址、乡土情结或私人联系方式。",
                "等待用户复核或待补证的角色边不得作为已确认控制关系、招商意愿或个人资金能力事实。",
            ]
            if has_relationships
            else [
                "当前台账尚未登记该线索的公开关键人员关系；应先补充可追溯原始来源，不得由报告或模型猜测。"
            ]
        ),
        "caveat": (
            "本章节仅展示已登记的公开人员组织角色和证据引用，不构成个人画像、个人征信、投资能力判断或招商承诺。"
            if has_relationships
            else "当前未登记公开关键人员关系不代表企业没有法人、股东或高管，只表示台账尚无满足证据规则的图谱记录。"
        ),
    }


def _confirmed_industry_business_metrics_assessment(
    *,
    confirmed_metrics: list[dict[str, Any]],
    lead: dict[str, Any],
    evidence_chain: list[dict[str, Any]],
) -> dict[str, object]:
    """为标准研判报告投影一条线索已人工确认的经营指标，不生成任何混合总额。

    输入是台账中已确认指标、当前线索和该线索的完整已签名证据链，输出是有限指标卡片、
    证据引用与被排除的异常计数。函数只接受 ``lead_id``、实体、产业、指标种类、统计范围、
    年份、金额均能与当前线索和证据 ID 对齐的记录；无法追溯、字段异常或口径不匹配的记录
    不展示也不修复。函数不联网、不写台账、不接受调用方金额或用户 ID，且不会相加营业收入、
    产值、项目投资额，不能据此判定企业意愿、招商成果或自动推进线索。
    """

    lead_id = lead.get("lead_id")
    entity_name = lead.get("entity_name")
    sector = lead.get("sector")
    if not all(isinstance(value, str) and value for value in (lead_id, entity_name, sector)):
        raise ValueError("招商线索缺少经营指标报告所需的标准字段")
    evidence_ids = {
        item.get("evidence_id")
        for item in evidence_chain
        if isinstance(item.get("evidence_id"), str)
    }
    records: list[dict[str, object]] = []
    excluded_invalid_or_untraceable_count = 0
    for metric in confirmed_metrics:
        required_text_fields = (
            "metric_id",
            "metric_draft_id",
            "evidence_id",
            "lead_id",
            "entity_name",
            "sector",
            "metric_kind",
            "metric_label",
            "source_metric_label",
            "statistic_scope",
            "original_value_text",
            "confirmed_at",
        )
        if any(
            not isinstance(metric.get(field_name), str) or not metric[field_name]
            for field_name in required_text_fields
        ):
            excluded_invalid_or_untraceable_count += 1
            continue
        metric_kind = metric["metric_kind"]
        rule = CONFIRMED_METRIC_KIND_RULES.get(metric_kind)
        value_yuan = metric.get("value_yuan")
        period_year = metric.get("period_year")
        if (
            rule is None
            or metric["lead_id"] != lead_id
            or metric["entity_name"] != entity_name
            or metric["sector"] != sector
            or metric["evidence_id"] not in evidence_ids
            or metric["metric_label"] != rule["metric_label"]
            or metric["statistic_scope"] != rule["statistic_scope"]
            or not isinstance(value_yuan, int)
            or isinstance(value_yuan, bool)
            or value_yuan <= 0
            or not isinstance(period_year, int)
            or isinstance(period_year, bool)
            or not 2000 <= period_year <= 2100
        ):
            excluded_invalid_or_untraceable_count += 1
            continue
        records.append(
            {
                "metric_id": metric["metric_id"],
                "metric_draft_id": metric["metric_draft_id"],
                "evidence_id": metric["evidence_id"],
                "metric_kind": metric_kind,
                "metric_label": metric["metric_label"],
                "source_metric_label": metric["source_metric_label"],
                "statistic_scope": metric["statistic_scope"],
                "period_year": period_year,
                "value_yuan": value_yuan,
                "original_value_text": metric["original_value_text"],
                "confirmed_at": metric["confirmed_at"],
            }
        )
    records.sort(
        key=lambda item: (
            int(item["period_year"]),
            str(item["metric_kind"]),
            str(item["confirmed_at"]),
            str(item["metric_id"]),
        )
    )
    metric_kind_counts = Counter(str(item["metric_kind"]) for item in records)
    return {
        "section_key": "confirmed_industry_business_metrics",
        "title": "已确认经营指标（并列口径）",
        "status": "evidence_available" if records else "needs_evidence",
        "instruction": "仅核对已确认指标与其 evidence_id；营业收入、产值和项目投资额必须按各自口径并列分析。",
        "mixed_metric_rule": {
            "mode": "parallel_by_metric_kind_and_scope",
            "forbidden_aggregation": ["revenue", "output_value", "project_investment"],
            "reason": "营业收入、产值与项目投资额统计口径不同，不能相加或替代招商意愿判断。",
        },
        "recorded_confirmed_metric_count": len(records),
        "metric_kind_counts": dict(sorted(metric_kind_counts.items())),
        "records": records,
        "evidence_reference_ids": sorted({str(item["evidence_id"]) for item in records}),
        "excluded_invalid_or_untraceable_metric_count": excluded_invalid_or_untraceable_count,
        "caveat": (
            "以下是已人工确认且可沿 evidence_id 追溯的经营指标；需人工结合统计范围、项目阶段和原始来源继续研判。"
            if records
            else "当前线索尚无可追溯、已人工确认的经营指标；这不代表企业没有营收、产值或项目投资。"
        ),
    }


def get_investment_assessment_report(workspace_dir: Path, *, lead_id: str) -> dict[str, object]:
    """生成一份不持久化的企业或基金标准化研判报告。

    输入是当前工作区和服务器已存在的 lead_id，输出包括完整证据链、融资/团队/技术/徐州
    关联/合作扩张/风险章节、来源覆盖提示、产业链图谱、公开关键人员组织关系、已确认经营
    指标附件、关联的待补证观察和已有评分快照。所有章节都只引用现有记录；无证据时明确
    标为待补证，多个来源时也只提示“多域名覆盖”，关键人员章节不做个人画像或个人资金
    判断，经营指标也只按不同口径并列展示，不自动判定事实、投资意愿或招商可行性。函数
    不联网、不保存、不改 evidence、候选、评分、预警、人工复核或任何原始采集字段。
    """
    normalized_lead_id = _required_lead_id(lead_id)
    ledger = load_ledger(workspace_dir)
    leads = _ledger_list(ledger, "leads")
    evidence_records = _ledger_list(ledger, "evidence")
    observations = _ledger_list(ledger, "candidate_observations")
    score_snapshots = _ledger_list(ledger, "score_snapshots")
    confirmed_metrics = _ledger_list(ledger, "industry_business_metrics")
    lead = next((item for item in leads if item.get("lead_id") == normalized_lead_id), None)
    if lead is None:
        raise ValueError("未找到对应的招商线索")
    evidence_chain = _evidence_for_lead(evidence_records, normalized_lead_id)
    industry_business_metrics_assessment = _confirmed_industry_business_metrics_assessment(
        confirmed_metrics=confirmed_metrics,
        lead=lead,
        evidence_chain=evidence_chain,
    )
    industry_chain_relationship_assessment = _industry_chain_relationship_assessment(
        workspace_dir,
        lead_id=normalized_lead_id,
        evidence_chain=evidence_chain,
    )
    public_key_person_relationship_assessment = _public_key_person_relationship_assessment(
        workspace_dir,
        lead_id=normalized_lead_id,
        evidence_chain=evidence_chain,
    )
    standardized_sections = [
        _section_from_evidence(
            section_key="financing_history",
            title="融资与基金募资历程",
            instruction="核验融资轮次、金额、时间、资金用途或基金募资信息。",
            evidence_records=evidence_chain,
            claim_types={"financing", "fund_raising"},
        ),
        _section_from_evidence(
            section_key="core_team",
            title="核心团队与乡土关联",
            instruction="核验创始人、法人、核心团队经历及与徐州的可追溯关联。",
            evidence_records=evidence_chain,
            claim_types={"xuzhou_connection"},
            # 核心团队章节不能只因“徐州”一词出现就自动归类；还必须在同一来源中出现
            # 固定角色文本。该词表只用于报告展示筛选，不增加、修改或确认任何事实。
            required_text_terms=("创始人", "创始团队", "法人", "核心团队", "ceo", "高管"),
        ),
        _section_from_evidence(
            section_key="technology_and_industry",
            title="技术与产业方向",
            instruction="核验具体技术方向、产品或基金行业研究方向与目标产业的匹配。",
            evidence_records=evidence_chain,
            claim_types={"industry_focus"},
        ),
        _section_from_evidence(
            section_key="xuzhou_association",
            title="徐州关联",
            instruction="核验祖籍、成长经历、业务、供应链、高校、园区或合作关系。",
            evidence_records=evidence_chain,
            claim_types={"xuzhou_connection"},
            association_tags={
                "ancestry_or_birthplace",
                "business_connection",
                "founder_or_team",
                "university_alumni",
                "organization_cooperation",
                "industry_chain",
            },
        ),
        _section_from_evidence(
            section_key="cooperation_and_landing",
            title="合作、扩张与落地窗口",
            instruction="核验地方合作需求、扩产、选址、项目签约或资源需求的原文与时间。",
            evidence_records=evidence_chain,
            claim_types={"cooperation", "expansion"},
        ),
        _section_from_evidence(
            section_key="risk_and_counter_signals",
            title="风险与反向信号",
            instruction="核验直接竞品、阶段不匹配、来源矛盾、过期或其他风险信息。",
            evidence_records=evidence_chain,
            claim_types={"risk"},
        ),
    ]
    needs_evidence_sections = [
        section["section_key"]
        for section in standardized_sections
        if section["status"] == "needs_evidence"
    ]
    claim_type_counts = Counter(
        record.get("claim_type")
        for record in evidence_chain
        if isinstance(record.get("claim_type"), str)
    )
    source_level_counts = Counter(
        record.get("source_level")
        for record in evidence_chain
        if isinstance(record.get("source_level"), str)
    )
    return {
        "report_type": "xuzhou_investment_assessment_readonly_v6",
        "generated_at": _utc_now(),
        "operation_scope": "readonly_existing_ledger_evidence_observation_and_confirmed_metric_summary_only",
        "lead": copy.deepcopy(lead),
        "evidence_chain": evidence_chain,
        "evidence_summary": {
            "total_evidence_count": len(evidence_chain),
            "claim_type_counts": dict(sorted(claim_type_counts.items())),
            "source_level_counts": dict(sorted(source_level_counts.items())),
            "unique_source_domains": sorted(
                {
                    domain
                    for domain in (_source_domain(record.get("source_url")) for record in evidence_chain)
                    if domain is not None
                }
            ),
        },
        "standardized_sections": standardized_sections,
        "evidence_timeline": _evidence_timeline(evidence_chain),
        "report_completeness": _report_completeness(standardized_sections),
        "cross_source_consistency_review": _cross_source_consistency_review(evidence_chain),
        "industry_chain_relationship_assessment": industry_chain_relationship_assessment,
        "public_key_person_relationship_assessment": public_key_person_relationship_assessment,
        "industry_business_metrics_assessment": industry_business_metrics_assessment,
        "needs_evidence_sections": needs_evidence_sections,
        "cross_source_coverage_checks": _cross_source_coverage(evidence_chain),
        "unverified_candidate_observations": _unverified_observations_for_lead(
            evidence_chain,
            observations,
        ),
        "score_snapshots": _latest_score_snapshots(score_snapshots, normalized_lead_id),
        "automated_business_conclusion": {
            "status": "not_generated",
            "reason": "本报告只展示现有记录；是否推进、搁置、判定意愿或调整权重必须由普通登录用户人工决定。",
        },
        "ledger_path": str(ledger_path(workspace_dir)),
    }


def _activity_day(timestamp: object, window_started_at: datetime) -> str | None:
    """将一条审计时间归入统计窗口内的 UTC 日期，异常或窗口外时间不计入活动量。"""
    parsed = _parse_utc_timestamp(timestamp)
    if parsed is None or parsed < window_started_at:
        return None
    return parsed.date().isoformat()


def _increment_daily_activity(
    daily_activity: dict[str, dict[str, int]],
    day: str | None,
    field_name: str,
) -> None:
    """累加只读日统计计数，字段由本模块固定，不能由台账文本注入任意统计键。"""
    if day is None:
        return
    if day not in daily_activity:
        daily_activity[day] = {
            "new_leads": 0,
            "new_evidence": 0,
            "review_actions": 0,
            "relationship_review_actions": 0,
            # 关键人员关系草稿的业务判断与实际图谱登记分开计数：前者是草稿复核，后者
            # 只是创建仍待复核的图谱边，二者都不等同招商推进或个人招商结论。
            "relationship_draft_review_actions": 0,
            "relationship_draft_graph_registration_actions": 0,
            # 协同投资信号复核只记录组织级公开信号的人工判断，不代表资金募集、个人出资、
            # 金融交易或线索推进，因此单独计数，不能并入招商成果。
            "co_investment_signal_review_actions": 0,
            "new_candidate_observations": 0,
            # 候选的高优先级仅表示先看原始快照，不是企业红色风险预警。
            "high_priority_observations": 0,
            # 指标草稿确认与线索复核分开计数：前者只确认统计口径，绝不等同招商推进。
            "industry_metric_review_actions": 0,
            "confirmed_industry_business_metrics": 0,
        }
    daily_activity[day][field_name] += 1


def _industry_metric_review_states(
    metric_drafts: list[dict[str, Any]],
    metric_draft_reviews: list[dict[str, Any]],
) -> dict[str, str]:
    """从追加式指标复核审计派生每个草稿当前状态，不修改草稿或正式证据。

    输入是台账现有指标草稿和按保存顺序追加的复核审计，输出为服务器草稿 ID 到最后一次
    已记录状态的映射。草稿没有审计时沿用 ``pending_user_review``；异常审计不被猜测为
    有效复核。函数仅用于只读统计，不接受用户身份、金额、来源或状态写入，不能覆盖
    evidence、草稿、复核审计、线索状态或任何招商结论。
    """

    known_draft_ids = {
        item.get("metric_draft_id")
        for item in metric_drafts
        if isinstance(item.get("metric_draft_id"), str)
    }
    states = {draft_id: "pending_user_review" for draft_id in known_draft_ids}
    for review in metric_draft_reviews:
        draft_id = review.get("metric_draft_id")
        new_state = review.get("new_state")
        if (
            isinstance(draft_id, str)
            and draft_id in states
            and isinstance(new_state, str)
            and new_state in {"confirmed", "need_evidence", "invalid"}
        ):
            # 台账审计是只追加列表，最后一条有效记录代表当前派生状态；不更新草稿本体。
            states[draft_id] = new_state
    return states


def _relationship_draft_review_states(
    relationship_drafts: list[dict[str, Any]],
    relationship_draft_reviews: list[dict[str, Any]],
) -> dict[str, str]:
    """从只追加关系草稿审计派生当前业务状态，不改写草稿、图谱或正式证据。

    输入为台账中的自动关系草稿和人工复核审计；输出为草稿 ID 到最后一次有效结论的映射。
    没有审计的草稿保持 ``pending_user_review``，异常条目不猜测为有效。函数仅供只读工作
    统计使用，不接受用户、人员、关系、URL、摘录或写入字段，不能自动登记图谱或改变原始
    evidence、线索、评分与任何人工业务状态。
    """

    known_draft_ids = {
        item.get("relationship_draft_id")
        for item in relationship_drafts
        if isinstance(item.get("relationship_draft_id"), str)
    }
    states = {draft_id: "pending_user_review" for draft_id in known_draft_ids}
    allowed_states = {"ready_for_graph_registration", "need_evidence", "observe", "invalid"}
    for review in relationship_draft_reviews:
        draft_id = review.get("relationship_draft_id")
        new_state = review.get("new_state")
        # 先校验状态是字符串，避免被篡改/损坏的列表或字典参与集合判断而抛出 TypeError；
        # 统计遇到异常审计只跳过，绝不能据此猜测草稿已被人工确认。
        if (
            isinstance(draft_id, str)
            and draft_id in states
            and isinstance(new_state, str)
            and new_state in allowed_states
        ):
            states[draft_id] = new_state
    return states


def _co_investment_signal_draft_review_states(
    co_investment_drafts: list[dict[str, Any]],
    co_investment_draft_reviews: list[dict[str, Any]],
) -> dict[str, str]:
    """从只追加协同投资信号审计派生当前业务状态，不处理个人资金或写原始证据。

    输入为组织级公开信号草稿及追加式人工判断；输出为草稿 ID 到最后一次有效状态的映射。
    未复核草稿保持 ``pending_user_review``，结构异常的审计不会被猜测为可对接。函数仅服务
    于只读统计，不接受人员、资产、联系方式、金额、来源或用户写入，不能募资、联系主体、
    推进线索，亦不能改 evidence、URL、摘录、采集时间、评分或其他人工审计。
    """

    known_draft_ids = {
        item.get("co_investment_draft_id")
        for item in co_investment_drafts
        if isinstance(item.get("co_investment_draft_id"), str)
    }
    states = {draft_id: "pending_user_review" for draft_id in known_draft_ids}
    allowed_states = {"ready_for_contact_validation", "need_evidence", "observe", "invalid"}
    for review in co_investment_draft_reviews:
        draft_id = review.get("co_investment_draft_id")
        new_state = review.get("new_state")
        if (
            isinstance(draft_id, str)
            and draft_id in states
            and isinstance(new_state, str)
            and new_state in allowed_states
        ):
            states[draft_id] = new_state
    return states


def get_investment_work_summary(
    workspace_dir: Path,
    *,
    window_days: int = 30,
) -> dict[str, object]:
    """生成招商日常工作的只读统计汇总，不改变任何业务数据或人员审计。

    输入是当前工作区和 1–90 天窗口，输出为按产业、人工状态、来源等级、待补证快照、
    关系图谱待办、关键人员关系草稿、协同投资信号草稿、经营指标草稿与每日活动计数的
    汇总，并给出当前线索状态分布式漏斗。
    统计只数已有记录：新线索、新证据、状态历史、关系/指标复核审计和观察快照；不把
    计数视为招商成果、人员绩效、企业事实或实际投资金额。函数不联网、不创建报告文件、
    不写入台账，不修改 evidence、来源 URL、原始摘录、采集时间、评分或人工复核。
    """
    normalized_window_days = _validate_window_days(window_days)
    ledger = load_ledger(workspace_dir)
    leads = _ledger_list(ledger, "leads")
    evidence_records = _ledger_list(ledger, "evidence")
    observations = _ledger_list(ledger, "candidate_observations")
    discovery_candidates = _ledger_list(ledger, "discovery_candidates")
    relationship_graph_audit = _ledger_list(ledger, "relationship_graph_audit")
    relationship_drafts = _ledger_list(ledger, "relationship_drafts")
    relationship_draft_reviews = _ledger_list(ledger, "relationship_draft_reviews")
    co_investment_drafts = _ledger_list(ledger, "co_investment_signal_drafts")
    co_investment_draft_reviews = _ledger_list(ledger, "co_investment_signal_draft_reviews")
    metric_drafts = _ledger_list(ledger, "industry_metric_drafts")
    metric_draft_reviews = _ledger_list(ledger, "industry_metric_draft_reviews")
    confirmed_metrics = _ledger_list(ledger, "industry_business_metrics")
    relationship_review_queue = get_relationship_review_queue(workspace_dir, limit=1)
    generated_at = datetime.now(timezone.utc)
    window_started_at = generated_at - timedelta(days=normalized_window_days)
    daily_activity: dict[str, dict[str, int]] = {}

    for lead in leads:
        _increment_daily_activity(
            daily_activity,
            _activity_day(lead.get("created_at"), window_started_at),
            "new_leads",
        )
        history = lead.get("status_history")
        if not isinstance(history, list):
            continue
        for event in history:
            if isinstance(event, dict):
                _increment_daily_activity(
                    daily_activity,
                    _activity_day(event.get("at"), window_started_at),
                    "review_actions",
                )
    for evidence in evidence_records:
        _increment_daily_activity(
            daily_activity,
            _activity_day(evidence.get("created_at"), window_started_at),
            "new_evidence",
        )
    for observation in observations:
        observation_day = _activity_day(observation.get("collected_at"), window_started_at)
        _increment_daily_activity(daily_activity, observation_day, "new_candidate_observations")
        if observation.get("review_priority") in {"high_priority_review", "red_review"}:
            # 兼容旧台账中 ``red_review`` 的候选优先级；该旧值从来不表示已确认企业风险。
            _increment_daily_activity(daily_activity, observation_day, "high_priority_observations")
    for audit_item in relationship_graph_audit:
        if audit_item.get("action") == "relationship_edge_reviewed":
            _increment_daily_activity(
                daily_activity,
                _activity_day(audit_item.get("operated_at"), window_started_at),
                "relationship_review_actions",
            )
        if audit_item.get("action") == "approved_relationship_draft_registered_to_graph":
            _increment_daily_activity(
                daily_activity,
                _activity_day(audit_item.get("operated_at"), window_started_at),
                "relationship_draft_graph_registration_actions",
            )
    for relationship_draft_review in relationship_draft_reviews:
        _increment_daily_activity(
            daily_activity,
            _activity_day(relationship_draft_review.get("reviewed_at"), window_started_at),
            "relationship_draft_review_actions",
        )
    for co_investment_draft_review in co_investment_draft_reviews:
        _increment_daily_activity(
            daily_activity,
            _activity_day(co_investment_draft_review.get("reviewed_at"), window_started_at),
            "co_investment_signal_review_actions",
        )
    for metric_review in metric_draft_reviews:
        _increment_daily_activity(
            daily_activity,
            _activity_day(metric_review.get("reviewed_at"), window_started_at),
            "industry_metric_review_actions",
        )
    for metric in confirmed_metrics:
        _increment_daily_activity(
            daily_activity,
            _activity_day(metric.get("confirmed_at"), window_started_at),
            "confirmed_industry_business_metrics",
        )

    metric_review_states = _industry_metric_review_states(metric_drafts, metric_draft_reviews)
    relationship_draft_review_states = _relationship_draft_review_states(
        relationship_drafts, relationship_draft_reviews
    )
    co_investment_draft_review_states = _co_investment_signal_draft_review_states(
        co_investment_drafts, co_investment_draft_reviews
    )
    registered_relationship_draft_ids = {
        audit.get("target_id")
        for audit in relationship_graph_audit
        if audit.get("action") == "approved_relationship_draft_registered_to_graph"
        and audit.get("target_kind") == "relationship_draft"
        and isinstance(audit.get("target_id"), str)
    }
    pending_relationship_drafts = [
        draft
        for draft in relationship_drafts
        if isinstance(draft.get("relationship_draft_id"), str)
        and draft["relationship_draft_id"] not in registered_relationship_draft_ids
        and relationship_draft_review_states.get(draft["relationship_draft_id"])
        in {"pending_user_review", "need_evidence", "ready_for_graph_registration"}
    ]
    relationship_draft_state_counts = Counter(relationship_draft_review_states.values())
    pending_co_investment_drafts = [
        draft
        for draft in co_investment_drafts
        if isinstance(draft.get("co_investment_draft_id"), str)
        and co_investment_draft_review_states.get(draft["co_investment_draft_id"])
        in {"pending_user_review", "need_evidence", "ready_for_contact_validation"}
    ]
    co_investment_draft_state_counts = Counter(co_investment_draft_review_states.values())
    pending_metric_drafts = [
        draft
        for draft in metric_drafts
        if isinstance(draft.get("metric_draft_id"), str)
        and metric_review_states.get(draft["metric_draft_id"]) == "pending_user_review"
    ]
    metric_review_state_counts = Counter(metric_review_states.values())

    sector_summary: dict[str, dict[str, object]] = {}
    for sector in SECTOR_LABELS:
        sector_leads = [lead for lead in leads if lead.get("sector") == sector]
        sector_summary[sector] = {
            "label": SECTOR_LABELS[sector],
            "lead_count": len(sector_leads),
            "red_alert_count": sum(has_open_enterprise_risk_alert(lead) for lead in sector_leads),
            "pending_user_review_count": sum(
                lead.get("review_state") == "pending_user_review" for lead in sector_leads
            ),
            # 指标数量只按草稿关联的既有线索归类；无法关联线索的异常记录不会被猜测归属。
            "pending_metric_draft_count": sum(
                draft.get("lead_id") in {lead.get("lead_id") for lead in sector_leads}
                for draft in pending_metric_drafts
            ),
            "pending_key_person_relationship_draft_count": sum(
                draft.get("lead_id") in {lead.get("lead_id") for lead in sector_leads}
                for draft in pending_relationship_drafts
            ),
            "pending_co_investment_signal_draft_count": sum(
                draft.get("lead_id") in {lead.get("lead_id") for lead in sector_leads}
                for draft in pending_co_investment_drafts
            ),
            "confirmed_industry_business_metric_count": sum(
                metric.get("lead_id") in {lead.get("lead_id") for lead in sector_leads}
                for metric in confirmed_metrics
            ),
        }
    review_state_counts = Counter(
        lead.get("review_state")
        for lead in leads
        if isinstance(lead.get("review_state"), str)
    )
    source_level_counts = Counter(
        evidence.get("source_level")
        for evidence in evidence_records
        if isinstance(evidence.get("source_level"), str)
    )
    source_diversity = _candidate_source_diversity(
        discovery_candidates, window_started_at=window_started_at
    )
    funnel_state_order = (
        "pending_user_review",
        "advance",
        "need_evidence",
        "observe",
        "invalid",
    )
    additional_review_states = sorted(
        state for state in review_state_counts if state not in funnel_state_order
    )
    return {
        # Keep the externally consumed report type stable; the optional
        # candidate_source_diversity block is backwards-compatible metadata.
        "report_type": "xuzhou_investment_work_summary_readonly_v5",
        "generated_at": generated_at.isoformat(),
        "window_days": normalized_window_days,
        "window_started_at": window_started_at.isoformat(),
        "operation_scope": "readonly_existing_ledger_work_statistics_only",
        "current_totals": {
            "lead_count": len(leads),
            "evidence_count": len(evidence_records),
            "red_alert_lead_count": sum(has_open_enterprise_risk_alert(lead) for lead in leads),
            "candidate_observation_count": len(observations),
            "pending_evidence_observation_count": sum(
                observation.get("state") == "pending_evidence_verification"
                for observation in observations
            ),
            "relationship_review_queue_item_count": relationship_review_queue["summary"][
                "queued_item_count"
            ],
            "relationship_need_evidence_edge_count": relationship_review_queue["summary"][
                "review_state_counts"
            ].get("need_evidence", 0),
            "key_person_relationship_draft_count": len(relationship_drafts),
            "pending_key_person_relationship_draft_action_count": len(pending_relationship_drafts),
            "registered_key_person_relationship_draft_count": len(registered_relationship_draft_ids),
            "co_investment_signal_draft_count": len(co_investment_drafts),
            "pending_co_investment_signal_draft_action_count": len(pending_co_investment_drafts),
            "industry_metric_draft_count": len(metric_drafts),
            "pending_user_review_industry_metric_draft_count": len(pending_metric_drafts),
            "confirmed_industry_business_metric_count": len(confirmed_metrics),
        },
        "sector_summary": sector_summary,
        "review_state_counts": {
            state: {"label": REVIEW_STATE_LABELS.get(state, state), "count": count}
            for state, count in sorted(review_state_counts.items())
        },
        "lead_review_funnel": {
            "entry_lead_count": len(leads),
            "current_state_stages": [
                {
                    "state": state,
                    "label": REVIEW_STATE_LABELS.get(state, state),
                    "current_lead_count": review_state_counts.get(state, 0),
                }
                for state in (*funnel_state_order, *additional_review_states)
            ],
            "state_counts_are_mutually_exclusive": True,
            "caveat": "这是当前线索状态的分布，不是转化率、招商成果或自动推进建议；所有状态变化仍须普通登录用户复核。",
        },
        "source_level_counts": dict(sorted(source_level_counts.items())),
        "candidate_source_diversity": source_diversity,
        "industry_metric_summary": {
            "draft_review_state_counts": dict(sorted(metric_review_state_counts.items())),
            "pending_user_review_draft_count": len(pending_metric_drafts),
            "confirmed_metric_count": len(confirmed_metrics),
            "mixed_metric_rule": {
                "mode": "parallel_by_metric_kind_and_scope",
                "forbidden_aggregation": ["revenue", "output_value", "project_investment"],
                "reason": "工作统计只计数，不汇总营业收入、产值和项目投资额，三类口径不得相加。",
            },
            "report_cross_reference": {
                "assessment_report_field": "industry_business_metrics_assessment",
                "reason": "单条线索的已确认指标及其 evidence_id 应在标准研判报告中查看；本汇总不复制或修改原始证据。",
            },
        },
        "key_person_relationship_draft_summary": {
            "draft_review_state_counts": dict(sorted(relationship_draft_state_counts.items())),
            "pending_action_draft_count": len(pending_relationship_drafts),
            "registered_to_graph_draft_count": len(registered_relationship_draft_ids),
            "caveat": "仅统计公开关键人员—组织关系草稿及人工动作；不统计个人资产、联系方式、投资能力或招商意愿，登记到图谱后的边仍须单独复核。",
        },
        "co_investment_signal_draft_summary": {
            "draft_review_state_counts": dict(sorted(co_investment_draft_state_counts.items())),
            "pending_action_draft_count": len(pending_co_investment_drafts),
            "caveat": "仅统计一手公开材料中的组织级协同投资信号及人工判断；不统计或推断个人资产、出资能力、资金承诺、金融产品适当性或实际投资金额，后续联系须人工合规核验。",
        },
        "relationship_review_queue_summary": copy.deepcopy(relationship_review_queue["summary"]),
        "daily_activity": [
            {"date": day, **daily_activity[day]}
            for day in sorted(daily_activity, reverse=True)
        ],
        "interpretation_caveat": "统计仅反映台账记录量与人工状态事件，不等同于招商成果、企业真实意愿、人员绩效或实际投资金额。",
        "ledger_path": str(ledger_path(workspace_dir)),
    }


def _subdirection_matches_evidence(evidence: dict[str, Any], subdirection: str) -> bool:
    """仅按固定细分词在已保存字段中的字面出现判断“待人工核验的匹配信号”。

    输入是一条已入库证据和来源发现配置中预定义的细分方向，输出布尔值；匹配范围仅限
    实体名称、来源标题和原始摘录，不能从语义相近词、模型总结或网页外信息推断分类。
    ``True`` 只表示当前证据文本出现该固定词，绝不等同于企业主营、技术能力、投资意愿
    或产业链事实。函数不接收用户输入、不联网、不写标签、不改写 evidence、评分或复核。
    """
    searchable_text = " ".join(
        value.casefold()
        for value in (
            # 正式证据不重复保存企业名；此内部字段由下方函数按不可变 lead_id 从已验证
            # 线索副本补入，只存在本次报告内存，绝不会写回 evidence 或台账。
            evidence.get("_report_entity_name"),
            evidence.get("source_title"),
            evidence.get("excerpt"),
        )
        if isinstance(value, str)
    )
    return subdirection.casefold() in searchable_text


def _evidence_with_report_lead_context(
    evidence: dict[str, Any],
    *,
    leads_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """按不可变 lead_id 为证据副本补齐报告所需企业名与产业，不向原始证据写入冗余字段。

    输入是一条正式证据和当前工作区已验证的线索索引，输出仅供本次报告使用的深拷贝，
    内部增加 ``_report_entity_name`` 与 ``_report_sector``。证据必须关联存在的线索，且
    线索名称与产业字段必须有效；否则显式失败，避免从标题或 URL 猜测归属。该函数不
    接收客户端 ID、没有权限副作用、不保存结果，更不会修改 evidence 的来源 URL、原始
    摘录、采集时间、哈希、评分依据或任何人工复核记录。
    """
    lead_id = evidence.get("lead_id")
    if not isinstance(lead_id, str) or not lead_id:
        raise ValueError("细分方向报告中的证据缺少 lead_id")
    lead = leads_by_id.get(lead_id)
    if lead is None:
        raise ValueError("细分方向报告中的证据关联了不存在的招商线索")
    entity_name = lead.get("entity_name")
    sector = lead.get("sector")
    if not isinstance(entity_name, str) or not entity_name or not isinstance(sector, str) or not sector:
        raise ValueError("细分方向报告关联线索缺少企业名称或产业")
    result = copy.deepcopy(evidence)
    result["_report_entity_name"] = entity_name
    result["_report_sector"] = sector
    return result


def _subdirection_coverage_entry(
    *,
    subdirection: str,
    evidence_records: list[dict[str, Any]],
    leads_by_id: dict[str, dict[str, Any]],
) -> dict[str, object]:
    """为一个固定细分方向汇总证据记录、线索状态和人工补证提示，不创建产业分类事实。

    输入是预定义细分词、当前窗口内同一大产业的已存证据及线索索引；输出为字面匹配的
    来源卡片、来源域名覆盖、关联线索状态和待人工核验动作。没有匹配只能说明当前窗口
    的台账未见该词，绝不代表该细分方向没有企业、项目或投资机会；多个域名也只提示
    人工对齐。函数完全只读，无管理员或普通用户身份判断，不新增标签、证据、线索、
    评分、预警或人工复核，也不会修改原始 URL、摘录和采集时间。
    """
    matched_records = [
        record
        for record in evidence_records
        if _subdirection_matches_evidence(record, subdirection)
    ]
    evidence_ids = [record.get("evidence_id") for record in matched_records]
    if not all(isinstance(evidence_id, str) and evidence_id for evidence_id in evidence_ids):
        raise ValueError("细分方向报告中的证据缺少 evidence_id")
    matched_lead_ids = sorted(
        {
            record.get("lead_id")
            for record in matched_records
            if isinstance(record.get("lead_id"), str) and record.get("lead_id")
        }
    )
    matched_leads: list[dict[str, object]] = []
    for lead_id in matched_lead_ids:
        lead = leads_by_id.get(lead_id)
        if lead is None:
            raise ValueError("细分方向报告中的证据关联了不存在的招商线索")
        entity_name = lead.get("entity_name")
        review_state = lead.get("review_state")
        if not isinstance(entity_name, str) or not isinstance(review_state, str):
            raise ValueError("细分方向报告关联线索缺少名称或人工状态")
        matched_leads.append(
            {
                "lead_id": lead_id,
                "entity_name": entity_name,
                "review_state": review_state,
                "red_alert": has_open_enterprise_risk_alert(lead),
            }
        )
    cards = _bounded_report_evidence_cards(
        matched_records,
        limit=MAX_SUBDIRECTION_EVIDENCE_CARDS,
    )
    domains = sorted(
        {
            domain
            for domain in (_source_domain(record.get("source_url")) for record in matched_records)
            if domain is not None
        }
    )
    if not matched_records:
        coverage_status = "needs_traceable_evidence"
        manual_action = "补充至少一条明确出现该细分词的可追溯公开来源，再由人工核验其业务含义。"
    elif len(domains) >= 2:
        coverage_status = "multiple_source_domain_signals_pending_alignment"
        manual_action = "人工核对多个来源的日期、标题、原始摘录和链接是否描述同一具体事件。"
    else:
        coverage_status = "traceable_text_match_available"
        manual_action = "人工核对原始摘录中的细分词是否确实描述企业或基金的技术、产品或投资方向。"
    return {
        "subdirection": subdirection,
        "coverage_status": coverage_status,
        "recorded_evidence_count": len(matched_records),
        "evidence_ids": evidence_ids,
        "evidence_cards": cards["records"],
        "evidence_cards_truncated": cards["truncated"],
        "unique_source_domains": domains,
        "unique_source_domain_count": len(domains),
        "matched_lead_count": len(matched_leads),
        "matched_leads": matched_leads,
        "red_alert_lead_count": sum(lead["red_alert"] is True for lead in matched_leads),
        "pending_user_review_lead_count": sum(
            lead["review_state"] == "pending_user_review" for lead in matched_leads
        ),
        "manual_action": manual_action,
        "caveat": "匹配只来自台账字段中固定细分词的字面出现，不是自动产业分类、交叉验证或招商结论。",
    }


def get_industry_subdirection_coverage_report(
    workspace_dir: Path,
    *,
    window_days: int = 30,
) -> dict[str, object]:
    """生成三条重点产业全部已配置细分方向的只读证据与来源覆盖报告。

    输入是当前工作区和 1–90 天窗口，输出为具身智能、半导体、生物医药每个已配置细分
    方向的字面证据匹配、关联线索状态、待补证清单和该大产业的宽查询来源覆盖。所有普通
    用户都可查看当前工作区报告；函数不接受行业、词表、URL、用户 ID 或写入参数，避免
    模型或前端扩大查询范围。报告不联网、不触发搜索、不自动给企业打细分标签、不改变
    证据、来源 URL、摘录、采集时间、评分、预警、关系图谱或人工复核；“覆盖缺口”仅指
    当前窗口台账与固定来源目录记录不足，不代表市场、企业或项目不存在。
    """
    normalized_window_days = _validate_window_days(window_days)
    from .source_discovery import SECTOR_DISCOVERY_CONFIG

    ledger = load_ledger(workspace_dir)
    leads = _ledger_list(ledger, "leads")
    evidence_records = _ledger_list(ledger, "evidence")
    leads_by_id: dict[str, dict[str, Any]] = {}
    for lead in leads:
        lead_id = lead.get("lead_id")
        if not isinstance(lead_id, str) or not lead_id:
            raise ValueError("细分方向报告中的招商线索缺少 lead_id")
        if lead_id in leads_by_id:
            raise ValueError("细分方向报告中的招商线索存在重复 lead_id")
        leads_by_id[lead_id] = lead
    generated_at = datetime.now(timezone.utc)
    window_started_at = generated_at - timedelta(days=normalized_window_days)
    recent_evidence: list[dict[str, Any]] = []
    excluded_evidence_count = 0
    for evidence in evidence_records:
        collected_at = _parse_utc_timestamp(evidence.get("created_at"))
        if collected_at is None or collected_at < window_started_at:
            excluded_evidence_count += 1
            continue
        recent_evidence.append(
            _evidence_with_report_lead_context(
                evidence,
                leads_by_id=leads_by_id,
            )
        )
    broad_source_coverage = get_source_coverage_report(
        workspace_dir,
        window_days=normalized_window_days,
    )
    raw_source_sectors = broad_source_coverage.get("sectors")
    raw_coverage_gaps = broad_source_coverage.get("coverage_gaps")
    if not isinstance(raw_source_sectors, dict) or not isinstance(raw_coverage_gaps, list):
        raise ValueError("来源覆盖报告缺少细分方向汇总所需字段")

    sector_reports: dict[str, dict[str, object]] = {}
    needs_evidence_subdirections: list[dict[str, str]] = []
    for sector, raw_config in SECTOR_DISCOVERY_CONFIG.items():
        if not isinstance(raw_config, dict):
            raise ValueError("产业发现配置必须是对象")
        label = raw_config.get("label")
        subdirections = raw_config.get("subdirections")
        if not isinstance(label, str) or not isinstance(subdirections, list) or not all(
            isinstance(item, str) and item for item in subdirections
        ):
            raise ValueError("产业发现配置缺少有效细分方向")
        sector_evidence = [
            evidence for evidence in recent_evidence if evidence.get("_report_sector") == sector
        ]
        entries = [
            _subdirection_coverage_entry(
                subdirection=subdirection,
                evidence_records=sector_evidence,
                leads_by_id=leads_by_id,
            )
            for subdirection in subdirections
        ]
        for entry in entries:
            if entry["coverage_status"] == "needs_traceable_evidence":
                needs_evidence_subdirections.append(
                    {"sector": sector, "subdirection": entry["subdirection"]}
                )
        matched_subdirections = {
            entry["subdirection"]
            for entry in entries
            if entry["recorded_evidence_count"] > 0
        }
        unclassified_evidence_count = sum(
            not any(_subdirection_matches_evidence(evidence, subdirection) for subdirection in subdirections)
            for evidence in sector_evidence
        )
        source_sector = raw_source_sectors.get(sector)
        if not isinstance(source_sector, dict) or not isinstance(source_sector.get("recipes"), dict):
            raise ValueError("来源覆盖报告缺少重点产业目录")
        sector_gap_types = sorted(
            {
                gap.get("gap_type")
                for gap in raw_coverage_gaps
                if isinstance(gap, dict)
                and gap.get("sector") == sector
                and isinstance(gap.get("gap_type"), str)
            }
        )
        sector_reports[sector] = {
            "label": label,
            "configured_subdirection_count": len(subdirections),
            "subdirection_count_with_traceable_text_match": len(matched_subdirections),
            "unclassified_recent_evidence_count": unclassified_evidence_count,
            "subdirection_coverage": entries,
            "broad_discovery_coverage": {
                "fixed_recipe_coverage": copy.deepcopy(source_sector["recipes"]),
                "current_window_gap_types": sector_gap_types,
                "subdirection_query_scope": "all_configured_subdirections_are_included_in_broad_sector_queries",
                "caveat": "现有发现查询把该产业全部配置细分词放入同一宽查询表达式；目录覆盖不能证明某一细分方向已被独立检索或充分覆盖。",
            },
        }
    return {
        "report_type": "xuzhou_industry_subdirection_coverage_readonly_v1",
        "generated_at": generated_at.isoformat(),
        "window_days": normalized_window_days,
        "window_started_at": window_started_at.isoformat(),
        "operation_scope": "readonly_fixed_subdirection_text_match_and_source_coverage_summary",
        "recent_evidence_count": len(recent_evidence),
        "excluded_outside_or_invalid_window_evidence_count": excluded_evidence_count,
        "sectors": sector_reports,
        "needs_evidence_subdirections": needs_evidence_subdirections,
        "needs_evidence_subdirection_count": len(needs_evidence_subdirections),
        "source_coverage_generated_at": copy.deepcopy(broad_source_coverage.get("generated_at")),
        "automated_industry_conclusion": {
            "status": "not_generated",
            "reason": "报告只显示固定词在当前窗口台账字段中的出现与来源目录覆盖；产业归属、技术能力和招商优先级必须由普通登录用户核验原始证据后决定。",
        },
        "ledger_path": str(ledger_path(workspace_dir)),
    }
