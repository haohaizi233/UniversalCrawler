"""展开抖音和 TikTok 文本中的短链，并按需读取响应内容。"""

from asyncio import to_thread
from json import loads
from re import compile
from typing import TYPE_CHECKING
from urllib.parse import unquote, urljoin, urlsplit

from shared.network.pinned_transport import (
    PinnedTransport,
    PinnedTransportNetworkError,
    canonicalize_host,
    canonicalize_request_target,
)
from shared.runtime_options import DomainPolicyEngine, DomainPolicyViolation

try:
    from ..tools import (
        TIMEOUT,
        DownloaderError,
        Retry,
        capture_error_request,
        wait,
    )
    from ..tools.retry import abort_if_stopped
except ImportError:
    # 独立加载该文件时保留最小兼容层，避免工具包导入失败阻断链接解析。
    TIMEOUT = 10

    class DownloaderError(Exception):
        """表示链接响应类型不在调用方支持范围内。"""
        pass

    class Retry:
        """提供最小化的重试装饰器兼容层。"""

        @staticmethod
        def retry(func):
            """在兜底模式下直接返回原函数，避免兼容层再引入副作用。"""
            return func

    def capture_error_request(func):
        return func

    async def wait():
        pass

    async def abort_if_stopped(_owner):
        return False

if TYPE_CHECKING:
    from httpx import AsyncClient, get, head
    from ..tools.parameter import Parameter

__all__ = [
    "Requester",
    "is_douyin_public_host",
    "is_douyin_live_reflow_url",
    "is_douyin_public_url",
]

_DOUYIN_PUBLIC_SUFFIXES = (
    "douyin.com",
    "iesdouyin.com",
    "tiktok.com",
    "tiktokv.com",
)
_DOUYIN_LIVE_REFLOW_HOST = "webcast.amemv.com"
_DOUYIN_LIVE_REFLOW_PATH = "/douyin/webcast/reflow/"
_MAX_LIVE_PATH_DECODE_ROUNDS = 4
_SENSITIVE_LIVE_HEADERS = frozenset(
    {"authorization", "cookie", "host", "proxy-authorization"}
)


def is_douyin_public_host(host: str) -> bool:
    """Return whether *host* is one complete canonical platform label suffix."""

    try:
        canonical = canonicalize_host(host)
    except (DomainPolicyViolation, UnicodeError, ValueError):
        return False
    return any(
        canonical == suffix or canonical.endswith(f".{suffix}")
        for suffix in _DOUYIN_PUBLIC_SUFFIXES
    )


def is_douyin_public_url(url: str) -> bool:
    """Validate one HTTPS platform URL before the public pinned transport sees it."""

    try:
        target = canonicalize_request_target(url)
    except (DomainPolicyViolation, UnicodeError, ValueError):
        return False
    return target.scheme == "https" and is_douyin_public_host(target.host)


def is_platform_redirect_hop(url: str, *, first_hop: bool, live_text: bool = False) -> bool:
    """允许平台后缀上的 https；http 只可能出现在短链首跳。"""

    if live_text and is_douyin_live_reflow_url(url):
        return True
    try:
        target = canonicalize_request_target(url)
    except (DomainPolicyViolation, UnicodeError, ValueError):
        return False
    if not is_douyin_public_host(target.host):
        return False
    if target.scheme == "https":
        return True
    return bool(first_hop and target.scheme == "http")


def headers_for_platform_hop(
    headers: dict[str, str] | None,
    url: str,
    *,
    live: bool = False,
) -> dict[str, str]:
    """http 首跳去掉 Cookie 和 Authorization；直播回流额外去掉敏感头。"""

    blocked: set[str] = set()
    if live:
        blocked.update(_SENSITIVE_LIVE_HEADERS)
    try:
        scheme = urlsplit(url).scheme.lower()
    except ValueError:
        scheme = ""
    if scheme != "https":
        blocked.update({"authorization", "cookie", "proxy-authorization"})
    source = headers or {}
    if not blocked:
        return dict(source)
    return {
        str(key): value
        for key, value in source.items()
        if str(key).lower() not in blocked
    }


def _redirect_location(headers: dict | None) -> str:
    if not isinstance(headers, dict):
        return ""
    for key, value in headers.items():
        if str(key).lower() == "location" and value:
            return str(value)
    return ""


