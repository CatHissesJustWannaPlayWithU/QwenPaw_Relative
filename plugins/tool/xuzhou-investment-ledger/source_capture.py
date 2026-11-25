"""公开来源证据快照的受控采集实现。

本模块只读取受控流程提供的公开 HTTP/HTTPS 链接，目的是留下可复查的来源正文、时间
和哈希值。链接可以来自人工登记或服务器执行的低频公开搜索，但搜索结果本身不会直接
入库为事实证据。模块不登录网站、不执行网页脚本，也不尝试绕过访问限制，从而降低
采集边界不清和伪造来源的风险。
"""

from __future__ import annotations

import hashlib
import ipaddress
import socket
from html.parser import HTMLParser
from typing import Final
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from xml.etree import ElementTree


MAX_RESPONSE_BYTES: Final = 1_000_000
REQUEST_TIMEOUT_SECONDS: Final = 12
USER_AGENT: Final = "QwenPaw-XuzhouInvestmentEvidence/0.2"
ALLOWED_CONTENT_TYPES: Final = {
    "text/html",
    "application/xhtml+xml",
    "text/plain",
}
RSS_CONTENT_TYPES: Final = {
    "application/rss+xml",
    "application/xml",
    "text/xml",
}
PUBLIC_SEARCH_RSS_ENDPOINT: Final = "https://www.bing.com/search"


class _VisibleTextExtractor(HTMLParser):
    """从 HTML 中提取标题和可见文本，不运行任何页面脚本。"""

    _IGNORED_TAGS = {"script", "style", "noscript", "template", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self._inside_title = False
        self._title_parts: list[str] = []
        self._text_parts: list[str] = []

    @property
    def title(self) -> str:
        """返回去除多余空白后的网页标题。"""

        return " ".join("".join(self._title_parts).split())

    @property
    def visible_text(self) -> str:
        """返回去除多余空白后的可见正文。"""

        return " ".join(" ".join(self._text_parts).split())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """进入被忽略标签时记录嵌套深度，避免提取脚本和样式内容。"""

        del attrs
        normalized_tag = tag.lower()
        if normalized_tag in self._IGNORED_TAGS:
            self._ignored_depth += 1
        elif normalized_tag == "title" and self._ignored_depth == 0:
            self._inside_title = True

    def handle_endtag(self, tag: str) -> None:
        """离开标题或忽略标签时恢复正确的解析状态。"""

        normalized_tag = tag.lower()
        if normalized_tag in self._IGNORED_TAGS and self._ignored_depth > 0:
            self._ignored_depth -= 1
        elif normalized_tag == "title":
            self._inside_title = False

    def handle_data(self, data: str) -> None:
        """仅保留普通可见文本，并单独积累标题文本。"""

        if self._ignored_depth != 0:
            return
        if self._inside_title:
            self._title_parts.append(data)
        self._text_parts.append(data)


def _validate_public_url(url: str) -> tuple[str, str]:
    """校验链接格式并拒绝包含账号密码的 URL，避免凭据意外泄露。"""

    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("来源链接必须是完整的公开 HTTP 或 HTTPS 地址")
    if parsed.username or parsed.password:
        raise ValueError("来源链接不能包含用户名或密码")
    return parsed.geturl(), parsed.hostname.rstrip(".").lower()


def _ensure_public_host(hostname: str) -> None:
    """解析域名并拒绝本机、内网和保留地址，防止工具被用于访问内部服务。"""

    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(
        (".localhost", ".local", ".internal")
    ):
        raise ValueError("来源链接不能指向本机或内部网络地址")

    try:
        addresses = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"无法解析来源域名：{hostname}") from exc

    if not addresses:
        raise ValueError(f"无法解析来源域名：{hostname}")

    for address in addresses:
        ip_value = ipaddress.ip_address(address[4][0])
        if not ip_value.is_global:
            raise ValueError("来源链接不能解析到本机、内网或保留 IP 地址")


