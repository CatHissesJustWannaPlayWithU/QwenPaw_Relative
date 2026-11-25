"""徐州招商线索与证据台账的核心业务逻辑。

本模块的职责是把“某个企业或基金可能值得关注”这类信息，转化为可追溯的
线索与公开证据记录。它刻意不负责网页采集、自动评分和投资结论：这些能力
应在拥有足够数据来源、审批规则和人工反馈后，再作为独立阶段接入。
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .evidence_integrity import sign_next_evidence
from .storage import (
    evidence_snapshot_path,
    ledger_path,
    load_ledger,
    save_evidence_snapshot,
    save_ledger,
)


# 统一保存英文枚举值，避免同一状态因中文写法不同而无法被稳定查询。``startup_team``
# 只表示尚未以公司主体登记或以团队形式对外活动的创业主体；它不会降低正式证据、来源
# 快照或人工复核要求，也绝不能仅凭名称把企业自动改判为创业团队。
ENTITY_KINDS = frozenset({"enterprise", "fund", "startup_team"})
SECTORS = frozenset({"semiconductor", "embodied_intelligence", "biomedicine", "other"})
SOURCE_LEVELS = frozenset({"primary", "secondary", "lead_only"})
SOURCE_TYPES = frozenset(
    {
        "manual_public_entry",
        "automated_public_search_capture",
        "automated_trusted_catalog_capture",
    }
)
MAX_RAW_EVIDENCE_BYTES = 1_048_576
COLLECTION_METHODS = frozenset({"manual_entry", "public_search_then_snapshot"})
CLAIM_TYPES = frozenset(
    {
        "financing",
        "fund_raising",
        "xuzhou_connection",
        "cooperation",
        "expansion",
        "industry_focus",
        "fund_investment",
        "risk",
        # 仅由服务器在原文不支持调用方所选分类时写入；不是可由模型、前端或人工直接
        # 声明的事实类别，表示“已保存来源，但现有固定规则无法安全分类”。
        "unclassified",
    }
)
REQUESTABLE_CLAIM_TYPES = frozenset(CLAIM_TYPES - {"unclassified"})
REVIEW_STATES = frozenset(
    {"pending_user_review", "advance", "need_evidence", "observe", "invalid"}
)
MANUAL_REVIEW_STATES = frozenset({"advance", "need_evidence", "observe", "invalid"})
# 红色只用于“有明确原文支持、可能影响招商合作继续推进的企业风险”。融资、扩产、
# 合作、产业方向和徐州关联都是招商机会或研判维度，不能仅因本身重要而被标红。
RISK_ALERT_LEVEL_RANK = {"high": 1, "critical": 2}
RISK_ALERT_REVIEW_STATUS_BY_DECISION = {
    "need_evidence": "need_evidence",
    "observe": "observe",
    "advance": "closed",
    "invalid": "closed",
}
# 每组词语必须出现在同一条服务器抓取并签名的标题或摘录中，才会触发风险预警。这里
# 不把“亏损”“融资较少”“招聘减少”等可有多种解释的弱信号写成企业没钱的事实。
RISK_ALERT_TERM_RULES: tuple[dict[str, object], ...] = (
    {
        "risk_type": "liquidity_or_solvency",
        "label": "资金链或偿债风险",
        "critical_terms": ("资金链断裂", "无力偿还", "债务违约", "破产清算", "破产重整", "拖欠工资"),
        "high_terms": ("资金链紧张", "流动性紧张", "现金流紧张", "融资失败"),
    },
    {
        "risk_type": "project_or_operation_disruption",
        "label": "项目或经营中断风险",
        "critical_terms": ("停产", "停工", "关停", "项目终止"),
        "high_terms": ("项目暂停", "取消扩产", "撤回投资", "延期投产"),
    },
    {
        "risk_type": "major_cooperation_change",
        "label": "重大合作变化风险",
        "critical_terms": ("核心客户终止合作", "主要合作方退出"),
        "high_terms": ("终止合作", "解除合作", "合作方退出", "订单取消"),
    },
    {
        "risk_type": "compliance_or_legal",
        "label": "合规、执行或诉讼风险",
        "critical_terms": ("重大违法", "失信被执行人", "被列入失信", "立案调查"),
        "high_terms": ("行政处罚", "被执行人", "重大诉讼"),
    },
    {
        "risk_type": "key_person_change",
        "label": "核心团队变化风险",
        "critical_terms": (),
        "high_terms": ("创始人离职", "CEO辞职", "核心团队离职", "核心高管离职"),
    },
)
# ``claim_type`` 只是帮助人工归档和检索的“原文分类”，不能作为企业事实或招商结论。每项
# 规则只做字面支持校验，不尝试从上下文推理融资金额、合作真实性或投资意愿；无匹配时保守
# 写为 ``unclassified``，并保留调用方请求分类和原始摘录供人工重新判断。
CLAIM_TYPE_TEXT_SUPPORT_TERMS: dict[str, tuple[str, ...]] = {
    "financing": ("融资", "获投", "获得投资", "天使轮", "pre-a", "a轮", "b轮", "c轮"),
    "fund_raising": ("募资", "募集", "基金募集", "基金募资", "设立基金", "产业基金"),
    "xuzhou_connection": ("徐州", "徐州人", "祖籍", "籍贯", "家乡"),
    "cooperation": ("合作", "协同", "签约", "联合", "伙伴"),
    "expansion": ("扩产", "扩建", "扩张", "新增产线", "新建", "投产", "开工"),
    "industry_focus": (
        "半导体",
        "芯片",
        "具身智能",
        "机器人",
        "医药",
        "生物",
        "医疗",
        "技术方向",
        "研发",
    ),
    "fund_investment": ("投资", "被投", "投资组合", "出资"),
}
# v2 在固定词命中的基础上，要求目标主体名称与类别词位于同一句原文，避免把报道中
# 其他企业、行业背景或评论者的融资/合作信息误贴给当前线索。
CLAIM_TYPE_TEXT_SUPPORT_METHOD = "server_entity_attributed_fixed_term_check_v2"
CLAIM_TYPE_TEXT_SUPPORT_STATUSES = frozenset(
    {"matched_fixed_terms", "server_reclassified_risk", "unclassified_no_fixed_term_match"}
)
SUPPORTED_CLAIM_TYPE_TEXT_SUPPORTS = frozenset(
    {"matched_fixed_terms", "server_reclassified_risk"}
)
XUZHOU_ASSOCIATION_TAGS = frozenset(
    {
        "ancestry_or_birthplace",
        "business_connection",
        "founder_or_team",
        "university_alumni",
        "organization_cooperation",
        "industry_chain",
    }
)


# 工具入口同时接受中文和英文，内部始终归一化为英文枚举值。中文“团队”只在操作者或
# 已核验草稿明确给出该主体类型时映射为 ``startup_team``，不根据实体名称自行推断。
ENTITY_KIND_ALIASES = {
    "企业": "enterprise",
    "机构": "fund",
    "基金": "fund",
    "创业团队": "startup_team",
    "团队": "startup_team",
}
SECTOR_ALIASES = {
    "半导体": "semiconductor",
    "具身智能": "embodied_intelligence",
    "生物医药": "biomedicine",
    "医药": "biomedicine",
    "其他": "other",
}
SOURCE_LEVEL_ALIASES = {"一手": "primary", "一手来源": "primary", "二手": "secondary", "线索": "lead_only"}
CLAIM_TYPE_ALIASES = {
    "融资": "financing",
    "基金募资": "fund_raising",
    "徐州关联": "xuzhou_connection",
    "合作": "cooperation",
    "扩张": "expansion",
    "产业方向": "industry_focus",
    "投资案例": "fund_investment",
    "风险": "risk",
}
TAG_ALIASES = {
    "祖籍或出生地": "ancestry_or_birthplace",
    "徐州祖籍": "ancestry_or_birthplace",
    "业务关联": "business_connection",
    "创始人或团队": "founder_or_team",
    "校友": "university_alumni",
    "高校校友": "university_alumni",
    "机构合作": "organization_cooperation",
    "产业链": "industry_chain",
}
REVIEW_STATE_ALIASES = {
    "等待用户复核": "pending_user_review",
    "等待管理员复核": "pending_user_review",
    "pending_admin_review": "pending_user_review",
    "推进": "advance",
    "待补证": "need_evidence",
    "观察": "observe",
    "无效": "invalid",
}


def _utc_now() -> str:
    """返回统一的 UTC 时间字符串，便于跨机器审计和排序。"""
    return datetime.now(timezone.utc).isoformat()


def _required_text(value: object, field_name: str, *, max_length: int = 500) -> str:
    """校验必填文本，并去除用户输入两端的无意义空白。"""
    if not isinstance(value, str) or not (text := value.strip()):
        raise ValueError(f"{field_name}不能为空")
    if len(text) > max_length:
        raise ValueError(f"{field_name}不能超过{max_length}个字符")
    return text


def _canonical(
    value: object,
    field_name: str,
    allowed_values: frozenset[str],
    aliases: dict[str, str] | None = None,
) -> str:
    """把可读输入转换为受控枚举值，阻止未定义状态进入台账。"""
    raw_value = _required_text(value, field_name, max_length=100).casefold()
    normalized_value = (aliases or {}).get(raw_value, raw_value)
    if normalized_value not in allowed_values:
        accepted_values = "、".join(sorted(allowed_values))
        raise ValueError(f"{field_name}不合法，可选值为：{accepted_values}")
    return normalized_value


def _validate_http_url(value: object) -> str:
    """只允许可访问的 HTTP(S) 来源地址进入证据链。"""
    source_url = _required_text(value, "source_url", max_length=2_000)
    parsed = urlparse(source_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("source_url必须是完整的http或https地址")
    return source_url


def _validate_source_date(value: object) -> str:
    """校验来源公开日期，台账只保存 ISO 日期，避免模糊时间无法比较。"""
    source_published_at = _required_text(value, "source_published_at", max_length=30)
    try:
        return date.fromisoformat(source_published_at[:10]).isoformat()
    except ValueError as exc:
        raise ValueError("source_published_at必须使用YYYY-MM-DD格式") from exc


def _normalize_xuzhou_tags(value: object) -> list[str]:
    """归一化徐州关联类型，并去重后保持录入顺序。"""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("xuzhou_association_tags必须是列表")

    tags: list[str] = []
    for raw_tag in value:
        tag = _canonical(
            raw_tag,
            "xuzhou_association_tags中的标签",
            XUZHOU_ASSOCIATION_TAGS,
            TAG_ALIASES,
        )
        if tag not in tags:
            tags.append(tag)
    return tags


def _normalize_capture_metadata(value: object, *, collected_at: str) -> dict[str, str]:
    """规范化服务器生成的采集元数据，补齐来源类型和实际采集时间。

    输入是仅供内部受控抓取流程传入的元数据及本次服务器时间，输出是可附加到新证据
    的受控字段。正式证据不接受缺少元数据的手工文本，避免模型或前端自行声明来源
    等级、质量、链接或摘录并制造红色预警；网页、模型和前端也不能伪造内容哈希、
    自动发现批次或采集时间。该函数不读取、更改或删除任何既有证据，只为即将新增
    的记录建立可追溯来源。
    """
    if value is None:
        raise ValueError("正式证据必须来自服务器受控抓取，不接受手工声明的来源字段")
    if not isinstance(value, dict):
        raise ValueError("capture_metadata必须是对象")

    allowed_fields = {
        "source_type",
        "collection_method",
        "raw_sha256",
        "discovery_run_id",
        "discovery_candidate_id",
        "trusted_source_id",
        "trusted_source_type",
    }
    unexpected_fields = set(value) - allowed_fields
    if unexpected_fields:
        raise ValueError(f"capture_metadata包含不允许的字段：{', '.join(sorted(unexpected_fields))}")

    source_type = _canonical(
        value.get("source_type"),
        "capture_metadata.source_type",
        SOURCE_TYPES,
    )
    collection_method = _canonical(
        value.get("collection_method"),
        "capture_metadata.collection_method",
        COLLECTION_METHODS,
    )
    raw_sha256 = _required_text(value.get("raw_sha256"), "capture_metadata.raw_sha256", max_length=64)
    if len(raw_sha256) != 64 or any(character not in "0123456789abcdef" for character in raw_sha256.casefold()):
        raise ValueError("capture_metadata.raw_sha256必须是64位十六进制SHA-256值")

    discovery_run_id = _required_text(
        value.get("discovery_run_id"),
        "capture_metadata.discovery_run_id",
        max_length=100,
    )
    discovery_candidate_id = _required_text(
        value.get("discovery_candidate_id"),
        "capture_metadata.discovery_candidate_id",
        max_length=100,
    )
    metadata = {
        "source_type": source_type,
        "collection_method": collection_method,
        "collected_at": collected_at,
        "raw_sha256": raw_sha256.casefold(),
        "discovery_run_id": discovery_run_id,
        "discovery_candidate_id": discovery_candidate_id,
    }
    trusted_source_id = value.get("trusted_source_id")
    trusted_source_type = value.get("trusted_source_type")
    if source_type == "automated_trusted_catalog_capture":
        metadata["trusted_source_id"] = _required_text(
            trusted_source_id,
            "capture_metadata.trusted_source_id",
            max_length=100,
        )
        metadata["trusted_source_type"] = _required_text(
            trusted_source_type,
            "capture_metadata.trusted_source_type",
            max_length=100,
        )
    elif trusted_source_id is not None or trusted_source_type is not None:
        raise ValueError("仅可信来源目录抓取可保存trusted_source字段")
    return metadata


def _fingerprint(prefix: str, payload: dict[str, object]) -> str:
    """基于稳定字段生成短 ID，使重复登记能被可靠识别。"""
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _append_status_history(
    lead: dict[str, Any],
    *,
    state: str,
    reason: str,
    reviewer_user_id: str | None = None,
) -> None:
    """为每次状态变化写入审计记录，不覆盖此前的人工判断。"""
    event: dict[str, str] = {"state": state, "reason": reason, "at": _utc_now()}
    if reviewer_user_id:
        event["reviewer_user_id"] = reviewer_user_id
    lead.setdefault("status_history", []).append(event)


def _risk_signal_from_source_text(
    *,
    entity_name: str,
    source_title: str,
    excerpt: str,
) -> dict[str, str] | None:
    """从标题和原始摘录中找出可明确归属于目标主体的固定风险词。

    输入是服务器已核验将要入证的主体名称、标题与原始摘录，输出最高风险等级对应的类别、
    等级、原因和命中词，或没有明确主体对应关系时返回 ``None``。函数只做同句文字匹配：
    它不把“本公司”、代词、行业评论或模型概括当作目标企业风险，也不访问网络、写入台账
    或决定是否标红；来源等级和质量门槛由调用方在此基础上另行执行。
    """
    normalized_entity_name = entity_name.casefold()
    sentences = _source_sentences(source_title=source_title, excerpt=excerpt)
    matched_alerts: list[dict[str, str]] = []
    for rule in RISK_ALERT_TERM_RULES:
        critical_terms = rule["critical_terms"]
        high_terms = rule["high_terms"]
        if not isinstance(critical_terms, tuple) or not isinstance(high_terms, tuple):
            raise RuntimeError("企业风险预警词表配置格式异常")
        for risk_level, terms in (("critical", critical_terms), ("high", high_terms)):
            for matched_term in terms:
                if not isinstance(matched_term, str):
                    continue
                normalized_term = matched_term.casefold()
                if not any(
                    normalized_entity_name in sentence and normalized_term in sentence
                    for sentence in sentences
                ):
                    continue
                risk_type = rule["risk_type"]
                label = rule["label"]
                if not isinstance(risk_type, str) or not isinstance(label, str):
                    raise RuntimeError("企业风险预警词表缺少标准字段")
                matched_alerts.append(
                    {
                        "risk_type": risk_type,
                        "risk_level": risk_level,
                        "risk_reason": f"{label}：原始来源明确出现“{matched_term}”",
                        "matched_term": matched_term,
                    }
                )
    if not matched_alerts:
        return None
    # 同一条公告可能同时出现“资金链紧张”和“停产”等多个风险；固定选择等级最高、词表
    # 顺序最靠前的一项作为当前主预警，其他原始风险词仍留在不可变摘录中供人工复核。
    return max(
        matched_alerts,
        key=lambda alert: RISK_ALERT_LEVEL_RANK[alert["risk_level"]],
    )


def _source_sentences(*, source_title: str, excerpt: str) -> tuple[str, ...]:
    """把服务器保存的标题和摘录拆成可做主体归属校验的句子。

    输入只能是即将签名保存的来源标题和原始摘录，输出为已小写化且去空的句子元组。
    分类与风险识别必须在同一句中同时找到目标主体和固定词，不能跨句拼接“企业名称”与
    “融资/合作/风险”等词；这降低了行业背景、同篇其他主体或模型摘要被误归属的风险。
    本函数不使用标签、模型结论或网络数据，也不读写台账和证据链。
    """
    return tuple(
        sentence.strip()
        for sentence in re.split(r"[。！？；\n]+", f"{source_title}\n{excerpt}".casefold())
        if sentence.strip()
    )


def classify_claim_type_from_source_text(
    *,
    requested_claim_type: object,
    entity_name: str,
    source_title: str,
    excerpt: str,
) -> dict[str, object]:
    """将调用方请求的类别降级为可审查的服务器原文分类，不把主观标签写成事实。

    输入为调用方请求类别和已受控抓取的主体、标题、摘录；输出为实际保存的 ``claim_type``、
    原请求类别、固定命中词、分类状态和方法。若原文出现可归属于该主体的固定风险词，服务端
    必须覆盖请求分类为 ``risk``；否则只在请求分类确有固定字面支持时保留它，不能支持时
    写为 ``unclassified``。正向类别同样要求目标主体与固定词同句出现，不能把不同段落、
    其他主体或行业背景词拼成事实。这个函数不使用模型判断、不读取外部页面、不写台账，
    因此前端或模型无法靠自称“融资/合作/风险”使下游自动把推测当成事实。
    """
    normalized_requested_type = _canonical(
        requested_claim_type,
        "claim_type",
        REQUESTABLE_CLAIM_TYPES,
        CLAIM_TYPE_ALIASES,
    )
    risk_signal = _risk_signal_from_source_text(
        entity_name=entity_name,
        source_title=source_title,
        excerpt=excerpt,
    )
    if risk_signal is not None:
        return {
            "claim_type": "risk",
            "claim_type_requested": normalized_requested_type,
            "claim_type_matched_terms": [risk_signal["matched_term"]],
            "claim_type_text_support": "server_reclassified_risk",
            "claim_type_classification_method": CLAIM_TYPE_TEXT_SUPPORT_METHOD,
        }
    normalized_entity_name = entity_name.casefold()
    sentences = _source_sentences(source_title=source_title, excerpt=excerpt)
    terms = CLAIM_TYPE_TEXT_SUPPORT_TERMS.get(normalized_requested_type)
    # ``risk`` 不能只因页面任意位置出现负面词便保留：必须先通过上方“目标主体同句”
    # 规则，未通过时安全归入未分类，而不是给其他企业的风险贴到当前实体上。
    if normalized_requested_type == "risk":
        terms = ()
    if terms is None:
        raise RuntimeError("事实类别固定词表配置缺少请求类别")
    # 正向归档也必须满足“主体名称 + 类别词同句”。若来源只提到行业融资、他方合作或
    # 泛泛徐州背景，即使调用方给出了醒目的类别标签，也只能保守保留为待人工分类。
    matched_terms = [
        term
        for term in terms
        if any(
            normalized_entity_name in sentence and term.casefold() in sentence
            for sentence in sentences
        )
    ]
    if matched_terms:
        return {
            "claim_type": normalized_requested_type,
            "claim_type_requested": normalized_requested_type,
            "claim_type_matched_terms": matched_terms,
            "claim_type_text_support": "matched_fixed_terms",
            "claim_type_classification_method": CLAIM_TYPE_TEXT_SUPPORT_METHOD,
        }
    return {
        "claim_type": "unclassified",
        "claim_type_requested": normalized_requested_type,
        "claim_type_matched_terms": [],
        "claim_type_text_support": "unclassified_no_fixed_term_match",
        "claim_type_classification_method": CLAIM_TYPE_TEXT_SUPPORT_METHOD,
    }


def has_open_enterprise_risk_alert(lead: dict[str, Any]) -> bool:
    """判断线索是否存在当前仍需人工处理的结构化企业风险预警。

    输入是一条已从台账读取的线索，输出仅为布尔值。只有 ``core.py`` 依照正式一手风险
    证据写入的红色标记、风险等级、风险类型、未关闭处理状态和至少一个触发证据 ID 同时
    完整时才返回 ``True``；旧版只写 ``red_alert=True`` 的融资/扩产机会记录一律返回
    ``False``。函数不补齐、纠正或写回历史台账，因此报表、图谱和行动中心可安全读取旧
    数据而不把过时语义误报为企业异常，也不会改动证据链或人工复核状态。
    """
    risk_evidence_ids = lead.get("risk_alert_evidence_ids")
    return (
        lead.get("red_alert") is True
        and lead.get("risk_alert_level") in RISK_ALERT_LEVEL_RANK
        and isinstance(lead.get("risk_alert_type"), str)
        and bool(lead.get("risk_alert_type"))
        and lead.get("risk_alert_status") in {"open", "need_evidence", "observe"}
        and isinstance(risk_evidence_ids, list)
        and bool(risk_evidence_ids)
        and all(isinstance(evidence_id, str) and evidence_id for evidence_id in risk_evidence_ids)
    )


def _risk_alert_from_evidence(
    *,
    entity_name: str,
    source_level: str,
    evidence_quality: int,
    source_title: str,
    excerpt: str,
) -> dict[str, str] | None:
    """从一条正式证据中严格识别可进入红色队列的企业风险，不把招商机会误报为风险。

    输入是已经由服务器抓取、哈希校验且准备签名的主体名称、来源等级、质量、事实类别、
    标题和原始摘录；输出为 ``risk_type``、``risk_level`` 与可审查原因，或在证据不足时
    返回 ``None``。只有一手来源、质量至少 4，且主体名称与受控风险词出现在同一句原文中
    时才创建预警：``critical`` 表示持续经营/重大合规风险，
    ``high`` 表示需要尽快人工决定去留的明确异常。这样不会因一篇“甲公司评论乙公司停产”
    的报道误把甲公司标红；无法确认指向本企业的代词（如“本公司”）宁可待人工补证。
    融资、扩产、合作、徐州关联等机会信号永远不会从这里飘红；弱猜测、单纯亏损或模型
    自称的风险也不会通过。函数不读写台账、不联网、不改证据或人工状态。
    """
    if source_level != "primary" or evidence_quality < 4:
        return None
    return _risk_signal_from_source_text(
        entity_name=entity_name,
        source_title=source_title,
        excerpt=excerpt,
    )


def _verified_raw_snapshot(
    raw_content: object,
    *,
    expected_raw_sha256: str,
) -> tuple[bytes, str]:
    """校验受控抓取的原始字节，并生成可写入不可变快照的 Base64 文本。

    输入是仅由服务器抓取器交给核心流程的原始字节及其已登记的 SHA-256；输出为经核验的
    字节和 Base64 表示。函数拒绝模型、前端常见的文本、空内容和超限响应，并以实际哈希
    比对元数据，防止摘录、URL 和原文来自不同页面；它不联网、不写台账，也不允许复核
    接口调用。
    """
    if not isinstance(raw_content, bytes) or not raw_content:
        raise ValueError("正式证据必须包含服务器抓取的原始响应字节")
    if len(raw_content) > MAX_RAW_EVIDENCE_BYTES:
        raise ValueError("正式证据原始响应超过允许的 1 MiB 上限")
    actual_raw_sha256 = hashlib.sha256(raw_content).hexdigest()
    if actual_raw_sha256 != expected_raw_sha256:
        raise ValueError("正式证据原始响应哈希与服务器采集元数据不一致")
    return raw_content, base64.b64encode(raw_content).decode("ascii")


def record_public_evidence(
    workspace_dir: Path,
    *,
    entity_name: str,
    entity_kind: str,
    sector: str,
    source_url: str,
    source_title: str,
    source_published_at: str,
    source_level: str,
    claim_type: str,
    evidence_quality: int,
    excerpt: str,
    xuzhou_association_tags: list[str] | None = None,
    capture_metadata: dict[str, object] | None = None,
    raw_content: bytes | None = None,
) -> dict[str, Any]:
    """登记一条已获得的公开证据，并仅在严格风险阈值满足时创建待复核红色预警。

    输入是已核验的来源事实、内部 ``capture_metadata`` 和仅由服务器抓取器传入的原始字节；
    后两者的 SHA-256 必须一致，缺少任一项的手工文本会被拒绝。输出为新增或去重后的线索
    与证据。本函数先把原文写入不可覆盖的私有快照，再把快照路径和哈希纳入证据 HMAC；
    因此人工复核无法根据新页面覆盖已有 URL、原始摘录、来源日期、采集时间、原文或评分
    依据。调用方的 ``claim_type`` 会先被服务器用原文固定词表核验，无法支持时只保存为
    ``unclassified``；主体同句命中风险词的一手高质量来源可被服务器重分类为 ``risk`` 并
    写入企业风险预警。融资、扩产、合作和徐州关联等正向招商机会仍只进入人工研判。自动
    搜索结果默认以低证据等级入库，不能因此直接制造高意愿结论或企业风险事实。
    """
    normalized_entity_name = _required_text(entity_name, "entity_name")
    normalized_entity_kind = _canonical(entity_kind, "entity_kind", ENTITY_KINDS, ENTITY_KIND_ALIASES)
    normalized_sector = _canonical(sector, "sector", SECTORS, SECTOR_ALIASES)
    normalized_source_url = _validate_http_url(source_url)
    normalized_source_title = _required_text(source_title, "source_title")
    normalized_source_date = _validate_source_date(source_published_at)
    normalized_source_level = _canonical(source_level, "source_level", SOURCE_LEVELS, SOURCE_LEVEL_ALIASES)
    normalized_excerpt = _required_text(excerpt, "excerpt", max_length=2_000)
    normalized_tags = _normalize_xuzhou_tags(xuzhou_association_tags)
    claim_classification = classify_claim_type_from_source_text(
        requested_claim_type=claim_type,
        entity_name=normalized_entity_name,
        source_title=normalized_source_title,
        excerpt=normalized_excerpt,
    )
    normalized_claim_type = claim_classification.get("claim_type")
    if normalized_claim_type not in CLAIM_TYPES:
        raise RuntimeError("服务器事实分类返回了不受支持的类别")
    if claim_classification.get("claim_type_text_support") not in CLAIM_TYPE_TEXT_SUPPORT_STATUSES:
        raise RuntimeError("服务器事实分类缺少可审查状态")

    if not isinstance(evidence_quality, int) or isinstance(evidence_quality, bool) or not 1 <= evidence_quality <= 5:
        raise ValueError("evidence_quality必须是1到5之间的整数")

    now = _utc_now()
    normalized_capture_metadata = _normalize_capture_metadata(
        capture_metadata,
        collected_at=now,
    )
    verified_raw_content, encoded_raw_content = _verified_raw_snapshot(
        raw_content,
        expected_raw_sha256=normalized_capture_metadata["raw_sha256"],
    )
    lead_id = _fingerprint(
        "lead",
        {"entity_kind": normalized_entity_kind, "entity_name": normalized_entity_name.casefold()},
    )
    evidence_id = _fingerprint(
        "evidence",
        {
            "lead_id": lead_id,
            "source_url": normalized_source_url,
            "source_title": normalized_source_title,
            "source_published_at": normalized_source_date,
            "excerpt": normalized_excerpt,
            "raw_sha256": normalized_capture_metadata.get("raw_sha256"),
        },
    )

    ledger = load_ledger(workspace_dir)
    leads: list[dict[str, Any]] = ledger["leads"]
    evidence_records: list[dict[str, Any]] = ledger["evidence"]
    lead = next((item for item in leads if item["lead_id"] == lead_id), None)

    if lead is None:
        lead = {
            "lead_id": lead_id,
            "entity_name": normalized_entity_name,
            "entity_kind": normalized_entity_kind,
            "sector": normalized_sector,
            "review_state": "pending_user_review",
            "red_alert": False,
            "alert_reason": None,
            # 风险字段只记录由正式 evidence 触发的企业异常；招商机会不会写入这些字段。
            # 历史风险依据 ID 永远保留，即使人工决定继续推进或关闭预警，也可回查原始证据。
            "risk_alert_level": None,
            "risk_alert_type": None,
            "risk_alert_status": None,
            "risk_alert_evidence_ids": [],
            "evidence_ids": [],
            "status_history": [],
            "created_at": now,
            "updated_at": now,
        }
        _append_status_history(
            lead,
            state="pending_user_review",
            reason="首次登记公开证据，等待用户复核",
        )
        leads.append(lead)
    else:
        # 兼容旧版线索：旧版 ``red_alert`` 曾表示机会待办，不能据此补造企业风险。这里只
        # 补齐新版空字段，后续只有新的、已签名风险证据才能写入等级、类型和触发引用。
        lead.setdefault("risk_alert_level", None)
        lead.setdefault("risk_alert_type", None)
        lead.setdefault("risk_alert_status", None)
        lead.setdefault("risk_alert_evidence_ids", [])
        # 历史版本曾将正向机会也写成 ``red_alert``。这不是原始证据字段，且新版没有结构化
        # 风险依据时只能安全降为非红色；保留既有 evidence 和 status_history，不补造风险。
        if lead.get("red_alert") is True and not has_open_enterprise_risk_alert(lead):
            lead["red_alert"] = False
            lead["alert_reason"] = None

    existing_evidence = next((item for item in evidence_records if item["evidence_id"] == evidence_id), None)
    if existing_evidence is not None:
        return {
            "created": False,
            "message": "相同公开证据已经登记，未重复写入台账",
            "lead": lead,
            "evidence": existing_evidence,
            "ledger_path": str(ledger_path(workspace_dir)),
        }

    evidence = {
        "evidence_id": evidence_id,
        "lead_id": lead_id,
        "source_url": normalized_source_url,
        "source_title": normalized_source_title,
        "source_published_at": normalized_source_date,
        "source_level": normalized_source_level,
        "claim_type": normalized_claim_type,
        # 请求类别和服务器原文字面分类同时签名保存，使报告能明确区分“页面支持的分类”
        # 与“未获得固定词支持的调用方请求”，后续人工不能改写其中任何一个字段。
        "claim_type_requested": claim_classification["claim_type_requested"],
        "claim_type_matched_terms": claim_classification["claim_type_matched_terms"],
        "claim_type_text_support": claim_classification["claim_type_text_support"],
        "claim_type_classification_method": claim_classification["claim_type_classification_method"],
        "evidence_quality": evidence_quality,
        "excerpt": normalized_excerpt,
        "xuzhou_association_tags": normalized_tags,
        "created_at": now,
        **normalized_capture_metadata,
    }
    snapshot_path = evidence_snapshot_path(workspace_dir, evidence_id)
    evidence["raw_snapshot_relative_path"] = str(snapshot_path.relative_to(workspace_dir))
    evidence["raw_snapshot_byte_size"] = len(verified_raw_content)
    # 只在服务器受控抓取已经规范化完所有原始字段后签名；调用方无权提交签名，也不能
    # 通过人工复核或后续功能重签旧证据，因此来源 URL、摘录、采集时间和评分依据可被
    # 整条链校验。函数仅更新新证据和链头元数据，不覆盖任何历史 evidence。
    sign_next_evidence(workspace_dir, ledger, evidence)
    snapshot_created = False
    try:
        save_evidence_snapshot(
            workspace_dir,
            evidence_id=evidence_id,
            payload={
                "evidence_id": evidence_id,
                "source_url": normalized_source_url,
                "collected_at": now,
                "raw_sha256": normalized_capture_metadata["raw_sha256"],
                "raw_content_base64": encoded_raw_content,
            },
        )
        snapshot_created = True
        evidence_records.append(evidence)
        lead["evidence_ids"].append(evidence_id)
        lead["updated_at"] = now

        risk_alert = _risk_alert_from_evidence(
            entity_name=normalized_entity_name,
            source_level=normalized_source_level,
            evidence_quality=evidence_quality,
            source_title=normalized_source_title,
            excerpt=normalized_excerpt,
        )
        if risk_alert is not None and lead["review_state"] != "invalid":
            risk_evidence_ids = lead.get("risk_alert_evidence_ids")
            if not isinstance(risk_evidence_ids, list) or not all(
                isinstance(item, str) for item in risk_evidence_ids
            ):
                raise RuntimeError("招商线索风险预警证据引用格式异常")
            if evidence_id not in risk_evidence_ids:
                risk_evidence_ids.append(evidence_id)
            previous_level = lead.get("risk_alert_level")
            previous_rank = RISK_ALERT_LEVEL_RANK.get(previous_level, 0)
            new_level = risk_alert["risk_level"]
            if new_level not in RISK_ALERT_LEVEL_RANK:
                raise RuntimeError("企业风险预警等级不受支持")
            lead["red_alert"] = True
            lead["risk_alert_status"] = "open"
            if RISK_ALERT_LEVEL_RANK[new_level] >= previous_rank:
                lead["risk_alert_level"] = new_level
                lead["risk_alert_type"] = risk_alert["risk_type"]
                lead["alert_reason"] = risk_alert["risk_reason"]
            if lead["review_state"] != "pending_user_review":
                lead["review_state"] = "pending_user_review"
                _append_status_history(
                    lead,
                    state="pending_user_review",
                    reason=str(lead["alert_reason"]),
                )

        saved_path = save_ledger(workspace_dir, ledger)
    except Exception:
        if snapshot_created and snapshot_path.exists() and snapshot_path.is_file() and not snapshot_path.is_symlink():
            snapshot_path.unlink()
        raise
    return {"created": True, "lead": lead, "evidence": evidence, "ledger_path": str(saved_path)}


def list_investment_leads(
    workspace_dir: Path,
    *,
    sector: str | None = None,
    review_state: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """按产业或人工复核状态读取台账线索，不触发采集和评分。"""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("limit必须是1到100之间的整数")

    normalized_sector = _canonical(sector, "sector", SECTORS, SECTOR_ALIASES) if sector else None
    normalized_review_state = (
        _canonical(review_state, "review_state", REVIEW_STATES, REVIEW_STATE_ALIASES)
        if review_state
        else None
    )

    ledger = load_ledger(workspace_dir)
    leads = ledger["leads"]
    if normalized_sector:
        leads = [lead for lead in leads if lead["sector"] == normalized_sector]
    if normalized_review_state:
        leads = [lead for lead in leads if lead["review_state"] == normalized_review_state]

    ordered_leads = sorted(leads, key=lambda lead: lead["updated_at"], reverse=True)[:limit]
    return {
        "leads": ordered_leads,
        "count": len(ordered_leads),
        "ledger_path": str(ledger_path(workspace_dir)),
    }


def get_investment_lead(workspace_dir: Path, *, lead_id: str) -> dict[str, Any]:
    """读取一条线索及其可追溯入账材料链，并明确标识服务器或人工来源。"""
    normalized_lead_id = _required_text(lead_id, "lead_id", max_length=100)
    ledger = load_ledger(workspace_dir)
    lead = next((item for item in ledger["leads"] if item["lead_id"] == normalized_lead_id), None)
    if lead is None:
        raise ValueError("未找到对应的招商线索")

    evidence_ids = set(lead["evidence_ids"])
    evidence = [item for item in ledger["evidence"] if item["evidence_id"] in evidence_ids]
    evidence.sort(key=lambda item: item["source_published_at"], reverse=True)
    return {"lead": lead, "evidence": evidence, "ledger_path": str(ledger_path(workspace_dir))}


def review_investment_lead(
    workspace_dir: Path,
    *,
    lead_id: str,
    decision: str,
    review_note: str,
    reviewer_user_id: str,
) -> dict[str, Any]:
    """由已登录用户完成招商线索复核，并保留不可覆盖的业务审计记录。

    输入为线索 ID、复核结论、复核备注，以及仅能由服务器认证层从当前登录主体
    取得的 ``reviewer_user_id``；输出为更新后的线索和台账路径。此处不以管理员
    身份作为业务门槛，普通已登录用户可复核。调用方不得把浏览器或模型传来的
    用户 ID 原样传入，否则会伪造审计操作者。复核只允许改变业务状态、预警字段和
    状态历史；写入前后都会校验证据记录及线索证据关联未变，防止来源、摘录、时间
    或评分依据被人工判断覆盖。
    """
    normalized_lead_id = _required_text(lead_id, "lead_id", max_length=100)
    normalized_decision = _canonical(
        decision,
        "decision",
        MANUAL_REVIEW_STATES,
        REVIEW_STATE_ALIASES,
    )
    # “推进”不再是一个只写备注即可触发的泛化状态。它会被业务人员理解为已经值得
    # 招商跟进，因而必须经过独立的招商跟进审批卡：确认正式证据、产业/地域匹配、风险
    # 处理、负责人和下一步。此处保留待补证、观察和不纳入等日常处置，避免旧按钮绕过
    # 那张可审计的人工判断卡。
    if normalized_decision == "advance":
        raise ValueError("“推进”必须通过“招商跟进审批卡”完成，不能只填写一条泛化复核备注")
    normalized_note = _required_text(review_note, "review_note", max_length=2_000)
    normalized_reviewer_user_id = _required_text(reviewer_user_id, "reviewer_user_id", max_length=100)

    ledger = load_ledger(workspace_dir)
    lead = next((item for item in ledger["leads"] if item["lead_id"] == normalized_lead_id), None)
    if lead is None:
        raise ValueError("未找到对应的招商线索")

    evidence_snapshot = json.dumps(ledger["evidence"], ensure_ascii=False, sort_keys=True)
    evidence_ids_snapshot = json.dumps(lead["evidence_ids"], ensure_ascii=False)
    previous_state = lead["review_state"]
    reviewed_at = _utc_now()
    lead["review_state"] = normalized_decision
    # 风险待补证或观察时必须继续保留在风险队列，不能因为“已经看过一次”就静默消失。
    # 选择推进或无效才关闭红色风险提示；触发原因和证据 ID 仍保留以供后续追溯。
    if has_open_enterprise_risk_alert(lead):
        risk_status = RISK_ALERT_REVIEW_STATUS_BY_DECISION[normalized_decision]
        lead["risk_alert_status"] = risk_status
        lead["red_alert"] = risk_status != "closed"
    elif lead.get("red_alert") is True:
        # 复核到旧版“机会被标红”线索时，同步清除过时展示标记；这只改业务展示字段，
        # 不删除原始 evidence、来源 URL、摘录、时间、评分或此前状态审计。
        lead["red_alert"] = False
        lead["alert_reason"] = None
    lead["updated_at"] = reviewed_at
    lead.setdefault("status_history", []).append(
        {
            "state": normalized_decision,
            "reason": normalized_note,
            "old_state": previous_state,
            "new_state": normalized_decision,
            "review_note": normalized_note,
            "reviewer_user_id": normalized_reviewer_user_id,
            "operation_scope": "business_review_status_only",
            "at": reviewed_at,
        }
    )
    if (
        evidence_snapshot != json.dumps(ledger["evidence"], ensure_ascii=False, sort_keys=True)
        or evidence_ids_snapshot != json.dumps(lead["evidence_ids"], ensure_ascii=False)
    ):
        raise RuntimeError("人工复核不得修改原始证据链")
    saved_path = save_ledger(workspace_dir, ledger)
    return {"lead": lead, "ledger_path": str(saved_path)}
