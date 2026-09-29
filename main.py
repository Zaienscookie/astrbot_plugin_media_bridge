import asyncio
import os
import re
import tempfile
import urllib.parse
import uuid

import aiohttp

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import Image, Video, Plain

try:
    from astrbot.core import AstrBotConfig
except Exception:
    AstrBotConfig = dict


@register("media_bridge", "zaiens", "解析并下载 YouTube/Bluesky/Twitter/GIF/图片媒体，支持代理与画质", "1.3.3")
class MediaBridgePlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self.proxy_cfg = (self.config or {}).get("proxy", {}) or {}
        self.quality_cfg = (self.config or {}).get("quality", {}) or {}
        self.proxy_url = self.proxy_cfg.get("url", "http://127.0.0.1:7890")
        self.proxy_enabled = self.proxy_cfg.get("enabled", True)
        self.max_video_mb = float((self.config or {}).get("max_video_mb", 50))
        self.cache_seconds = int((self.config or {}).get("cache_seconds", 300))
        self._cache = {}  # url -> (local_path, timestamp)
        self.tmp_dir = os.path.join(tempfile.gettempdir(), "media_bridge")
        os.makedirs(self.tmp_dir, exist_ok=True)
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

    # ---------- 工具 ----------
    def _clean_cache(self):
        """清理过期缓存"""
        import time as _t
        now = _t.time()
        expired = [u for u, (p, ts) in self._cache.items() if now - ts >= self.cache_seconds]
        for u in expired:
            p, _ = self._cache.pop(u)
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass

    def _proxy_for(self, platform: str):
        if not self.proxy_enabled:
            return None
        flag = self.proxy_cfg.get(platform, True)
        return self.proxy_url if flag else None

    def _extract_url(self, event, pattern: str):
        text = getattr(event, "message_str", "") or ""
        m = re.search(pattern, text)
        return m.group(1) if m else None

    def _check_whitelist(self, event) -> bool:
        """白名单检查：开启后，仅白名单群/用户可用"""
        wl = (self.config or {}).get("whitelist", {}) or {}
        if not wl.get("enabled", False):
            return True
        gid = ""
        uid = ""
        try:
            gid = str(event.get_group_id() or "")
        except Exception:
            pass
        try:
            uid = str(event.get_sender_id() or "")
        except Exception:
            pass
        groups = wl.get("groups", "") or ""
        users = wl.get("users", "") or ""
        # 支持逗号/空格/换行分隔
        gset = set(re.split(r"[,\s]+", groups.strip())) if groups.strip() else set()
        uset = set(re.split(r"[,\s]+", users.strip())) if users.strip() else set()
        gset.discard(""); uset.discard("")
        # 白名单为空 -> 视为全部允许(避免误锁)
        if not gset and not uset:
            return True
        if gid and gid in gset:
            return True
        if uid and uid in uset:
            return True
        return False

    def _quality(self, platform: str):
        return self.quality_cfg.get(platform, "max")

    async def _download(self, url: str, proxy: str = None, ext_hint: str = "", type_hint: str = "") -> str | None:
        """下载媒体到本地，返回路径；超限/失败返回 None。命中缓存则复用。"""
        import time as _t
        # 缓存命中检查
        if url in self._cache:
            p, ts = self._cache[url]
            if _t.time() - ts < self.cache_seconds and os.path.exists(p):
                logger.info(f"[media_bridge] 缓存命中: {url[:60]}")
                return p
        try:
            timeout = aiohttp.ClientTimeout(total=180)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(url, proxy=proxy, headers=self.headers) as resp:
                    if resp.status != 200:
                        logger.warning(f"[media_bridge] 下载失败 HTTP {resp.status}: {url[:80]}")
                        return None
                    # 大小检查
                    clen = resp.headers.get("Content-Length")
                    if clen and int(clen) > self.max_video_mb * 1024 * 1024:
                        logger.warning(f"[media_bridge] 文件超限 {int(clen)//1048576}MB > {self.max_video_mb}MB")
                        return None
                    # 扩展名（优先 type_hint: photo/image/video/gif）
                    ext = ext_hint
                    if not ext and type_hint:
                        th = type_hint.lower()
                        if th in ("video", "gif"):  # Twitter的gif实为mp4
                            ext = ".mp4"
                        elif th in ("photo", "image"):
                            ext = ".jpg"
                    if not ext:
                        ct = resp.headers.get("Content-Type", "")
                        if "mp4" in ct or "video" in ct:
                            ext = ".mp4"
                        elif "gif" in ct:
                            ext = ".gif"
                        elif "png" in ct:
                            ext = ".png"
                        elif "webp" in ct:
                            ext = ".webp"
                        elif "jpeg" in ct or "jpg" in ct:
                            ext = ".jpg"
                        else:
                            ext = os.path.splitext(url.split("?")[0])[1] or ".bin"
                    fname = os.path.join(self.tmp_dir, f"{uuid.uuid4().hex}{ext}")
                    total = 0
                    limit = self.max_video_mb * 1024 * 1024
                    with open(fname, "wb") as f:
                        async for chunk in resp.content.iter_chunked(65536):
                            total += len(chunk)
                            if total > limit:
                                f.close()
                                os.remove(fname)
                                logger.warning("[media_bridge] 下载超限中断")
                                return None
                            f.write(chunk)
                    self._cache[url] = (fname, _t.time())
                    self._clean_cache()
                    return fname
        except Exception as e:
            logger.warning(f"[media_bridge] 下载异常: {str(e)[:100]}")
            return None

    def _is_video(self, path: str):
        return os.path.splitext(path)[1].lower() in (".mp4", ".mov", ".m4v", ".webm", ".mkv")

    async def _transcode_video(self, path: str):
        """用 ffmpeg 转码为标准 mp4(H.264+AAC+faststart)，无音轨则加静音音轨，解决 QQ 发视频超时"""
        try:
            # 检测音轨
            probe = await asyncio.create_subprocess_exec(
                "ffprobe", "-v", "error", "-select_streams", "a",
                "-show_entries", "stream=index", "-of", "csv=p=0", path,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
            )
            aout, _ = await probe.communicate()
            has_audio = bool(aout.strip())
            out = os.path.join(self.tmp_dir, f"tc_{uuid.uuid4().hex}.mp4")
            cmd = ["ffmpeg", "-y", "-i", path]
            if not has_audio:
                cmd += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-map", "0:v:0", "-map", "1:a:0"]
            cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "23",
                    "-c:a", "aac", "-b:a", "128k"]
            if not has_audio:
                cmd += ["-shortest"]
            cmd += ["-movflags", "+faststart", out]
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
            _, err = await asyncio.wait_for(proc.communicate(), timeout=180)
            if proc.returncode == 0 and os.path.exists(out):
                return out
            logger.warning(f"[media_bridge] 转码失败: {err.decode(errors='ignore')[:120]}")
            return None
        except Exception as e:
            logger.warning(f"[media_bridge] 转码异常: {str(e)[:100]}")
            return None

    async def _send_media(self, event, urls: list, proxy: str, text: str = ""):
        """下载并发送媒体。多图/多视频/混合 全部下载后合并为一条消息链发送。"""
        if text:
            await event.send(event.plain_result(text))
        comps = []
        tmp_transcoded = []
        for item in urls:
            if isinstance(item, dict):
                u = item.get("url", "")
                th = item.get("type", "")
            else:
                u = item
                th = ""
            if not u:
                continue
            p = await self._download(u, proxy=proxy, type_hint=th)
            if not p:
                continue
            try:
                if self._is_video(p):
                    tpath = await self._transcode_video(p)
                    sp = tpath or p
                    comps.append(Video.fromFileSystem(sp))
                    if tpath:
                        tmp_transcoded.append(tpath)
                else:
                    comps.append(Image.fromFileSystem(p))
            except Exception as e:
                logger.warning(f"[media_bridge] 构建媒体组件失败: {str(e)[:80]}")
        if comps:
            await event.send(event.chain_result(comps))
        elif text:
            await event.send(event.plain_result("⚠️ 媒体下载失败(可能超限或代理异常)"))
        # 清理转码临时文件（原视频保留在缓存）
        for t in tmp_transcoded:
            try:
                if t and os.path.exists(t):
                    os.remove(t)
            except Exception:
                pass

    # ---------- YouTube ----------
    @filter.regex(r"(https?://(?:www\.)?(?:youtube\.com|youtu\.be)/[^\s]+)")
    async def on_youtube(self, event: AstrMessageEvent):
        if not self._check_whitelist(event):
            return
        url = self._extract_url(event, r"(https?://(?:www\.)?(?:youtube\.com|youtu\.be)/[^\s]+)")
        if not url:
            return
        proxy = self._proxy_for("youtube")
        try:
            oembed = f"https://www.youtube.com/oembed?url={urllib.parse.quote(url)}&format=json"
            async with aiohttp.ClientSession() as s:
                async with s.get(oembed, proxy=proxy, headers=self.headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status != 200:
                        await event.send(event.plain_result(f"⚠️ YouTube 解析失败({r.status})"))
                        return
                    d = await r.json()
            title = d.get("title", "")
            author = d.get("author_name", "")
            thumb = d.get("thumbnail_url", "")
            # 尝试用 yt-dlp 下载(最高画质)
            path = await self._ytdlp_download(url, proxy)
            if path:
                await event.send(event.plain_result(f"🎬 {title} — {author}"))
                await event.send(event.chain_result([Video.fromFileSystem(path)]))
                try: os.remove(path)
                except Exception: pass
            else:
                # 退化: 发缩略图
                if thumb:
                    p = await self._download(thumb, proxy=proxy)
                    if p:
                        await event.send(event.chain_result([Image.fromFileSystem(p)]))
                        try: os.remove(p)
                        except Exception: pass
                await event.send(event.plain_result(f"🎬 {title} — {author}"))
        except Exception as e:
            await event.send(event.plain_result(f"❌ YouTube 处理错误: {str(e)[:80]}"))

    async def _ytdlp_download(self, url: str, proxy: str):
        """用 yt-dlp 下载最高画质视频"""
        try:
            out_tmpl = os.path.join(self.tmp_dir, f"yt_{uuid.uuid4().hex}.%(ext)s")
            cmd = ["yt-dlp", "-f", "bv*+ba/b", "--merge-output-format", "mp4",
                   "-o", out_tmpl, "--no-playlist", "--max-filesize", f"{int(self.max_video_mb)}M"]
            if proxy:
                cmd += ["--proxy", proxy]
            cmd.append(url)
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            _, err = await asyncio.wait_for(proc.communicate(), timeout=300)
            if proc.returncode != 0:
                logger.warning(f"[media_bridge] yt-dlp 失败: {err.decode(errors='ignore')[:150]}")
                return None
            for f in os.listdir(self.tmp_dir):
                if f.startswith("yt_") and os.path.splitext(f)[1] in (".mp4", ".mkv", ".webm"):
                    return os.path.join(self.tmp_dir, f)
            return None
        except Exception as e:
            logger.warning(f"[media_bridge] yt-dlp 异常: {str(e)[:100]}")
            return None

    # ---------- Bluesky ----------
    def _bsky_cdn(self, did: str, blob: dict, kind: str = "image") -> str:
        """从 Bluesky blob 构造 CDN 直链"""
        try:
            cid = blob.get("ref", {}).get("$link", "")
            mime = blob.get("mimeType", "image/jpeg")
            ext = mime.split("/")[-1]
            if ext == "jpeg":
                ext = "jpeg"
            if kind == "video":
                return f"https://cdn.bsky.app/img/feed_fullsize/plain/{did}/{cid}@mp4"
            return f"https://cdn.bsky.app/img/feed_fullsize/plain/{did}/{cid}@{ext}"
        except Exception:
            return ""

    @filter.regex(r"(https?://(?:bsky\.app|bsky\.social)/profile/[^\s]+)")
    async def on_bluesky(self, event: AstrMessageEvent):
        if not self._check_whitelist(event):
            return
        url = self._extract_url(event, r"(https?://(?:bsky\.app|bsky\.social)/profile/[^\s]+)")
        if not url:
            return
        proxy = self._proxy_for("bluesky")
        try:
            m = re.search(r"/profile/([^/]+)/post/([\w]+)", url)
            if not m:
                await event.send(event.plain_result("⚠️ 无法解析 Bluesky 帖子链接"))
                return
            handle, rkey = m.group(1), m.group(2)
            async with aiohttp.ClientSession() as s:
                ru = f"https://public.api.bsky.app/xrpc/com.atproto.identity.resolveHandle?handle={handle}"
                async with s.get(ru, proxy=proxy, headers=self.headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status != 200:
                        await event.send(event.plain_result("⚠️ Bluesky 用户解析失败"))
                        return
                    did = (await r.json()).get("did", "")
                if not did:
                    await event.send(event.plain_result("⚠️ 未找到用户 DID"))
                    return
                uri = f"at://{did}/app.bsky.feed.post/{rkey}"
                pu = f"https://public.api.bsky.app/xrpc/app.bsky.feed.getPosts?uris={urllib.parse.quote(uri)}"
                async with s.get(pu, proxy=proxy, headers=self.headers, timeout=aiohttp.ClientTimeout(total=15)) as r2:
                    if r2.status != 200:
                        await event.send(event.plain_result(f"⚠️ Bluesky 帖子获取失败({r2.status})"))
                        return
                    posts = (await r2.json()).get("posts", [])
            if not posts:
                await event.send(event.plain_result("⚠️ Bluesky 帖子不存在"))
                return
            rec = posts[0].get("record", {})
            text = (rec.get("text") or "")[:120]
            embed = rec.get("embed", {}) or {}
            et = embed.get("$type", "")
            media = []
            if et == "app.bsky.embed.images":
                for img in embed.get("images", []):
                    u = self._bsky_cdn(did, img.get("image", {}), "image")
                    if u:
                        media.append({"url": u, "type": "image"})
            elif et == "app.bsky.embed.video":
                u = self._bsky_cdn(did, embed.get("video", {}), "video")
                if u:
                    media.append({"url": u, "type": "video"})
            elif et == "app.bsky.embed.external":
                ext = (embed.get("external") or {})
                eu = ext.get("uri", "")
                if eu:
                    media.append({"url": eu, "type": ""})
            # recordWithMedia (带媒体)
            elif et == "app.bsky.embed.recordWithMedia":
                m2 = embed.get("media", {}) or {}
                if m2.get("$type") == "app.bsky.embed.images":
                    for img in m2.get("images", []):
                        u = self._bsky_cdn(did, img.get("image", {}), "image")
                        if u:
                            media.append({"url": u, "type": "image"})
            if media:
                await self._send_media(event, media, proxy, text=f"📘 {text}")
            else:
                await event.send(event.plain_result(f"📘 {text}" if text else "📘 (无媒体内容)"))
        except Exception as e:
            await event.send(event.plain_result(f"❌ Bluesky 处理错误: {str(e)[:80]}"))

    # ---------- Twitter / X ----------
    @filter.regex(r"(https?://(?:twitter\.com|x\.com)/[^\s]+)")
    async def on_twitter(self, event: AstrMessageEvent):
        if not self._check_whitelist(event):
            return
        url = self._extract_url(event, r"(https?://(?:twitter\.com|x\.com)/[^\s]+)")
        if not url:
            return
        proxy = self._proxy_for("twitter")
        m = re.search(r"(?:twitter\.com|x\.com)/([^/]+)/status/(\d+)", url)
        if not m:
            await event.send(event.plain_result("⚠️ 无法解析 Twitter 链接"))
            return
        user, sid = m.group(1), m.group(2)
        try:
            async with aiohttp.ClientSession() as s:
                fx = f"https://api.fxtwitter.com/{user}/status/{sid}"
                async with s.get(fx, proxy=proxy, headers=self.headers, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    if r.status != 200:
                        await event.send(event.plain_result(f"⚠️ Twitter 解析失败({r.status})"))
                        return
                    d = await r.json()
            t = d.get("tweet", {})
            author = t.get("author", {}).get("screen_name", user)
            text = (t.get("text") or "")[:120]
            medias = list(t.get("media", {}).get("all", []))
            # 引用转推：合并被引用推文的媒体（外层+引用都发）
            q = t.get("quote")
            if isinstance(q, dict):
                medias += list((q.get("media", {}) or {}).get("all", []))
            urls = [{"url": mm.get("url", ""), "type": mm.get("type", "")} for mm in medias if mm.get("url")]
            if urls:
                await self._send_media(event, urls, proxy, text=f"🐦 @{author}: {text}")
            else:
                await event.send(event.plain_result(f"🐦 @{author}: {text}"))
        except Exception as e:
            await event.send(event.plain_result(f"❌ Twitter 处理错误: {str(e)[:80]}"))

    # ---------- GIF / 图片 ----------
    @filter.regex(r"(https?://[^\s]+\.(?:gif|jpe?g|png|webp)(?:\?[^\s]*)?)")
    async def on_image_gif(self, event: AstrMessageEvent):
        if not self._check_whitelist(event):
            return
        url = self._extract_url(event, r"(https?://[^\s]+\.(?:gif|jpe?g|png|webp)(?:\?[^\s]*)?)")
        if not url:
            return
        p = await self._download(url)
        if p:
            try:
                await event.send(event.chain_result([Image.fromFileSystem(p)]))
            finally:
                try: os.remove(p)
                except Exception: pass
