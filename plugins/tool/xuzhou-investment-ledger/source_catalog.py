"""徐州招商可信公开来源目录及其可追溯分级逻辑。"""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .core import CLAIM_TYPES, CLAIM_TYPE_ALIASES, REQUESTABLE_CLAIM_TYPES, _canonical
from .storage import ledger_path, load_ledger, save_ledger


SOURCE_STATUSES = frozenset({"active", "disabled", "superseded"})
MAX_TRUSTED_SOURCE_AUDIT_LIST_LIMIT = 500
# 业务人员直填的网址属于“公开发现入口”，而不是经来源分级确认的一手/二手事实来源。
# 它的 URL、robots 与访问结果均由专用接口逐次校验；普通可信域名登记接口不能借用这一
# 类型绕过预检。即使后续自动发现到材料，它仍只会作为 lead_only 候选进入人工核验队列。
HUMAN_PUBLIC_SUPPLEMENT_SOURCE_TYPE = "human_added_public_source"
MAX_ACTIVE_HUMAN_PUBLIC_SUPPLEMENTS = 8
HUMAN_PUBLIC_SUPPLEMENT_CRAWL_SCOPE = (
    "seed_url_and_robots_permitted_same_domain_indexes_only"
)
SOURCE_TYPE_RULES: dict[str, dict[str, object]] = {
    "enterprise_official": {
        "allowed_levels": {"primary"},
        "quality_range": (4, 5),
        "allowed_claim_types": REQUESTABLE_CLAIM_TYPES,
    },
    "fund_official": {
        "allowed_levels": {"primary"},
        "quality_range": (4, 5),
        "allowed_claim_types": REQUESTABLE_CLAIM_TYPES,
    },
    "government_investment_portal": {
        "allowed_levels": {"primary"},
        "quality_range": (4, 5),
        "allowed_claim_types": {
            "xuzhou_connection",
            "cooperation",
            "expansion",
            "industry_focus",
        },
    },
    # 官方统计网站可用于核验公开产业口径、统计发布与项目扩张背景，但不能仅凭宏观
    # 数据推定单个企业融资、个人投资能力或确定落地意愿，因此严格限制允许事实类别。
    "official_statistics": {
        "allowed_levels": {"primary"},
        "quality_range": (4, 5),
        "allowed_claim_types": {"expansion", "industry_focus"},
    },
    "university_alumni": {
        "allowed_levels": {"primary"},
        "quality_range": (4, 4),
        "allowed_claim_types": {"xuzhou_connection", "cooperation", "industry_focus"},
    },
    "industry_association": {
        "allowed_levels": {"secondary"},
        "quality_range": (2, 3),
        "allowed_claim_types": {
            "cooperation",
            "expansion",
            "industry_focus",
            "fund_investment",
            "risk",
        },
    },
    "verified_media": {
        "allowed_levels": {"secondary"},
        "quality_range": (2, 3),
        "allowed_claim_types": REQUESTABLE_CLAIM_TYPES,
    },
}
DOMAIN_PATTERN = re.compile(r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\Z")
# 以下域名逐一来自国家统计局公开门户和国家数据平台；种子仅提供未来抓取的来源分级，
# 不等于系统已读取、验证或保存其中任一页面。普通用户可停用或以同域人工配置替换，
# 系统引导函数见下方，且绝不覆盖已有任何状态的同域目录记录。
OFFICIAL_STATISTICS_SOURCE_SEEDS: tuple[dict[str, object], ...] = (
    {
        "source_id": "trusted-national-bureau-statistics",
        "domain": "stats.gov.cn",
        "source_type": "official_statistics",
        "source_level": "primary",
        "evidence_quality": 5,
        "allowed_claim_types": ["expansion", "industry_focus"],
        "change_reason": "系统内置的免费国家统计局公开来源种子；仅用于未来来源分级。",
    },
    {
        "source_id": "trusted-national-data-platform",
        "domain": "data.stats.gov.cn",
        "source_type": "official_statistics",
        "source_level": "primary",
        "evidence_quality": 5,
        "allowed_claim_types": ["expansion", "industry_focus"],
        "change_reason": "系统内置的免费国家数据平台来源种子；仅用于未来来源分级。",
    },
)
# These two fixed public portals complement the local XuZhou portal.  They are
# discovery directories, not proof that a listed organisation will invest or
# has a XuZhou project.  Every actual visit still goes through the same robots,
# redirect, size and source-health checks as all other trusted sources.
GOVERNMENT_INVESTMENT_SOURCE_SEEDS: tuple[dict[str, object], ...] = (
    {
        "source_id": "trusted-jiangsu-commerce-investment",
        "domain": "doc.jiangsu.gov.cn",
        "source_type": "government_investment_portal",
        "source_level": "primary",
        "evidence_quality": 4,
        "allowed_claim_types": ["xuzhou_connection", "cooperation", "expansion", "industry_focus"],
        "change_reason": "系统内置的江苏省商务厅公开招商信息入口；仅用于未来公开来源发现与分级，不代表页面中的企业事实。",
    },
    {
        "source_id": "trusted-jiangsu-international-investment-promotion",
        "domain": "iinvest.org.cn",
        "source_type": "government_investment_portal",
        "source_level": "primary",
        "evidence_quality": 4,
        "allowed_claim_types": ["xuzhou_connection", "cooperation", "expansion", "industry_focus"],
        "change_reason": "系统内置的江苏国际投资促进公开信息入口；仅用于未来公开来源发现与分级，不代表页面中的企业事实。",
    },
)
# 以下媒体域名不是一手企业事实来源，而是经过本插件 robots 和首页产业链接预检后提供
# 的免费“发现入口”。它们只以 secondary/3 分登记：页面标题最多触发待核验候选，后续仍
# 须抓取原页、保存快照并交叉核验，绝不能凭媒体转载直接认定企业融资、在华业务或落地
# 意愿。任何同域人工 disabled/superseded 配置都会阻止系统重新加入，确保普通用户保有
# 业务处置权；若未来版权、robots 或质量发生变化，应停用该域名而不是绕过访问限制。
FREE_VERIFIED_MEDIA_SOURCE_SEEDS: tuple[dict[str, object], ...] = (
    {
        "source_id": "trusted-securities-times-media",
        "domain": "stcn.com",
        "source_type": "verified_media",
        "source_level": "secondary",
        "evidence_quality": 3,
        "system_curated_chinese_business_media": True,
        "allowed_claim_types": [
            "financing",
            "fund_raising",
            "xuzhou_connection",
            "cooperation",
            "expansion",
            "industry_focus",
            "fund_investment",
            "risk",
        ],
        "change_reason": "系统内置的免费媒体发现入口：robots 与首页产业链接预检通过；仅作待核验二手线索。",
    },
    {
        "source_id": "trusted-jiemian-media",
        "domain": "jiemian.com",
        "source_type": "verified_media",
        "source_level": "secondary",
        "evidence_quality": 3,
        "system_curated_chinese_business_media": True,
        "allowed_claim_types": [
            "financing",
            "fund_raising",
            "xuzhou_connection",
            "cooperation",
            "expansion",
            "industry_focus",
            "fund_investment",
            "risk",
        ],
        "change_reason": "系统内置的免费媒体发现入口：robots 与首页产业链接预检通过；仅作待核验二手线索。",
    },
    {
        "source_id": "trusted-nbd-media",
        "domain": "nbd.com.cn",
        "source_type": "verified_media",
        "source_level": "secondary",
        "evidence_quality": 3,
        "system_curated_chinese_business_media": True,
        "allowed_claim_types": [
            "financing",
            "fund_raising",
            "xuzhou_connection",
            "cooperation",
            "expansion",
            "industry_focus",
            "fund_investment",
            "risk",
        ],
        "change_reason": "系统内置的免费媒体发现入口：robots 与首页产业链接预检通过；仅作待核验二手线索。",
    },
    {
        "source_id": "trusted-eastmoney-media",
        "domain": "eastmoney.com",
        "source_type": "verified_media",
        "source_level": "secondary",
        "evidence_quality": 3,
        "system_curated_chinese_business_media": True,
        "allowed_claim_types": [
            "financing",
            "fund_raising",
            "xuzhou_connection",
            "cooperation",
            "expansion",
            "industry_focus",
            "fund_investment",
            "risk",
        ],
        "change_reason": "系统内置的免费媒体发现入口：robots 与首页产业链接预检通过；仅作待核验二手线索。",
    },
    {
        "source_id": "trusted-pedaily-investment-events",
        "domain": "pedaily.cn",
        "source_type": "verified_media",
        "source_level": "secondary",
        "evidence_quality": 3,
        "system_curated_chinese_business_media": True,
        "allowed_claim_types": [
            "financing",
            "fund_raising",
            "cooperation",
            "expansion",
            "industry_focus",
            "fund_investment",
            "risk",
        ],
        # 该来源的具体栏目 URL 不保存进可编辑目录，而在来源发现器中以源码固定映射维护。
        # 这样普通用户、模型和网页都不能借来源登记接口注入任意抓取地址；每一次实际访问
        # 仍会先读取对应路径的 robots.txt。栏目仅输出待补证二手线索，绝不能直接作为企业
        # 融资、在华业务、徐州关联或落地意愿的事实依据。
        "change_reason": "系统内置的免费投融资事件索引入口：经 robots 与公开栏目结构预检；仅作待核验二手线索。",
    },
)


def _utc_now() -> str:
    """返回来源目录变更审计统一使用的 UTC 时间，不依赖客户端传入时钟。"""
    return datetime.now(timezone.utc).isoformat()


def _required_text(value: object, field_name: str, *, max_length: int) -> str:
    """校验来源目录业务字段，拒绝空白、过长与非字符串输入。"""
    if not isinstance(value, str) or not (text := value.strip()):
        raise ValueError(f"{field_name}不能为空")
    if len(text) > max_length:
        raise ValueError(f"{field_name}不能超过{max_length}个字符")
    return text


def _stable_id(prefix: str, payload: dict[str, object]) -> str:
    """为目录记录和审计事件生成稳定短 ID，便于来源分级的回溯。"""
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}-{hashlib.sha256(serialized.encode('utf-8')).hexdigest()[:16]}"


