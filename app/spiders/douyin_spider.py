import os
import json
import asyncio
import re
import time
from datetime import datetime
from types import SimpleNamespace
from typing import Optional

# Playwright 用于扫码登录
from playwright.sync_api import sync_playwright

# UCP 基础类
from app.spiders.base_spider import BaseSpider
from app.models import VideoItem

# ================= DouK-Downloader 核心库引入 =================
# 1. 工具与配置
from app.core.lib.douyin.tools.parameter import Parameter
# 注意：这里我们不再需要 ColorfulConsole 的实际逻辑，只需要 Parameter 引用它
from app.core.lib.douyin.tools import USERAGENT

# 2. 接口 (策略模式)
from app.core.lib.douyin.interface.search import Search
from app.core.lib.douyin.interface.detail import Detail
from app.core.lib.douyin.interface.account import Account
from app.core.lib.douyin.interface.mix import Mix
from app.core.lib.douyin.extract.extractor import Extractor
from app.core.lib.douyin.link.extractor import Extractor as LinkExtractor
from multiprocessing import Process, Queue

def _run_login_process(auth_file, user_agent, result_queue):
    """在独立进程中运行 Playwright，避免与 PyQt 线程冲突"""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False, args=['--disable-blink-features=AutomationControlled'])
            context = browser.new_context(user_agent=user_agent)
            page = context.new_page()

            page.goto("https://www.douyin.com/", timeout=60000)

            try:
                login_btn = page.locator("header div:has-text('登录')").last
                if login_btn.is_visible():
                    login_btn.click()
            except:
                pass

            # 轮询检测
            for _ in range(120):
                cookies = context.cookies()
                cookie_dict = {c['name']: c['value'] for c in cookies}
                if 'sessionid_ss' in cookie_dict:
                    # 保存文件
                    with open(auth_file, 'w', encoding='utf-8') as f:
                        json.dump(cookies, f, indent=4)
                    result_queue.put("success")
                    browser.close()
                    return
                time.sleep(1)

            result_queue.put("timeout")
            browser.close()
    except Exception as e:
        result_queue.put(str(e))
# ================= 适配器类 =================

class MockSettings:
    """模拟 DouK 的 Settings 类，用于欺骗 Parameter 初始化"""

    def __init__(self):
        pass


class MockCookie:
    """模拟 DouK 的 Cookie 类，实际 cookie 通过参数注入"""

    def __init__(self):
        self.STATE_KEY = "sessionid_ss"

    def extract(self, *args, **kwargs):
        return {}


class MockLogger:
    """将 DouK 的日志重定向到 UCP 的信号系统"""

    def __init__(self, root, console):
        self.root = root
        self.console = console

    def run(self):
        pass

    def info(self, msg, output=True, **kwargs):
        # 只有当 output=True 时才发送到 UI，避免刷屏
        if output and self.console:
            self.console.print(str(msg))

    def warning(self, msg, output=True, **kwargs):
        if output and self.console:
            self.console.warning(str(msg))

    def error(self, msg, output=True, **kwargs):
        if output and self.console:
            self.console.error(str(msg))

    def debug(self, msg, **kwargs):
        # 调试信息默认不发送到 UI，防止卡死
        pass


class SignalConsole:
    """
    [CRITICAL FIX]
    完全重写的控制台适配器。
    绝不能继承 rich.console.Console，否则在 QThread 中会导致 0xC0000409 栈溢出崩溃。
    这里只实现 Parameter 类需要的接口。
    """

    def __init__(self, signal_func):
        self.signal_func = signal_func

    def print(self, *args, **kwargs):
        # 过滤掉 rich 的样式参数，只保留内容
        msg = " ".join(str(a) for a in args)
        self.signal_func(msg)

    def info(self, *args, **kwargs):
        msg = " ".join(str(a) for a in args)
        self.signal_func(f"[INFO] {msg}")

    def warning(self, *args, **kwargs):
        msg = " ".join(str(a) for a in args)
        self.signal_func(f"⚠️ {msg}")

    def error(self, *args, **kwargs):
        msg = " ".join(str(a) for a in args)
        self.signal_func(f"❌ {msg}")

    def debug(self, *args, **kwargs):
        pass

    def input(self, prompt="", **kwargs):
        # 爬虫模式下不支持控制台输入，直接返回空
        return ""


