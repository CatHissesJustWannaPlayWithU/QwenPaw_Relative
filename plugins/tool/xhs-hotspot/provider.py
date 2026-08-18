"""可替换的第三方热点数据源客户端。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

from .models import ProviderResponse


class HotspotProviderError(RuntimeError):
    """第三方热点数据源不可用或返回异常时抛出。"""


class AidataHotspotClient:
    """调用 AIDATA 小红书热搜接口的客户端。

    API Key 只存在于请求头中；原始响应、规范化快照和错误信息都不会记录它。
    """

    _SAFE_RESPONSE_HEADERS = {"content-type", "date", "etag", "last-modified"}

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        timeout_seconds: float = 20,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key.strip()
        self._base_url = self._validate_base_url(base_url)
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    @staticmethod
    def _validate_base_url(base_url: str) -> str:
        parsed = urlparse(base_url.strip())
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise HotspotProviderError("热点 API 地址必须是完整的 HTTP(S) URL")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise HotspotProviderError("生产数据源必须使用 HTTPS 地址")
        return base_url.strip()

    async def fetch(self, *, force_refresh: bool = True) -> ProviderResponse:
        """请求一次热点数据，并仅保留可安全存档的响应头。"""
        if not self._api_key:
            raise HotspotProviderError(
                "尚未配置 API Key。请在智能体的“小红书热点采集”工具配置中填写 AIDATA API Key。"
            )

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        params = {"refresh": "true"} if force_refresh else None
        timeout = httpx.Timeout(self._timeout_seconds)

        async with httpx.AsyncClient(
            timeout=timeout,
            transport=self._transport,
            follow_redirects=False,
        ) as client:
            response: httpx.Response | None = None
            for attempt in range(3):
                try:
                    response = await client.get(
                        self._base_url,
                        headers=headers,
                        params=params,
                    )
                except httpx.RequestError as exc:
                    if attempt == 2:
                        raise HotspotProviderError("请求热点 API 失败，请检查网络和 API 地址") from exc
                    await asyncio.sleep(0.4 * (attempt + 1))
                    continue

                if response.status_code in {401, 403}:
                    raise HotspotProviderError("热点 API 鉴权失败，请检查 API Key 是否有效")
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt == 2:
                        raise HotspotProviderError(
                            f"热点 API 暂时不可用，HTTP {response.status_code}"
                        )
                    await asyncio.sleep(0.4 * (attempt + 1))
                    continue
                if response.status_code >= 400:
                    raise HotspotProviderError(
                        f"热点 API 返回 HTTP {response.status_code}，本次数据未保存"
                    )
                break

            if response is None:
                raise HotspotProviderError("热点 API 没有返回响应")
            try:
                payload: dict[str, Any] = response.json()
            except ValueError as exc:
                raise HotspotProviderError("热点 API 返回的不是 JSON 数据，本次数据未保存") from exc
            if not isinstance(payload, dict):
                raise HotspotProviderError("热点 API 返回格式异常，顶层必须是对象")

            safe_headers = {
                key: value
                for key, value in response.headers.items()
                if key.lower() in self._SAFE_RESPONSE_HEADERS
            }
            return ProviderResponse(
                payload=payload,
                provider_name="aidata",
                source_url=str(response.url),
                fetched_at=datetime.now(timezone.utc).isoformat(),
                status_code=response.status_code,
                response_headers=safe_headers,
            )


class UapiHotspotClient:
    """调用 UAPI 免费公开接口的小红书热点客户端。

    这个来源目前不需要 API Key。它仍是第三方聚合服务，因此每次采集都会
    保存原始响应、来源 URL 和数据源更新时间，不能把它误认为小红书官方档案。
    """

    DEFAULT_BASE_URL = "https://uapis.cn/api/v1/misc/hotboard?type=xiaohongshu"
    _SAFE_RESPONSE_HEADERS = {"content-type", "date", "etag", "last-modified"}

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 20,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = AidataHotspotClient._validate_base_url(base_url)
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def fetch(self, *, force_refresh: bool = True) -> ProviderResponse:
        """请求一次免费热点数据。

        UAPI 不提供 refresh 参数；该参数保留只是为了让两个数据源拥有同一
        调用方式，避免上层工具因切换来源而改变业务逻辑。
        """
        del force_refresh
        timeout = httpx.Timeout(self._timeout_seconds)
        async with httpx.AsyncClient(
            timeout=timeout,
            transport=self._transport,
            follow_redirects=False,
        ) as client:
            response: httpx.Response | None = None
            for attempt in range(3):
                try:
                    response = await client.get(
                        self._base_url,
                        headers={"Accept": "application/json"},
                    )
                except httpx.RequestError as exc:
                    if attempt == 2:
                        raise HotspotProviderError("请求免费热点 API 失败，请检查网络") from exc
                    await asyncio.sleep(0.4 * (attempt + 1))
                    continue

                if response.status_code == 429 or response.status_code >= 500:
                    if attempt == 2:
                        raise HotspotProviderError(
                            f"免费热点 API 暂时不可用，HTTP {response.status_code}"
                        )
                    await asyncio.sleep(0.4 * (attempt + 1))
                    continue
                if response.status_code >= 400:
                    raise HotspotProviderError(
                        f"免费热点 API 返回 HTTP {response.status_code}，本次数据未保存"
                    )
                break

            if response is None:
                raise HotspotProviderError("免费热点 API 没有返回响应")
            try:
                payload: dict[str, Any] = response.json()
            except ValueError as exc:
                raise HotspotProviderError("免费热点 API 返回的不是 JSON 数据") from exc
            if not isinstance(payload, dict):
                raise HotspotProviderError("免费热点 API 返回格式异常，顶层必须是对象")
            if payload.get("type") != "xiaohongshu" or not isinstance(payload.get("list"), list):
                raise HotspotProviderError("免费热点 API 没有返回可用的小红书榜单")

            safe_headers = {
                key: value
                for key, value in response.headers.items()
                if key.lower() in self._SAFE_RESPONSE_HEADERS
            }
            return ProviderResponse(
                payload=payload,
                provider_name="uapi_free",
                source_url=str(response.url),
                fetched_at=datetime.now(timezone.utc).isoformat(),
                status_code=response.status_code,
                response_headers=safe_headers,
            )