def is_douyin_live_reflow_url(url: str) -> bool:
    """Recognize the one legacy live-share endpoint without widening host policy."""

    try:
        target = canonicalize_request_target(url)
    except (DomainPolicyViolation, UnicodeError, ValueError):
        return False
    path = urlsplit(target.url).path
    return (
        target.scheme == "https"
        and target.host == _DOUYIN_LIVE_REFLOW_HOST
        and target.port == 443
        and path.startswith(_DOUYIN_LIVE_REFLOW_PATH)
        and path != _DOUYIN_LIVE_REFLOW_PATH
        and not _has_unsafe_live_path_segments(path)
    )


def _has_unsafe_live_path_segments(path: str) -> bool:
    """Reject traversal at every bounded percent-decoding layer."""

    current = path
    for round_index in range(_MAX_LIVE_PATH_DECODE_ROUNDS + 1):
        normalized = current.replace("\\", "/")
        if any(segment in {".", ".."} for segment in normalized.split("/")):
            return True
        if any(ord(character) < 0x20 or ord(character) == 0x7F for character in current):
            return True
        decoded = unquote(current)
        if decoded == current:
            return False
        if round_index == _MAX_LIVE_PATH_DECODE_ROUNDS:
            return True
        current = decoded
    return True


def _is_retryable_transport_error(error: BaseException) -> bool:
    if isinstance(error, (OSError, TimeoutError, PinnedTransportNetworkError)):
        return True
    try:
        from curl_cffi import CurlError
    except ImportError:
        return False
    return isinstance(error, CurlError)

class Requester:
    """识别文本 URL，并通过异步客户端跟随目标平台的重定向。"""

    URL = compile(r"(https?://[^\s\"<>\\^`{|}，。；！？、【】《》]+)")

    def __init__(
            self,
            params: "Parameter",
            client: "AsyncClient",
            headers: dict[str, str],
            *,
            transport: PinnedTransport | None = None,
    ):
        """复用调用方提供的客户端、日志器和重试配置。"""
        self.client = client
        self.headers = headers
        self.log = params.logger
        self.max_retry = params.max_retry
        self.timeout = params.timeout
        self._stop_check = getattr(params, "stop_requested", None)
        self.transport = transport or PinnedTransport(timeout=float(self.timeout))

    async def aclose(self) -> None:
        close = getattr(self.client, "aclose", None)
        if callable(close):
            await close()

    async def run(
            self,
            text: str,
            proxy: str = None,
    ) -> str:
        """逐个展开文本 URL，并以空格连接有效结果。"""
        if not isinstance(text, str) or not text.strip():
            return ""
        urls = self.URL.finditer(text)
        if not urls:
            return ""
        result = []
        for i in urls:
            result.append(
                await self.request_url(
                    u := i.group(),
                    proxy=proxy,
                )
                or u
            )
            await wait()
        return " ".join(i for i in result if i)

    @Retry.retry
    @capture_error_request
    async def request_url(
            self,
            url: str,
            content="url",
            proxy: str = None,
    ):
        del proxy
        original = url
        live_text = content == "text" and is_douyin_live_reflow_url(url)
        if not is_platform_redirect_hop(url, first_hop=True, live_text=live_text):
            return url

        self.log.info("Resolving a public Douyin/TikTok URL", False)
        current = url
        response = None
        for hop_index in range(6):
            if await abort_if_stopped(self):
                self.finished = True
                return None
            try:
                response = await to_thread(
                    self.transport.request,
                    "GET",
                    current,
                    headers=headers_for_platform_hop(
                        self.headers,
                        current,
                        live=live_text,
                    ),
                    max_redirects=0,
                )
            except DomainPolicyViolation:
                self.log.warning("Blocked an unsafe Douyin/TikTok redirect", False)
                return original
            except Exception as error:
                if not _is_retryable_transport_error(error):
                    raise
                self.log.warning("Transport request failed; retrying", False)
                return None

            self.log.info(f"Response Code: {response.status_code}", False)
            location = _redirect_location(getattr(response, "headers", None))
            if response.status_code not in DomainPolicyEngine.REDIRECT_STATUS_CODES or not location:
                break
            if hop_index >= 5:
                self.log.warning("Blocked an unsafe Douyin/TikTok redirect", False)
                return original
            next_url = urljoin(str(getattr(response, "url", None) or current), location)
            if not is_platform_redirect_hop(next_url, first_hop=False, live_text=live_text):
                self.log.warning("Blocked an unsafe Douyin/TikTok redirect", False)
                return original
            current = next_url

        if response is None:
            return original

        match content:
            case "text":
                return response.text
            case "content":
                return response.body
            case "json":
                return loads(response.body)
            case "headers":
                return response.headers
            case "url":
                return str(response.url)
            case _:
                raise DownloaderError