def _ensure_public_connected_peer(response: object) -> None:
    """在 TCP/TLS 连接已建立后校验实际对端 IP，缩小 DNS 重绑定访问内网的窗口。

    输入是标准库 URL 打开器返回的响应对象，成功时无返回；函数沿常见 ``fp/raw/_sock``
    包装层寻找已连接套接字并检查 ``getpeername`` 返回的 IP 必须为公网。DNS 预解析与
    建连之间域名可能改变指向，因此只检查域名解析结果不足；无法可靠取得实际对端时
    失败关闭，不下载任何页面。函数不写台账、不处理证据或业务状态，也不泄露对端地址。
    """
    pending: list[object] = [response]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        current_id = id(current)
        if current_id in visited:
            continue
        visited.add(current_id)
        getpeername = getattr(current, "getpeername", None)
        if callable(getpeername):
            try:
                peer = getpeername()
                peer_address = peer[0] if isinstance(peer, tuple) and peer else None
                peer_ip = ipaddress.ip_address(peer_address)
            except (OSError, TypeError, ValueError) as exc:
                raise ValueError("无法验证公开来源连接的实际对端地址") from exc
            if not peer_ip.is_global:
                raise ValueError("来源连接实际指向本机、内网或保留 IP 地址")
            return
        for attribute_name in ("fp", "raw", "_fp", "_sock", "sock", "connection"):
            nested = getattr(current, attribute_name, None)
            if nested is not None:
                pending.append(nested)
    raise ValueError("无法验证公开来源连接的实际对端地址")


class _SafeRedirectHandler(HTTPRedirectHandler):
    """每次跳转前重新检查目标域名，避免初始公开链接跳向内网。"""

    def __init__(self, *, required_hostname: str | None = None) -> None:
        """初始化重定向边界，可为候选原始快照要求跳转始终留在同一域名。

        输入是可选的服务器初始域名；普通人工证据抓取不传该参数，仍会执行公网 DNS
        校验。自动候选观察传入初始域名后，跨域跳转会在发出下一次请求前被拒绝，因为
        robots 许可只适用于原域名。此构造函数不联网、不写台账，也不改变任何证据。
        """
        super().__init__()
        self._required_hostname = required_hostname.casefold() if required_hostname else None

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        """校验重定向目标并交给标准库构造下一次只读请求。

        输入是标准库重定向回调提供的原请求、响应对象、状态码、响应信息、响应头和
        目标 URL；输出是标准库生成的下一跳请求或 ``None``。任何来源 URL 都必须先
        通过 ``_validate_public_url`` 和 ``_ensure_public_host``，包括重定向目标，
        才能防止看似公开的链接跳转到本机或内网。校验完成后必须原样保留并传递响应
        参数给父类；删除这些参数会导致重定向时出现未绑定变量错误，进而中断证据链
        采集。
        """
        _, hostname = _validate_public_url(newurl)
        _ensure_public_host(hostname)
        if self._required_hostname is not None and hostname.casefold() != self._required_hostname:
            raise ValueError("自动候选观察不允许跨域重定向，避免越过原域名 robots 规则")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch_public_bytes(
    source_url: str,
    *,
    allowed_content_types: set[str],
    restrict_redirect_to_initial_host: bool = False,
) -> dict[str, str | bytes | int]:
    """以统一网络边界下载小型公开响应，供页面快照和搜索 RSS 共同使用。

    输入是服务器生成或已保存的公开 URL 及允许的 MIME 类型集合，输出包含最终 URL、
    内容类型和原始字节。函数会在首跳与每次重定向时校验公网地址、拒绝内网 DNS 解析、
    限制响应大小和超时；候选原始快照可要求重定向始终留在初始域名，避免把该域 robots
    许可扩展至其他域名。调用方不能借搜索功能访问任意内部服务。该函数只读取网络响应，
    不写入台账，也不解释网页内容或修改任何原始证据。
    """
    normalized_url, hostname = _validate_public_url(source_url)
    _ensure_public_host(hostname)

    request = Request(normalized_url, headers={"User-Agent": USER_AGENT})
    # 禁用环境代理，确保下方校验的是目标站点实际对端而不是代理服务器；否则代理可在
    # 本进程 DNS 校验后代为解析不同地址，重新打开 SSRF 边界。
    opener = build_opener(
        ProxyHandler({}),
        _SafeRedirectHandler(required_hostname=hostname if restrict_redirect_to_initial_host else None),
    )

    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            _ensure_public_connected_peer(response)
            content_type = response.headers.get_content_type().lower()
            if content_type not in allowed_content_types:
                raise ValueError(f"不支持的来源内容类型：{content_type or '未知'}")

            raw_content = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw_content) > MAX_RESPONSE_BYTES:
                raise ValueError("来源页面超过 1 MB，暂不保存，避免异常大响应占用工作区")

            charset = response.headers.get_content_charset() or "utf-8"
            final_url = response.geturl()
    except HTTPError as exc:
        raise ValueError(f"读取公开来源失败，HTTP 状态码：{exc.code}") from exc
    except URLError as exc:
        raise ValueError(f"无法连接公开来源：{exc.reason}") from exc
    except TimeoutError as exc:
        raise ValueError("读取公开来源超时，请稍后重试") from exc

    # 防御性再次检查最终 URL；即使重定向处理器已检查，也不把网络库实现细节当成边界。
    _, final_hostname = _validate_public_url(final_url)
    _ensure_public_host(final_hostname)
    return {
        "source_url": final_url,
        "content_type": content_type,
        "charset": charset,
        "raw_content": raw_content,
        "byte_size": len(raw_content),
    }


