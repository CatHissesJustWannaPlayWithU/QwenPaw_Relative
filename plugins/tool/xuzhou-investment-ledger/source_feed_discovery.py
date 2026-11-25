"""徐州招商可信公开来源的 robots、RSS、Sitemap 与固定产业栏目合规候选发现。"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse
from xml.etree import ElementTree

from .source_capture import (
    ALLOWED_CONTENT_TYPES,
    RSS_CONTENT_TYPES,
    _fetch_public_bytes,
    _validate_public_url,
)
from .source_catalog import HUMAN_PUBLIC_SUPPLEMENT_SOURCE_TYPE
from .source_monitoring import apply_source_health_outcome, is_source_eligible
from .storage import ledger_path, load_ledger, save_ledger


# 上限按“一个定时批次”设计，避免可信目录增多后将公开站点变成高频抓取目标。
MAX_TRUSTED_SOURCES_PER_RUN = 5
# 业务人员补充来源虽然已经过一次预检，仍必须和所有目录来源共用固定低频预算；单轮最多
# 两条，避免新增一批网址后挤掉官方/既有来源，也避免将公开网站变成批量抓取目标。
MAX_HUMAN_PUBLIC_SUPPLEMENTS_PER_RUN = 2
MAX_RSS_FEEDS_PER_SOURCE = 3
MAX_SITEMAPS_PER_SOURCE = 3
MAX_ITEMS_PER_FEED = 8
MAX_URLS_PER_SITEMAP = 12
MAX_HOME_PAGE_LINKS = 12
# 同一来源的一轮固定栏目读取上限。它不是可配置参数，防止“可靠来源”日后被误扩成高频
# 批量采集；三条生物医药细分栏目加起来仍只会低频读取三页公开索引，不读取候选正文。
MAX_FIXED_SECTOR_INDEX_PAGES_PER_SOURCE = 3
ROBOTS_CONTENT_TYPES = {"text/plain"}
XML_CONTENT_TYPES = {"application/xml", "text/xml", "application/rss+xml", "application/atom+xml"}
ROBOTS_USER_AGENT = "QwenPaw-XuzhouInvestmentEvidence"

# 这是经过人工代码审查、并由本轮实际 robots 与 HTML 结构预检过的固定公开栏目，而不是
# 用户、模型、网页或来源目录可提交的 URL。值只指向已登记 ``pedaily.cn`` 的同域子域名；
# 每次运行仍会单独检查该路径的 robots，失败即跳过。栏目页只提供“企业/机构待补证线索”，
# 不替代企业官网、融资公告、工商材料或徐州关联的一手证据。
FIXED_SECTOR_INDEX_URLS_BY_SOURCE_ID: dict[str, dict[str, tuple[str, ...]]] = {
    "trusted-pedaily-investment-events": {
        "embodied_intelligence": ("https://vc.pedaily.cn/invest/f2844",),
        "semiconductor": ("https://vc.pedaily.cn/invest/f2842",),
        "biomedicine": (
            "https://vc.pedaily.cn/invest/f2780",
            "https://vc.pedaily.cn/invest/f2782",
            "https://vc.pedaily.cn/invest/f2777",
        ),
    },
}


class _HomepageResourceExtractor(HTMLParser):
    """从可信来源首页提取 RSS/Atom 声明和可见锚文本，不执行脚本。"""

    _IGNORED_TAGS = {"script", "style", "noscript", "template", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.feed_hrefs: list[str] = []
        self.anchor_records: list[tuple[str, str]] = []
        self._ignored_depth = 0
        self._anchor_href: str | None = None
        self._anchor_text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """提取明确 RSS 声明，或开始收集可见同页锚文本。"""
        normalized_tag = tag.casefold()
        if normalized_tag in self._IGNORED_TAGS:
            self._ignored_depth += 1
            return
        values = {name.casefold(): value for name, value in attrs if value is not None}
        if normalized_tag == "link":
            rel = values.get("rel", "").casefold().split()
            content_type = values.get("type", "").casefold().split(";", 1)[0].strip()
            href = values.get("href")
            if "alternate" in rel and content_type in {
                "application/rss+xml",
                "application/atom+xml",
            }:
                if href:
                    self.feed_hrefs.append(href)
        elif normalized_tag == "a" and self._ignored_depth == 0:
            href = values.get("href")
            if href:
                self._anchor_href = href
                self._anchor_text_parts = []

    def handle_endtag(self, tag: str) -> None:
        """在可见锚标签结束时保留其链接和标题，不保存首页全文。"""
        normalized_tag = tag.casefold()
        if normalized_tag in self._IGNORED_TAGS and self._ignored_depth > 0:
            self._ignored_depth -= 1
            return
        if normalized_tag != "a" or self._anchor_href is None:
            return
        anchor_text = " ".join(" ".join(self._anchor_text_parts).split())
        if anchor_text:
            self.anchor_records.append((self._anchor_href, anchor_text[:500]))
        self._anchor_href = None
        self._anchor_text_parts = []

    def handle_data(self, data: str) -> None:
        """只累计当前可见锚文本，忽略脚本、样式和没有链接归属的首页内容。"""
        if self._ignored_depth == 0 and self._anchor_href is not None:
            self._anchor_text_parts.append(data)


class _InvestmentEventIndexExtractor(HTMLParser):
    """从固定投融资栏目解析公开事件卡片，不读取任何候选公司正文。

    输入是已通过 robots 检查的、源码固定栏目页 HTML；输出保留每张 ``item`` 卡片中可见的
    公司页链接、显示名、行业、轮次/金额、日期与摘要。解析只接受 ``/company/<数字>.html``
    这类同站结构，因而不会把导航、机构链接、广告或任意锚文本误当作公司候选。它没有
    网络、台账或证据写入能力；字段仍只是二手索引摘要，后续必须由原页快照和人工核验。
    """

    _COMPANY_PATH_PATTERN = re.compile(r"^/company/\d+\.html$")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[dict[str, str]] = []
        self._item_div_depth = 0
        self._current_item: dict[str, str] | None = None
        self._active_field: str | None = None
        self._field_text_parts: list[str] = []
        self._h3_depth = 0
        self._company_href: str | None = None
        self._company_text_parts: list[str] = []

    @staticmethod
    def _class_tokens(attrs: list[tuple[str, str | None]]) -> set[str]:
        """规范化 HTML class 属性，供固定卡片结构判断而不解释页面业务内容。"""
        for name, value in attrs:
            if name.casefold() == "class" and isinstance(value, str):
                return {token.casefold() for token in value.split()}
        return set()

    def _start_field(self, field_name: str) -> None:
        """开始收集一段可见卡片文本；同一时刻只允许一个字段，避免跨卡片混写。"""
        self._active_field = field_name
        self._field_text_parts = []

    def _finish_field(self) -> None:
        """结束当前字段并写入内存卡片，空白文本不伪造默认值。"""
        if self._current_item is None or self._active_field is None:
            return
        value = " ".join(" ".join(self._field_text_parts).split())
        if value:
            self._current_item[self._active_field] = value[:2_000]
        self._active_field = None
        self._field_text_parts = []

    def _finish_item(self) -> None:
        """结束一张固定卡片，仅在公司 URL 和显示名完整时追加内存结果。"""
        if self._current_item is None:
            return
        self._finish_field()
        company_url = self._current_item.get("company_url")
        company_name = self._current_item.get("company_name")
        if isinstance(company_url, str) and isinstance(company_name, str) and company_name:
            self.items.append(dict(self._current_item))
        self._current_item = None
        self._item_div_depth = 0
        self._h3_depth = 0
        self._company_href = None
        self._company_text_parts = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """按固定卡片的 div/h3/a 结构提取字段，不处理脚本和跨域资源。"""
        normalized_tag = tag.casefold()
        classes = self._class_tokens(attrs)
        if normalized_tag == "div" and "item" in classes and self._current_item is None:
            self._current_item = {}
            self._item_div_depth = 1
            return
        if self._current_item is None:
            return
        if normalized_tag == "div":
            self._item_div_depth += 1
            if "ai-summary" in classes:
                self._start_field("summary")
            elif "ind" in classes:
                self._start_field("industry")
            elif "d" in classes:
                self._start_field("published_at")
            elif "t" in classes:
                self._start_field("round_and_amount")
        elif normalized_tag == "h3":
            self._h3_depth += 1
        elif normalized_tag == "a" and self._h3_depth > 0:
            href = next(
                (value for name, value in attrs if name.casefold() == "href"),
                None,
            )
            if isinstance(href, str) and self._COMPANY_PATH_PATTERN.fullmatch(urlparse(href).path):
                self._company_href = href
                self._company_text_parts = []

    def handle_endtag(self, tag: str) -> None:
        """在标签闭合时完成字段和公司链接，防止后一张卡片继承前一张内容。"""
        normalized_tag = tag.casefold()
        if self._current_item is None:
            return
        if normalized_tag == "a" and self._company_href is not None:
            company_name = " ".join(" ".join(self._company_text_parts).split())
            if company_name:
                self._current_item["company_url"] = self._company_href
                self._current_item["company_name"] = company_name[:500]
            self._company_href = None
            self._company_text_parts = []
        elif normalized_tag == "h3" and self._h3_depth > 0:
            self._h3_depth -= 1
        elif normalized_tag == "div":
            self._finish_field()
            self._item_div_depth -= 1
            if self._item_div_depth == 0:
                self._finish_item()

    def handle_data(self, data: str) -> None:
        """只累计当前卡片内可见文字；页面未出现的字段绝不由程序猜测补齐。"""
        if self._current_item is None:
            return
        if self._active_field is not None:
            self._field_text_parts.append(data)
        if self._company_href is not None:
            self._company_text_parts.append(data)


def _utc_now() -> str:
    """返回 robots、RSS、Sitemap 发现审计使用的服务器 UTC 时间。"""
    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, payload: dict[str, object]) -> str:
    """依据服务器生成的审计输入生成稳定 ID，不把网页文本作为 ID 或事实依据。"""
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}-{hashlib.sha256(serialized.encode('utf-8')).hexdigest()[:16]}"


def _decode_response(response: dict[str, str | bytes | int]) -> str:
    """解码受控抓取器返回的小型公开响应，格式异常时显式失败而不猜测内容。"""
    raw_content = response.get("raw_content")
    charset = response.get("charset")
    if not isinstance(raw_content, bytes) or not isinstance(charset, str):
        raise RuntimeError("公开来源响应格式异常")
    return raw_content.decode(charset, errors="replace")


def _origin_for_url(source_url: str) -> str:
    """提取公开 URL 的协议和主机，供 robots 与首页探测使用，舍弃路径和查询参数。"""
    normalized_url, _ = _validate_public_url(source_url)
    parsed = urlparse(normalized_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _trusted_origin_candidates(domain: str) -> tuple[str, ...]:
    """为已登记可信主域名生成受限首页探测顺序，不扩展到站外域名。

    输入只能是来源目录已保存的主域名，输出为裸域名和（必要时）同一主域名的 ``www``
    子域名。部分高校官网仅在 ``www`` 主机配置 DNS/TLS，先前裸域名失败会错误减少免费
    一手来源覆盖；该回退仅在裸域名 robots 不可读取时使用，且 ``www.example.edu`` 仍由
    ``_matches_domain`` 限制在 ``example.edu`` 域内。函数不联网、不写入目录、候选或证据，
    更不会用 IP、重定向站外域名、伪造 TLS 或绕过 robots。
    """

    normalized_domain = domain.casefold().strip().rstrip(".")
    if not normalized_domain or "/" in normalized_domain or ":" in normalized_domain:
        raise ValueError("可信来源域名格式异常")
    origins = [f"https://{normalized_domain}"]
    if not normalized_domain.startswith("www."):
        origins.append(f"https://www.{normalized_domain}")
    # 统一走现有公网 URL 校验，拒绝私网、凭据、非常规端口等异常目录内容。
    return tuple(_origin_for_url(origin) for origin in origins)


def _matches_domain(source_url: str, domain: str) -> bool:
    """确认自动发现的 RSS/Sitemap 链接仍属于已启用目录域名或其子域名。"""
    parsed = urlparse(source_url)
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    normalized_domain = domain.casefold().rstrip(".")
    return hostname == normalized_domain or hostname.endswith(f".{normalized_domain}")


def _clean_public_url(source_url: str, *, base_url: str, domain: str) -> str | None:
    """规范化一条页面、RSS 或 Sitemap 链接，拒绝跨域、私网和凭据 URL。

    输入来自已读取的公开 robots、首页或 XML，不接受客户端 URL；输出为同一可信域名
    范围内的完整公开链接或空。函数只做格式与域名边界校验，不联网、不写入候选，也不
    把链接视为网页事实或可信证据。
    """
    resolved = urljoin(base_url, source_url.strip())
    try:
        normalized_url, _ = _validate_public_url(resolved)
    except ValueError:
        return None
    return normalized_url if _matches_domain(normalized_url, domain) else None


def _local_name(element: ElementTree.Element) -> str:
    """去除 XML 命名空间，兼容 RSS、Atom、Sitemap 的标准元素名称。"""
    return element.tag.rsplit("}", 1)[-1].casefold()


def _robots_rule_matches(target_path: str, rule_path: str) -> bool:
    """按 robots 通配符和末尾 $ 规则匹配目标路径，避免把 ``/*.pdf`` 误判为整站禁止。"""
    if not rule_path:
        return False
    end_anchored = rule_path.endswith("$")
    raw_pattern = rule_path[:-1] if end_anchored else rule_path
    expression = re.escape(raw_pattern).replace(r"\*", ".*")
    return re.match(f"^{expression}{'$' if end_anchored else ''}", target_path) is not None


def _parse_robots(
    robots_text: str,
    *,
    target_path: str,
    robots_url: str,
) -> dict[str, Any]:
    """按 robots 的 Allow/Disallow 最长匹配规则判断本插件用户代理是否可访问。

    输入是服务器下载的 robots 文本、目标路径和 robots URL；输出为允许结果、原因及
    声明的 Sitemap 列表。解析只支持公开标准指令，不尝试规避未知限制；不能确定的
    策略一律由调用方按保守方式处理，且不会修改证据、评分或人工复核数据。
    """
    groups: list[dict[str, list[tuple[str, str]] | list[str]]] = []
    current_agents: list[str] = []
    current_rules: list[tuple[str, str]] = []
    sitemap_urls: list[str] = []

    def flush_group() -> None:
        """在新的 User-agent 组开始前保存前一组规则，避免不同抓取器策略混在一起。"""
        nonlocal current_agents, current_rules
        if current_agents:
            groups.append({"agents": current_agents, "rules": current_rules})
        current_agents = []
        current_rules = []

    for raw_line in robots_text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        name, raw_value = line.split(":", 1)
        directive = name.strip().casefold()
        value = raw_value.strip()
        if directive == "sitemap" and value:
            sitemap_urls.append(value)
            continue
        if directive == "user-agent":
            if current_rules:
                flush_group()
            if value:
                current_agents.append(value.casefold())
            continue
        if directive in {"allow", "disallow"} and current_agents:
            current_rules.append((directive, value))
    flush_group()

    matching_groups: list[tuple[int, list[tuple[str, str]]]] = []
    for group in groups:
        agents = group.get("agents")
        rules = group.get("rules")
        if not isinstance(agents, list) or not isinstance(rules, list):
            continue
        matched_lengths = [
            len(agent)
            for agent in agents
            if agent == "*" or ROBOTS_USER_AGENT.casefold().startswith(agent)
        ]
        if matched_lengths:
            matching_groups.append((max(matched_lengths), rules))
    if not matching_groups:
        return {
            "allowed": True,
            "reason": "robots_no_matching_user_agent_rule",
            "robots_url": robots_url,
            "sitemap_urls": sitemap_urls,
        }

    best_group_length = max(length for length, _ in matching_groups)
    matching_rules = [
        rule for length, rules in matching_groups if length == best_group_length for rule in rules
    ]
    best_rule: tuple[int, str] | None = None
    for directive, path_pattern in matching_rules:
        if not path_pattern:
            continue
        if not _robots_rule_matches(target_path, path_pattern):
            continue
        candidate = (len(path_pattern.replace("*", "").rstrip("$")), directive)
        if best_rule is None or candidate[0] > best_rule[0] or (
            candidate[0] == best_rule[0] and candidate[1] == "allow"
        ):
            best_rule = candidate
    allowed = best_rule is None or best_rule[1] == "allow"
    return {
        "allowed": allowed,
        "reason": "robots_allow" if allowed else "robots_disallow",
        "robots_url": robots_url,
        "sitemap_urls": sitemap_urls,
    }


def check_robots_access(source_url: str) -> dict[str, Any]:
    """读取同源 robots.txt 并保守判断候选页是否允许本自动化流程访问。

    输入是服务器已保存或自动发现的公开 URL；输出含允许标记、策略原因和 Sitemap
    声明。robots 缺失（404）按标准视为无规则、允许访问；其他读取或 MIME 异常均
    返回 ``allowed=False``，从而停止自动请求而非尝试伪装或绕过。函数只读网络，不写
    台账、不生成证据，也不修改任何来源 URL、原始摘录、采集时间或评分依据。
    """
    normalized_url, _ = _validate_public_url(source_url)
    parsed = urlparse(normalized_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        response = _fetch_public_bytes(robots_url, allowed_content_types=ROBOTS_CONTENT_TYPES)
    except (OSError, RuntimeError, ValueError) as exc:
        message = str(exc)
        if "HTTP 状态码：404" in message:
            return {
                "allowed": True,
                "reason": "robots_not_found",
                "robots_url": robots_url,
                "sitemap_urls": [],
            }
        return {
            "allowed": False,
            "reason": f"robots_unavailable:{message[:300]}",
            "robots_url": robots_url,
            "sitemap_urls": [],
        }
    target_path = parsed.path or "/"
    if parsed.query:
        target_path = f"{target_path}?{parsed.query}"
    return _parse_robots(
        _decode_response(response),
        target_path=target_path,
        robots_url=robots_url,
    )


def _discover_homepage_resources(
    origin: str,
    *,
    domain: str,
    restrict_redirect_to_initial_host: bool = False,
) -> dict[str, list[object]]:
    """从已获 robots 许可的可信首页或固定栏目提取受限公开索引。

    输入是已通过 robots 检查的同域首页或源码固定栏目地址和目录域名；输出包含 RSS/Atom
    地址、锚文本已命中预设产业词的同域链接，以及（若页面恰为固定投融资栏目）结构化
    投资事件卡片。函数只读取一次索引 HTML，不运行脚本、不读取候选公司正文，并在 URL
    规范化时再次限制同域和公网。所有输出只是待补证索引，不是页面摘录、企业事实或证据；
    没有满足严格资格门槛或固定栏目受限规则的项目仍会被拒绝。
    """
    if restrict_redirect_to_initial_host:
        response = _fetch_public_bytes(
            origin,
            allowed_content_types=ALLOWED_CONTENT_TYPES,
            restrict_redirect_to_initial_host=True,
        )
    else:
        response = _fetch_public_bytes(origin, allowed_content_types=ALLOWED_CONTENT_TYPES)
    page_text = _decode_response(response)
    extractor = _HomepageResourceExtractor()
    extractor.feed(page_text)
    extractor.close()
    investment_event_extractor = _InvestmentEventIndexExtractor()
    investment_event_extractor.feed(page_text)
    investment_event_extractor.close()
    feed_urls: list[str] = []
    for href in extractor.feed_hrefs:
        if len(feed_urls) >= MAX_RSS_FEEDS_PER_SOURCE:
            break
        normalized_url = _clean_public_url(href, base_url=origin, domain=domain)
        if normalized_url and normalized_url not in feed_urls:
            feed_urls.append(normalized_url)

    homepage_items: list[dict[str, str]] = []
    for href, title in extractor.anchor_records:
        normalized_url = _clean_public_url(href, base_url=origin, domain=domain)
        if not normalized_url or any(item["source_url"] == normalized_url for item in homepage_items):
            continue
        # 先按标题/URL 识别目标产业，避免导航、登录、隐私政策等链接占满有限的首页额度；
        # 是否是中国企业招商机会仍必须交由 add_candidate 中的严格资格门槛决定。
        if not _matching_sectors(title=title, snippet="", source_url=normalized_url):
            continue
        homepage_items.append(
            {
                "source_title": title,
                "source_url": normalized_url,
                "search_snippet": "已登记可信来源目录首页的公开链接，需结合原页面继续核验。",
            }
        )
        if len(homepage_items) >= MAX_HOME_PAGE_LINKS:
            break
    investment_event_items: list[dict[str, str]] = []
    for item in investment_event_extractor.items:
        company_url = item.get("company_url")
        company_name = item.get("company_name")
        if not isinstance(company_url, str) or not isinstance(company_name, str):
            continue
        normalized_url = _clean_public_url(company_url, base_url=origin, domain=domain)
        if not normalized_url:
            continue
        # 卡片中行业文字是来源页面可见字段；预筛只用于限制三条目标产业，不据此声明该
        # 公司真实所属赛道。实际招商判断仍需要原页和人工交叉核验。
        industry = str(item.get("industry", ""))
        summary = str(item.get("summary", ""))
        if not _matching_sectors(
            title=company_name,
            snippet=f"{industry} {summary}",
            source_url=normalized_url,
        ):
            continue
        investment_event_items.append(
            {
                "source_title": company_name[:500],
                "source_url": normalized_url,
                "search_snippet": _investment_event_snippet(item),
            }
        )
        if len(investment_event_items) >= MAX_HOME_PAGE_LINKS:
            break
    return {
        "feed_urls": feed_urls,
        "homepage_items": homepage_items,
        "investment_event_items": investment_event_items,
    }


def _investment_event_snippet(item: dict[str, str]) -> str:
    """组装固定投资事件卡片的可展示索引摘要，不添加页面不存在的业务事实。

    输入是 HTML 解析器已提取的卡片字段；输出为带字段名称的有限摘要，供资格规则和人工
    看到为何该公司页被发现。缺失字段直接省略，绝不用“中国企业”“徐州项目”等推断性
    文本填充；该摘要仍是二手媒体索引，不能覆盖后续原页快照或正式 evidence。
    """
    labels = (
        ("industry", "页面行业"),
        ("round_and_amount", "融资轮次及金额"),
        ("published_at", "页面日期"),
        ("summary", "页面摘要"),
    )
    parts = [
        f"{label}：{value.strip()}"
        for field_name, label in labels
        if isinstance((value := item.get(field_name)), str) and value.strip()
    ]
    return "；".join(parts)[:2_000]


def _fixed_sector_index_urls_for_source(
    *,
    source_id: str,
    domain: str,
    requested_sector: str | None,
) -> tuple[str, ...]:
    """返回唯一源码固定来源在本产业可访问的栏目 URL，拒绝目录/模型注入。

    输入是已经从完整性校验台账读取的来源 ID、域名和可选规范产业标识；输出是最多三条
    同域公网栏目 URL。只有 ``FIXED_SECTOR_INDEX_URLS_BY_SOURCE_ID`` 中的静态映射可返回
    结果，普通来源目录即使伪造相同字段也无法带入路径；URL 再经同域与公网校验，以避免
    未来代码修改时把二手来源变成任意网页抓取器。函数不联网、不写台账或 evidence。
    """
    if requested_sector is None:
        return ()
    source_mapping = FIXED_SECTOR_INDEX_URLS_BY_SOURCE_ID.get(source_id, {})
    raw_urls = source_mapping.get(requested_sector, ())
    urls: list[str] = []
    base_url = f"https://{domain}/"
    for raw_url in raw_urls[:MAX_FIXED_SECTOR_INDEX_PAGES_PER_SOURCE]:
        normalized_url = _clean_public_url(raw_url, base_url=base_url, domain=domain)
        if normalized_url is None:
            raise RuntimeError("源码固定产业栏目 URL 未通过同域公网校验")
        if normalized_url not in urls:
            urls.append(normalized_url)
    return tuple(urls)


def _parse_feed_items(
    feed_url: str,
    *,
    domain: str,
    restrict_redirect_to_initial_host: bool = False,
) -> list[dict[str, str]]:
    """解析一个受控公开 RSS/Atom 响应为标题、链接和摘要候选，不写入台账或事实。"""
    allowed_types = RSS_CONTENT_TYPES | {"application/atom+xml"}
    if restrict_redirect_to_initial_host:
        response = _fetch_public_bytes(
            feed_url,
            allowed_content_types=allowed_types,
            restrict_redirect_to_initial_host=True,
        )
    else:
        response = _fetch_public_bytes(feed_url, allowed_content_types=allowed_types)
    try:
        root = ElementTree.fromstring(response["raw_content"])
    except (ElementTree.ParseError, TypeError) as exc:
        raise ValueError("可信来源 RSS 或 Atom 格式无效") from exc
    items: list[dict[str, str]] = []
    for element in root.iter():
        if _local_name(element) not in {"item", "entry"}:
            continue
        title = ""
        summary = ""
        item_url = ""
        for child in element:
            child_name = _local_name(child)
            text = " ".join((child.text or "").split())
            if child_name == "title":
                title = text
            elif child_name in {"description", "summary", "content"} and not summary:
                summary = text
            elif child_name == "link" and not item_url:
                item_url = child.get("href") or text
        normalized_url = _clean_public_url(item_url, base_url=feed_url, domain=domain)
        if not title or not normalized_url:
            continue
        items.append(
            {
                "source_title": title[:500],
                "source_url": normalized_url,
                "search_snippet": summary[:2_000],
            }
        )
        if len(items) >= MAX_ITEMS_PER_FEED:
            break
    return items


def _parse_sitemap_urls(
    sitemap_url: str,
    *,
    domain: str,
    restrict_redirect_to_initial_host: bool = False,
) -> list[str]:
    """从受控 Sitemap 读取有限页面链接，仅供后续候选核验而非直接生成事实证据。"""
    if restrict_redirect_to_initial_host:
        response = _fetch_public_bytes(
            sitemap_url,
            allowed_content_types=XML_CONTENT_TYPES,
            restrict_redirect_to_initial_host=True,
        )
    else:
        response = _fetch_public_bytes(sitemap_url, allowed_content_types=XML_CONTENT_TYPES)
    try:
        root = ElementTree.fromstring(response["raw_content"])
    except (ElementTree.ParseError, TypeError) as exc:
        raise ValueError("可信来源 Sitemap 格式无效") from exc
    urls: list[str] = []
    for element in root.iter():
        if _local_name(element) != "loc" or not element.text:
            continue
        normalized_url = _clean_public_url(element.text, base_url=sitemap_url, domain=domain)
        if normalized_url and normalized_url not in urls:
            urls.append(normalized_url)
        if len(urls) >= MAX_URLS_PER_SITEMAP:
            break
    return urls


def _matching_sectors(*, title: str, snippet: str, source_url: str) -> list[str]:
    """按服务器固定产业总称和细分词筛选 RSS/Sitemap 链接，不把未匹配链接写入候选。"""
    from .source_discovery import SECTOR_DISCOVERY_CONFIG

    searchable_text = " ".join([title, snippet, unquote(source_url)]).casefold()
    sectors: list[str] = []
    for sector, config in SECTOR_DISCOVERY_CONFIG.items():
        label = config.get("label")
        subdirections = config.get("subdirections")
        keywords = [label, *(subdirections if isinstance(subdirections, list) else [])]
        if any(isinstance(keyword, str) and keyword.casefold() in searchable_text for keyword in keywords):
            sectors.append(sector)
    return sectors


def _source_feed_lists(ledger: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """取得可信目录 RSS/Sitemap 运行审计和既有候选，结构异常时拒绝继续访问。"""
    runs = ledger.get("source_feed_runs")
    candidates = ledger.get("discovery_candidates")
    if not isinstance(runs, list) or not isinstance(candidates, list):
        raise ValueError("招商线索台账中的来源目录发现字段必须是列表")
    return runs, candidates


def _select_active_sources_for_run(
    sources: list[object],
    source_feed_runs: list[dict[str, Any]],
    *,
    curated_media_first: bool = False,
    requested_sector: str | None = None,
    fixed_sector_index_only: bool = False,
    excluded_source_ids: frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    """按“从未尝试优先、最久未尝试优先”轮换选取有限可信来源。

    输入是已通过完整性校验的来源目录、历史发现审计和服务器固定的手动优先标记；输出最多
    ``MAX_TRUSTED_SOURCES_PER_RUN`` 条可访问来源。函数只读取目录与审计，不联网、不写
    台账，也不依据网页文字或模型偏好改变事实判断。定时运行按轮换顺序选取，避免新增来源
    永远排在末尾；仅由工具内部传入 ``curated_media_first=True`` 的手动发现，会把源码固定、
    已预检的媒体入口排在前面，确保人工测试不再依赖 Bing。来源的 ``active`` 状态仍由普通
    登录用户维护，非 active 记录绝不因轮换被重新访问。人工补充来源还必须保留最近一次
    ``ready_for_public_crawl`` 预检结果，单轮最多两条；检测失败的来源只能由人工重新检测，
    不会被自动反复访问。后台产业主发现可额外传入源码
    固定的 ``fixed_sector_index_only=True``：此时只选择为该产业配置了静态栏目 URL 的来源，
    从根源阻止定时任务重新回落到通用搜索或低相关首页；该标记不是工具参数，不能被模型、
    前端或目录记录改变。
    """
    active_sources: list[tuple[int, dict[str, Any]]] = []
    for directory_index, source in enumerate(sources):
        if (
            isinstance(source, dict)
            and source.get("status") == "active"
            and isinstance(source.get("source_id"), str)
            and isinstance(source.get("domain"), str)
            and (
                source.get("source_type") != HUMAN_PUBLIC_SUPPLEMENT_SOURCE_TYPE
                or source.get("crawl_status") == "ready_for_public_crawl"
            )
        ):
            active_sources.append((directory_index, source))

    if fixed_sector_index_only:
        if requested_sector is None:
            raise ValueError("固定产业栏目发现必须指定规范产业标识")
        active_sources = [
            item
            for item in active_sources
            if requested_sector
            in FIXED_SECTOR_INDEX_URLS_BY_SOURCE_ID.get(str(item[1]["source_id"]), {})
        ]
    if excluded_source_ids:
        active_sources = [
            item for item in active_sources if str(item[1]["source_id"]) not in excluded_source_ids
        ]

    last_attempt_at: dict[str, str] = {}
    for source_feed_run in source_feed_runs:
        started_at = source_feed_run.get("started_at")
        source_results = source_feed_run.get("source_results")
        if not isinstance(started_at, str) or not isinstance(source_results, list):
            continue
        for source_result in source_results:
            if not isinstance(source_result, dict):
                continue
            source_id = source_result.get("source_id")
            if not isinstance(source_id, str):
                continue
            previous_attempt = last_attempt_at.get(source_id)
            if previous_attempt is None or started_at > previous_attempt:
                last_attempt_at[source_id] = started_at

    # ``False``（从未运行）先于 ``True``（已有运行记录）；同一批新来源保留目录顺序，
    # 已运行来源再按最早运行时间排序。这是可复现的轮换策略，不依赖客户端时间或随机数。
    active_sources.sort(
        key=lambda item: (
            item[1]["source_id"] in last_attempt_at,
            last_attempt_at.get(item[1]["source_id"], ""),
            item[0],
        )
    )
    if curated_media_first:
        # 只有固定种子具有该布尔标记，目录 API 和模型都不能提交它；手动模式最多仍访问
        # 五个来源，不会把“优先媒体”扩展成任意域名、高频批量抓取或来源等级升级。
        prioritized = [
            item
            for item in active_sources
            if item[1].get("system_curated_chinese_business_media") is True
        ]
        remaining = [item for item in active_sources if item not in prioritized]
        active_sources = [*prioritized, *remaining]
    selected_sources: list[dict[str, Any]] = []
    selected_human_supplements = 0
    for _, source in active_sources:
        if len(selected_sources) >= MAX_TRUSTED_SOURCES_PER_RUN:
            break
        is_human_supplement = source.get("source_type") == HUMAN_PUBLIC_SUPPLEMENT_SOURCE_TYPE
        if is_human_supplement and selected_human_supplements >= MAX_HUMAN_PUBLIC_SUPPLEMENTS_PER_RUN:
            continue
        selected_sources.append(source)
        if is_human_supplement:
            selected_human_supplements += 1
    return selected_sources


def discover_trusted_source_feeds(
    workspace_dir: Path,
    *,
    requested_sector: str | None = None,
    curated_media_first: bool = False,
    fixed_sector_index_only: bool = False,
    excluded_source_ids: frozenset[str] | None = None,
) -> dict[str, Any]:
    """对少量已启用可信域名执行 robots、RSS 与 Sitemap 候选发现。

    输入是当前智能体服务器工作区、可选的服务器规范产业标识和内部固定的媒体优先标记；
    输出为新增的待核验候选和追加式目录发现审计。函数只使用台账中已启用域名、robots
    声明、首页 RSS/Atom 和 Sitemap，且所有 URL 均须同域、公网和受限大小校验。传入产业
    时只保留该产业候选，避免手动测试“半导体”时混入其他产业链接；该参数不能携带 URL、
    关键词或来源域名。仅服务器内部可传 ``fixed_sector_index_only=True``，让产业主发现
    只访问源码固定且已预检的栏目来源；普通目录轮换仍可在独立调用中保留。发现页仍只标为
    ``lead_only``，不抓取候选正文、不写 evidence、不生成事实、评分、红色预警或推进状态。
    访问被 robots 禁止或不可读取时会进入来源健康度退避，绝不采用隐身、伪造指纹或规避手段。
    """
    ledger = load_ledger(workspace_dir)
    source_feed_runs, candidates = _source_feed_lists(ledger)
    sources = ledger.get("trusted_sources")
    if not isinstance(sources, list):
        raise ValueError("招商线索台账中的 trusted_sources 必须是列表")
    if requested_sector is not None:
        # 产业标识只能来自工具预先调用的 ``build_discovery_queries``；即使内部调用也再次
        # 校验，避免未来代码把前端/模型的自由词作为可信目录抓取范围。
        from .source_discovery import SECTOR_DISCOVERY_CONFIG

        if requested_sector not in SECTOR_DISCOVERY_CONFIG:
            raise ValueError("可信来源手动发现的产业标识不合法")
    if fixed_sector_index_only and requested_sector is None:
        raise ValueError("固定产业栏目发现必须指定规范产业标识")
    started_at = _utc_now()
    run_id = _stable_id("source-feed-run", {"started_at": started_at})
    known_urls = {
        candidate.get("source_url")
        for candidate in candidates
        if isinstance(candidate, dict) and isinstance(candidate.get("source_url"), str)
    }
    protected_evidence = json.dumps(ledger.get("evidence"), ensure_ascii=False, sort_keys=True)
    if excluded_source_ids is not None and (
        not isinstance(excluded_source_ids, frozenset)
        or not all(isinstance(item, str) and item for item in excluded_source_ids)
    ):
        raise ValueError("内部来源排除列表格式异常")
    selected_sources = _select_active_sources_for_run(
        sources,
        source_feed_runs,
        curated_media_first=curated_media_first,
        requested_sector=requested_sector,
        fixed_sector_index_only=fixed_sector_index_only,
        excluded_source_ids=excluded_source_ids,
    )
    new_candidates: list[dict[str, Any]] = []
    rejected_candidate_count = 0
    rejected_candidates: list[dict[str, object]] = []
    source_results: list[dict[str, Any]] = []

    def add_candidate(
        *,
        source_url: str,
        source_title: str,
        search_snippet: str,
        catalog_id: str,
        curated_chinese_media_context: bool,
        fixed_investment_event_subject: bool = False,
    ) -> None:
        """将通过中国/徐州业务机会门槛的目录链接追加为候选。

        输入是同域 RSS/Sitemap/固定事件卡片已解析的标题、摘要和链接；输出为空，仅在
        服务器确认其匹配目标产业并通过资格门槛时才追加候选。可信目录只说明域名可访问，
        不自动证明某条内容是中国企业或招商项目；固定事件卡片也只能用其受限结构规则进入
        待补证队列。拒绝项只进入本轮目录审计，不读取正文、不创建 evidence、评分或线索。
        """
        nonlocal known_urls, rejected_candidate_count
        if source_url in known_urls:
            return
        sectors = _matching_sectors(
            title=source_title,
            snippet=search_snippet,
            source_url=source_url,
        )
        if not sectors:
            return
        if requested_sector is not None and requested_sector not in sectors:
            return
        # 只有源码固定、且本轮目录记录明确声明的中国财经媒体，才能在“仅缺中国标题
        # 语境”时进入待补证快照队列；所有普通目录和用户新增来源仍走原严格门槛。两条
        # 评估函数均为纯内存规则，不联网、不写证据，也不会把媒体描述当企业中国业务事实。
        from .source_discovery import (
            assess_curated_chinese_media_candidate,
            assess_fixed_investment_event_candidate,
            assess_public_discovery_candidate,
        )

        if fixed_investment_event_subject:
            # 此参数不来自来源目录、模型或网页：只有下面源码固定栏目解析分支才能传 True。
            # 该规则仍要求机会信号并强制“需核验中国业务”，不可借普通媒体标题伪造主体。
            assess_candidate = assess_fixed_investment_event_candidate
        elif curated_chinese_media_context:
            assess_candidate = assess_curated_chinese_media_candidate
        else:
            assess_candidate = assess_public_discovery_candidate
        eligibility_assessment = assess_candidate(
            source_url=source_url,
            source_title=source_title,
            search_snippet=search_snippet,
        )
        if eligibility_assessment["eligible"] is not True:
            rejected_candidate_count += 1
            if len(rejected_candidates) < 50:
                rejected_candidates.append(
                    {
                        "source_url": source_url,
                        "source_title": source_title[:500],
                        "search_snippet": search_snippet[:2_000],
                        "catalog_id": catalog_id,
                        "eligibility_assessment": eligibility_assessment,
                    }
                )
            return
        # 台账按 URL 去重；一个页面命中多个行业时保留配置顺序中的首个行业，避免复制
        # 同一来源来人为放大信号。后续人工可基于同一原始证据补充跨产业业务判断。
        sector = requested_sector if requested_sector is not None else sectors[0]
        candidate = {
            "candidate_id": _stable_id(
                "discovery-candidate",
                {"run_id": run_id, "source_url": source_url},
            ),
            "run_id": run_id,
            "source_url": source_url,
            "source_title": source_title[:500],
            "search_snippet": search_snippet[:2_000],
            "sector": sector,
            "source_catalog_id": catalog_id,
            "discovery_query": "可信来源目录 robots/RSS/Sitemap 自动发现",
            "discovered_at": _utc_now(),
            "source_type": "trusted_source_directory_discovery",
            "source_level": "lead_only",
            "state": "discovered",
            "capture_attempts": 0,
            "captured_evidence_id": None,
            "last_capture_error": None,
            "last_captured_at": None,
            # ``True`` 只表示标题尚未给出企业中国/徐州业务依据；候选可由低频快照补证，
            # 但 source_discovery 的正式抓取入口会拒绝它直接成为 evidence。
            "requires_china_business_verification": (
                eligibility_assessment.get("requires_china_business_verification") is True
            ),
            # 与公开搜索候选一致：此为自动补证范围筛选结果，不能替代页面原文、正式证据或
            # 人工对中国业务、徐州关联和投资意愿的确认。
            "eligibility_assessment": eligibility_assessment,
        }
        candidates.append(candidate)
        new_candidates.append(candidate)
        known_urls.add(source_url)

    for source in selected_sources:
        source_id = str(source["source_id"])
        domain = str(source["domain"])
        # 此标记仅存在于源码固定、经过人工代码审查的免费媒体种子。普通用户通过目录 API
        # 登记的域名不能提交该字段，模型或网页也无法影响它；因此不会将“任意 .cn 新闻站”
        # 放宽为可跳过中国业务语境的来源。
        curated_chinese_media_context = (
            source.get("source_type") == "verified_media"
            and source.get("system_curated_chinese_business_media") is True
        )
        source_key = f"trusted_source_directory:{source_id}"
        if not is_source_eligible(ledger, source_key=source_key):
            source_results.append({"source_id": source_id, "status": "skipped_due_to_cooldown"})
            continue
        fixed_index_urls = _fixed_sector_index_urls_for_source(
            source_id=source_id,
            domain=domain,
            requested_sector=requested_sector,
        )
        if fixed_index_urls:
            # 固定栏目优先于域名首页：其页面直接提供“公司页—产业—融资摘要”结构，避免
            # 再从新闻首页导航链接中碰运气。每个栏目路径独立检查 robots，任何一页不可
            # 访问都只记录并跳过，绝不会回退到搜索引擎、IP、无头浏览器或规避访问策略。
            fixed_index_results: list[dict[str, object]] = []
            fixed_event_candidate_count = 0
            allowed_index_count = 0
            for fixed_index_url in fixed_index_urls:
                page_robots = check_robots_access(fixed_index_url)
                if page_robots["allowed"] is not True:
                    fixed_index_results.append(
                        {
                            "index_url": fixed_index_url,
                            "status": "robots_blocked",
                            "reason": page_robots["reason"],
                        }
                    )
                    continue
                try:
                    index_resources = _discover_homepage_resources(
                        fixed_index_url,
                        domain=domain,
                    )
                    event_items = index_resources.get("investment_event_items", [])
                    if not isinstance(event_items, list) or not all(
                        isinstance(item, dict) for item in event_items
                    ):
                        raise RuntimeError("固定投融资栏目事件解析结果格式异常")
                    page_candidate_count = 0
                    for item in event_items:
                        source_url = item.get("source_url")
                        source_title = item.get("source_title")
                        search_snippet = item.get("search_snippet")
                        if not all(
                            isinstance(value, str)
                            for value in (source_url, source_title, search_snippet)
                        ):
                            raise RuntimeError("固定投融资栏目事件候选格式异常")
                        count_before = len(new_candidates)
                        add_candidate(
                            source_url=source_url,
                            source_title=source_title,
                            search_snippet=search_snippet,
                            catalog_id="trusted_fixed_investment_event_index",
                            curated_chinese_media_context=curated_chinese_media_context,
                            fixed_investment_event_subject=True,
                        )
                        page_candidate_count += len(new_candidates) - count_before
                    fixed_event_candidate_count += page_candidate_count
                    allowed_index_count += 1
                    fixed_index_results.append(
                        {
                            "index_url": fixed_index_url,
                            "status": "completed",
                            "event_item_count": len(event_items),
                            "event_candidate_count": page_candidate_count,
                        }
                    )
                except (OSError, RuntimeError, ValueError) as exc:
                    fixed_index_results.append(
                        {
                            "index_url": fixed_index_url,
                            "status": "failed",
                            "error": str(exc)[:500],
                        }
                    )
            if allowed_index_count == 0:
                error = "；".join(
                    str(item.get("reason") or item.get("error") or "固定栏目不可用")
                    for item in fixed_index_results
                )
                apply_source_health_outcome(
                    ledger,
                    source_key=source_key,
                    success=False,
                    error=error[:500],
                )
                source_results.append(
                    {
                        "source_id": source_id,
                        "status": "robots_blocked",
                        "fixed_sector_index_results": fixed_index_results,
                    }
                )
                continue
            apply_source_health_outcome(ledger, source_key=source_key, success=True)
            source_results.append(
                {
                    "source_id": source_id,
                    "status": "completed",
                    "fixed_sector_index_results": fixed_index_results,
                    "fixed_event_candidate_count": fixed_event_candidate_count,
                }
            )
            continue
        # 人工补充来源严格从业务人员已通过服务器预检的 seed_url 开始，不擅自回退到
        # 根域名或 www 子域名；否则会把该网址的 robots 许可扩展到未检测入口。普通可信
        # 目录仍维持既有裸域名 / www 有限回退逻辑。
        is_human_public_supplement = (
            source.get("source_type") == HUMAN_PUBLIC_SUPPLEMENT_SOURCE_TYPE
        )
        if is_human_public_supplement:
            seed_url = source.get("seed_url")
            if not isinstance(seed_url, str) or not seed_url:
                source_results.append(
                    {
                        "source_id": source_id,
                        "status": "skipped_invalid_public_supplement",
                        "reason": "人工补充公开来源缺少已检测的网址",
                    }
                )
                continue
            origin_candidates = (seed_url,)
        else:
            origin_candidates = _trusted_origin_candidates(domain)
        origin = origin_candidates[0]
        robots = check_robots_access(origin)
        # 仅在裸域名的 robots 端点不可用时，尝试同一目录主域名的 www 子域名。若 robots
        # 已明确 Disallow，必须立即停止，不能把“备用网址”当成规避抓取规则的方式；若
        # www 的 robots 仍不可用，同样失败关闭并记录最终原因。
        robots_reason = robots.get("reason")
        if (
            not robots["allowed"]
            and isinstance(robots_reason, str)
            and robots_reason.startswith("robots_unavailable:")
            and not is_human_public_supplement
            and len(origin_candidates) > 1
        ):
            fallback_origin = origin_candidates[1]
            fallback_robots = check_robots_access(fallback_origin)
            origin = fallback_origin
            robots = fallback_robots
        if not robots["allowed"]:
            apply_source_health_outcome(
                ledger,
                source_key=source_key,
                success=False,
                error=str(robots["reason"]),
            )
            source_results.append(
                {
                    "source_id": source_id,
                    "status": "robots_blocked",
                    "reason": robots["reason"],
                    "attempted_origin": origin,
                }
            )
            continue
        try:
            if is_human_public_supplement:
                homepage_resources = _discover_homepage_resources(
                    origin,
                    domain=domain,
                    restrict_redirect_to_initial_host=True,
                )
            else:
                homepage_resources = _discover_homepage_resources(origin, domain=domain)
            feed_urls = homepage_resources.get("feed_urls")
            homepage_items = homepage_resources.get("homepage_items")
            if not isinstance(feed_urls, list) or not all(
                isinstance(feed_url, str) for feed_url in feed_urls
            ):
                raise RuntimeError("可信来源首页 RSS 发现结果格式异常")
            if not isinstance(homepage_items, list) or not all(
                isinstance(item, dict) for item in homepage_items
            ):
                raise RuntimeError("可信来源首页链接发现结果格式异常")
            sitemap_urls = [
                normalized_url
                for raw_url in robots["sitemap_urls"][:MAX_SITEMAPS_PER_SOURCE]
                if (normalized_url := _clean_public_url(raw_url, base_url=origin, domain=domain))
            ]
            rss_candidate_count = 0
            sitemap_candidate_count = 0
            homepage_candidate_count = 0
            # 首页链接在已登记域名和已允许 robots 范围内产生；它们仍只是索引，不抓正文，
            # 只有 add_candidate 的正式门槛通过后才会成为待补证候选。
            for item in homepage_items:
                source_url = item.get("source_url")
                source_title = item.get("source_title")
                search_snippet = item.get("search_snippet")
                if not all(
                    isinstance(value, str)
                    for value in (source_url, source_title, search_snippet)
                ):
                    raise RuntimeError("可信来源首页链接候选格式异常")
                count_before = len(new_candidates)
                add_candidate(
                    source_url=source_url,
                    source_title=source_title,
                    search_snippet=search_snippet,
                    catalog_id="trusted_homepage_link",
                    curated_chinese_media_context=curated_chinese_media_context,
                )
                homepage_candidate_count += len(new_candidates) - count_before
            for feed_url in feed_urls:
                # 首页允许不代表 /feed.xml 也允许；每条实际读取的目录资源都单独遵守
                # 同源 robots 路径规则，禁止时仅跳过，不改用其他伪装访问方式。
                if not check_robots_access(feed_url)["allowed"]:
                    continue
                feed_items = (
                    _parse_feed_items(
                        feed_url,
                        domain=domain,
                        restrict_redirect_to_initial_host=True,
                    )
                    if is_human_public_supplement
                    else _parse_feed_items(feed_url, domain=domain)
                )
                for item in feed_items:
                    count_before = len(new_candidates)
                    add_candidate(
                        catalog_id="trusted_rss_feed",
                        curated_chinese_media_context=curated_chinese_media_context,
                        **item,
                    )
                    rss_candidate_count += len(new_candidates) - count_before
            for sitemap_url in sitemap_urls:
                # Sitemap 同样是一次实际网络读取，必须具有独立的 robots 许可。
                if not check_robots_access(sitemap_url)["allowed"]:
                    continue
                sitemap_pages = (
                    _parse_sitemap_urls(
                        sitemap_url,
                        domain=domain,
                        restrict_redirect_to_initial_host=True,
                    )
                    if is_human_public_supplement
                    else _parse_sitemap_urls(sitemap_url, domain=domain)
                )
                for page_url in sitemap_pages:
                    count_before = len(new_candidates)
                    add_candidate(
                        source_url=page_url,
                        source_title=f"{domain} Sitemap 页面候选",
                        search_snippet=f"由 {robots['robots_url']} 声明的 Sitemap 自动发现。",
                        catalog_id="trusted_sitemap",
                        curated_chinese_media_context=curated_chinese_media_context,
                    )
                    sitemap_candidate_count += len(new_candidates) - count_before
        except (OSError, RuntimeError, ValueError) as exc:
            apply_source_health_outcome(
                ledger,
                source_key=source_key,
                success=False,
                error=str(exc),
            )
            source_results.append(
                {"source_id": source_id, "status": "failed", "error": str(exc)[:500]}
            )
            continue
        apply_source_health_outcome(ledger, source_key=source_key, success=True)
        source_results.append(
            {
                "source_id": source_id,
                "status": "completed",
                "resolved_origin": origin,
                "rss_feed_count": len(feed_urls),
                "sitemap_count": len(sitemap_urls),
                "homepage_link_count": len(homepage_items),
                "rss_candidate_count": rss_candidate_count,
                "sitemap_candidate_count": sitemap_candidate_count,
                "homepage_candidate_count": homepage_candidate_count,
            }
        )

    completed_at = _utc_now()
    failed_count = sum(1 for result in source_results if result["status"] in {"failed", "robots_blocked"})
    run = {
        "source_feed_run_id": run_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "source_count": len(selected_sources),
        "requested_sector": requested_sector,
        "curated_media_first": curated_media_first,
        # 该值解释本轮是产业主发现还是普通可信目录补充；它不代表企业页面的可信度、
        # 中国业务、融资真实性或招商意愿，也不能由模型/前端控制。
        "fixed_sector_index_only": fixed_sector_index_only,
        # 记录本轮实际选取的目录 ID，便于人工确认轮换是否覆盖了新旧来源；它只是来源
        # 调度审计，不包含网页原文、证据 URL、评分或任何企业事实。
        "selected_source_ids": [str(source["source_id"]) for source in selected_sources],
        "candidate_count": len(new_candidates),
        # 被严格门槛拒绝的目录项仍可供来源治理复盘，但不会进入候选、观察或证据队列。
        "rejected_candidate_count": rejected_candidate_count,
        "rejected_candidates": rejected_candidates,
        "status": "completed" if failed_count == 0 else "partial_failure",
        "source_results": source_results,
        "operation_scope": (
            "trusted_directory_robots_rss_sitemap_fixed_investment_event_"
            "candidate_discovery_only"
        ),
    }
    source_feed_runs.append(run)
    if json.dumps(ledger.get("evidence"), ensure_ascii=False, sort_keys=True) != protected_evidence:
        raise RuntimeError("来源目录自动发现不得修改既有原始证据链")
    saved_path = save_ledger(workspace_dir, ledger)
    return {"run": run, "candidates": new_candidates, "ledger_path": str(saved_path)}
