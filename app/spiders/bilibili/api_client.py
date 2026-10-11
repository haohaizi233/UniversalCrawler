"""Bilibili HTTP API client."""

import threading
import urllib.parse

import requests

from app.config import DEFAULT_USER_AGENT, cfg, get_setting_default
from app.debug_logger import debug_logger
from app.exceptions import InvalidCookieStateError, LoginCheckError, SpiderParseError, StreamResolveError
from app.services.auth_service import AuthService
from app.spiders.bilibili.parser import BilibiliParser
from app.utils.bilibili_wbi import BILIBILI_WBI_SIGNER
from app.utils.user_agents import resolve_user_agent
from shared.network_proxy import configure_requests_session


class BilibiliRequestStopped(Exception):
    """用户已停止，后续 Bilibili 请求不应再发出。"""

HEADERS = {
    'User-Agent': resolve_user_agent(
        "bilibili",
        None,
        configured_user_agent=cfg.get("bilibili", "user_agent", DEFAULT_USER_AGENT),
        default_user_agent=DEFAULT_USER_AGENT,
    ),
    'Referer': 'https://www.bilibili.com'
}


class BiliAPI:
    """B 站 API 访问层，负责登录态检查、详情读取和取流。"""

    def __init__(self, cookie_path, parser: BilibiliParser):
        """配置持久会话、解析器、认证服务和 Cookie。"""
        self.sess = configure_requests_session(requests.Session())
        self._session_lock = threading.RLock()
        with self._session_guard():
            self.sess.headers.update(HEADERS)
        self.cookie_path = cookie_path
        self.parser = parser
        self.auth_service = AuthService()
        self.request_timeout = cfg.get("bilibili", "timeout", get_setting_default("bilibili", "timeout"))
        self._video_info_errors: dict[str, dict[str, object]] = {}
        self.load_cookies()

    def _request_timeout(self):
        """兼容直接 `__new__` 构造的测试对象，延迟读取超时配置。"""
        return getattr(self, "request_timeout", cfg.get("bilibili", "timeout", get_setting_default("bilibili", "timeout")))

    def close(self) -> None:
        with self._session_guard():
            close = getattr(getattr(self, "sess", None), "close", None)
            if callable(close):
                close()

    def _stop_requested(self) -> bool:
        check = getattr(self, "stop_check", None)
        if not callable(check):
            return False
        try:
            return bool(check())
        except Exception:
            return False

    def _raise_if_stopped(self) -> None:
        """每次发出请求前确认任务仍在运行；停止时先关掉会话。"""
        if not self._stop_requested():
            return
        self.close()
        raise BilibiliRequestStopped()

    def _guarded_get(self, *args, **kwargs):
        self._raise_if_stopped()
        return self.sess.get(*args, **kwargs)

    def _session_guard(self) -> threading.RLock:
        lock = getattr(self, "_session_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._session_lock = lock
        return lock

    def _remember_video_info_error(self, target: str, resp: dict, http_status: int) -> None:
        errors = getattr(self, "_video_info_errors", None)
        if not isinstance(errors, dict):
            errors = {}
            self._video_info_errors = errors
        errors[str(target)] = {
            "code": resp.get("code"),
            "message": resp.get("message") or resp.get("msg") or "",
            "http_status": http_status,
        }

    def consume_video_info_error(self, target: object) -> dict[str, object] | None:
        errors = getattr(self, "_video_info_errors", None)
        if not isinstance(errors, dict):
            return None
        return errors.pop(str(target), None)

    def load_cookies(self):
        """从认证文件加载 Cookie，并写入持久 HTTP 会话。"""
        cookies = self.auth_service.load_json_file(self.cookie_path)
        cookie_dict = self.auth_service.extract_cookie_dict(cookies)
        with self._session_guard():
            if cookie_dict:
                for key, value in cookie_dict.items():
                    self.sess.cookies.set(key, value, domain=".bilibili.com")
            elif cookies is not None:
                raise InvalidCookieStateError("Bilibili Cookie 格式非法")

    def snapshot_cookies(self) -> dict[str, str]:
        with self._session_guard():
            return {c.name: c.value for c in self.sess.cookies if c.name}

    def check_login(self):
        
        try:
            self._raise_if_stopped()
            url = "https://api.bilibili.com/x/web-interface/nav"
            with self._session_guard():
                response = self._guarded_get(url, timeout=self._request_timeout())
            resp = response.json()
            BILIBILI_WBI_SIGNER.update_from_nav_data(resp.get("data") if isinstance(resp, dict) else None)
            debug_logger.log_api(
                component="BiliAPI",
                api_name="check_login",
                request={"url": url},
                response_summary={
                    "api_code": resp.get("code"),
                    "is_login": resp.get("data", {}).get("isLogin", False),
                    "mid": resp.get("data", {}).get("mid"),
                    "uname": resp.get("data", {}).get("uname"),
                },
                message="检查 Bilibili 登录状态",
                status_code=response.status_code,
            )
            return resp['code'] == 0 and resp['data']['isLogin']
        except BilibiliRequestStopped:
            raise
        except (requests.RequestException, ValueError, KeyError) as e:
            debug_logger.log_exception("BiliAPI", "check_login", e, context={"cookie_path": self.cookie_path})
            raise LoginCheckError("Bilibili 登录状态校验失败") from e

    def _signed_api_get(
        self,
        endpoint: str,
        params: dict[str, object],
        *,
        unsigned_endpoint: str | None = None,
    ) -> tuple[requests.Response, bool, str]:
        self._raise_if_stopped()
        headers = getattr(self.sess, "headers", None)
        if not isinstance(headers, dict):
            headers = HEADERS
        with self._session_guard():
            signed_params, signed = BILIBILI_WBI_SIGNER.sign_params(
                params,
                request_get=self._guarded_get,
                headers=headers,
                timeout=self._request_timeout(),
            )
            self._raise_if_stopped()
            request_endpoint = endpoint if signed or not unsigned_endpoint else unsigned_endpoint
            response = self.sess.get(request_endpoint, params=signed_params, timeout=self._request_timeout())
        response_url = getattr(response, "url", None)
        if not isinstance(response_url, str) or not response_url:
            response_url = f"{request_endpoint}?{urllib.parse.urlencode(signed_params)}"
        return response, signed, response_url

    def _unsigned_api_get(
        self,
        endpoint: str,
        params: dict[str, object],
    ) -> tuple[requests.Response, str]:
        self._raise_if_stopped()
        with self._session_guard():
            response = self._guarded_get(endpoint, params=params, timeout=self._request_timeout())
        response_url = getattr(response, "url", None)
        if not isinstance(response_url, str) or not response_url:
            response_url = f"{endpoint}?{urllib.parse.urlencode(params)}"
        return response, response_url

    def get_video_info(self, bvid: str | None = None, trace_id=None, *, aid: str | int | None = None):
        """按 bvid 获取视频详情；旧版 av 链接则按 aid 查询。"""
        target = str(aid if aid is not None else bvid or "").strip()
        if not target:
            raise SpiderParseError("missing Bilibili video id")
        query_key = "aid" if aid is not None else "bvid"
        try:
            endpoint = "https://api.bilibili.com/x/web-interface/view"
            response, signed, request_url = self._signed_api_get(endpoint, {query_key: target})
            resp = response.json()
            if not isinstance(resp, dict):
                raise ValueError("unexpected Bilibili video detail response")
            unsigned_retry_attempted = False
            unsigned_retry_used = False
            if signed and resp.get("code") != 0:
                unsigned_retry_attempted = True
                try:
                    retry_response, retry_url = self._unsigned_api_get(endpoint, {query_key: target})
                    retry_resp = retry_response.json()
                    if not isinstance(retry_resp, dict):
                        raise ValueError("unexpected unsigned Bilibili video detail response")
                    if retry_resp.get("code") == 0:
                        response = retry_response
                        resp = retry_resp
                        request_url = retry_url
                        signed = False
                        unsigned_retry_used = True
                except (requests.RequestException, ValueError, TypeError) as retry_exc:
                    debug_logger.log_exception(
                        "BiliAPI",
                        "get_video_info_unsigned_retry",
                        retry_exc,
                        context={"bvid": bvid, "aid": aid},
                        trace_id=trace_id,
                    )
            data = resp.get('data') or {}
            debug_logger.log_api(
                component="BiliAPI",
                api_name="get_video_info",
                request={
                    "trace_id": trace_id,
                    "url": request_url,
                    "bvid": bvid,
                    "aid": aid,
                    "wbi_signed": signed,
                    "unsigned_retry_attempted": unsigned_retry_attempted,
                    "unsigned_retry_used": unsigned_retry_used,
                },
                response_summary={
                    "api_code": resp.get("code"),
                    "title": data.get("title"),
                    "owner": data.get("owner", {}).get("name"),
                    "pages": len(data.get("pages", [])),
                    "is_season": bool(data.get("ugc_season")),
                    "season_title": (data.get("ugc_season") or {}).get("title"),
                },
                message="fetch video detail",
                status_code=response.status_code,
                trace_id=trace_id,
            )
            if resp.get('code') != 0:
                self._remember_video_info_error(target, resp, response.status_code)
                return None
            self.consume_video_info_error(target)
            return self.parser.parse_video_info_response(data)
        except BilibiliRequestStopped:
            raise
        except (requests.RequestException, ValueError, KeyError, TypeError) as e:
            debug_logger.log_exception(
                "BiliAPI",
                "get_video_info",
                e,
                context={"bvid": bvid, "aid": aid},
                trace_id=trace_id,
            )
            raise SpiderParseError(f"failed to fetch Bilibili video info: {query_key}={target}") from e

    def _play_response_is_usable(self, resp: object) -> bool:
        classify = getattr(getattr(self, "parser", None), "classify_play_url_response", None)
        if not callable(classify):
            return False
        try:
            _video_url, _audio_url, _quality_id, kind = classify(resp)
        except (TypeError, ValueError):
            return False
        return kind in {"dash", "durl"}

    def get_play_url(self, bvid, cid, trace_id=None):
        
        def _request(fnval):
            """按指定 fnval 请求播放流，并保留签名状态与原始响应。"""
            endpoint = "https://api.bilibili.com/x/player/wbi/playurl"
            params = {
                "bvid": bvid,
                "cid": cid,
                "qn": 120,
                "fnval": fnval,
                "fourk": 1,
                "platform": "pc",
            }
            response, signed, request_url = self._signed_api_get(
                endpoint,
                params,
                unsigned_endpoint="https://api.bilibili.com/x/player/playurl",
            )
            return request_url, response.status_code, response.json(), signed
        try:
            request_url, http_status, resp, signed = _request(4048)
            request_mode = 4048
            if not self._play_response_is_usable(resp):
                # 第二次请求前再看停止标志，避免降级取流在任务结束后仍打出去。
                self._raise_if_stopped()
                request_url, http_status, resp, signed = _request(80)
                request_mode = 80
        except BilibiliRequestStopped:
            return None, None, 0
        if resp.get("code") != 0 or "data" not in resp:
            raise StreamResolveError(f"B站取流失败: code={resp.get('code')}")
        data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
        dash = data.get("dash") if isinstance(data.get("dash"), dict) else {}
        video_list = dash.get("video") if isinstance(dash.get("video"), list) else []
        audio_list = dash.get("audio") if isinstance(dash.get("audio"), list) else []
        video_stream = video_list[0] if video_list and isinstance(video_list[0], dict) else {}
        audio_stream = audio_list[0] if audio_list and isinstance(audio_list[0], dict) else {}
        debug_logger.log_api(
            component="BiliAPI",
            api_name="get_play_url",
            request={
                "trace_id": trace_id,
                "url": request_url,
                "bvid": bvid,
                "cid": cid,
                "fnval": request_mode,
                "wbi_signed": signed,
            },
            response_summary={
                "api_code": resp.get("code"),
                "accept_quality": resp.get("data", {}).get("accept_quality", []),
                "video_quality_id": video_stream.get("id"),
                "video_bandwidth": video_stream.get("bandwidth"),
                "has_audio": bool(dash.get("audio")),
                "video_url": video_stream.get("baseUrl"),
                "audio_url": audio_stream.get("baseUrl"),
            },
            message="获取播放流地址",
            status_code=http_status,
            trace_id=trace_id,
        )
        return self.parser.parse_play_url_response(resp)
