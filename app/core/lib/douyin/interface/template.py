"""提供抖音与 TikTok 接口共用的分页、签名和请求模板。"""

import json
import re
from time import time as time_func
from typing import TYPE_CHECKING, Callable, Coroutine, Mapping, Type, Union
from urllib.parse import quote, urlencode

from httpx import AsyncClient, HTTPStatusError, get, post
from rich.progress import (
    BarColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
)

# 允许在工具包尚不可用时导入接口定义；兜底对象只提供最小兼容行为。
try:
    from ..tools import (
        PROGRESS,
        USERAGENT,
        wait,
        DownloaderError,
        FakeProgress,
        Retry,
        capture_error_request,
    )
    from ..tools.retry import abort_if_stopped
except ImportError:
    PROGRESS = "bold magenta"
    USERAGENT = ""

    async def wait():
        
        pass

    class DownloaderError(Exception):
        pass

    class FakeProgress:
        """提供一个最小可用的进度条替身，保证兜底分支仍能正常运行。"""

        def __enter__(self):
            """进入上下文管理器并返回当前对象。"""
            return self

        def __exit__(self, *args):
            """退出上下文管理器时不执行额外清理。"""
            pass

        def add_task(self, *args, **kwargs):
            """兼容真实进度条接口，返回一个占位任务句柄。"""
            return 0

        def update(self, *args, **kwargs):
            """兼容真实进度条接口，但在兜底模式下不做任何展示。"""
            pass

    class Retry:
        """提供最小化的重试装饰器兼容层。"""

        @staticmethod
        def retry(func):
            """在兜底模式下直接返回原函数，避免导入失败影响主流程。"""
            return func

    def capture_error_request(func):
        
        return func

    async def abort_if_stopped(_owner):
        return False

try:
    from ..translation import _
except ImportError:
    def _(x):
        return x

if TYPE_CHECKING:
    from typing import Any

    Parameter = Any
    Params = Any

__all__ = [
    "API",
    "APITikTok",
]

def _extract_chrome_version(user_agent: str) -> str:
    """从 UA 中提取 Chrome 主版本串，避免把整段 Mozilla 标识误传给接口。"""
    match = re.search(r"Chrome/([\d.]+)", user_agent or "")
    return match.group(1) if match else "139.0.0.0"

CHROME_VERSION = _extract_chrome_version(USERAGENT)
_SENSITIVE_LOG_HEADERS = frozenset({"authorization", "cookie", "set-cookie"})
_WORK_ID_PATTERN = re.compile(r"(?:^|&)(?:aweme_id|item_id)=(\d{1,32})(?:&|$)")


def redact_logged_headers(headers: Mapping[str, object] | None) -> dict[str, object]:
    """删除记录前的凭据头，避免 Cookie 和 Authorization 进入日志。"""
    if not isinstance(headers, Mapping):
        return {}
    return {
        str(key): value
        for key, value in headers.items()
        if str(key).lower() not in _SENSITIVE_LOG_HEADERS
    }


try:
    from ..tools.log_redact import redact_logged_text, strip_ms_token_query
except ImportError:
    def strip_ms_token_query(value: str | None) -> str:
        return value or ""

    def redact_logged_text(value: object) -> str:
        return "" if value is None else str(value)


def extract_work_id(params: dict | str | None) -> str:
    """从查询参数中提取作品 ID，不返回令牌或其他查询值。"""
    if isinstance(params, dict):
        for key in ("aweme_id", "item_id"):
            value = params.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                text = str(value)
            elif isinstance(value, str):
                text = value
            else:
                continue
            if text.isdigit() and len(text) <= 32:
                return text
        return ""
    if not isinstance(params, str):
        return ""
    query = strip_ms_token_query(params)
    query = query.split("?", 1)[-1]
    match = _WORK_ID_PATTERN.search(query)
    return match.group(1) if match else ""