class DouyinSpider(BaseSpider):
    AUTH_FILE = "dy_auth.json"

    def run(self):
        # [修改] 确保 multiprocessing 支持打包环境
        # try:
        #     from multiprocessing import freeze_support
        #     freeze_support()
        # except: pass

        self.log(f"🚀 启动抖音任务 | 目标: {self.keyword}")

        cookie_str = self._load_or_login()
        if not self.is_running: return
        if not cookie_str:
            self.log("❌ 无法获取 Cookie，任务终止")
            self.sig_finished.emit()
            return

        try:
            asyncio.run(self._async_main(cookie_str))
        except Exception as e:
            self.log(f"💥 运行时异常: {e}")
            import traceback
            print(traceback.format_exc())
        finally:
            self.sig_finished.emit()

    def _load_or_login(self) -> str:
        if os.path.exists(self.AUTH_FILE):
            try:
                with open(self.AUTH_FILE, 'r', encoding='utf-8') as f:
                    cookies = json.load(f)
                    cookie_dict = {c['name']: c['value'] for c in cookies}
                    if 'sessionid_ss' in cookie_dict:
                        self.log("👤 加载本地 Cookie 成功")
                        return self._cookies_to_str(cookies)
            except:
                pass

        self.log("🔒 未登录或 Cookie 失效，启动扫码...")
        return self._perform_scan_login()

    def _perform_scan_login(self, run_login_process=None) -> str:
        # [修改] 使用多进程隔离 Playwright
        self.log("🔗 正在启动独立登录进程...")

        result_queue = Queue()
        # 注意：这里不能传递 self.log 或 self，因为它们包含 PyQt 对象，无法被 pickle
        p = Process(target=run_login_process, args=(self.AUTH_FILE, USERAGENT, result_queue))
        p.start()

        # 等待进程结束，同时保持响应停止信号
        while p.is_alive():
            if not self.is_running:
                p.terminate()
                return ""
            p.join(timeout=1)

        # 获取结果
        if not result_queue.empty():
            res = result_queue.get()
            if res == "success":
                self.log("✅ 登录成功！")
                # 重新读取文件
                try:
                    with open(self.AUTH_FILE, 'r', encoding='utf-8') as f:
                        cookies = json.load(f)
                    return self._cookies_to_str(cookies)
                except:
                    return ""
            else:
                self.log(f"❌ 登录失败: {res}")
                return ""
        else:
            self.log("❌ 登录进程异常退出")
            return ""

    def _cookies_to_str(self, cookies_list: list) -> str:
        return "; ".join([f"{c['name']}={c['value']}" for c in cookies_list])
    
    async def _async_main(self, cookie_str: str):
        # 1. 初始化 DouK Parameter (上帝对象)
        console_adapter = SignalConsole(self.log)
        settings_mock = MockSettings()

        proxy_str = self.config.get("proxy", "")

        # [优化] 提供符合校验规则的默认值，消除 Warning
        params = Parameter(
            settings=settings_mock,
            cookie_object=MockCookie(),
            logger=MockLogger,
            console=console_adapter,
            cookie=cookie_str,
            cookie_tiktok="",
            root="",
            accounts_urls=[], accounts_urls_tiktok=[], mix_urls=[], mix_urls_tiktok=[],
            folder_name="Download",  # 有效文件夹名
            name_format="create_time type nickname desc",  # 有效格式
            desc_length=64,  # >= 16
            name_length=128,  # >= 32
            date_format="%Y-%m-%d %H:%M:%S",
            split="-",
            music=False, folder_mode=False,
            truncate=50,  # >= 25
            storage_format="",
            dynamic_cover=False, static_cover=False,
            proxy=proxy_str, proxy_tiktok="", twc_tiktok="",
            download=False, max_size=0,
            chunk=1024 * 1024,  # >= 128KB
            max_retry=3, max_pages=9999,  # max_pages > 0
            run_command="", owner_url={}, owner_url_tiktok={},
            live_qualities="", ffmpeg="", recorder=None,
            browser_info={}, browser_info_tiktok={}
        )

        # 更新参数 (获取 ttwid, msToken)
        await params.update_params()

        # 2. 智能路由解析
        raw_text = self.keyword.strip()
        link_extractor = LinkExtractor(params)

        resolved_links = []
        if "http" in raw_text:
            self.log("🔍 正在解析链接重定向...")

            if "user/" in raw_text:
                res = await link_extractor.run(raw_text, type_="user")
                if res:
                    for sec_uid in res:
                        await self._process_user(params, sec_uid)
                return

            elif "collection/" in raw_text or "mix/" in raw_text:
                # 合集
                is_mix, ids = link_extractor.mix(await link_extractor.requester.run(raw_text))
                if is_mix and ids:
                    mix_id = ids[0]
                    await self._process_mix(params, mix_id)
                return

            else:
                # 默认为作品 (Video/Note)
                res = await link_extractor.run(raw_text, type_="detail")
                if res:
                    await self._process_detail(params, res)
                else:
                    self.log("⚠️ 无法识别的链接格式")
                return

        else:
            # 关键词搜索
            await self._process_search(params, raw_text)

    async def _process_detail(self, params, ids: list):
        self.log(f"🎬 识别到 {len(ids)} 个作品 ID，开始获取详情...")
        api = Detail(params, detail_id=ids[0])

        tasks_data = []
        for vid in ids:
            if not self.is_running: break
            api.detail_id = vid
            data = await api.run(single_page=True, data_key="aweme_detail")
            if data:
                tasks_data.append(data)

        if not tasks_data:
            self.log("❌ 获取作品详情失败")
            return

        extractor = Extractor(params)
        processed_items = []

        for item in tasks_data:
            cleaned_list = await extractor.run([item], None, type_="detail", tiktok=False)
            if cleaned_list:
                processed_items.extend(cleaned_list)

        self._submit_tasks(processed_items)

    async def _process_user(self, params, sec_uid: str):
        self.log(f"👤 识别到用户 SecUID: {sec_uid}，开始爬取主页...")
        account_api = Account(params, sec_user_id=sec_uid)

        all_data = []
        page = 0
        # [修复] 使用 finished 属性判断循环，而不是 has_more
        while self.is_running and not account_api.finished:
            page += 1
            self.log(f"📄 正在获取第 {page} 页...")

            await account_api.run_single()

            if not account_api.response:
                break

            raw_list = account_api.response
            account_api.response = []

            batch_items = []
            for aweme in raw_list:
                item = self._extract_to_video_item(aweme)
                if item: batch_items.append(item)

            all_data.extend(batch_items)
            await asyncio.sleep(1)

        if not all_data:
            self.log("❌ 未找到公开作品")
            return

        self._handle_selection(all_data, f"用户 {sec_uid} 的作品")

    async def _process_mix(self, params, mix_id: str):
        self.log(f"📀 识别到合集 ID: {mix_id}")
        mix_api = Mix(params, mix_id=mix_id)

        all_data = []
        # [修复] 使用 finished 属性判断循环
        while self.is_running and not mix_api.finished:
            await mix_api.run_single()
            raw_list = mix_api.response
            mix_api.response = []

            for aweme in raw_list:
                item = self._extract_to_video_item(aweme)
                if item: all_data.append(item)

            await asyncio.sleep(0.5)

        self._handle_selection(all_data, f"合集 {mix_id}")

    async def _process_search(self, params, keyword: str):
        max_pages = self.config.get("search_max_pages", 1)
        self.log(f"🔍 搜索关键词: {keyword} (最大 {max_pages} 页)")

        search_api = Search(params, keyword=keyword, type=0)  # 0=综合

        all_data = []
        for i in range(max_pages):
            if not self.is_running: break
            self.log(f"   📄 搜索第 {i + 1} 页...")

            await search_api.run_single(data_key="data")

            raw_list = search_api.response
            search_api.response = []

            if not raw_list: break

            for item in raw_list:
                if 'aweme_info' in item:
                    vid = self._extract_to_video_item(item['aweme_info'])
                    if vid: all_data.append(vid)

            # [修复] 使用 finished 属性判断
            if search_api.finished: break
            await asyncio.sleep(1)

        self._handle_selection(all_data, f"搜索: {keyword}")

    def _extract_to_video_item(self, data: dict) -> Optional[VideoItem]:
        """将 Douyin 原始 JSON 转换为 VideoItem"""
        try:
            # 1. 基础信息
            aweme_id = data.get('aweme_id')
            desc = data.get('desc', aweme_id)

            # 2. 视频地址 (无水印)
            video_url = ""
            if 'video' in data and 'play_addr' in data['video']:
                url_list = data['video']['play_addr'].get('url_list', [])
                if url_list:
                    video_url = url_list[-1]  # 取最后一个通常是最高质/CDN

            # 3. 图集处理
            images = []
            if 'images' in data and data['images']:
                for img in data['images']:
                    if 'url_list' in img:
                        images.append(img['url_list'][-1])

            # 4. 构建对象
            if video_url:
                item = VideoItem(
                    url=video_url,
                    title=desc,
                    source="douyin"
                )
                item.meta = {
                    "aweme_id": aweme_id,
                    "author": data.get('author', {}).get('nickname', 'Unknown'),
                    "folder_name": data.get('author', {}).get('nickname', 'Unknown')
                }
                return item
            elif images:
                item = VideoItem(
                    url=images[0],
                    title=f"[图集] {desc}",
                    source="douyin"
                )
                item.meta = {
                    "is_gallery": True,
                    "images": images,
                    "aweme_id": aweme_id,
                    "author": data.get('author', {}).get('nickname', 'Unknown'),
                    "folder_name": data.get('author', {}).get('nickname', 'Unknown')
                }
                return item

        except Exception:
            pass
        return None

    def _handle_selection(self, items: list[VideoItem], title_hint: str):
        if not items:
            self.log("❌ 未找到有效视频")
            return

        display_items = []
        for i, vid in enumerate(items):
            display_items.append({
                'title': vid.title,
                'index': i
            })

        self.log(f"🔔 {title_hint} - 扫描完成，共 {len(items)} 个，请选择...")

        selected_indices = self.ask_user_selection(display_items)

        if not selected_indices:
            self.log("❌ 用户取消下载")
            return

        self.log(f"✅ 选中 {len(selected_indices)} 个任务")

        final_items = [items[i] for i in selected_indices]
        self._submit_tasks(final_items)

    def _submit_tasks(self, items: list[VideoItem]):
        for item in items:
            if item.meta.get("is_gallery"):
                images = item.meta['images']
                base_title = item.title
                for idx, img_url in enumerate(images):
                    sub_item = VideoItem(
                        url=img_url,
                        title=f"{base_title}_{idx + 1}",
                        source="douyin"
                    )
                    sub_item.meta = item.meta.copy()
                    self.sig_item_found.emit(sub_item)
            else:
                self.sig_item_found.emit(item)