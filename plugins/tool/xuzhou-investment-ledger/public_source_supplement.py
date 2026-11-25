"""业务人员补充“可直接抓取”的公开来源前置校验。

这个模块只处理业务人员主动提交的、无需账号或密钥的公开网页入口。它不是通用抓取器：
调用方不能提交 Cookie、请求头、API 参数、抓取深度或搜索词；服务器也不会登录、执行
页面脚本或绕过 robots。通过校验的入口随后仍只会在既有低频来源监控中，以同域、
robots 允许、有限 RSS/Sitemap/首页链接的范围运行。
"""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

from .source_capture import fetch_public_source_for_observation
from .source_catalog import (
    get_public_source_supplement,
    list_public_source_supplements,
    record_public_source_supplement_preflight,
    register_public_source_supplement,
)
from .source_feed_discovery import check_robots_access


MAX_PUBLIC_SOURCE_URL_LENGTH = 2_000
PUBLIC_HOST_PATTERN = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\Z"
)
PUBLIC_SOURCE_CRAWL_SCOPE = (
    "仅低频读取登记网址；以及该网址声明、同域且 robots 允许的少量 RSS、Sitemap 和首页链接。"
)
PUBLIC_SOURCE_STATUS_LABELS = {
    "ready_for_public_crawl": "服务器可直接抓取",
    "robots_disallowed": "网站不允许机器抓取",
    "login_or_paid_access": "需要登录、会员或付费访问",
    "unsupported_api_or_credentials": "看起来是 API / 带凭据入口",
    "temporarily_unreachable": "暂时无法访问",
    "unsafe_target": "不是可安全访问的公开网站",
}
_SENSITIVE_URL_PARTS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "cookie",
        "id_token",
        "password",
        "session",
        "signature",
        "sig",
        "token",
    }
)
_UNSAFE_HOST_SUFFIXES = (".localhost", ".local", ".internal")


def _utc_now() -> str:
    """返回服务器生成的检测时间；浏览器时钟不参与来源审计。"""

    return datetime.now(timezone.utc).isoformat()


def _public_result(
    status: str,
    detail: str,
    *,
    canonical_url: str | None = None,
    domain: str | None = None,
    content_type: str | None = None,
    byte_size: int | None = None,
    source_title: str | None = None,
) -> dict[str, object]:
    """构建不含页面正文、原始字节、凭据或内部网络细节的前置检测结果。"""

    return {
        "status": status,
        "status_label": PUBLIC_SOURCE_STATUS_LABELS[status],
        "direct_crawl_allowed": status == "ready_for_public_crawl",
        "detail": detail[:800],
        "canonical_url": canonical_url,
        "domain": domain,
        "checked_at": _utc_now(),
        "content_type": content_type,
        "byte_size": byte_size,
        "source_title": source_title[:200] if isinstance(source_title, str) else None,
        "crawl_scope": PUBLIC_SOURCE_CRAWL_SCOPE,
        "paid_api_hint": "付费或需密钥的 API 不能填在这里；请由开发者在后端源码和密钥配置中按供应商许可接入。",
    }


def _looks_like_api_or_credential_endpoint(hostname: str, path: str) -> bool:
    """保守识别不适合业务页面直填的 API/凭据式入口，不向其发起网络请求。"""

    path_parts = [part.casefold() for part in unquote(path).split("/") if part]
    return (
        hostname.startswith("api.")
        or (path_parts and (path_parts[0] == "api" or re.fullmatch(r"v\d+", path_parts[0]) is not None))
        or any(part in _SENSITIVE_URL_PARTS for part in path_parts)
    )


