import asyncio
import re
import os
import tempfile
import urllib.parse

import aiohttp

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.core import AstrBotConfig


@register("media_bridge", "zaiens", "解析 YouTube/Bluesky/Twitter/GIF/图片媒体直链，支持代理", "1.0.0")
class MediaBridgePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self.proxy_cfg = (config or {}).get("proxy", {}) if config else {}
        self.proxy_url = self.proxy_cfg.get("url", "http://127.0.0.1:7890")
        self.proxy_enabled = self.proxy_cfg.get("enabled", True)
        self.max_video_mb = (config or {}).get("max_video_mb", 50) if config else 50
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

    def _proxy_for(self, platform: str):
        """按平台决定是否走代理"""
        if not self.proxy_enabled:
            return None
        flag = self.proxy_cfg.get(platform, True) if self.proxy_cfg else True
        return self.proxy_url if flag else None

    async def _fetch(self, url, proxy=None, **kw):
        """带代理的 GET 请求"""
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(url, proxy=proxy, headers=self.headers, **kw) as resp:
                return resp

    @filter.regex(r"(https?://(?:www\.)?(?:youtube\.com|youtu\.be)/[^\s]+)")
    async def on_youtube(self, event: AstrMessageEvent, url: str = None):
        pass

    @filter.regex(r"(https?://(?:bsky\.app|bsky\.social)/profile/[^\s]+)")
    async def on_bluesky(self, event: AstrMessageEvent, url: str = None):
        pass

    @filter.regex(r"(https?://(?:twitter\.com|x\.com)/[^\s]+)")
    async def on_twitter(self, event: AstrMessageEvent, url: str = None):
        pass

    @filter.regex(r"(https?://[^\s]+\.(?:gif|jpe?g|png|webp)(?:\?[^\s]*)?)")
    async def on_image_gif(self, event: AstrMessageEvent, url: str = None):
        pass

    async def reply(self, event: AstrMessageEvent, text: str):
        yield event.plain_result(text)


    # ========== YouTube ==========
    @filter.regex(r"(https?://(?:www\.)?(?:youtube\.com|youtu\.be)/[^\s]+)")
    async def on_youtube(self, event: AstrMessageEvent, url: str = None):
        if not url:
            yield event.plain_result("未识别到链接")
            return
        proxy = self._proxy_for("youtube")
        try:
            # 用 oEmbed 拿标题/作者/缩略图
            oembed = f"https://www.youtube.com/oembed?url={urllib.parse.quote(url)}&format=json"
            async with aiohttp.ClientSession() as s:
                async with s.get(oembed, proxy=proxy, headers=self.headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        title = data.get("title", "")
                        author = data.get("author_name", "")
                        thumb = data.get("thumbnail_url", "")
                        vid_id = self._extract_yt_id(url)
                        msg = f"🎬 YouTube: {title}\n👤 {author}"
                        if vid_id:
                            msg += f"\n🔗 直链: https://youtu.be/{vid_id}"
                        yield event.plain_result(msg)
                    else:
                        yield event.plain_result(f"⚠️ YouTube 解析失败(HTTP {resp.status})")
        except Exception as e:
            yield event.plain_result(f"❌ YouTube 解析错误: {str(e)[:80]}")

    def _extract_yt_id(self, url):
        m = re.search(r"(?:v=|youtu\.be/|shorts/)([\w-]{11})", url)
        return m.group(1) if m else None

    # ========== Bluesky ==========
    @filter.regex(r"(https?://(?:bsky\.app|bsky\.social)/profile/[^\s]+)")
    async def on_bluesky(self, event: AstrMessageEvent, url: str = None):
        if not url:
            yield event.plain_result("未识别到链接")
            return
        proxy = self._proxy_for("bluesky")
        try:
            # Bluesky: 提取 post/feed 的 rkey
            m = re.search(r"/profile/([^/]+)/post/([\w]+)", url)
            if not m:
                yield event.plain_result("⚠️ 无法解析 Bluesky 帖子链接")
                return
            handle, rkey = m.group(1), m.group(2)
            # 通过 public API 解析
            resolve_url = f"https://public.api.bsky.app/xrpc/com.atproto.identity.resolveHandle?handle={handle}"
            async with aiohttp.ClientSession() as s:
                async with s.get(resolve_url, proxy=proxy, headers=self.headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        yield event.plain_result("⚠️ Bluesky 用户解析失败")
                        return
                    did = (await resp.json()).get("did", "")
                if not did:
                    yield event.plain_result("⚠️ 未找到用户 DID")
                    return
                post_url = f"https://public.api.bsky.app/xrpc/app.bsky.feed.getPost?repo={did}&rkey={rkey}"
                async with s.get(post_url, proxy=proxy, headers=self.headers, timeout=aiohttp.ClientTimeout(total=15)) as resp2:
                    if resp2.status != 200:
                        yield event.plain_result("⚠️ Bluesky 帖子获取失败")
                        return
                    post = await resp2.json()
                    record = post.get("post", {}).get("record", {})
                    text = (record.get("text") or "")[:100]
                    embeds = record.get("embed", {})
                    media = []
                    # 图片
                    if embeds.get("$type") == "app.bsky.embed.images":
                        for img in embeds.get("images", []):
                            media.append(img.get("fullsize") or img.get("thumb"))
                    # 外部链接(含视频/图片卡片)
                    elif embeds.get("$type") == "app.bsky.embed.external":
                        ext = embeds.get("external", {})
                        media.append(ext.get("uri", ""))
                    msg = f"📘 Bluesky: {text}"
                    for m_ in media:
                        msg += f"\n🔗 {m_}"
                    yield event.plain_result(msg)
        except Exception as e:
            yield event.plain_result(f"❌ Bluesky 解析错误: {str(e)[:80]}")

    # ========== Twitter / X (多级 fallback + 高清) ==========
    @filter.regex(r"(https?://(?:twitter\.com|x\.com)/[^\s]+)")
    async def on_twitter(self, event: AstrMessageEvent, url: str = None):
        if not url:
            yield event.plain_result("未识别到链接")
            return
        proxy = self._proxy_for("twitter")
        # 提取 用户/推文ID
        m = re.search(r"(?:twitter\.com|x\.com)/([^/]+)/status/(\d+)", url)
        if not m:
            yield event.plain_result("⚠️ 无法解析 Twitter 链接")
            return
        user, sid = m.group(1), m.group(2)
        hd = (self.config or {}).get("quality", {}).get("twitter", "hd")
        try:
            async with aiohttp.ClientSession() as s:
                # 1) fxtwitter (信息最全)
                fx = f"https://api.fxtwitter.com/{user}/status/{sid}"
                async with s.get(fx, proxy=proxy, headers=self.headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status == 200:
                        d = await r.json()
                        t = d.get("tweet", {})
                        author = t.get("author", {}).get("screen_name", user)
                        text = (t.get("text") or "")[:100]
                        media = t.get("media", {}).get("all", [])
                        msg = f"🐦 @{author}: {text}"
                        for mm in media:
                            msg += f"\n🔗 {mm.get('url', '')}"
                        yield event.plain_result(msg)
                        return
                # 2) syndication API
                sy = f"https://cdn.syndication.twimg.com/tweet-result?id={sid}&lang=en"
                async with s.get(sy, proxy=proxy, headers=self.headers, timeout=aiohttp.ClientTimeout(total=12)) as r2:
                    if r2.status == 200:
                        d2 = await r2.json()
                        if d2.get("user"):
                            author = d2["user"].get("screen_name", user)
                            text = (d2.get("text") or "")[:100]
                            media = [md.get("media_url_https", "") for md in d2.get("mediaDetails", [])]
                            msg = f"🐦 @{author}: {text}"
                            for mu in media:
                                msg += f"\n🔗 {mu}"
                            yield event.plain_result(msg)
                            return
                # 3) 页面 OG 抓取
                pg = f"https://x.com/{user}/status/{sid}"
                async with s.get(pg, proxy=proxy, headers=self.headers, timeout=aiohttp.ClientTimeout(total=12)) as r3:
                    if r3.status == 200:
                        html = await r3.text()
                        title = re.search(r"<title>([^<]+)</title>", html)
                        og_img = re.findall(r'og:image(?:\?[^>]*)? content="([^"]+)"', html)
                        yield event.plain_result(f"🐦 {title.group(1) if title else url}\n🖼️ {og_img[0] if og_img else ''}")
                        return
                yield event.plain_result("⚠️ Twitter 解析失败(所有服务不可用)")
        except Exception as e:
            yield event.plain_result(f"❌ Twitter 解析错误: {str(e)[:80]}")

    # ========== GIF / 图片 ==========
    @filter.regex(r"(https?://[^\s]+\.(?:gif|jpe?g|png|webp)(?:\?[^\s]*)?)")
    async def on_image_gif(self, event: AstrMessageEvent, url: str = None):
        if not url:
            yield event.plain_result("未识别到链接")
            return
        # 直接返回原图链接(由平台展示)
        yield event.plain_result(f"🖼️ {url}")
