#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
快手单个视频下载脚本
用法: python kuaishou_single.py <分享链接>
"""

import sys
import os
import re
import time
import json
import requests
from playwright.sync_api import sync_playwright

def download_video(video_url, title, output_dir):
    """下载视频"""
    output_path = os.path.join(output_dir, f"{title}.mp4")
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://www.kuaishou.com/'
    }
    
    print(f"   正在下载: {title}")
    try:
        with requests.get(video_url, headers=headers, stream=True, timeout=60) as r:
            r.raise_for_status()
            total = int(r.headers.get('content-length', 0))
            downloaded = 0
            with open(output_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total > 0 and downloaded % (1024*1024*5) == 0:
                        print(f"      {downloaded/(1024*1024):.1f}MB / {total/(1024*1024):.1f}MB")
        print(f"   完成: {output_path}")
        return output_path
    except Exception as e:
        print(f"   下载失败: {e}")
        return None

def crawl_single_video(share_url, output_dir):
    """用Playwright嗅探单个视频"""
    print(f"[1/4] 正在启动浏览器...")
    
    # 加载Cookie
    auth_file = "ks_auth.json"
    cookies = None
    if os.path.exists(auth_file):
        try:
            with open(auth_file, 'r', encoding='utf-8') as f:
                auth_data = json.load(f)
                if isinstance(auth_data, dict) and 'cookies' in auth_data:
                    cookies = auth_data['cookies']
                elif isinstance(auth_data, list):
                    cookies = auth_data
                print(f"   已加载登录Cookie")
        except Exception as e:
            print(f"   加载Cookie失败: {e}")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,  # 需要非无头模式
            args=['--disable-blink-features=AutomationControlled']
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            viewport={"width": 1280, "height": 800}
        )
        
        # 添加Cookie
        if cookies:
            context.add_cookies(cookies)
        
        page = context.new_page()
        
        # 存储捕获的视频URL
        video_urls = []
        video_title = "kuaishou_video"
        
        all_media_urls = []  # 调试：记录所有媒体URL
        
        def handle_response(response):
            nonlocal video_title
            url = response.url
            ctype = response.headers.get("content-type", "")
            resource_type = response.request.resource_type
            
            # 记录所有媒体类型的请求
            if resource_type in ["media", "video", "xhr"] or "video" in url.lower() or "mp4" in url.lower():
                all_media_urls.append(f"[{resource_type}] {ctype} -> {url[:100]}")
            
            # 捕获视频流 - 更宽松的匹配
            if (resource_type == "media" or 
                "video/mp4" in ctype or 
                "mpegurl" in ctype.lower() or
                ".m3u8" in url or
                ".mp4" in url):
                if response.status == 200:
                    # 检查文件大小，过滤掉小文件
                    try:
                        content_length = int(response.headers.get("content-length", 0))
                        if content_length > 10000:  # 大于10KB
                            video_urls.append(url)
                            print(f"[3/4] 捕获到视频流: {url[:80]}...")
                    except:
                        video_urls.append(url)
                        print(f"[3/4] 捕获到视频流: {url[:80]}...")
            
        page.on("response", handle_response)
        
        print(f"[2/4] 正在访问页面...")
        try:
            # 先访问短链接获取真实URL
            page.goto(share_url, timeout=30000)
            time.sleep(3)
            
            # 获取标题
            try:
                title_el = page.locator(".video-info-title, .caption, h1, .photo-title").first
                if title_el.is_visible(timeout=2000):
                    video_title = title_el.inner_text()[:50]
                    # 清理标题
                    video_title = re.sub(r'[\\/:*?"<>|]', '_', video_title).strip()
            except:
                pass
            
            # 等待视频卡片或播放器出现
            print(f"[3/4] 等待视频加载...")
            
            # 尝试点击视频卡片进入详情页（这是关键！）
            try:
                # 等待页面加载
                page.wait_for_load_state("networkidle", timeout=10000)
                time.sleep(2)
                
                # 查找视频卡片
                card = page.locator(".photo-card, .video-card, .video-item, [class*='video']").first
                if card.is_visible(timeout=3000):
                    print(f"   点击视频卡片...")
                    card.click()
                    time.sleep(5)  # 等待视频开始播放
                    
                    # 尝试按空格键播放
                    page.keyboard.press("Space")
                    time.sleep(2)
            except Exception as e:
                print(f"   点击卡片失败: {e}")
            
            # 等待视频播放，让网络请求触发
            print(f"[3/4] 等待视频流...")
            time.sleep(5)
            
            # 尝试直接从页面提取视频URL
            if not video_urls:
                print("   尝试从页面提取视频...")
                try:
                    # 查找video标签
                    video_src = page.evaluate("""() => {
                        const video = document.querySelector('video');
                        if (video) {
                            return video.src || video.currentSrc;
                        }
                        return null;
                    }""")
                    if video_src:
                        video_urls.append(video_src)
                        print(f"   从video标签提取: {video_src[:80]}...")
                except Exception as e:
                    print(f"   提取失败: {e}")
            
            # 如果还没捕获到，尝试滚动页面触发
            if not video_urls:
                print("   尝试滚动触发...")
                for i in range(3):
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    time.sleep(1)
                    page.evaluate("window.scrollTo(0, 0)")
                    time.sleep(1)
                    if video_urls:
                        break
            
        except Exception as e:
            print(f"访问页面失败: {e}")
            browser.close()
            return None
        
        browser.close()
    
    # 调试输出
    if all_media_urls:
        print(f"\n[DEBUG] 捕获到 {len(all_media_urls)} 个媒体请求:")
        for u in all_media_urls[:10]:
            print(f"  {u}")
    
    if not video_urls:
        print("[4/4] 未能捕获到视频URL")
        return None
    
    # 选择最长的URL（通常是高清版本）
    best_url = max(video_urls, key=len)
    print(f"[4/4] 开始下载: {video_title}")
    
    return download_video(best_url, video_title, output_dir)

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("用法: python kuaishou_single.py <分享链接>")
        sys.exit(1)
    
    share_url = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else "./downloads"
    
    os.makedirs(output_dir, exist_ok=True)
    
    result = crawl_single_video(share_url, output_dir)
    if result:
        print(f"\n下载完成: {result}")
    else:
        print("\n下载失败")