def _fetch_and_parse_public_source(
    source_url: str,
    *,
    restrict_redirect_to_initial_host: bool = False,
) -> tuple[dict[str, str | int], bytes]:
    """抓取并解析公开页面，返回可阅读字段及原始字节，供证据与观察两种流程复用。

    输入是已通过上层来源边界控制的公开 URL；输出是页面标题、可见正文、内容哈希等
    字段及服务器实际下载的原始字节。函数沿用公网 DNS、跳转、超时、大小和 MIME 限制，
    不写台账、不判断招商事实，也不调用脚本或绕过访问控制。调用方必须决定原始字节
    是否保存为观察快照；普通证据登记仍只接受经过核验的摘录。
    """

    response = _fetch_public_bytes(
        source_url,
        allowed_content_types=ALLOWED_CONTENT_TYPES,
        restrict_redirect_to_initial_host=restrict_redirect_to_initial_host,
    )
    raw_content = response["raw_content"]
    charset = response["charset"]
    content_type = response["content_type"]
    final_url = response["source_url"]
    if not isinstance(raw_content, bytes) or not isinstance(charset, str):
        raise RuntimeError("公开来源响应格式异常")
    decoded_content = raw_content.decode(charset, errors="replace")

    if content_type == "text/plain":
        title = urlparse(final_url).hostname or "公开文本来源"
        visible_text = " ".join(decoded_content.split())
    else:
        extractor = _VisibleTextExtractor()
        extractor.feed(decoded_content)
        extractor.close()
        title = extractor.title or urlparse(final_url).hostname or "公开网页来源"
        visible_text = extractor.visible_text

    if not visible_text:
        raise ValueError("公开来源没有可用于复核的正文文本")

    parsed_snapshot = {
        "source_url": final_url,
        "source_title": title,
        "content_type": content_type,
        "visible_text": visible_text,
        "raw_sha256": hashlib.sha256(raw_content).hexdigest(),
        "byte_size": len(raw_content),
    }
    return parsed_snapshot, raw_content


