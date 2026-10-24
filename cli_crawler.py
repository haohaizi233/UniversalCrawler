#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
UniversalCrawler CLI - 命令行独立爬虫脚本
用法: python cli_crawler.py <平台> <关键词/URL> [--output 输出目录]
平台: bilibili, kuaishou, missav
"""

import sys
import os
import re
import time
import json
import argparse
import urllib.parse
import requests
from pathlib import Path

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ===================== B站爬虫 =====================
class BilibiliCrawler:
    def __init__(self):
        self.sess = requests.Session()
        self.sess.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://www.bilibili.com'
        })
        self.load_cookies()
    
    def load_cookies(self):
        cookie_file = "bili_auth.json"
        if os.path.exists(cookie_file):
            try:
                with open(cookie_file, 'r', encoding='utf-8') as f:
                    cookies = json.load(f)
                    if isinstance(cookies, list):
                        for c in cookies:
                            self.sess.cookies.set(c['name'], c['value'], domain=c.get('domain', '.bilibili.com'))
                print("✅ 已加载B站Cookie")
            except:
                pass
    
    def get_video_info(self, bvid):
        """获取视频信息"""
        url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
        try:
            resp = self.sess.get(url, timeout=10).json()
            if resp['code'] != 0:
                return None
            data = resp['data']
            return {
                'bvid': data['bvid'],
                'title': data['title'],
                'owner': data['owner']['name'],
                'pages': data['pages']
            }
        except Exception as e:
            print(f"❌ 获取视频信息失败: {e}")
            return None
    
    def get_play_url(self, bvid, cid):
        """获取播放链接"""
        url = f"https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}&qn=120&fnval=4048&fourk=1"
        try:
            resp = self.sess.get(url, timeout=10).json()
            if resp['code'] == 0 and 'data' in resp and 'dash' in resp['data']:
                dash = resp['data']['dash']
                video_url = dash['video'][0]['baseUrl']
                audio_url = dash['audio'][0]['baseUrl'] if dash.get('audio') else None
                return video_url, audio_url
        except:
            pass
        return None, None
    
    def crawl(self, keyword, output_dir):
        print(f"🚀 B站爬虫启动 | 目标: {keyword}")
        
        # 解析BV号
        bvid = None
        if re.match(r'(?i)^BV\w+$', keyword):
            bvid = keyword
        elif "http" in keyword:
            match = re.search(r'(BV\w+)', keyword)
            if match:
                bvid = match.group(1)
        
        if not bvid:
            print("❌ 请提供有效的BV号或B站视频链接")
            return []
        
        print(f"📹 正在获取视频信息: {bvid}")
        info = self.get_video_info(bvid)
        if not info:
            return []
        
        print(f"📝 标题: {info['title']}")
        print(f"👤 UP主: {info['owner']}")
        print(f"📄 共 {len(info['pages'])} 个分P")
        
        results = []
        for i, page in enumerate(info['pages']):
            print(f"\n🎬 处理第 {i+1}/{len(info['pages'])} P: {page['part']}")
            video_url, audio_url = self.get_play_url(info['bvid'], page['cid'])
            if video_url:
                title = self.clean_name(page['part'])
                results.append({
                    'title': title,
                    'video_url': video_url,
                    'audio_url': audio_url,
                    'referer': f"https://www.bilibili.com/video/{info['bvid']}",
                    'source': 'bilibili'
                })
                print(f"   ✅ 获取成功")
            else:
                print(f"   ❌ 获取失败")
        
        return results
    
    def clean_name(self, name):
        return re.sub(r'[\\/:*?"<>|]', '_', str(name)).strip()


# ===================== 快手爬虫 =====================
class KuaishouCrawler:
    def __init__(self):
        pass
    
    def crawl(self, keyword, output_dir):
        print(f"🚀 快手爬虫启动 | 目标: {keyword}")
        print("⚠️ 快手需要浏览器环境，请使用主程序或提供视频直链")
        
        # 如果是直链，尝试下载
        if "http" in keyword and (".mp4" in keyword or "kuaishou" in keyword):
            print("📥 检测到直链，尝试下载...")
            return [{
                'title': 'kuaishou_video',
                'video_url': keyword,
                'audio_url': None,
                'referer': 'https://www.kuaishou.com/',
                'source': 'kuaishou'
            }]
        
        return []


# ===================== MissAV爬虫 =====================
class MissAVCrawler:
    def __init__(self):
        self.sess = requests.Session()
        self.sess.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        })
    
    def crawl(self, keyword, output_dir):
        print(f"🚀 MissAV爬虫启动 | 目标: {keyword}")
        
        # 构造URL
        if "http" in keyword:
            target_url = keyword
        else:
            target_url = f"https://missav.ai/cn/search/{urllib.parse.quote(keyword)}"
        
        print(f"🔗 访问: {target_url}")
        print("⚠️ MissAV需要浏览器嗅探m3u8，建议使用主程序")
        
        # 如果是m3u8直链
        if ".m3u8" in keyword:
            return [{
                'title': 'missav_video',
                'video_url': keyword,
                'audio_url': None,
                'referer': 'https://missav.ai/',
                'source': 'missav'
            }]
        
        return []


# ===================== 下载器 =====================
def download_bilibili(item, output_dir):
    """下载B站视频（需要ffmpeg合并）"""
    output_path = os.path.join(output_dir, f"{item['title']}.mp4")
    
    # 检查ffmpeg
    ffmpeg_path = "ffmpeg.exe"
    if not os.path.exists(ffmpeg_path):
        print("❌ 未找到ffmpeg.exe，无法合并音视频")
        return None
    
    temp_v = output_path + ".video.m4s"
    temp_a = output_path + ".audio.m4s"
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': item['referer']
    }
    
    # 下载视频
    print(f"   📥 下载视频流...")
    try:
        with requests.get(item['video_url'], headers=headers, stream=True, timeout=60) as r:
            r.raise_for_status()
            total = int(r.headers.get('content-length', 0))
            downloaded = 0
            with open(temp_v, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total > 0 and downloaded % (1024*1024) == 0:
                        print(f"      {downloaded/(1024*1024):.1f}MB / {total/(1024*1024):.1f}MB")
    except Exception as e:
        print(f"   ❌ 下载视频流失败: {e}")
        return None
    
    # 下载音频
    if item.get('audio_url'):
        print(f"   📥 下载音频流...")
        try:
            with requests.get(item['audio_url'], headers=headers, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(temp_a, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
        except Exception as e:
            print(f"   ⚠️ 下载音频流失败: {e}")
    
    # 合并
    print(f"   🔧 合并音视频...")
    cmd = [ffmpeg_path, "-y", "-i", temp_v]
    if os.path.exists(temp_a):
        cmd.extend(["-i", temp_a])
    cmd.extend(["-c", "copy", output_path])
    
    import subprocess
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    subprocess.run(cmd, startupinfo=startupinfo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    # 清理
    for f in [temp_v, temp_a]:
        if os.path.exists(f):
            os.remove(f)
    
    print(f"   ✅ 完成: {output_path}")
    return output_path


def download_simple(item, output_dir):
    """简单下载（直接下载文件）"""
    output_path = os.path.join(output_dir, f"{item['title']}.mp4")
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': item.get('referer', '')
    }
    
    print(f"   📥 下载中...")
    try:
        with requests.get(item['video_url'], headers=headers, stream=True, timeout=60) as r:
            r.raise_for_status()
            total = int(r.headers.get('content-length', 0))
            downloaded = 0
            with open(output_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total > 0 and downloaded % (1024*1024) == 0:
                        print(f"      {downloaded/(1024*1024):.1f}MB / {total/(1024*1024):.1f}MB")
        print(f"   ✅ 完成: {output_path}")
        return output_path
    except Exception as e:
        print(f"   ❌ 下载失败: {e}")
        return None


# ===================== 主函数 =====================
def main():
    parser = argparse.ArgumentParser(description='UniversalCrawler CLI')
    parser.add_argument('platform', choices=['bilibili', 'kuaishou', 'missav'], help='平台名称')
    parser.add_argument('keyword', help='关键词或URL')
    parser.add_argument('--output', '-o', default='./downloads', help='输出目录')
    parser.add_argument('--download', '-d', action='store_true', help='下载视频')
    
    args = parser.parse_args()
    
    # 创建输出目录
    output_dir = os.path.abspath(args.output)
    os.makedirs(output_dir, exist_ok=True)
    
    # 选择爬虫
    crawlers = {
        'bilibili': BilibiliCrawler(),
        'kuaishou': KuaishouCrawler(),
        'missav': MissAVCrawler()
    }
    
    crawler = crawlers[args.platform]
    results = crawler.crawl(args.keyword, output_dir)
    
    if not results:
        print("\n❌ 未获取到任何视频")
        return
    
    print(f"\n✅ 共获取 {len(results)} 个视频")
    
    if args.download:
        print("\n" + "="*50)
        print("开始下载...")
        print("="*50)
        
        downloaded_files = []
        for i, item in enumerate(results):
            print(f"\n[{i+1}/{len(results)}] {item['title']}")
            
            if item['source'] == 'bilibili':
                path = download_bilibili(item, output_dir)
            else:
                path = download_simple(item, output_dir)
            
            if path:
                downloaded_files.append(path)
        
        print(f"\n🎉 下载完成! 共 {len(downloaded_files)} 个文件")
        for f in downloaded_files:
            print(f"   📁 {f}")
    else:
        print("\n视频信息:")
        for i, item in enumerate(results):
            print(f"  [{i+1}] {item['title']}")
            print(f"      URL: {item['video_url'][:60]}...")


if __name__ == '__main__':
    main()