def normalize_public_source_url(value: object) -> tuple[str, str]:
    """仅接受无账号、无参数、默认端口的公开 HTTP(S) 网页入口。

    输入来自业务页面，输出为服务器统一规范的 URL 和精确主机名。它先拒绝 IP、内网名、
    URL 用户名密码、查询参数、片段和非常规端口，再交由既有抓取器做 DNS、对端 IP 与每次
    重定向校验。查询参数一律不接受，因此不会把签名下载链接、会话 URL 或 API token 写入
    团队台账。
    """

    if not isinstance(value, str) or not (raw_url := value.strip()):
        raise ValueError("请填写完整的公开网页网址，例如 https://example.com/news")
    if len(raw_url) > MAX_PUBLIC_SOURCE_URL_LENGTH:
        raise ValueError(f"网址不能超过 {MAX_PUBLIC_SOURCE_URL_LENGTH} 个字符")
    if any(ord(character) < 0x20 for character in raw_url):
        raise ValueError("网址不能包含控制字符")
    try:
        parsed = urlsplit(raw_url)
    except ValueError as exc:
        raise ValueError("网址格式无效") from exc
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("网址必须是完整的 HTTP 或 HTTPS 公开网址")
    if parsed.username or parsed.password:
        raise ValueError("网址不能包含用户名、密码或访问凭据")
    if parsed.query or parsed.fragment:
        raise ValueError("请填写不带参数和片段的公开网页网址，避免保存访问凭据或临时链接")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("网址端口格式无效") from exc
    if port is not None:
        raise ValueError("只接受标准 HTTP/HTTPS 网址，不接受自定义端口")
    hostname = parsed.hostname.casefold().rstrip(".")
    if not hostname.isascii() or not PUBLIC_HOST_PATTERN.fullmatch(hostname):
        raise ValueError("网址必须使用有效的公开 ASCII 域名")
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(_UNSAFE_HOST_SUFFIXES):
        raise ValueError("网址不能指向本机或内部网络域名")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise ValueError("网址必须使用公开域名，不能直接填写 IP 地址")
    decoded_path = unquote(parsed.path or "/")
    if any(ord(character) < 0x20 for character in decoded_path):
        raise ValueError("网址路径不能包含控制字符")
    canonical_url = urlunsplit((parsed.scheme.casefold(), hostname, parsed.path or "/", "", ""))
    return canonical_url, hostname


def preflight_public_source(source_url: object) -> dict[str, object]:
    """在不落库、不抓取后续页面的前提下检测一个人工补充来源能否安全直接抓取。

    依次执行：本地 URL 安全校验、API/凭据式入口拦截、robots 检查、受限同域页面读取及
    最终 URL 再校验。成功只表示服务器能读取这个公开入口，并不代表来源可信、页面事实
    成立、会形成正式证据或会自动招商。
    """

    try:
        canonical_url, domain = normalize_public_source_url(source_url)
    except ValueError as exc:
        return _public_result("unsafe_target", str(exc))
    parsed = urlsplit(canonical_url)
    if _looks_like_api_or_credential_endpoint(domain, parsed.path):
        return _public_result(
            "unsupported_api_or_credentials",
            "这个网址看起来是 API、版本接口或凭据式入口。业务人员只能补充无需密钥的公开网页；付费或授权 API 请由开发者在后端按供应商许可接入。",
            canonical_url=canonical_url,
            domain=domain,
        )
    try:
        robots = check_robots_access(canonical_url)
    except (OSError, RuntimeError, ValueError) as exc:
        return _public_result(
            "temporarily_unreachable",
            f"服务器暂时无法读取该网站的 robots 规则：{str(exc)[:300]}",
            canonical_url=canonical_url,
            domain=domain,
        )
    if robots.get("allowed") is not True:
        reason = str(robots.get("reason") or "robots 规则未允许访问")
        status = "robots_disallowed" if "robots_disallow" in reason else "temporarily_unreachable"
        detail = (
            "该网站的 robots 规则不允许本系统自动读取此网址。你仍可在有权访问时使用“人工材料入账”，但不能把它设为机器自动抓取来源。"
            if status == "robots_disallowed"
            else f"服务器暂时无法确认该网站的 robots 访问规则：{reason[:300]}"
        )
        return _public_result(status, detail, canonical_url=canonical_url, domain=domain)
    try:
        snapshot = fetch_public_source_for_observation(canonical_url)
    except (OSError, RuntimeError, ValueError) as exc:
        message = str(exc)
        normalized = message.casefold()
        if any(marker in normalized for marker in ("http 状态码：401", "http 状态码：403", "captcha", "登录", "会员")):
            status = "login_or_paid_access"
            detail = "该网址需要登录、会员权限或额外访问授权；不要在此粘贴账号、Cookie 或 API Key。你可以提交有权使用的人工材料，或由开发者按许可接入服务端数据源。"
        elif "不支持的来源内容类型" in message or "application/json" in normalized:
            status = "unsupported_api_or_credentials"
            detail = "该网址返回的不是可直接抓取的公开网页文本，可能是 API 或数据接口。付费/授权 API 请由开发者在后端按供应商许可接入。"
        else:
            status = "temporarily_unreachable"
            detail = f"服务器暂时不能安全读取该公开网页：{message[:300]}"
        return _public_result(status, detail, canonical_url=canonical_url, domain=domain)
    final_url = snapshot.get("source_url")
    try:
        normalized_final_url, final_domain = normalize_public_source_url(final_url)
    except ValueError:
        return _public_result(
            "unsafe_target",
            "该网址重定向后的地址不符合公开来源安全规则，因此不会加入自动抓取目录。",
            canonical_url=canonical_url,
            domain=domain,
        )
    if final_domain != domain:
        return _public_result(
            "unsafe_target",
            "该网址重定向到不同域名。为避免把原网站的 robots 许可扩展到其他网站，不能加入自动抓取目录。",
            canonical_url=canonical_url,
            domain=domain,
        )
    return _public_result(
        "ready_for_public_crawl",
        "服务器已读取公开页面，robots 规则允许；该来源可加入低频自动发现。它只会生成待人工核验候选，不会自动入证、下招商结论或联系企业。",
        canonical_url=normalized_final_url,
        domain=final_domain,
        content_type=snapshot.get("content_type") if isinstance(snapshot.get("content_type"), str) else None,
        byte_size=snapshot.get("byte_size") if isinstance(snapshot.get("byte_size"), int) else None,
        source_title=snapshot.get("source_title") if isinstance(snapshot.get("source_title"), str) else None,
    )