def fetch_public_source(source_url: str) -> dict[str, str | int]:
    """抓取指定公开页面并生成原始内容哈希与可阅读正文。

    返回值中的 ``raw_sha256`` 对应实际下载字节；``visible_text`` 则是供人工复核和
    引用核验使用的可见文本。二者分开保存，既便于阅读，也能证明文本来自哪一次抓取。
    本函数不返回原始字节，防止一般查看与证据流程在无专门快照审计时保存整页原文。
    """
    parsed_snapshot, _ = _fetch_and_parse_public_source(source_url)
    return parsed_snapshot


def fetch_public_source_for_observation(source_url: str) -> dict[str, str | int | bytes]:
    """抓取公开页面并将原始字节交给受控观察快照流程保存。

    输入是机器人规则已允许的服务器候选 URL；输出包含普通页面字段及 ``raw_content``。
    此函数只供自动观察服务调用，调用方必须将原始字节连同 URL、哈希和服务器采集时间
    保存为只追加快照，不能把关键词命中直接当事实证据、评分依据或人工状态变更。
    """
    parsed_snapshot, raw_content = _fetch_and_parse_public_source(
        source_url,
        restrict_redirect_to_initial_host=True,
    )
    return {**parsed_snapshot, "raw_content": raw_content}


def fetch_public_search_feed(search_query: str) -> list[dict[str, str]]:
    """通过免费公开 RSS 搜索发现候选页面，不将搜索摘要当作招商事实。

    输入是仅由服务器侧产业查询生成器构造的检索语句，输出为标题、链接和摘要组成的
    小批量候选列表。函数固定访问公开搜索 RSS 端点，不接受调用方提供的搜索 URL，且
    复用公网地址、跳转、超时和大小限制。RSS 项目仅用于后续服务器抓取与摘录核验；
    不在这里写入线索、证据、评分或任何人工业务状态。付费升级方案：如需提高早期融资、
    基金和工商变更的覆盖率，可在取得授权后接入有明确再分发许可的商业企业情报/新闻数据
    API，并将凭据仅配置在服务器密钥库、逐条保留供应商记录 ID 与许可范围；不得用代理、
    TLS 指纹伪装或无头浏览器绕过站点访问控制来替代合规数据授权。

    ===== 付费升级（暂不启用；仅供后续工程师预算）=====
    合规企业情报、新闻、融资或工商变更 API 通常按来源、席位、调用量和再分发范围报价；
    本项目可先按约 2 万–20 万元/年预留采购评估额度，实际必须以供应商书面报价、数据许可
    和法务审查为准。接入前还须新增供应商记录 ID、许可范围、到期日与调用审计，不能仅替换
    本免费 RSS 函数或以技术手段规避网站限制。
    """
    normalized_query = " ".join(search_query.split())
    if not normalized_query:
        raise ValueError("搜索语句不能为空")
    if len(normalized_query) > 500:
        raise ValueError("搜索语句不能超过500个字符")

    search_url = f"{PUBLIC_SEARCH_RSS_ENDPOINT}?{urlencode({'format': 'rss', 'setlang': 'zh-hans', 'q': normalized_query})}"
    response = _fetch_public_bytes(search_url, allowed_content_types=RSS_CONTENT_TYPES)
    raw_content = response["raw_content"]
    if not isinstance(raw_content, bytes):
        raise RuntimeError("公开搜索响应格式异常")
    try:
        root = ElementTree.fromstring(raw_content)
    except ElementTree.ParseError as exc:
        raise ValueError("公开搜索返回的 RSS 格式无效") from exc

    results: list[dict[str, str]] = []
    for item in root.findall(".//item"):
        title = " ".join((item.findtext("title") or "").split())
        source_url = " ".join((item.findtext("link") or "").split())
        snippet = " ".join((item.findtext("description") or "").split())
        if not title or not source_url:
            continue
        try:
            _validate_public_url(source_url)
        except ValueError:
            continue
        results.append(
            {
                "source_title": title[:500],
                "source_url": source_url,
                "search_snippet": snippet[:2_000],
            }
        )
    return results