class API:
    """抖音接口基类，统一处理分页、a_bogus 签名、重试和响应解析。"""
    
    domain = "https://www.douyin.com/"
    short_domain = "https://www.iesdouyin.com/"
    referer = f"{domain}?recommend=1"
    params = {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "update_version_code": "170400",
        "pc_client_type": "1",
        "pc_libra_divert": "Windows",
        "support_h265": "1",
        "support_dash": "1",
        "version_code": "290100",
        "version_name": "29.1.0",
        "cookie_enabled": "true",
        "screen_width": "1536",
        "screen_height": "864",
        "browser_language": "zh-CN",
        "browser_platform": "Win32",
        "browser_name": "Chrome",
        "browser_version": CHROME_VERSION,
        "browser_online": "true",
        "engine_name": "Blink",
        "engine_version": CHROME_VERSION,
        "os_name": "Windows",
        "os_version": "10",
        "cpu_core_num": "16",
        "device_memory": "8",
        "platform": "PC",
        "downlink": "10",
        "effective_type": "4g",
        "round_trip_time": "200",
        "uifid": "",
        "msToken": "",
    }
    progress_object: Callable

    def __init__(
            self,
            params: Union["Parameter", "Params"],
            cookie: str = "",
            proxy: str = None,
            *args,
            **kwargs,
    ):
        """绑定共享客户端、请求头和签名器，并初始化分页状态。"""
        self.headers = params.headers.copy()
        self.log = params.logger
        self.ab = params.ab
        self.console = params.console
        self.api = ""
        self.proxy = proxy
        self.max_retry = params.max_retry
        self.timeout = params.timeout
        self.cookie = cookie
        self.client: AsyncClient = params.client
        shared_params = getattr(params, "params", None)
        if isinstance(shared_params, dict):
            self.params = shared_params
        else:
            self.params = dict(getattr(type(self), "params", {}) or {})
        self._stop_check = getattr(params, "stop_requested", None)
        self._logged_work_id = ""
        self.pages = 99999
        self.cursor = 0
        self.response = []
        self.finished = False
        self.text = ""
        self.set_temp_cookie(cookie)

    async def _abort_request(self) -> bool:
        """请求开始前检查停止标志，并关闭当前客户端。"""
        if await abort_if_stopped(self):
            self.finished = True
            return True
        return False

    def set_temp_cookie(self, cookie: str = ""):
        """仅在显式传入 Cookie 时覆盖当前实例的请求头。"""
        if cookie:
            self.headers["Cookie"] = cookie

    def generate_params(
            self,
    ) -> dict:
        
        return self.params

    def generate_data(self, *args, **kwargs) -> dict:
        
        return {}

    async def run(
            self,
            referer: str = None,
            single_page=False,
            data_key: str = "",
            error_text="",
            cursor="cursor",
            has_more="has_more",
            params: Callable = lambda: {},
            data: Callable = lambda: {},
            method="GET",
            headers: dict = None,
            *args,
            **kwargs,
    ):
        """设置 Referer 后分派到单页或分页流程。"""
        self.set_referer(referer)
        match single_page:
            case True:
                await self.run_single(
                    data_key,
                    error_text,
                    cursor,
                    has_more,
                    params,
                    data,
                    method,
                    headers,
                    *args,
                    **kwargs,
                )
            case False:
                await self.run_batch(
                    data_key,
                    error_text,
                    cursor,
                    has_more,
                    params,
                    data,
                    method,
                    headers,
                    *args,
                    **kwargs,
                )
            case _:
                raise DownloaderError
        return self.response

    async def run_single(
            self,
            data_key: str,
            error_text="",
            cursor="cursor",
            has_more="has_more",
            params: Callable = lambda: {},
            data: Callable = lambda: {},
            method="GET",
            headers: dict = None,
            *args,
            **kwargs,
    ):
        
        if data := await self.request_data(
                self.api,
                params=params() or self.generate_params(),
                data=data() or self.generate_data(),
                method=method,
                headers=headers,
                finished=True,
        ):
            self.check_response(
                data, data_key, error_text, cursor, has_more, *args, **kwargs
            )
        else:
            self.log.warning(_("获取{self_text}数据失败").format(self_text=self.text))

    async def run_batch(
            self,
            data_key: str,
            error_text="",
            cursor="cursor",
            has_more="has_more",
            params: Callable = lambda: {},
            data: Callable = lambda: {},
            method="GET",
            headers: dict = None,
            callback: Type[Coroutine] = None,
            *args,
            **kwargs,
    ):
        
        with self.progress_object() as progress:
            task_id = progress.add_task(
                _("正在获取{text}数据").format(text=self.text),
                total=None,
            )
            while not self.finished and self.pages > 0:
                if await self._abort_request():
                    break
                progress.update(task_id)
                await self.run_single(
                    data_key,
                    error_text,
                    cursor,
                    has_more,
                    params,
                    data,
                    method,
                    headers,
                    *args,
                    **kwargs,
                )
                self.pages -= 1
                if callback:
                    await callback()

    def check_response(
            self,
            data_dict: dict,
            data_key: str,
            error_text="",
            cursor="cursor",
            has_more="has_more",
            *args,
            **kwargs,
    ):
        
        try:
            if not (d := data_dict[data_key]):
                self.log.warning(error_text)
                self.finished = True
            else:
                self.cursor = data_dict[cursor]
                self.append_response(d)
                self.finished = not data_dict[has_more]
        except KeyError:
            self.log.error(
                _("数据解析失败，请告知作者处理: {data}").format(data=data_dict)
            )
            self.finished = True

    def set_referer(self, url: str = None) -> None:
        """设置本次请求的 Referer；未传值时恢复平台默认入口。"""
        self.headers["Referer"] = url or self.referer

    async def request_data(
            self,
            url: str,
            params: dict = None,
            data: dict = None,
            method="GET",
            headers: dict = None,
            encryption="GET",
            finished=False,
            *args,
            **kwargs,
    ):
        params = self.deal_url_params(
            params,
            encryption,
        )
        match (method, bool(self.proxy)):
            case ("GET", False):
                return await self.request_data_get(
                    url,
                    params,
                    headers or self.headers,
                    finished=finished,
                    *args,
                    **kwargs,
                )
            case ("GET", True):
                return await self.request_data_get_proxy(
                    url,
                    params,
                    headers or self.headers,
                    finished=finished,
                    *args,
                    **kwargs,
                )
            case ("POST", False):
                return await self.request_data_post(
                    url,
                    params,
                    data,
                    headers or self.headers,
                    finished=finished,
                    *args,
                    **kwargs,
                )
            case ("POST", True):
                return await self.request_data_post_proxy(
                    url,
                    params,
                    data,
                    headers or self.headers,
                    finished=finished,
                    *args,
                    **kwargs,
                )
            case _:
                raise DownloaderError

    @Retry.retry
    @capture_error_request
    async def request_data_get(
            self,
            url: str,
            params: str,
            headers: dict,
            finished=False,
            **kwargs,
    ):
        
        if await self._abort_request():
            return None
        self.__record_request_messages(
            url,
            params,
            None,
            headers,
            **kwargs,
        )
        response = await self.client.get(
            f"{url}?{params}",
            headers=headers,
            **kwargs,
        )
        return await self.__return_response(response)

    @Retry.retry
    @capture_error_request
    async def request_data_get_proxy(
            self,
            url: str,
            params: str,
            headers: dict,
            finished=False,
            **kwargs,
    ):
        
        if await self._abort_request():
            return None
        verify = kwargs.pop("verify", True)
        self.__record_request_messages(
            url,
            params,
            None,
            headers,
            **kwargs,
        )
        response = get(
            f"{url}?{params}",
            headers=headers,
            proxy=self.proxy,
            follow_redirects=True,
            verify=verify,
            timeout=self.timeout,
            **kwargs,
        )
        return await self.__return_response(response)

    @Retry.retry
    @capture_error_request
    async def request_data_post(
            self, url: str, params: str, data: dict, headers: dict, finished=False, **kwargs
    ):
        
        if await self._abort_request():
            return None
        self.__record_request_messages(
            url,
            params,
            data,
            headers,
            **kwargs,
        )
        response = await self.client.post(
            f"{url}?{params}",
            data=data,
            headers=headers,
            **kwargs,
        )
        return await self.__return_response(response)

    @Retry.retry
    @capture_error_request
    async def request_data_post_proxy(
            self, url: str, params: str, data: dict, headers: dict, finished=False, **kwargs
    ):
        
        if await self._abort_request():
            return None
        verify = kwargs.pop("verify", True)
        self.__record_request_messages(
            url,
            params,
            data,
            headers,
            **kwargs,
        )
        response = post(
            f"{url}?{params}",
            data=data,
            headers=headers,
            proxy=self.proxy,
            follow_redirects=True,
            verify=verify,
            timeout=self.timeout,
            **kwargs,
        )
        return await self.__return_response(response)

    async def __return_response(self, response):
        """校验 HTTP 状态并解析 JSON；请求或解析失败时返回 None。"""
        self.log.info(f"Response Code: {response.status_code}", False)
        work_id = getattr(self, "_logged_work_id", "")
        if work_id:
            self.log.info(f"Work ID: {work_id}", False)

        try:
            response.raise_for_status()
        except HTTPStatusError as e:
            self.log.error(f"HTTP 请求异常: {redact_logged_text(e)}")
            return None

        await wait()

        try:
            return response.json()
        except json.JSONDecodeError:
            # 限制预览长度，避免非 JSON 响应把整页 HTML 写入日志。
            content = response.text
            preview = content[:2000] if len(content) > 2000 else content
            self.log.error(f"❌ 响应不是有效的 JSON 格式！")
            self.log.error(f"📄 响应内容预览:\n{preview}")
            return None

    def __record_request_messages(
            self,
            url: str,
            params: str | None,
            data: dict | None,
            headers: dict,
            **kwargs,
    ):
        """只记录作品 ID；URL、查询串、请求体和请求头一律不进入日志。"""
        del url, data, headers
        work_id = extract_work_id(params)
        self._logged_work_id = work_id
        if work_id:
            self.log.info(f"Work ID: {work_id}", False)

    def deal_url_params(
            self,
            params: dict,
            method="GET",
            **kwargs,
    ) -> str:
        
        if params:
            params_str = urlencode(
                params,
                quote_via=quote,
            )
            # a_bogus 必须基于最终 URL 编码串生成，否则服务端看到的参数与摘要不一致。
            a_bogus = self.ab.get_value(params_str, method)

            params_str += f"&a_bogus={a_bogus}"
            return params_str
        return ""

    def summary_works(
            self,
    ) -> None:
        
        self.log.info(
            _("共获取到 {count} 个{text}").format(
                count=len(self.response), text=self.text
            )
        )

    @classmethod
    def init_progress_object(
            cls,
            server_mode: bool = False,
    ) -> None:
        
        if server_mode:
            cls._progress_factory = cls.__fake_progress_object
        else:
            cls._progress_factory = cls.__general_progress_object

    def progress_object(self):
        
        factory = getattr(self, "_progress_factory", self.__general_progress_object)
        return factory()

    def __general_progress_object(self):
        """创建交互模式下使用的 Rich 进度条。"""
        return Progress(
            TextColumn(
                "[progress.description]{task.description}",
                style=PROGRESS,
                justify="left",
            ),
            "•",
            BarColumn(),
            "•",
            TimeElapsedColumn(),
            console=self.console,
            transient=True,
            expand=True,
        )

    @staticmethod
    def __fake_progress_object(*args, **kwargs):
        """服务器模式下返回无输出的进度条兼容对象。"""
        return FakeProgress()

    def append_response(
            self,
            data: list[dict],
            start: int = None,
            end: int = None,
            *args,
            **kwargs,
    ) -> None:
        
        for item in data[start:end]:
            self.response.append(item)