def _ready_preflight_or_error(preflight: dict[str, object]) -> dict[str, object]:
    """只让已经完成安全检测的公开网页进入可抓取来源目录。"""

    if preflight.get("status") != "ready_for_public_crawl":
        detail = preflight.get("detail")
        raise ValueError(str(detail or "服务器未确认该网址可直接抓取"))
    return preflight


def add_public_source_supplement(
    workspace_dir: Path,
    *,
    source_url: object,
    note: object | None,
    authenticated_user_id: str,
) -> dict[str, Any]:
    """检测并登记一条可直接抓取的公开来源；失败时不把网址写入台账。"""

    preflight = _ready_preflight_or_error(preflight_public_source(source_url))
    return register_public_source_supplement(
        workspace_dir,
        preflight=preflight,
        note=note,
        authenticated_user_id=authenticated_user_id,
    )


def retest_public_source_supplement(
    workspace_dir: Path,
    *,
    source_id: str,
    authenticated_user_id: str,
) -> dict[str, Any]:
    """重新检测已经登记的公开来源，不接受浏览器替换网址或抓取参数。"""

    source = get_public_source_supplement(workspace_dir, source_id=source_id)
    preflight = preflight_public_source(source["seed_url"])
    return record_public_source_supplement_preflight(
        workspace_dir,
        source_id=source_id,
        preflight=preflight,
        authenticated_user_id=authenticated_user_id,
        requested_status=None,
    )


def set_public_source_supplement_status(
    workspace_dir: Path,
    *,
    source_id: str,
    new_status: str,
    authenticated_user_id: str,
) -> dict[str, Any]:
    """停用无需联网；重新启用必须在同一请求内重新通过安全检测。"""

    normalized_status = new_status.strip().casefold() if isinstance(new_status, str) else ""
    if normalized_status not in {"active", "disabled"}:
        raise ValueError("new_status只能是active或disabled")
    if normalized_status == "disabled":
        return record_public_source_supplement_preflight(
            workspace_dir,
            source_id=source_id,
            preflight=None,
            authenticated_user_id=authenticated_user_id,
            requested_status="disabled",
        )
    source = get_public_source_supplement(workspace_dir, source_id=source_id)
    preflight = _ready_preflight_or_error(preflight_public_source(source["seed_url"]))
    return record_public_source_supplement_preflight(
        workspace_dir,
        source_id=source_id,
        preflight=preflight,
        authenticated_user_id=authenticated_user_id,
        requested_status="active",
    )


def list_public_source_supplement_status(workspace_dir: Path) -> dict[str, Any]:
    """返回已登记的补充公开来源，不在读取时再次联网。"""

    return list_public_source_supplements(workspace_dir)