def _normalize_domain(value: object) -> str:
    """把普通域名或根 URL 规范化为安全的主机名，不接受路径、凭据或 IP 地址。

    输入是普通用户维护目录时提交的域名，输出为小写根域名。该字段只用于未来页面的
    来源分级，不能成为网络访问白名单；实际抓取仍由独立采集器执行公网 DNS、跳转和
    响应限制。拒绝本地、保留 IP 和带路径 URL，避免把基础设施地址误作为业务来源。
    """
    raw_value = _required_text(value, "domain", max_length=253).casefold().rstrip(".")
    parsed = urlparse(raw_value if "://" in raw_value else f"https://{raw_value}")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("domain必须是公开域名或根HTTP/HTTPS地址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("domain不能包含账号、参数或片段")
    if parsed.path not in {"", "/"}:
        raise ValueError("domain只能登记域名，不能包含页面路径")
    hostname = parsed.hostname.casefold().rstrip(".")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise ValueError("domain不能是IP地址")
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(
        (".localhost", ".local", ".internal")
    ):
        raise ValueError("domain不能是本机或内部网络域名")
    if not DOMAIN_PATTERN.fullmatch(hostname):
        raise ValueError("domain必须是有效的ASCII公开域名")
    return hostname


def _normalize_source_type(value: object) -> str:
    """校验来源类型，使类型、证据等级和允许事实类别能受到统一约束。"""
    source_type = _required_text(value, "source_type", max_length=100).casefold()
    if source_type not in SOURCE_TYPE_RULES:
        accepted = "、".join(sorted(SOURCE_TYPE_RULES))
        raise ValueError(f"source_type不合法，可选值为：{accepted}")
    return source_type


def _normalize_source_level(value: object, source_type: str) -> str:
    """校验目录声明的来源等级，避免二手或未知站点被误升为一手来源。"""
    source_level = _required_text(value, "source_level", max_length=100).casefold()
    allowed_levels = SOURCE_TYPE_RULES[source_type]["allowed_levels"]
    if not isinstance(allowed_levels, set) or source_level not in allowed_levels:
        accepted = "、".join(sorted(allowed_levels)) if isinstance(allowed_levels, set) else ""
        raise ValueError(f"{source_type}只允许source_level为：{accepted}")
    return source_level


def _normalize_evidence_quality(value: object, source_type: str) -> int:
    """校验目录的证据质量上限，防止配置将低可信来源直接标成最高可信。"""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("evidence_quality必须是整数")
    quality_range = SOURCE_TYPE_RULES[source_type]["quality_range"]
    if not isinstance(quality_range, tuple) or len(quality_range) != 2:
        raise RuntimeError("可信来源类型配置格式异常")
    minimum, maximum = quality_range
    if not isinstance(minimum, int) or not isinstance(maximum, int) or not minimum <= value <= maximum:
        raise ValueError(f"{source_type}的evidence_quality必须是{minimum}到{maximum}之间的整数")
    return value


def _normalize_allowed_claim_types(value: object, source_type: str) -> list[str]:
    """规范化目录允许支持的事实类别，禁止来源越权支持不相称的高价值结论。"""
    if not isinstance(value, list) or not value:
        raise ValueError("allowed_claim_types必须是非空列表")
    type_allowed_claims = SOURCE_TYPE_RULES[source_type]["allowed_claim_types"]
    if not isinstance(type_allowed_claims, frozenset) and not isinstance(type_allowed_claims, set):
        raise RuntimeError("可信来源类型配置格式异常")

    normalized_claim_types: list[str] = []
    for raw_claim_type in value:
        claim_type = _canonical(
            raw_claim_type,
            "allowed_claim_types中的事实类别",
            REQUESTABLE_CLAIM_TYPES,
            CLAIM_TYPE_ALIASES,
        )
        if claim_type not in type_allowed_claims:
            raise ValueError(f"{source_type}不能支持事实类别：{claim_type}")
        if claim_type not in normalized_claim_types:
            normalized_claim_types.append(claim_type)
    return normalized_claim_types


def _catalog_lists(ledger: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """获取已兼容补全的可信目录和审计列表，结构异常时拒绝猜测或覆盖。"""
    sources = ledger.get("trusted_sources")
    audit = ledger.get("trusted_source_audit")
    if not isinstance(sources, list) or not isinstance(audit, list):
        raise ValueError("招商线索台账中的可信来源目录字段必须是列表")
    return sources, audit


def _append_audit(
    audit: list[dict[str, Any]],
    *,
    source_id: str,
    action: str,
    old_status: str | None,
    new_status: str,
    reason: str,
    authenticated_user_id: str,
    operated_at: str,
) -> None:
    """追加可信目录的业务审计事件，不接触任何线索、证据或评分历史。"""
    audit.append(
        {
            "audit_id": _stable_id(
                "trusted-source-audit",
                {
                    "source_id": source_id,
                    "action": action,
                    "operated_at": operated_at,
                    "authenticated_user_id": authenticated_user_id,
                },
            ),
            "source_id": source_id,
            "action": action,
            "old_status": old_status,
            "new_status": new_status,
            "reason": reason,
            "operated_at": operated_at,
            "operated_by_user_id": authenticated_user_id,
            "operation_scope": "trusted_source_business_configuration_only",
        }
    )


def _assert_evidence_related_data_unchanged(
    before: dict[str, str],
    ledger: dict[str, Any],
) -> None:
    """确认目录变更不改写线索、原始证据、评分快照或人工反馈。"""
    for field_name, expected_value in before.items():
        current_value = json.dumps(ledger[field_name], ensure_ascii=False, sort_keys=True)
        if current_value != expected_value:
            raise RuntimeError("可信来源目录变更不得修改线索、证据、评分或反馈历史")


def _non_catalog_snapshot(ledger: dict[str, Any]) -> dict[str, str]:
    """快照可信来源目录以外的完整业务数据，供系统种子写入后防篡改比对。

    输入是已完成完整性校验的当前台账，输出为除目录、目录审计和保存时必变的完整性元
    数据之外的稳定 JSON 快照。该快照让系统仅追加官方统计来源时，也必须证明线索、
    原始证据、快照、评分、人工复核、图谱、候选与来源健康记录逐字不变；函数不写文件、
    不联网、不接受前端/模型输入，也不判断任何企业或个人的招商价值。
    """
    mutable_catalog_fields = {
        "trusted_sources",
        "trusted_source_audit",
        # ``save_ledger`` 会按当前完整台账重新计算这些全局完整性元数据，不能把其自然
        # 变化误判为业务记录改写；其他字段则必须在保存前后保持不变。
        "ledger_integrity",
        "ledger_revision",
        "updated_at",
    }
    return {
        field_name: json.dumps(value, ensure_ascii=False, sort_keys=True)
        for field_name, value in ledger.items()
        if field_name not in mutable_catalog_fields
    }


def _assert_non_catalog_snapshot_unchanged(
    before: dict[str, str],
    ledger: dict[str, Any],
) -> None:
    """验证系统官方统计来源引导未触及目录以外任何台账记录。

    输入为引导写入前快照及准备保存的台账；无返回值，任一非目录字段变化立即失败。该
    检查是对来源目录业务配置的额外防线，确保自动补齐的只是两条公开域名配置和其审计，
    永不改写证据 URL、原始摘录、采集时间、证据签名、线索状态、评分或人工业务结论。
    """
    if _non_catalog_snapshot(ledger) != before:
        raise RuntimeError("系统官方统计来源引导不得修改可信目录以外的台账记录")


def list_trusted_sources(workspace_dir: Path, *, active_only: bool = False) -> dict[str, Any]:
    """读取当前工作区可信来源目录，所有登录用户均可查看但不会修改任何记录。"""
    ledger = load_ledger(workspace_dir)
    sources, _ = _catalog_lists(ledger)
    selected_sources = [
        source
        for source in sources
        if isinstance(source, dict) and (not active_only or source.get("status") == "active")
    ]
    return {
        "sources": copy.deepcopy(selected_sources),
        "count": len(selected_sources),
        "ledger_path": str(ledger_path(workspace_dir)),
    }


def list_trusted_source_audit(workspace_dir: Path, *, limit: int = 100) -> dict[str, Any]:
    """读取可信来源配置的追加式审计，供团队追溯目录变更而不修改业务数据。

    输入是已由认证路由隔离的工作区路径和 1–500 条展示上限，输出为按服务器记录时间倒序
    的独立审计副本。审计用于说明谁在何时登记、替代、启用或停用了哪一来源配置；函数不
    接收来源 URL、状态、用户 ID、证据或评分写入字段，也不联网、不变更目录或任何台账。
    ``load_ledger`` 会先执行完整性校验，校验失败时安全拒绝读取，避免向浏览器展示可能被
    篡改的审计。历史 evidence 的来源 URL、摘录、采集时间、哈希和评级不因查看审计改变。
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_TRUSTED_SOURCE_AUDIT_LIST_LIMIT:
        raise ValueError(f"limit必须是1到{MAX_TRUSTED_SOURCE_AUDIT_LIST_LIMIT}之间的整数")
    ledger = load_ledger(workspace_dir)
    _, audit = _catalog_lists(ledger)
    selected_audit = [copy.deepcopy(item) for item in audit if isinstance(item, dict)]
    selected_audit.sort(key=lambda item: str(item.get("operated_at", "")), reverse=True)
    return {
        "audit_events": selected_audit[:limit],
        "count": min(len(selected_audit), limit),
        "ledger_path": str(ledger_path(workspace_dir)),
    }


def _normalize_public_supplement_source_id(value: object) -> str:
    """校验专用公开来源 ID；浏览器不能以路径、对象或超长值定位目录记录。"""

    return _required_text(value, "source_id", max_length=100)


def _public_supplement_source(
    sources: list[dict[str, Any]],
    *,
    source_id: str,
) -> dict[str, Any]:
    """只返回已由专用流程登记的公开来源，避免普通目录记录误走抓取启停接口。"""

    source = next(
        (
            item
            for item in sources
            if isinstance(item, dict)
            and item.get("source_id") == source_id
            and item.get("source_type") == HUMAN_PUBLIC_SUPPLEMENT_SOURCE_TYPE
        ),
        None,
    )
    if source is None:
        raise ValueError("未找到对应的人工补充公开来源")
    seed_url = source.get("seed_url")
    if not isinstance(seed_url, str) or not seed_url:
        raise RuntimeError("人工补充公开来源缺少受控入口网址")
    return source


def _normalize_public_supplement_preflight(
    preflight: object,
    *,
    expected_seed_url: str | None = None,
) -> dict[str, object]:
    """接收服务器前置检测的最小展示字段，拒绝浏览器伪造的目录/抓取元数据。

    这里不联网，也不接受调用方指定爬取深度、Cookie、请求头、搜索词、证据或来源等级。
    网络检测只能由 ``public_source_supplement`` 在调用本函数前完成；目录层只保存其可审计
    结论摘要，且成功检测的规范网址必须和既有入口一致。
    """

    if not isinstance(preflight, dict):
        raise ValueError("公开来源检测结果格式异常")
    raw_status = preflight.get("status")
    allowed_statuses = {
        "ready_for_public_crawl",
        "robots_disallowed",
        "login_or_paid_access",
        "unsupported_api_or_credentials",
        "temporarily_unreachable",
        "unsafe_target",
    }
    if not isinstance(raw_status, str) or raw_status not in allowed_statuses:
        raise ValueError("公开来源检测状态异常")
    detail = _required_text(preflight.get("detail"), "公开来源检测说明", max_length=800)
    checked_at = _required_text(preflight.get("checked_at"), "公开来源检测时间", max_length=80)
    canonical_url = preflight.get("canonical_url")
    domain = preflight.get("domain")
    normalized: dict[str, object] = {
        "status": raw_status,
        "detail": detail,
        "checked_at": checked_at,
        "canonical_url": None,
        "domain": None,
        "content_type": None,
        "byte_size": None,
    }
    if canonical_url is not None:
        if not isinstance(canonical_url, str):
            raise ValueError("公开来源检测网址格式异常")
        parsed = urlparse(canonical_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("公开来源检测网址必须是不含凭据或参数的 HTTP/HTTPS 地址")
        normalized_domain = _normalize_domain(parsed.hostname)
        if parsed.hostname.casefold().rstrip(".") != normalized_domain:
            raise ValueError("公开来源检测网址主机名异常")
        if domain is not None and _normalize_domain(domain) != normalized_domain:
            raise ValueError("公开来源检测域名与网址不一致")
        normalized["canonical_url"] = canonical_url
        normalized["domain"] = normalized_domain
    elif raw_status == "ready_for_public_crawl":
        raise ValueError("可直接抓取的公开来源必须包含规范网址")
    if expected_seed_url is not None and normalized["canonical_url"] not in {None, expected_seed_url}:
        raise ValueError("重新检测不得替换已经登记的公开来源网址")
    content_type = preflight.get("content_type")
    if content_type is not None:
        normalized["content_type"] = _required_text(
            content_type,
            "公开来源响应类型",
            max_length=100,
        )
    byte_size = preflight.get("byte_size")
    if byte_size is not None:
        if not isinstance(byte_size, int) or isinstance(byte_size, bool) or byte_size < 0:
            raise ValueError("公开来源响应大小异常")
        normalized["byte_size"] = byte_size
    return normalized


def _public_supplement_source_copy(source: dict[str, Any]) -> dict[str, Any]:
    """返回浏览器可读的补充来源元数据，不添加网页正文、抓取字节或内部路径。"""

    allowed_fields = {
        "source_id",
        "domain",
        "source_type",
        "source_level",
        "evidence_quality",
        "allowed_claim_types",
        "status",
        "created_at",
        "created_by_user_id",
        "change_reason",
        "seed_url",
        "crawl_status",
        "crawl_detail",
        "last_preflight_at",
        "last_preflight_content_type",
        "last_preflight_byte_size",
        "crawl_scope",
    }
    return {field_name: copy.deepcopy(source.get(field_name)) for field_name in allowed_fields}


def list_public_source_supplements(workspace_dir: Path) -> dict[str, Any]:
    """读取人工补充的公开来源及上次检测结果；读取不触发联网或重试。"""

    ledger = load_ledger(workspace_dir)
    sources, _ = _catalog_lists(ledger)
    supplements = [
        _public_supplement_source_copy(source)
        for source in sources
        if isinstance(source, dict)
        and source.get("source_type") == HUMAN_PUBLIC_SUPPLEMENT_SOURCE_TYPE
    ]
    supplements.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return {
        "sources": supplements,
        "count": len(supplements),
        "active_limit": MAX_ACTIVE_HUMAN_PUBLIC_SUPPLEMENTS,
        "crawl_scope": HUMAN_PUBLIC_SUPPLEMENT_CRAWL_SCOPE,
        "ledger_path": str(ledger_path(workspace_dir)),
    }


def get_public_source_supplement(workspace_dir: Path, *, source_id: object) -> dict[str, Any]:
    """按服务器保存的 ID 读取一个补充来源，供重新检测而非浏览器任意换网址。"""

    normalized_source_id = _normalize_public_supplement_source_id(source_id)
    ledger = load_ledger(workspace_dir)
    sources, _ = _catalog_lists(ledger)
    return copy.deepcopy(_public_supplement_source(sources, source_id=normalized_source_id))


def _assert_no_active_domain_conflict(
    sources: list[dict[str, Any]],
    *,
    domain: str,
    excluded_source: dict[str, Any] | None = None,
) -> None:
    """避免同一域名被两个生效目录记录以不同规则或入口重复自动读取。"""

    if any(
        item is not excluded_source
        and isinstance(item, dict)
        and item.get("domain") == domain
        and item.get("status") == "active"
        for item in sources
    ):
        raise ValueError("该域名已有生效来源配置；请先使用现有来源的启停或重新检测入口")


def register_public_source_supplement(
    workspace_dir: Path,
    *,
    preflight: object,
    note: object | None,
    authenticated_user_id: object,
) -> dict[str, Any]:
    """把已通过服务器预检的公开网页登记为低频发现入口，且不改任何历史业务事实。"""

    normalized_preflight = _normalize_public_supplement_preflight(preflight)
    if normalized_preflight["status"] != "ready_for_public_crawl":
        raise ValueError("服务器尚未确认该网址可直接抓取，不能加入自动来源目录")
    canonical_url = normalized_preflight.get("canonical_url")
    domain = normalized_preflight.get("domain")
    if not isinstance(canonical_url, str) or not isinstance(domain, str):
        raise RuntimeError("公开来源预检缺少规范网址或域名")
    normalized_note = (
        "业务人员补充公开来源；服务器已在登记前完成公开访问、robots 与安全检测。"
        if note is None or (isinstance(note, str) and not note.strip())
        else _required_text(note, "note", max_length=1_000)
    )
    normalized_user_id = _required_text(
        authenticated_user_id,
        "authenticated_user_id",
        max_length=100,
    )
    ledger = load_ledger(workspace_dir)
    sources, audit = _catalog_lists(ledger)
    protected_before = _non_catalog_snapshot(ledger)
    active_supplements = sum(
        1
        for item in sources
        if isinstance(item, dict)
        and item.get("source_type") == HUMAN_PUBLIC_SUPPLEMENT_SOURCE_TYPE
        and item.get("status") == "active"
    )
    if active_supplements >= MAX_ACTIVE_HUMAN_PUBLIC_SUPPLEMENTS:
        raise ValueError(
            f"最多同时启用 {MAX_ACTIVE_HUMAN_PUBLIC_SUPPLEMENTS} 个人工补充公开来源；请先暂停不再需要的来源"
        )
    _assert_no_active_domain_conflict(sources, domain=domain)
    operated_at = _utc_now()
    source_id = _stable_id(
        "public-source-supplement",
        {
            "seed_url": canonical_url,
            "operated_at": operated_at,
            "authenticated_user_id": normalized_user_id,
        },
    )
    source = {
        "source_id": source_id,
        "domain": domain,
        "source_type": HUMAN_PUBLIC_SUPPLEMENT_SOURCE_TYPE,
        "source_level": "lead_only",
        "evidence_quality": 1,
        "allowed_claim_types": ["industry_focus"],
        "status": "active",
        "created_at": operated_at,
        "created_by_user_id": normalized_user_id,
        "change_reason": normalized_note,
        "seed_url": canonical_url,
        "crawl_status": normalized_preflight["status"],
        "crawl_detail": normalized_preflight["detail"],
        "last_preflight_at": normalized_preflight["checked_at"],
        "last_preflight_content_type": normalized_preflight["content_type"],
        "last_preflight_byte_size": normalized_preflight["byte_size"],
        "crawl_scope": HUMAN_PUBLIC_SUPPLEMENT_CRAWL_SCOPE,
    }
    sources.append(source)
    _append_audit(
        audit,
        source_id=source_id,
        action="public_source_supplement_registered",
        old_status=None,
        new_status="active",
        reason=normalized_note,
        authenticated_user_id=normalized_user_id,
        operated_at=operated_at,
    )
    _assert_non_catalog_snapshot_unchanged(protected_before, ledger)
    saved_path = save_ledger(workspace_dir, ledger)
    return {
        "source": _public_supplement_source_copy(source),
        "preflight": copy.deepcopy(normalized_preflight),
        "ledger_path": str(saved_path),
    }


def record_public_source_supplement_preflight(
    workspace_dir: Path,
    *,
    source_id: object,
    preflight: object | None,
    authenticated_user_id: object,
    requested_status: str | None,
) -> dict[str, Any]:
    """记录一次重新检测，或安全地启停补充来源；所有网页访问已经在上层完成。

    停用来源不会联网；重新启用必须同时提供成功的服务器前置检测。检测失败不会删除
    历史来源记录或审计，只将最近可抓取状态更新为失败原因，让业务人员选择人工材料、
    稍后重试或继续停用。
    """

    normalized_source_id = _normalize_public_supplement_source_id(source_id)
    normalized_user_id = _required_text(
        authenticated_user_id,
        "authenticated_user_id",
        max_length=100,
    )
    if requested_status not in {None, "active", "disabled"}:
        raise ValueError("requested_status只能是active、disabled或空")
    ledger = load_ledger(workspace_dir)
    sources, audit = _catalog_lists(ledger)
    source = _public_supplement_source(sources, source_id=normalized_source_id)
    protected_before = _non_catalog_snapshot(ledger)
    old_status = source.get("status")
    if old_status not in SOURCE_STATUSES:
        raise RuntimeError("人工补充公开来源状态异常")
    normalized_preflight: dict[str, object] | None = None
    if preflight is not None:
        seed_url = source.get("seed_url")
        if not isinstance(seed_url, str):
            raise RuntimeError("人工补充公开来源缺少受控入口网址")
        normalized_preflight = _normalize_public_supplement_preflight(
            preflight,
            expected_seed_url=seed_url,
        )
        source["crawl_status"] = normalized_preflight["status"]
        source["crawl_detail"] = normalized_preflight["detail"]
        source["last_preflight_at"] = normalized_preflight["checked_at"]
        source["last_preflight_content_type"] = normalized_preflight["content_type"]
        source["last_preflight_byte_size"] = normalized_preflight["byte_size"]
    if requested_status == "active":
        if normalized_preflight is None or normalized_preflight["status"] != "ready_for_public_crawl":
            raise ValueError("重新启用前，服务器必须先确认该网址可直接抓取")
        domain = source.get("domain")
        if not isinstance(domain, str):
            raise RuntimeError("人工补充公开来源缺少有效域名")
        _assert_no_active_domain_conflict(sources, domain=domain, excluded_source=source)
        source["status"] = "active"
        action = "public_source_supplement_enabled"
        reason = "服务器重新检测通过后，由业务人员重新启用自动公开发现。"
    elif requested_status == "disabled":
        source["status"] = "disabled"
        action = "public_source_supplement_disabled"
        reason = "业务人员暂停该公开来源的未来自动发现；历史材料和审计保留。"
    else:
        action = "public_source_supplement_retested"
        reason = str(
            normalized_preflight.get("detail") if normalized_preflight is not None else "重新检测未执行"
        )
    operated_at = _utc_now()
    _append_audit(
        audit,
        source_id=normalized_source_id,
        action=action,
        old_status=old_status if isinstance(old_status, str) else None,
        new_status=str(source.get("status")),
        reason=reason[:2_000],
        authenticated_user_id=normalized_user_id,
        operated_at=operated_at,
    )
    _assert_non_catalog_snapshot_unchanged(protected_before, ledger)
    saved_path = save_ledger(workspace_dir, ledger)
    return {
        "source": _public_supplement_source_copy(source),
        "preflight": copy.deepcopy(normalized_preflight),
        "ledger_path": str(saved_path),
    }


def _ensure_fixed_source_seeds(
    workspace_dir: Path,
    *,
    seeds: tuple[dict[str, object], ...],
    operation_scope: str,
) -> dict[str, Any]:
    """追加缺失的固定免费来源种子，并验证没有改写目录以外的业务台账。

    输入是服务器工作区、源码固定的来源定义和固定操作范围；输出为本次新增/跳过记录。
    此内部函数不接受模型、前端或网页传入的域名、操作者、事实或证据字段。它只追加
    ``trusted_sources`` 与对应审计；写入前后逐字段比对其余台账，所以不能改写线索、
    原始 evidence、URL、摘录、采集时间、评分或人工复核。实际网络访问不在这里发生，
    后续发现器仍须通过 robots、DNS、同域限制、限频与快照核验。
    """
    ledger = load_ledger(workspace_dir)
    sources, audit = _catalog_lists(ledger)
    protected_before = _non_catalog_snapshot(ledger)
    existing_domains = {
        source.get("domain")
        for source in sources
        if isinstance(source, dict) and isinstance(source.get("domain"), str)
    }
    created_at = _utc_now()
    added_sources: list[dict[str, object]] = []
    skipped_domains: list[str] = []
    for seed in seeds:
        domain = seed["domain"]
        if not isinstance(domain, str):
            raise RuntimeError("固定免费来源种子缺少有效域名")
        if domain in existing_domains:
            skipped_domains.append(domain)
            continue
        source = {
            **copy.deepcopy(seed),
            "status": "active",
            "created_at": created_at,
            "created_by_user_id": "system-default",
        }
        source_id = source.get("source_id")
        reason = source.get("change_reason")
        if not isinstance(source_id, str) or not isinstance(reason, str):
            raise RuntimeError("固定免费来源种子缺少审计字段")
        sources.append(source)
        _append_audit(
            audit,
            source_id=source_id,
            action="source_system_seeded",
            old_status=None,
            new_status="active",
            reason=reason,
            authenticated_user_id="system-default",
            operated_at=created_at,
        )
        existing_domains.add(domain)
        added_sources.append(copy.deepcopy(source))
    _assert_non_catalog_snapshot_unchanged(protected_before, ledger)
    if not added_sources:
        return {
            "status": "already_configured",
            "added_sources": [],
            "skipped_domains": sorted(skipped_domains),
            "operation_scope": operation_scope,
            "ledger_path": str(ledger_path(workspace_dir)),
        }
    saved_path = save_ledger(workspace_dir, ledger)
    return {
        "status": "seeded",
        "added_sources": added_sources,
        "skipped_domains": sorted(skipped_domains),
        "operation_scope": operation_scope,
        "ledger_path": str(saved_path),
    }


def ensure_official_statistics_source_seeds(workspace_dir: Path) -> dict[str, Any]:
    """只追加缺失的免费官方统计来源种子，且始终尊重普通用户已有目录决定。

    输入为服务器已隔离的工作区，输出为国家统计局与国家数据平台的新增/跳过审计。两条
    来源在源码中固定，只允许公开产业/扩张类事实；同域已有任何人工状态即跳过。函数不
    联网、不抓取页面、不创建线索或证据，并借内部快照校验确保不会改写 URL、原始摘录、
    采集时间、评分或人工业务状态。
    """
    return _ensure_fixed_source_seeds(
        workspace_dir,
        seeds=OFFICIAL_STATISTICS_SOURCE_SEEDS,
        operation_scope="system_official_statistics_source_seed_configuration_only",
    )


def ensure_government_investment_source_seeds(workspace_dir: Path) -> dict[str, Any]:
    """只追加缺失的省级公开招商来源，并尊重任何同域人工停用决定。"""
    return _ensure_fixed_source_seeds(
        workspace_dir,
        seeds=GOVERNMENT_INVESTMENT_SOURCE_SEEDS,
        operation_scope="system_government_investment_source_seed_configuration_only",
    )


def ensure_free_verified_media_source_seeds(workspace_dir: Path) -> dict[str, Any]:
    """只追加经代码预检的免费媒体发现入口，不把二手报道升级为招商事实。

    输入为服务器已隔离的工作区，输出为五个固定媒体域名的新增/跳过审计。来源清单不能
    由模型、前端或网页动态注入；每个域名仍会在访问时独立检查 robots。媒体记录固定为
    secondary/3 分，标题命中只会创建待核验候选，不能单独证明融资、徐州关系或落地意愿。
    任何同域人工 disabled/superseded 决定都会被保留，且内部快照校验保证不改写证据链。
    """
    return _ensure_fixed_source_seeds(
        workspace_dir,
        seeds=FREE_VERIFIED_MEDIA_SOURCE_SEEDS,
        operation_scope="system_free_verified_media_source_seed_configuration_only",
    )


def register_trusted_source(
    workspace_dir: Path,
    *,
    domain: str,
    source_type: str,
    source_level: str,
    evidence_quality: int,
    allowed_claim_types: list[str],
    change_reason: str,
    authenticated_user_id: str,
) -> dict[str, Any]:
    """由已认证普通用户登记新版可信来源配置，并保留同域旧配置的审计痕迹。

    输入为公开域名、受控来源类型、等级、质量、允许事实类别、变更原因，以及只能来自
    认证层的用户 ID；输出为新目录记录及台账路径。新增同域名配置时，旧的生效记录只
    会被标为 ``superseded`` 并写审计，新记录才生效；不会覆盖旧记录，更不会修改已有
    线索、证据、采集快照、评分或结果反馈。此为普通业务配置操作，无需管理员角色，
    但前端、模型或网页内容不得指定 ``authenticated_user_id``。
    """
    normalized_domain = _normalize_domain(domain)
    normalized_source_type = _normalize_source_type(source_type)
    normalized_source_level = _normalize_source_level(source_level, normalized_source_type)
    normalized_evidence_quality = _normalize_evidence_quality(
        evidence_quality,
        normalized_source_type,
    )
    normalized_claim_types = _normalize_allowed_claim_types(
        allowed_claim_types,
        normalized_source_type,
    )
    normalized_reason = _required_text(change_reason, "change_reason", max_length=2_000)
    normalized_user_id = _required_text(
        authenticated_user_id,
        "authenticated_user_id",
        max_length=100,
    )

    ledger = load_ledger(workspace_dir)
    sources, audit = _catalog_lists(ledger)
    protected_fields = ("leads", "evidence", "score_snapshots", "outcome_feedback")
    protected_before = {
        field_name: json.dumps(ledger[field_name], ensure_ascii=False, sort_keys=True)
        for field_name in protected_fields
    }
    operated_at = _utc_now()
    for existing_source in sources:
        if (
            isinstance(existing_source, dict)
            and existing_source.get("domain") == normalized_domain
            and existing_source.get("status") == "active"
        ):
            if existing_source.get("source_type") == HUMAN_PUBLIC_SUPPLEMENT_SOURCE_TYPE:
                # 人工补充网页是以 URL、robots 和当次访问结果为边界的低频发现入口。
                # 通用目录登记只接受域名及人工分级，不能把它直接升级为可信来源并绕过
                # 重新检测。因此先由业务人员停用该入口，再登记独立的可信来源配置。
                raise ValueError(
                    "该域名目前是已启用的人工补充公开网页；请先在“人工补充公开来源”中暂停它，"
                    "再登记独立的可信来源配置，不能自动替换或升级。"
                )
            existing_source["status"] = "superseded"
            _append_audit(
                audit,
                source_id=str(existing_source.get("source_id")),
                action="source_superseded",
                old_status="active",
                new_status="superseded",
                reason=f"同域名新配置生效：{normalized_reason}",
                authenticated_user_id=normalized_user_id,
                operated_at=operated_at,
            )

    source_id = _stable_id(
        "trusted-source",
        {
            "domain": normalized_domain,
            "operated_at": operated_at,
            "authenticated_user_id": normalized_user_id,
        },
    )
    source = {
        "source_id": source_id,
        "domain": normalized_domain,
        "source_type": normalized_source_type,
        "source_level": normalized_source_level,
        "evidence_quality": normalized_evidence_quality,
        "allowed_claim_types": normalized_claim_types,
        "status": "active",
        "created_at": operated_at,
        "created_by_user_id": normalized_user_id,
        "change_reason": normalized_reason,
    }
    sources.append(source)
    _append_audit(
        audit,
        source_id=source_id,
        action="source_registered",
        old_status=None,
        new_status="active",
        reason=normalized_reason,
        authenticated_user_id=normalized_user_id,
        operated_at=operated_at,
    )
    _assert_evidence_related_data_unchanged(protected_before, ledger)
    saved_path = save_ledger(workspace_dir, ledger)
    return {"source": source, "ledger_path": str(saved_path)}


def set_trusted_source_status(
    workspace_dir: Path,
    *,
    source_id: str,
    new_status: str,
    change_reason: str,
    authenticated_user_id: str,
) -> dict[str, Any]:
    """由已认证普通用户启用或停用目录记录，并追加审计且不改写任何历史证据。

    输入为目录 ID、目标业务状态、原因和服务器认证用户 ID，输出为更新后的目录记录。
    仅允许 ``active`` 与 ``disabled`` 两种人工可操作状态；若重新启用会先检查同域名
    是否存在另一条生效记录，以免未来页面分级不确定。操作不接受客户端操作者 ID，
    只更改目录配置与审计，绝不改变已保存证据的当时来源等级或红色预警历史。
    """
    normalized_source_id = _required_text(source_id, "source_id", max_length=100)
    normalized_status = _required_text(new_status, "new_status", max_length=100).casefold()
    if normalized_status not in {"active", "disabled"}:
        raise ValueError("new_status只能是active或disabled")
    normalized_reason = _required_text(change_reason, "change_reason", max_length=2_000)
    normalized_user_id = _required_text(
        authenticated_user_id,
        "authenticated_user_id",
        max_length=100,
    )

    ledger = load_ledger(workspace_dir)
    sources, audit = _catalog_lists(ledger)
    source = next(
        (item for item in sources if isinstance(item, dict) and item.get("source_id") == normalized_source_id),
        None,
    )
    if source is None:
        raise ValueError("未找到对应的可信来源目录记录")
    if source.get("source_type") == HUMAN_PUBLIC_SUPPLEMENT_SOURCE_TYPE:
        # 这一类来源需要在启用前即时复测 robots、页面访问和公网边界；普通目录状态接口
        # 只改配置、不联网，若允许它启用会绕开该安全前提。因此必须走专用补充来源接口。
        raise ValueError("人工补充公开来源请使用“重新检测并启用/暂停”入口，不能绕过服务器访问校验")
    domain = source.get("domain")
    if not isinstance(domain, str):
        raise ValueError("可信来源目录记录缺少有效域名")
    if normalized_status == "active" and any(
        item is not source
        and isinstance(item, dict)
        and item.get("domain") == domain
        and item.get("status") == "active"
        for item in sources
    ):
        raise ValueError("同一域名已有生效可信来源配置，不能同时启用")

    protected_fields = ("leads", "evidence", "score_snapshots", "outcome_feedback")
    protected_before = {
        field_name: json.dumps(ledger[field_name], ensure_ascii=False, sort_keys=True)
        for field_name in protected_fields
    }
    old_status = source.get("status")
    if old_status not in SOURCE_STATUSES:
        raise ValueError("可信来源目录记录状态异常")
    operated_at = _utc_now()
    source["status"] = normalized_status
    _append_audit(
        audit,
        source_id=normalized_source_id,
        action="source_status_changed",
        old_status=old_status,
        new_status=normalized_status,
        reason=normalized_reason,
        authenticated_user_id=normalized_user_id,
        operated_at=operated_at,
    )
    _assert_evidence_related_data_unchanged(protected_before, ledger)
    saved_path = save_ledger(workspace_dir, ledger)
    return {"source": source, "ledger_path": str(saved_path)}


def classify_captured_source(
    workspace_dir: Path,
    *,
    source_url: str,
    claim_type: str,
) -> dict[str, Any]:
    """按生效可信目录为已抓取页面确定可用证据等级，未匹配时安全降级为线索。

    输入是服务器抓取后得到的最终 URL 和待登记事实类别，输出为分级及可选目录快照。
    域名匹配采用完整域名或子域名后缀，并优先最长匹配；还必须满足目录允许的事实类别。
    这是一项纯读取判断：不改目录、不写线索或证据。未匹配、目录停用、类别不允许或
    结构异常时一律返回 ``lead_only`` 与质量 2，避免仅因页面自称官方而自动升格。
    """
    parsed = urlparse(source_url)
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise ValueError("source_url必须是完整的HTTP或HTTPS地址")
    normalized_claim_type = _canonical(
        claim_type,
        "claim_type",
        CLAIM_TYPES,
        CLAIM_TYPE_ALIASES,
    )
    ledger = load_ledger(workspace_dir)
    sources, _ = _catalog_lists(ledger)
    matched_sources = [
        source
        for source in sources
        if isinstance(source, dict)
        and source.get("status") == "active"
        and isinstance(source.get("domain"), str)
        and (hostname == source["domain"] or hostname.endswith(f".{source['domain']}"))
    ]
    if not matched_sources:
        return {
            "source_level": "lead_only",
            "evidence_quality": 2,
            "capture_source_type": "automated_public_search_capture",
            "trusted_source": None,
            "classification_reason": "最终域名未匹配生效可信来源目录",
        }
    matched_source = max(matched_sources, key=lambda source: len(str(source["domain"])))
    allowed_claim_types = matched_source.get("allowed_claim_types")
    if not isinstance(allowed_claim_types, list) or normalized_claim_type not in allowed_claim_types:
        return {
            "source_level": "lead_only",
            "evidence_quality": 2,
            "capture_source_type": "automated_public_search_capture",
            "trusted_source": None,
            "classification_reason": "可信域名不支持当前事实类别，已安全降级",
        }
    source_level = matched_source.get("source_level")
    evidence_quality = matched_source.get("evidence_quality")
    source_id = matched_source.get("source_id")
    source_type = matched_source.get("source_type")
    if (
        source_level not in {"primary", "secondary"}
        or not isinstance(evidence_quality, int)
        or not isinstance(source_id, str)
        or not isinstance(source_type, str)
    ):
        return {
            "source_level": "lead_only",
            "evidence_quality": 2,
            "capture_source_type": "automated_public_search_capture",
            "trusted_source": None,
            "classification_reason": "可信来源目录结构异常，已安全降级",
        }
    return {
        "source_level": source_level,
        "evidence_quality": evidence_quality,
        "capture_source_type": "automated_trusted_catalog_capture",
        "trusted_source": {
            "source_id": source_id,
            "source_type": source_type,
            "domain": matched_source["domain"],
        },
        "classification_reason": "最终域名与允许事实类别均匹配生效可信来源目录",
    }
