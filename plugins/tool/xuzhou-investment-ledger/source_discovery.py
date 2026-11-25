"""徐州招商公开来源的低频自动发现与受控证据采集。"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .core import classify_claim_type_from_source_text, record_public_evidence
from .source_catalog import classify_captured_source
from .source_capture import (
    fetch_public_search_feed,
    fetch_public_source,
    fetch_public_source_for_observation,
)
from .source_feed_discovery import check_robots_access
from .source_monitoring import (
    SEARCH_FEED_SOURCE_KEY,
    apply_source_health_outcome,
    is_source_eligible,
    source_health_key_for_url,
)
from .source_recipe_governance import get_active_source_recipe_rule_set
from .storage import ledger_path, load_ledger, save_ledger


MAX_QUERIES_PER_RUN = 12
MAX_RESULTS_PER_QUERY = 10
MAX_CAPTURE_ERROR_LENGTH = 500
MAX_INSPECTION_VISIBLE_TEXT_CHARS = 12_000
# 搜索引擎摘要只是“可能值得读取”的索引，不能证明页面对应中国企业、更不能证明企业
# 有投资徐州的意愿。这个门槛只决定是否值得占用一次受控页面观察，不创建任何事实。
PUBLIC_DISCOVERY_ELIGIBILITY_GATE_VERSION = "china_xuzhou_business_opportunity_gate_v1"
MAX_DISCOVERY_REJECTION_AUDIT_PER_RUN = 50
# 这些是云端拒绝审计已实测出现、且用途可由域名本身明确识别的站点：百科/词典、
# 酒店旅游、登录认证或企业后台入口。名单只使用完整主机名，绝不按国家、公司名称、
# 行业或 URL 中的普通词汇推断“无关”；普通企业官网和新闻页不在此处排除。
PUBLIC_DISCOVERY_SOURCE_PREFILTER_VERSION = "fixed_nonbusiness_source_pre_filter_v1"
PUBLIC_DISCOVERY_EXCLUDED_HOSTNAMES = frozenset(
    {
        "auth.alipay.com",
        "baike.baidu.com",
        "enterpriseportal.alipay.com",
        "species.sciencereading.cn",
        "www.hanyuguoxue.com",
        "www.jalan.net",
    }
)
CHINESE_CHARACTER_PATTERN = re.compile(r"[\u3400-\u9fff]")
CHINA_CONTEXT_TERMS = (
    "中国", "境内", "国内", "江苏", "徐州", "china", "jiangsu", "xuzhou",
)
XUZHOU_CONTEXT_TERMS = (
    "徐州", "徐州人", "祖籍徐州", "籍贯徐州", "回乡创业", "家乡建设", "xuzhou",
)
BUSINESS_SUBJECT_TERMS = (
    "公司", "企业", "集团", "基金", "创始人", "创始团队", "创业团队", "投资机构",
    "有限公司", "股份有限公司", "有限合伙", "管理人", "company", "corporation", "group",
    "fund", "startup", "venture", "limited", "inc.",
)
OPPORTUNITY_SIGNAL_TERMS = (
    "融资", "募资", "获投", "扩产", "扩建", "新建", "招商", "落地", "选址",
    "项目签约", "战略合作", "产业合作", "寻找合作伙伴", "回乡创业", "家乡建设",
    # 仅补充可从标题直接判断的企业资本/设立动作，不把泛化的“投资”单字当成机会信号：
    # ``投资成立/设立``、``增资`` 与 ``共同出资`` 仍须同时命中企业或基金等组织主体，
    # 且没有中国/徐州原文依据时只能进入受限待补证队列，不能直接写 evidence。
    "项目投资", "投资成立", "投资设立", "增资", "设立新公司", "新设公司", "共同出资",
    "新增产线", "投产", "开工", "financing", "fundraising", "investment",
    "expansion", "partnership", "site selection",
)

# 保留产业总称，确保未列出的细分方向仍可由宽查询覆盖；细分词只是提升早期信号命中率。
SECTOR_DISCOVERY_CONFIG: dict[str, dict[str, object]] = {
    "embodied_intelligence": {
        "label": "具身智能",
        "aliases": {"具身智能", "embodied_intelligence"},
        "subdirections": [
            "人形机器人",
            "工业机器人",
            "灵巧手",
            "机器人视觉",
            "具身大模型",
            "运动控制",
            "机器人操作系统",
            "仿真",
            "传感器",
        ],
    },
    "semiconductor": {
        "label": "半导体",
        "aliases": {"半导体", "semiconductor"},
        "subdirections": [
            "集成电路",
            "芯片设计",
            "晶圆制造",
            "先进封装",
            "半导体设备",
            "半导体材料",
            "EDA",
            "功率半导体",
            "存储芯片",
            "传感器",
        ],
    },
    "biomedicine": {
        "label": "生物医药",
        "aliases": {"生物医药", "医药", "biomedicine"},
        "subdirections": [
            "创新药",
            "医疗器械",
            "合成生物",
            "细胞与基因治疗",
            "AI制药",
            "体外诊断",
            "CXO",
            "生物制造",
        ],
    },
}

QUERY_RECIPES: tuple[dict[str, str], ...] = (
    {
        "catalog_id": "public_early_signal",
        "label": "公开早期机会与明确风险信号",
        # 风险词只用于发现候选链接，绝不直接生成企业风险事实或预警。词表保持狭窄：
        # 不用“亏损”“裁员”等容易产生歧义的词，只寻找资金链、项目中断、合作终止、
        # 重大合规与核心团队变化等需要人工决定招商去留的明确公开异常。
        "signals": (
            "融资 OR 募资 OR 获投 OR 扩产 OR 新建研发中心 OR 招商 OR 落地 OR "
            "资金链紧张 OR 流动性紧张 OR 融资失败 OR 项目暂停 OR 终止合作 OR "
            "停产 OR 破产重整 OR 行政处罚 OR 核心团队离职"
        ),
    },
    {
        "catalog_id": "xuzhou_connection",
        "label": "徐州关联与家乡建设信号",
        "signals": "徐州 OR 徐州人 OR 祖籍徐州 OR 籍贯徐州 OR 回乡创业 OR 家乡建设",
    },
    {
        "catalog_id": "university_alumni",
        "label": "重点高校校友创业公开信息",
        "signals": "清华校友 OR 北大校友 OR 高校校友 OR 创业 OR 创始人",
    },
    {
        "catalog_id": "cooperation_and_landing",
        "label": "合作与地方资源需求信号",
        "signals": "战略合作 OR 产业合作 OR 寻找合作伙伴 OR 产业园 OR 项目签约 OR 招聘",
    },
    # 经营规模与项目投入只作为可追溯来源的自动发现入口；搜索摘要中的金额、年度和
    # 口径都不能直接入库。后续必须经页面快照、原文摘录和人工复核，才可能进入指标分析。
    {
        "catalog_id": "industry_business_metrics",
        "label": "营业收入、产值与项目投资公开指标",
        "signals": "营业收入 OR 产值 OR 项目总投资 OR 计划投资 OR 完成投资 OR 统计公报",
    },
)


def _utc_now() -> str:
    """返回发现批次、候选状态和证据采集共同使用的 UTC 审计时间。"""
    return datetime.now(timezone.utc).isoformat()


def _required_text(value: object, field_name: str, *, max_length: int) -> str:
    """校验自动发现流程的文本输入，拒绝空白、过长和非字符串值。"""
    if not isinstance(value, str) or not (text := value.strip()):
        raise ValueError(f"{field_name}不能为空")
    if len(text) > max_length:
        raise ValueError(f"{field_name}不能超过{max_length}个字符")
    return text


def _normalize_text(value: str) -> str:
    """统一页面、标题和摘录的空白，便于验证调用方摘录确实存在于抓取正文。"""
    return " ".join(value.split())


def _stable_id(prefix: str, payload: dict[str, object]) -> str:
    """为发现批次和候选页面生成可复现短 ID，便于去重和审计关联。"""
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}-{hashlib.sha256(serialized.encode('utf-8')).hexdigest()[:16]}"


def _normalize_sector(value: object) -> str:
    """把中文或英文产业输入归一化为受控发现配置，不接受未定义行业。"""
    raw_sector = _required_text(value, "sector", max_length=100).casefold()
    for sector, config in SECTOR_DISCOVERY_CONFIG.items():
        aliases = config["aliases"]
        if isinstance(aliases, set) and raw_sector in {alias.casefold() for alias in aliases}:
            return sector
    accepted = "、".join(config["label"] for config in SECTOR_DISCOVERY_CONFIG.values())
    raise ValueError(f"sector不合法，可选值为：{accepted}")


def _validate_run_limit(value: object, field_name: str, *, maximum: int) -> int:
    """限制单次发现的查询和结果数量，避免免费公开搜索被批量滥用。"""
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
        raise ValueError(f"{field_name}必须是1到{maximum}之间的整数")
    return value


def build_discovery_queries(
    sector: str,
    *,
    max_queries: int = 4,
    recipe_order: list[str] | None = None,
) -> list[dict[str, str]]:
    """为指定产业构造有限的服务器侧公开搜索任务。

    输入是产业、查询上限和仅能由服务器来源规则提供的完整配方顺序，输出包含查询文本、
    来源目录标识和说明的任务列表。顺序为空时使用源码基线；非空顺序必须完整重排全部
    固定配方，不能删减产业入口或注入 URL/关键词。每条查询固定带“中国、企业、产业
    总称”，再按日期轮换一个代表性细分方向和一个配方信号。这样既保留产业总称以覆盖未
    列出的子赛道，又避免把十余个细分词和信号词用 ``OR`` 混成宽泛搜索，导致百科、登录
    页或境外无关内容挤占免费 RSS 结果。日期轮换只改变未来的搜索入口，不修改历史台账、
    原始证据或人工结论；“中国、企业”等查询词也不是企业事实，仍必须由后续标题、摘要、
    页面快照和人工复核分别核验。
    """
    normalized_sector = _normalize_sector(sector)
    normalized_max_queries = _validate_run_limit(
        max_queries,
        "max_queries",
        maximum=MAX_QUERIES_PER_RUN,
    )
    config = SECTOR_DISCOVERY_CONFIG[normalized_sector]
    label = config["label"]
    subdirections = config["subdirections"]
    if not isinstance(label, str) or not isinstance(subdirections, list):
        raise RuntimeError("产业发现配置格式异常")
    # RSS 搜索将空格理解为共同检索条件。与原来的大型 OR 表达式相比，短查询更可能
    # 返回同时包含中国企业、产业和机会线索的页面；资格门槛仍独立检查搜索摘要，不能
    # 因查询文本含有“中国”或“企业”就自动放行候选。
    # 产业总称已在每条查询中固定出现；轮换列表只保留细分方向，确保每天每条查询
    # 都能覆盖一个具体方向，同时不会把未列出的新方向排除在产业总称之外。
    sector_terms = list(subdirections)
    daily_rotation = date.today().toordinal()

    recipe_by_id = {recipe["catalog_id"]: recipe for recipe in QUERY_RECIPES}
    if recipe_order is None:
        ordered_recipe_ids = list(recipe_by_id)
    else:
        if not isinstance(recipe_order, list) or not all(
            isinstance(recipe_id, str) and recipe_id for recipe_id in recipe_order
        ):
            raise ValueError("recipe_order必须是完整的固定来源配方列表")
        if len(recipe_order) != len(recipe_by_id) or set(recipe_order) != set(recipe_by_id):
            raise ValueError("recipe_order必须完整且仅包含系统固定来源配方")
        ordered_recipe_ids = list(recipe_order)

    queries: list[dict[str, str]] = []
    for recipe_position, recipe_id in enumerate(
        ordered_recipe_ids[:normalized_max_queries]
    ):
        recipe = recipe_by_id[recipe_id]
        signal_terms = [
            signal.strip()
            for signal in recipe["signals"].split(" OR ")
            if signal.strip()
        ]
        if not signal_terms:
            raise RuntimeError(f"来源配方{recipe_id}缺少有效搜索信号")

        # 每日轮换细分产业方向而不是一次查询塞入所有词；每种固定来源配方则始终使用其
        # 首个受控信号词，确保人工批准后的“配方顺序”在每次执行中可重现、可审计。该
        # 轮换只用于低频公开发现，绝不影响招商评分、预警等级或已采集证据；人工也不能
        # 借此注入任意搜索词或访问范围。
        sector_term = sector_terms[(daily_rotation + recipe_position) % len(sector_terms)]
        signal_term = signal_terms[0]
        query_text = f"中国 企业 {label} {sector_term} {signal_term}"
        queries.append(
            {
                "sector": normalized_sector,
                "source_catalog_id": recipe["catalog_id"],
                "source_catalog_label": recipe["label"],
                "query": query_text[:500],
            }
        )
    return queries


def _validate_discovered_url(value: object) -> str:
    """校验搜索结果链接的基本公开格式，实际访问时仍由抓取器执行 DNS 安全校验。"""
    source_url = _required_text(value, "搜索结果链接", max_length=2_000)
    parsed = urlparse(source_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("搜索结果链接必须是完整的公开HTTP或HTTPS地址")
    if parsed.username or parsed.password:
        raise ValueError("搜索结果链接不能包含用户名或密码")
    return parsed.geturl()


def _matched_terms(text: str, terms: tuple[str, ...]) -> list[str]:
    """返回搜索标题/摘要中实际出现的固定词，不把词命中解释成企业事实。"""
    normalized_text = text.casefold()
    return [term for term in terms if term.casefold() in normalized_text]


def _has_sector_context_in_search_results(
    search_results: list[dict[str, str]],
    *,
    sector: str,
) -> bool:
    """确认 RSS 响应至少含有目标产业词，避免固定泛结果被误记为成功搜索。

    输入是一次公开 RSS 查询返回的标题、摘要和受控产业标识；输出只表示索引响应是否对
    当前产业有最基本的文本相关性。它不联网、不写台账、不认定任何企业事实，更不替代
    中国/徐州、组织主体和机会信号资格门槛。若 Bing 等搜索源无视查询词而反复返回“百科
    /政府首页”等固定泛结果，函数会使调用方将该请求记为来源质量失败并进入退避，而不是
    继续消耗候选审核预算或把监控显示为正常完成。
    """
    config = SECTOR_DISCOVERY_CONFIG.get(sector)
    if not isinstance(config, dict):
        raise RuntimeError("产业搜索质量检查配置异常")
    label = config.get("label")
    subdirections = config.get("subdirections")
    if not isinstance(label, str) or not isinstance(subdirections, list):
        raise RuntimeError("产业搜索质量检查配置格式异常")
    sector_terms = [label, *[term for term in subdirections if isinstance(term, str)]]
    for item in search_results:
        searchable_text = " ".join(
            [
                str(item.get("source_title", "")),
                str(item.get("search_snippet", "")),
            ]
        ).casefold()
        if any(term.casefold() in searchable_text for term in sector_terms):
            return True
    return False


def assess_fixed_source_prefilter(source_url: str) -> dict[str, object] | None:
    """判断 URL 是否属于固定的非招商内容入口，返回拒绝审计或 ``None``。

    输入是已经完成格式校验的公开 URL；输出仅是“该固定主机名不应消耗搜索候选预算”的
    服务器侧操作判断，或 ``None`` 表示继续通过正式资格门槛。它不联网、不读写台账，
    不对页面、企业、中国业务或投资意愿作任何事实判断。这样登录认证、百科和酒店旅游
    等明确非企业机会入口可在搜索阶段止损，同时任何未列入精确名单的域名仍会交由后续
    中国/徐州、组织主体和机会信号门槛公平评估，避免误伤正常企业官网。
    """
    hostname = (urlparse(source_url).hostname or "").casefold()
    if hostname not in PUBLIC_DISCOVERY_EXCLUDED_HOSTNAMES:
        return None
    return {
        "gate_version": PUBLIC_DISCOVERY_SOURCE_PREFILTER_VERSION,
        "eligible": False,
        "source_hostname": hostname,
        "language_hint": "not_evaluated_due_to_fixed_source_pre_filter",
        "china_context_terms": [],
        "xuzhou_context_terms": [],
        "business_subject_terms": [],
        "opportunity_signal_terms": [],
        "rejection_reasons": ["excluded_known_non_business_or_authentication_source"],
        "decision_reason": "固定主机名属于非招商内容或认证入口，未进入候选资格判断。",
        "operation_scope": "server_side_fixed_source_pre_filter_only",
    }


def assess_public_discovery_candidate(
    *,
    source_url: str,
    source_title: str,
    search_snippet: str,
) -> dict[str, object]:
    """以严格、可审计的门槛评估搜索结果能否进入自动招商候选队列。

    输入是服务器从公开搜索或可信目录得到的 URL、标题和摘要；输出只说明该条索引是否
    *值得进一步人工补证*，以及可复查的命中词或排除原因。通过条件是：(1) 页面摘要
    明示中国/徐州语境或使用 ``.cn`` 域名，(2) 明示企业、基金或创业团队等组织主体，
    (3) 明示融资、扩产、合作、落地等机会信号，或存在徐州关联。它不联网、不写台账、
    不推断中国业务已成立，更不把任何结果表述为“愿意投资徐州”。信息不足时宁可拒绝
    自动观察，后续可由有正式证据的人工流程另行入账。
    """
    searchable_text = "\n".join([source_title, search_snippet]).casefold()
    hostname = (urlparse(source_url).hostname or "").casefold()
    # ``.cn`` 只是域名注册信息，外资主体同样可能使用，不能单独证明其在中国有实际业务。
    # 因而中国语境只能来自标题或搜索摘要中的可见文本，域名仍作为审计字段保留但不放行。
    china_context_terms = _matched_terms(searchable_text, CHINA_CONTEXT_TERMS)
    xuzhou_context_terms = _matched_terms(searchable_text, XUZHOU_CONTEXT_TERMS)
    business_subject_terms = _matched_terms(searchable_text, BUSINESS_SUBJECT_TERMS)
    opportunity_signal_terms = _matched_terms(searchable_text, OPPORTUNITY_SIGNAL_TERMS)
    has_chinese_context = bool(china_context_terms)
    has_xuzhou_context = bool(xuzhou_context_terms)
    has_business_subject = bool(business_subject_terms)
    has_opportunity_signal = bool(opportunity_signal_terms)
    rejection_reasons: list[str] = []
    if not has_chinese_context:
        rejection_reasons.append("missing_china_or_xuzhou_context")
    if not has_business_subject:
        rejection_reasons.append("missing_enterprise_fund_or_team_subject")
    if not has_opportunity_signal and not has_xuzhou_context:
        rejection_reasons.append("missing_investment_landing_or_xuzhou_signal")
    # 纯英文海外内容即使偶然出现“investment”等词，也不能因行业关键词而进入国内招商
    # 队列；此字段是额外的审计提示，不作为“中文即中国企业”的事实判断。
    language_hint = "contains_chinese_characters" if CHINESE_CHARACTER_PATTERN.search(
        f"{source_title}\n{search_snippet}"
    ) else "no_chinese_characters"
    return {
        "gate_version": PUBLIC_DISCOVERY_ELIGIBILITY_GATE_VERSION,
        "eligible": not rejection_reasons,
        "source_hostname": hostname,
        "language_hint": language_hint,
        "china_context_terms": china_context_terms,
        "xuzhou_context_terms": xuzhou_context_terms,
        "business_subject_terms": business_subject_terms,
        "opportunity_signal_terms": opportunity_signal_terms,
        "rejection_reasons": rejection_reasons,
        "decision_reason": (
            "仅满足自动补证前的中国/徐州业务机会筛选；页面原文仍需核验。"
            if not rejection_reasons
            else "搜索摘要不足以支持中国/徐州招商候选，已拒绝自动观察。"
        ),
        "operation_scope": "server_side_public_discovery_scope_filter_only",
    }


def assess_curated_chinese_media_candidate(
    *,
    source_url: str,
    source_title: str,
    search_snippet: str,
) -> dict[str, object]:
    """评估固定中国媒体目录标题是否可进入“必须补证”的受限候选队列。

    输入只能是服务器从已预检的固定中国财经媒体目录读取的同域标题、链接和索引摘要；
    输出是严格资格评估，或“需先核验中国业务”的受限候选标记。它不是通用搜索放宽：
    只有基础门槛仅缺标题/摘要中的中国语境、但已明确组织主体和融资/投资/合作等机会词时
    才能暂入页面快照队列。媒体的中文编辑语境不能证明被报道企业在中国经营，因此该分支
    永远不创建 evidence、评分、红色预警或招商推进，并会阻止候选被直接正式抓取；后续
    必须依据 robots 允许的原页快照和人工核验补足中国/徐州业务证据。
    """
    assessment = assess_public_discovery_candidate(
        source_url=source_url,
        source_title=source_title,
        search_snippet=search_snippet,
    )
    rejection_reasons = assessment.get("rejection_reasons")
    business_subject_terms = assessment.get("business_subject_terms")
    opportunity_signal_terms = assessment.get("opportunity_signal_terms")
    if not (
        isinstance(rejection_reasons, list)
        and rejection_reasons == ["missing_china_or_xuzhou_context"]
        and isinstance(business_subject_terms, list)
        and business_subject_terms
        and isinstance(opportunity_signal_terms, list)
        and opportunity_signal_terms
    ):
        return assessment
    # 此处不是把中国媒体域名当作企业中国业务事实，而是明确记录“仅获准用一次低频
    # 原页快照来补证”的原因。正式抓取入口会读取此标记并拒绝直接入证。
    assessment["eligible"] = True
    assessment["rejection_reasons"] = []
    assessment["china_context_source"] = "curated_chinese_business_media_requires_page_verification"
    assessment["requires_china_business_verification"] = True
    assessment["decision_reason"] = (
        "固定中国媒体标题已明示组织主体和机会信号，但未明示企业中国/徐州业务；"
        "仅进入待补证原页快照队列，禁止直接入证或推进。"
    )
    assessment["operation_scope"] = "server_side_curated_media_pending_page_verification_only"
    return assessment


def assess_fixed_investment_event_candidate(
    *,
    source_url: str,
    source_title: str,
    search_snippet: str,
) -> dict[str, object]:
    """评估源码固定投融资事件卡片能否进入“必须补证”的受限候选队列。

    输入是服务器从唯一固定的公开投融资栏目 HTML 中解析出的公司页链接、公司显示名和
    卡片摘要；输出是资格判断，或拒绝原因。栏目 HTML 中 ``company`` 链接这一结构只能
    说明媒体将该行作为投资事件主体展示，不能证明名称对应的工商主体、企业中国业务、
    徐州关系、融资真实性或落地意愿。故本函数只在普通规则**恰好**缺少“中国/徐州语境”
    和“文字形式的主体词”，但已出现明确机会信号时，允许它进入待补证页面快照队列；
    一律写入 ``requires_china_business_verification``，正式抓取入口仍会拒绝直接创建
    evidence。调用方还必须同时限制来源 ID、固定栏目 URL、robots 与解析结构，本函数
    自身不联网、不读写台账，也不能被模型/前端作为通用放宽入口调用。
    """
    assessment = assess_curated_chinese_media_candidate(
        source_url=source_url,
        source_title=source_title,
        search_snippet=search_snippet,
    )
    rejection_reasons = assessment.get("rejection_reasons")
    opportunity_signal_terms = assessment.get("opportunity_signal_terms")
    if not (
        isinstance(rejection_reasons, list)
        and rejection_reasons
        == [
            "missing_china_or_xuzhou_context",
            "missing_enterprise_fund_or_team_subject",
        ]
        and isinstance(opportunity_signal_terms, list)
        and opportunity_signal_terms
    ):
        return assessment
    # 该固定标签描述的是“栏目卡片有 company 链接”的服务器解析事实，而不是从公司名称
    # 推测“这一定是企业”。保留该来源说明可让人工一眼知道为什么它没有普通主体关键词
    # 仍会进入待补证队列，并避免把虚构的“公司/企业”字样塞回原始标题或摘要。
    assessment["eligible"] = True
    assessment["rejection_reasons"] = []
    assessment["business_subject_terms"] = ["fixed_structured_investment_event_subject"]
    assessment["business_subject_source"] = (
        "fixed_structured_investment_event_company_link_requires_page_verification"
    )
    assessment["china_context_source"] = "fixed_investment_event_requires_page_verification"
    assessment["requires_china_business_verification"] = True
    assessment["decision_reason"] = (
        "源码固定投融资事件卡片含主体链接和机会信号，但未明示中国/徐州业务；"
        "仅进入待补证原页快照队列，禁止直接入证、评分或推进。"
    )
    assessment["operation_scope"] = (
        "server_side_fixed_investment_event_pending_page_verification_only"
    )
    return assessment


def _discovery_lists(ledger: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """取得已兼容补全的发现批次和候选列表，并在异常结构时显式失败。"""
    runs = ledger.get("discovery_runs")
    candidates = ledger.get("discovery_candidates")
    if not isinstance(runs, list) or not isinstance(candidates, list):
        raise ValueError("招商线索台账中的自动发现字段必须是列表")
    return runs, candidates


def discover_public_sources(
    workspace_dir: Path,
    *,
    sector: str,
    max_queries: int = 4,
    max_results_per_query: int = 5,
) -> dict[str, Any]:
    """自动执行低频免费公开搜索，并把链接作为待抓取候选而非事实证据保存。

    输入是产业与严格受限的批量参数，输出是本次发现批次、新增候选及台账路径。所有
    搜索语句由 ``build_discovery_queries`` 生成，调用方不能注入 URL；每项搜索失败只
    被记录为批次统计，不会中断其他小批量任务。若搜索源报告访问受限，会按服务器侧
    健康度退避并跳过后续查询，不使用伪装或绕过方式重试。结果默认 ``lead_only``，
    不能直接触发红色预警、推进线索或改变评分；后续必须经
    ``capture_discovery_candidate`` 实际抓取页面并验证原始摘录后，才可能追加一条可复核
    证据。
    """
    normalized_sector = _normalize_sector(sector)
    normalized_max_results = _validate_run_limit(
        max_results_per_query,
        "max_results_per_query",
        maximum=MAX_RESULTS_PER_QUERY,
    )
    # 当前生效顺序只能从经过完整性校验的工作区规则读取，工具、模型与前端都没有传入
    # ``recipe_order`` 的入口。它只重排五条固定免费配方，不能修改任何搜索词或产业范围。
    active_recipe_rule_result = get_active_source_recipe_rule_set(workspace_dir)
    active_recipe_rule_set = active_recipe_rule_result.get("rule_set")
    if not isinstance(active_recipe_rule_set, dict):
        raise RuntimeError("当前来源配方规则读取异常")
    active_recipe_order = active_recipe_rule_set.get("recipe_order")
    if not isinstance(active_recipe_order, list):
        raise RuntimeError("当前来源配方规则缺少完整顺序")
    queries = build_discovery_queries(
        normalized_sector,
        max_queries=max_queries,
        recipe_order=active_recipe_order,
    )
    started_at = _utc_now()
    run_id = _stable_id(
        "discovery-run",
        {"sector": normalized_sector, "started_at": started_at, "queries": queries},
    )
    ledger = load_ledger(workspace_dir)
    runs, candidates = _discovery_lists(ledger)
    known_urls = {
        item.get("source_url")
        for item in candidates
        if isinstance(item, dict) and isinstance(item.get("source_url"), str)
    }
    # 已拒绝 URL 不会重复产生审计噪声；若同一 URL 后来在搜索结果中出现了新的、满足严格
    # 门槛的标题/摘要，仍允许重新评估并入候选，不能把一次摘要不足永久当成业务事实。
    known_rejected_urls = {
        item.get("source_url")
        for run in runs
        if isinstance(run, dict) and isinstance(run.get("rejected_results"), list)
        for item in run["rejected_results"]
        if isinstance(item, dict) and isinstance(item.get("source_url"), str)
    }
    newly_discovered: list[dict[str, Any]] = []
    rejected_results: list[dict[str, object]] = []
    rejected_result_count = 0
    failed_query_count = 0
    non_topical_response_count = 0
    skipped_due_to_cooldown_count = 0

    for query_specification in queries:
        if not is_source_eligible(ledger, source_key=SEARCH_FEED_SOURCE_KEY):
            skipped_due_to_cooldown_count += 1
            continue
        try:
            search_results = fetch_public_search_feed(query_specification["query"])
        except (OSError, RuntimeError, ValueError) as exc:
            failed_query_count += 1
            apply_source_health_outcome(
                ledger,
                source_key=SEARCH_FEED_SOURCE_KEY,
                success=False,
                error=str(exc),
            )
            continue
        # 先验证搜索源是否真的响应了本产业。空结果可能只是近期没有公开新闻，不把它误作
        # 故障；但有结果却完全不含产业总称或任一细分词，说明搜索入口正在返回固定泛页。
        # 此时失败关闭并退避，不能把无关页面逐条塞给资格门槛制造“成功但零产出”的假象。
        bounded_results = search_results[:normalized_max_results]
        if bounded_results and not _has_sector_context_in_search_results(
            bounded_results,
            sector=normalized_sector,
        ):
            failed_query_count += 1
            non_topical_response_count += 1
            apply_source_health_outcome(
                ledger,
                source_key=SEARCH_FEED_SOURCE_KEY,
                success=False,
                error=(
                    "公开搜索响应未命中目标产业词，疑似返回固定泛结果："
                    f"{normalized_sector}/{query_specification['source_catalog_id']}"
                ),
            )
            continue
        apply_source_health_outcome(
            ledger,
            source_key=SEARCH_FEED_SOURCE_KEY,
            success=True,
        )

        for search_result in bounded_results:
            try:
                source_url = _validate_discovered_url(search_result.get("source_url"))
            except ValueError:
                continue
            if source_url in known_urls:
                continue
            source_title = _normalize_text(
                _required_text(search_result.get("source_title"), "搜索结果标题", max_length=500)
            )
            search_snippet = _normalize_text(
                str(search_result.get("search_snippet", ""))[:2_000]
            )
            # 先以精确主机名排除已实测的认证/百科/旅游入口；其余 URL 必须继续经过
            # 严格资格门槛，不能因预筛选存在而放宽中国/徐州、主体或机会信号要求。
            eligibility_assessment = assess_fixed_source_prefilter(source_url)
            if eligibility_assessment is None:
                eligibility_assessment = assess_public_discovery_candidate(
                    source_url=source_url,
                    source_title=source_title,
                    search_snippet=search_snippet,
                )
            if eligibility_assessment["eligible"] is not True:
                rejected_result_count += 1
                if (
                    source_url not in known_rejected_urls
                    and len(rejected_results) < MAX_DISCOVERY_REJECTION_AUDIT_PER_RUN
                ):
                    # 排除审计只保留搜索索引本身和固定规则结果，既不保存网页正文，也不把
                    # “境外/无关”写成企业事实。它使后续能解释为何某链接没有进入队列。
                    rejected_results.append(
                        {
                            "source_url": source_url,
                            "source_title": source_title,
                            "search_snippet": search_snippet,
                            "source_catalog_id": query_specification["source_catalog_id"],
                            "eligibility_assessment": eligibility_assessment,
                        }
                    )
                    known_rejected_urls.add(source_url)
                continue
            discovered_at = _utc_now()
            candidate = {
                "candidate_id": _stable_id(
                    "discovery-candidate",
                    {"run_id": run_id, "source_url": source_url},
                ),
                "run_id": run_id,
                "source_url": source_url,
                "source_title": source_title,
                "search_snippet": search_snippet,
                "sector": normalized_sector,
                "source_catalog_id": query_specification["source_catalog_id"],
                "discovery_query": query_specification["query"],
                "discovered_at": discovered_at,
                "source_type": "public_search_index",
                "source_level": "lead_only",
                "state": "discovered",
                "capture_attempts": 0,
                "captured_evidence_id": None,
                "last_capture_error": None,
                "last_captured_at": None,
                # 这个结果是服务器对“是否值得自动补证”的可复查筛选，不是对企业中国业务、
                # 徐州关联或投资意愿的断言；快照和人工复核仍须分别完成。
                "eligibility_assessment": eligibility_assessment,
            }
            candidates.append(candidate)
            newly_discovered.append(candidate)
            known_urls.add(source_url)

    completed_at = _utc_now()
    run = {
        "run_id": run_id,
        "sector": normalized_sector,
        "started_at": started_at,
        "completed_at": completed_at,
        "query_count": len(queries),
        "candidate_count": len(newly_discovered),
        "rejected_result_count": rejected_result_count,
        "rejected_results": rejected_results,
        "failed_query_count": failed_query_count,
        # 该计数单列“搜索源返回了内容但完全偏离本产业”的情况，便于区分没有新闻与搜索
        # 引擎失效；它是来源治理数据，绝不是企业负面标签或招商评分依据。
        "non_topical_response_count": non_topical_response_count,
        "skipped_due_to_cooldown_count": skipped_due_to_cooldown_count,
        "source_recipe_rule_set_id": active_recipe_rule_set.get("rule_set_id"),
        "source_recipe_rule_set_version": active_recipe_rule_set.get("version"),
        "source_recipe_order": copy.deepcopy(active_recipe_order),
        "operation_scope": "low_frequency_public_search_candidate_discovery_only",
    }
    runs.append(run)
    saved_path = save_ledger(workspace_dir, ledger)
    return {
        "run": run,
        "candidates": newly_discovered,
        "ledger_path": str(saved_path),
    }


def _source_date_appears_in_text(source_published_at: str, visible_text: str) -> bool:
    """验证来源发布日期可由抓取正文支持，避免把采集当天误写成来源发布时间。"""
    parsed_date = date.fromisoformat(source_published_at)
    variants = {
        parsed_date.isoformat(),
        f"{parsed_date.year}/{parsed_date.month}/{parsed_date.day}",
        f"{parsed_date.year}.{parsed_date.month}.{parsed_date.day}",
        f"{parsed_date.year}年{parsed_date.month}月{parsed_date.day}日",
    }
    return any(variant in visible_text for variant in variants)


def _assert_existing_evidence_unchanged(
    before: dict[str, str],
    after: list[dict[str, Any]],
) -> None:
    """确认自动抓取只追加新证据，绝不改写任何已存在证据对象。"""
    after_by_id = {
        evidence.get("evidence_id"): json.dumps(evidence, ensure_ascii=False, sort_keys=True)
        for evidence in after
        if isinstance(evidence, dict)
    }
    if any(after_by_id.get(evidence_id) != serialized for evidence_id, serialized in before.items()):
        raise RuntimeError("自动采集不得修改既有原始证据链")


def _assert_candidate_is_in_automatic_scope(
    candidate: dict[str, Any], *, allow_reviewed_xuzhou_waiver: bool = False
) -> None:
    """拒绝对未通过中国/徐州业务机会门槛的历史候选执行页面抓取或正式入证。

    输入是台账中由服务器定位的候选，输出为空或抛出明确错误。该保护覆盖“查看快照”和
    “正式抓取”两条入口，防止旧版搜索结果绕过新的自动队列筛选而重新下载境外/无关网页。
    它不删除候选，也不否定任何企业事实；若业务人员之后拿到可追溯的一手材料，应走
    正式证据录入流程，而不是把旧搜索摘要升级为招商结论。
    """
    assessment = candidate.get("eligibility_assessment")
    if not isinstance(assessment, dict):
        raise ValueError("该历史候选缺少中国/徐州业务机会核验，禁止自动抓取")
    if assessment.get("gate_version") != PUBLIC_DISCOVERY_ELIGIBILITY_GATE_VERSION:
        raise ValueError("该候选的范围核验版本不受当前自动抓取流程接受")
    if assessment.get("eligible") is not True:
        raise ValueError("该候选未通过中国/徐州业务机会门槛，禁止自动抓取")
    # 媒体标题缺中国业务语境时只可用于低频快照补证；即使后续页面存在相关内容，也必须
    # 经人工从可追溯原文确认，不能让候选链接绕过正式 evidence 的来源与事实核验流程。
    if assessment.get("requires_china_business_verification") is True and not allow_reviewed_xuzhou_waiver:
        raise ValueError("该媒体候选仍需核验中国/徐州业务，禁止直接抓取为正式证据")


def _mark_capture_failed(
    workspace_dir: Path,
    candidate_id: str,
    error: str,
    *,
    source_health_key: str | None = None,
    failure_stage: str = "page_or_content_verification",
) -> dict[str, Any]:
    """记录候选页面抓取失败，必要时更新访问退避，但绝不生成或覆盖证据。

    输入是服务器候选 ID、失败文本和可选的服务器域名健康键；输出为更新后的候选状态。
    只有页面访问本身失败才会传入健康键并触发退避；实体、日期或摘录核验失败属于业务
    证据不足，不能错误地把站点标记为不可访问。无论失败原因是什么，函数都不会改写
    已存在证据、来源 URL、原始摘录、采集时间或内容哈希。
    """
    ledger = load_ledger(workspace_dir)
    _, candidates = _discovery_lists(ledger)
    candidate = next(
        (item for item in candidates if item.get("candidate_id") == candidate_id),
        None,
    )
    if candidate is None:
        raise RuntimeError("自动发现候选在抓取过程中丢失")
    candidate["state"] = "capture_failed"
    candidate["last_capture_error"] = error[:MAX_CAPTURE_ERROR_LENGTH]
    candidate["last_captured_at"] = _utc_now()
    if source_health_key is not None:
        apply_source_health_outcome(
            ledger,
            source_key=source_health_key,
            success=False,
            error=error,
        )
    saved_path = save_ledger(workspace_dir, ledger)
    # Return the same bounded reason that was appended to the candidate audit.
    # Earlier callers only received ``captured=False``, which forced the UI to
    # say “verification failed” even when the server knew the precise reason.
    # This is still not a page body, credential or internal path.
    public_error = error[:MAX_CAPTURE_ERROR_LENGTH]
    return {
        "captured": False,
        "message": public_error,
        "failure_stage": failure_stage,
        "verification_failure": {
            "stage": failure_stage,
            "reason": public_error,
            "next_step": "阅读已保存页面材料；不要在浏览器改写日期、实体或摘录。若当前页面已有新的明确公开材料，可再次请求服务器核验；否则选择待补证、观察或无效。",
        },
        "candidate": candidate,
        "ledger_path": str(saved_path),
    }


def _capture_failure_stage(error: str) -> str:
    """Map a bounded verification error to a product-readable fixed stage.

    The returned code is operational metadata only.  It neither alters the
    candidate nor interprets a missing field as a business fact, and lets the
    browser explain whether the server could not read the page or read it but
    could not verify a required fact.
    """
    if error.startswith("source_published_at"):
        return "source_date_not_found_in_current_page"
    if error.startswith("entity_name"):
        return "entity_name_not_found_in_current_page"
    if error.startswith("excerpt"):
        return "original_excerpt_changed_or_not_found"
    if "robots.txt" in error:
        return "robots_access_not_allowed"
    return "page_or_content_verification"


def inspect_discovery_candidate(
    workspace_dir: Path,
    *,
    candidate_id: str,
) -> dict[str, Any]:
    """读取一个已自动发现候选的受限页面快照，供智能体提取可验证业务字段。

    输入仅为服务器生成的候选 ID，输出为候选信息和固定上限的标题、正文、最终 URL、
    内容哈希及读取时间。函数从台账中的候选 URL 抓取，而不接受调用方 URL；先读取同源
    robots.txt，禁止或不可读取时不访问页面。它不创建线索、不写入证据、不改变候选
    状态、不评分也不预警。正文只是下一步提取实体名、发布日期和原始摘录的工作材料，
    随后 ``capture_discovery_candidate`` 必须重新抓取并核验这些字段，避免检查阶段的
    网页内容被当作不可变证据或被调用方伪造。
    """
    normalized_candidate_id = _required_text(candidate_id, "candidate_id", max_length=100)
    ledger = load_ledger(workspace_dir)
    _, candidates = _discovery_lists(ledger)
    candidate = next(
        (item for item in candidates if item.get("candidate_id") == normalized_candidate_id),
        None,
    )
    if candidate is None:
        raise ValueError("未找到对应的自动发现候选")
    _assert_candidate_is_in_automatic_scope(candidate)
    source_url = candidate.get("source_url")
    if not isinstance(source_url, str):
        raise ValueError("自动发现候选缺少可抓取来源")
    robots_policy = check_robots_access(source_url)
    if not robots_policy["allowed"]:
        raise ValueError(f"自动候选查看被 robots.txt 限制：{robots_policy['reason']}")

    snapshot = fetch_public_source(source_url)
    visible_text = _normalize_text(str(snapshot["visible_text"]))
    source_title = _normalize_text(str(snapshot["source_title"]))
    final_url = snapshot.get("source_url")
    raw_sha256 = snapshot.get("raw_sha256")
    if not isinstance(final_url, str) or not isinstance(raw_sha256, str):
        raise RuntimeError("抓取页面缺少最终来源链接或内容哈希")
    return {
        "candidate": candidate,
        "snapshot": {
            "source_url": final_url,
            "source_title": source_title,
            "visible_text": visible_text[:MAX_INSPECTION_VISIBLE_TEXT_CHARS],
            "visible_text_truncated": len(visible_text) > MAX_INSPECTION_VISIBLE_TEXT_CHARS,
            "raw_sha256": raw_sha256,
            "collected_at": _utc_now(),
        },
        "ledger_path": str(ledger_path(workspace_dir)),
    }


def capture_discovery_candidate(
    workspace_dir: Path,
    *,
    candidate_id: str,
    entity_name: str,
    entity_kind: str,
    claim_type: str,
    source_published_at: str,
    excerpt: str,
    xuzhou_association_tags: list[str] | None = None,
    allow_reviewed_xuzhou_waiver: bool = False,
) -> dict[str, Any]:
    """抓取已发现候选页面并在可核验时追加低等级公开证据。

    输入只允许候选 ID 和从页面提取的业务字段，不允许调用方传入来源 URL、标题、采集
    时间或内容哈希；输出为抓取结果、候选状态和台账路径。服务使用候选中服务器发现的
    URL 抓取页面，要求实体名、发布日期和原始摘录都能在页面可见文本中找到，再由服务
    填入真实标题、UTC 采集时间和 SHA-256。任何失败只记录候选失败状态；任何成功只
    追加新证据，并校验原有证据对象逐字未变。抓取前先遵守同源 robots.txt；禁止或
    不可读取时只记录候选失败和访问退避，不请求页面。调用方的事实类别会先按服务器原文
    固定词表分类；若原文明确显示主体风险，服务端会覆盖为 ``risk``，并使用该分类匹配
    可信来源目录，避免模型借“融资”等主观标签获得不相称的来源等级。最终域名和服务器
    分类同时匹配目录，才使用目录声明的一手或二手等级；否则安全降级为 ``lead_only``。
    高价值一手风险仍只进入人工复核，不会自动评分或替代人工业务判断。
    """
    normalized_candidate_id = _required_text(candidate_id, "candidate_id", max_length=100)
    normalized_entity_name = _normalize_text(
        _required_text(entity_name, "entity_name", max_length=500)
    )
    normalized_excerpt = _normalize_text(_required_text(excerpt, "excerpt", max_length=2_000))
    normalized_source_date = _required_text(
        source_published_at,
        "source_published_at",
        max_length=30,
    )[:10]
    try:
        date.fromisoformat(normalized_source_date)
    except ValueError as exc:
        raise ValueError("source_published_at必须使用YYYY-MM-DD格式") from exc

    ledger = load_ledger(workspace_dir)
    _, candidates = _discovery_lists(ledger)
    candidate = next(
        (item for item in candidates if item.get("candidate_id") == normalized_candidate_id),
        None,
    )
    if candidate is None:
        raise ValueError("未找到对应的自动发现候选")
    # The only narrowly-scoped exception is a prior, append-only human review
    # that confirms China business and records an enterprise origin while
    # waiving the XuZhou field.  The caller cannot use the boolean by itself:
    # the ledger review is rechecked here immediately before any network work.
    if allow_reviewed_xuzhou_waiver:
        from .candidate_review import candidate_recapture_scope_blocker

        if candidate_recapture_scope_blocker(ledger, candidate_id=normalized_candidate_id) is not None:
            raise ValueError("候选没有可用的人工中国业务确认与徐州字段豁免，禁止重新抓取")
    _assert_candidate_is_in_automatic_scope(
        candidate,
        allow_reviewed_xuzhou_waiver=allow_reviewed_xuzhou_waiver,
    )
    if candidate.get("state") == "captured":
        return {
            "captured": False,
            "message": "该候选页面已经完成抓取，未重复写入证据",
            "candidate": candidate,
            "ledger_path": str(ledger_path(workspace_dir)),
        }
    if not isinstance(candidate.get("source_url"), str) or not isinstance(candidate.get("run_id"), str):
        raise ValueError("自动发现候选缺少可抓取来源或批次标识")

    source_health_key = source_health_key_for_url(candidate["source_url"])
    if not is_source_eligible(ledger, source_key=source_health_key):
        return {
            "captured": False,
            "message": "该来源仍在访问退避期或已被访问策略禁止，本次未重新请求页面",
            "failure_stage": "source_access_cooldown",
            "verification_failure": {
                "stage": "source_access_cooldown",
                "reason": "该来源仍在访问退避期或已被访问策略禁止，本次未重新请求页面",
                "next_step": "等待服务器的固定退避期结束后再由业务人员发起重新核验；不要以浏览器打开页面代替服务器留存。",
            },
            "candidate": candidate,
            "ledger_path": str(ledger_path(workspace_dir)),
        }
    robots_policy = check_robots_access(candidate["source_url"])
    if not robots_policy["allowed"]:
        return _mark_capture_failed(
            workspace_dir,
            normalized_candidate_id,
            f"自动候选抓取被 robots.txt 限制：{robots_policy['reason']}",
            source_health_key=source_health_key,
            failure_stage="robots_access_not_allowed",
        )

    evidence_before = {
        evidence.get("evidence_id"): json.dumps(evidence, ensure_ascii=False, sort_keys=True)
        for evidence in ledger["evidence"]
        if isinstance(evidence, dict) and isinstance(evidence.get("evidence_id"), str)
    }
    candidate["capture_attempts"] = int(candidate.get("capture_attempts", 0)) + 1
    save_ledger(workspace_dir, ledger)
    try:
        snapshot = fetch_public_source_for_observation(candidate["source_url"])
    except (OSError, RuntimeError, ValueError) as exc:
        return _mark_capture_failed(
            workspace_dir,
            normalized_candidate_id,
            str(exc),
            source_health_key=source_health_key,
            failure_stage="page_read_failed",
        )

    # 页面已成功读取才清除该域名此前的临时退避；正文核验不通过不代表网站不可访问。
    health_ledger = load_ledger(workspace_dir)
    apply_source_health_outcome(
        health_ledger,
        source_key=source_health_key,
        success=True,
    )
    save_ledger(workspace_dir, health_ledger)
    try:
        visible_text = _normalize_text(str(snapshot["visible_text"]))
        title = _normalize_text(str(snapshot["source_title"]))
        if normalized_entity_name.casefold() not in visible_text.casefold() and normalized_entity_name.casefold() not in title.casefold():
            raise ValueError("entity_name未在抓取页面标题或正文中找到，不能登记为证据")
        if normalized_excerpt.casefold() not in visible_text.casefold():
            raise ValueError("excerpt必须是抓取页面中的原始可见摘录，不能使用概括或推测")
        if not _source_date_appears_in_text(normalized_source_date, visible_text):
            raise ValueError("source_published_at未在抓取页面正文中找到，不能以采集日期替代")
        raw_sha256 = snapshot.get("raw_sha256")
        raw_content = snapshot.get("raw_content")
        source_url = snapshot.get("source_url")
        if (
            not isinstance(raw_sha256, str)
            or not isinstance(source_url, str)
            or not isinstance(raw_content, bytes)
        ):
            raise RuntimeError("抓取页面缺少原始响应、内容哈希或最终来源链接")
        # 先以已核验的页面标题和原始摘录得到服务器分类，再用它选择可信来源等级；调用方
        # 传入的类别只作为可回溯请求，不能单独决定“这是融资/合作/风险”的来源权限。
        source_text_classification = classify_claim_type_from_source_text(
            requested_claim_type=claim_type,
            entity_name=normalized_entity_name,
            source_title=title,
            excerpt=normalized_excerpt,
        )
        effective_claim_type = source_text_classification.get("claim_type")
        if not isinstance(effective_claim_type, str):
            raise RuntimeError("服务器事实分类缺少有效类别")
        classification = classify_captured_source(
            workspace_dir,
            source_url=source_url,
            claim_type=effective_claim_type,
        )
        capture_metadata = {
            "source_type": classification["capture_source_type"],
            "collection_method": "public_search_then_snapshot",
            "raw_sha256": raw_sha256,
            "discovery_run_id": candidate["run_id"],
            "discovery_candidate_id": normalized_candidate_id,
        }
        trusted_source = classification["trusted_source"]
        if isinstance(trusted_source, dict):
            capture_metadata["trusted_source_id"] = trusted_source["source_id"]
            capture_metadata["trusted_source_type"] = trusted_source["source_type"]
        result = record_public_evidence(
            workspace_dir,
            entity_name=normalized_entity_name,
            entity_kind=entity_kind,
            sector=str(candidate["sector"]),
            source_url=source_url,
            source_title=title,
            source_published_at=normalized_source_date,
            source_level=classification["source_level"],
            claim_type=claim_type,
            evidence_quality=classification["evidence_quality"],
            excerpt=normalized_excerpt,
            xuzhou_association_tags=xuzhou_association_tags,
            capture_metadata=capture_metadata,
            raw_content=raw_content,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        error = str(exc)
        return _mark_capture_failed(
            workspace_dir,
            normalized_candidate_id,
            error,
            failure_stage=_capture_failure_stage(error),
        )

    updated_ledger = load_ledger(workspace_dir)
    _assert_existing_evidence_unchanged(evidence_before, updated_ledger["evidence"])
    _, updated_candidates = _discovery_lists(updated_ledger)
    updated_candidate = next(
        (item for item in updated_candidates if item.get("candidate_id") == normalized_candidate_id),
        None,
    )
    if updated_candidate is None:
        raise RuntimeError("自动发现候选在写入证据后丢失")
    updated_candidate["state"] = "captured"
    updated_candidate["captured_evidence_id"] = result["evidence"]["evidence_id"]
    updated_candidate["last_capture_error"] = None
    updated_candidate["last_captured_at"] = _utc_now()
    saved_path = save_ledger(workspace_dir, updated_ledger)
    return {
        "captured": True,
        "candidate": updated_candidate,
        "result": result,
        "ledger_path": str(saved_path),
    }