class APITikTok(API):
    """TikTok 接口基类，为查询串追加 X-Bogus 与 X-Gnarly。"""
    
    domain = "https://www.tiktok.com/"
    short_domain = ""
    referer = f"{domain}explore"
    params = {
        "WebIdLastTime": int(time_func()),
        "aid": "1988",
        "app_language": "en",
        "app_name": "tiktok_web",
        "browser_language": "zh-SG",
        "browser_name": "Mozilla",
        "browser_online": "true",
        "browser_platform": "Win32",
        "browser_version": CHROME_VERSION,
        "channel": "tiktok_web",
        "cookie_enabled": "true",
        "data_collection_enabled": "true",
        "device_id": "",
        "device_platform": "web_pc",
        "focus_state": "true",
        "from_page": "user",
        "history_len": "4",
        "is_fullscreen": "false",
        "is_page_visible": "true",
        "language": "en",
        "os": "windows",
        "priority_region": "CN",
        "referer": "",
        "region": "JP",
        "screen_height": "864",
        "screen_width": "1536",
        "tz_name": "Asia/Shanghai",
        "user_is_login": "true",
        "webcast_language": "en",
        "msToken": "",
    }

    def __init__(
            self,
            params: Union["Parameter", "Params"],
            cookie: str = "",
            proxy: str = None,
            *args,
            **kwargs,
    ):
        """切换到 TikTok 请求头、客户端和两种签名器。"""
        super().__init__(params, cookie, proxy, *args, **kwargs)
        self.xb = params.xb
        self.xg = params.xg
        self.headers = params.headers_tiktok.copy()
        shared_params = getattr(params, "params_tiktok", None)
        if isinstance(shared_params, dict):
            self.params = shared_params
        self.cookie = cookie
        self.client: AsyncClient = params.client_tiktok
        self._stop_check = getattr(params, "stop_requested", None)
        self.set_temp_cookie(cookie)

    async def request_data(
            self,
            url: str,
            params: dict = None,
            data: dict = None,
            method="GET",
            headers: dict = None,
            encryption=8,
            finished=False,
            *args,
            **kwargs,
    ):
        
        return await super().request_data(
            url=url,
            params=params,
            data=data,
            method=method,
            headers=headers,
            encryption=encryption,
            finished=finished,
            *args,
            **kwargs,
        )

    def deal_url_params(
            self,
            params: dict,
            number=8,
            **kwargs,
    ) -> str:
        
        if params:
            params = urlencode(
                params,
                quote_via=quote,
            )
            # 两种签名必须基于同一参数串和 UA，避免请求上下文不一致。
            xb = self.xb.get_x_bogus(
                params, number, self.headers.get("User-Agent", USERAGENT)
            )
            xg = self.xg.generate(
                params, user_agent=self.headers.get("User-Agent", USERAGENT)
            )
            params += f"&X-Bogus={xb}&X-Gnarly={xg}"
            return params
        return ""
