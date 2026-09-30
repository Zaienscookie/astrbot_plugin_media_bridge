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
from astrbot.api.message_components import Image, Video, Plain, File

try:
    from astrbot.core import AstrBotConfig
except Exception:
    AstrBotConfig = dict


@register("media_bridge", "zaiens", "解析并下载 YouTube/Bluesky/Twitter/GIF/图片媒体，支持代理、画质、白名单", "1.4.1")
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
        self._cache = {}
        self.tmp_dir = os.path.join(tempfile.gettempdir(), "media_bridge")
        os.makedirs(self.tmp_dir, exist_ok=True)
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

    # ---------- 工具 ----------
    def _check_whitelist(self, event) -> bool:
        wl = (self.config or {}).get("whitelist", {}) or {}
        if not wl.get("enabled", False):
            return True
        gid = uid = ""
        try: gid = str(event.get_group_id() or "")
        except Exception: pass
        try: uid = str(event.get_sender_id() or "")
        except Exception: pass
        groups = (wl.get("groups", "") or "").strip()
        users = (wl.get("users", "") or "").strip()
        gset = set(re.split(r"[,\s]+", groups)) if groups else set()
        uset = set(re.split(r"[,\s]+", users)) if users else set()
        gset.discard(""); uset.discard("")
        if not gset and not uset:
            return True
        return bool((gid and gid in gset) or (uid and uid in uset))

    def _proxy_for(self, platform: str):
        if not self.proxy_enabled:
            return None
        return self.proxy_url if self.proxy_cfg.get(platform, True) else None

    def _extract_url(self, event, pattern: str):
        text = getattr(event, "message_str", "") or ""
        m = re.search(pattern, text)
        return m.group(1) if m else None

    def _clean_cache(self):
        import time as _t
        now = _t.time()
        expired = [u for u, (p, ts) in self._cache.items() if now - ts >= self.cache_seconds]
        for u in expired:
            p, _ = self._cache.pop(u)
            try:
                if os.path.exists(p): os.remove(p)
            except Exception: pass

    async def _download(self, url: str, proxy: str = None, type_hint: str = "") -> str | None:
        import time as _t
        if url in self._cache:
            p, ts = self._cache[url]
            if _t.time() - ts < self.cache_seconds and os.path.exists(p):
                return p
        try:
            timeout = aiohttp.ClientTimeout(total=180)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(url, proxy=proxy, headers=self.headers) as resp:
                    if resp.status != 200:
                        logger.warning(f"[media_bridge] 下载失败 HTTP {resp.status}: {url[:70]}")
                        return None
                    clen = resp.headers.get("Content-Length")
                    if clen and int(clen) > self.max_video_mb * 1024 * 1024:
                        logger.warning(f"[media_bridge] 文件超限 {int(clen)//1048576}MB")
                        return None
                    ext = ""
                    if type_hint:
                        th = type_hint.lower()
                        if th in ("video", "gif"): ext = ".mp4"
                        elif th in ("photo", "image"): ext = ".jpg"
                    if not ext:
                        ct = resp.headers.get("Content-Type", "")
                        if "mp4" in ct or "video" in ct: ext = ".mp4"
                        elif "gif" in ct: ext = ".gif"
                        elif "png" in ct: ext = ".png"
                        elif "webp" in ct: ext = ".webp"
                        elif "jpeg" in ct or "jpg" in ct: ext = ".jpg"
                        else: ext = os.path.splitext(url.split("?")[0])[1] or ".bin"
                    fname = os.path.join(self.tmp_dir, f"{uuid.uuid4().hex}{ext}")
                    total = 0
                    limit = self.max_video_mb * 1024 * 1024
                    with open(fname, "wb") as f:
                        async for chunk in resp.content.iter_chunked(65536):
                            total += len(chunk)
                            if total > limit:
                                f.close(); os.remove(fname)
                                logger.warning("[media_bridge] 下载超限中断")
                                return None
                            f.write(chunk)
                    self._cache[url] = (fname, _t.time())
                    self._clean_cache()
                    return fname
        except Exception as e:
            logger.warning(f"[media_bridge] 下载异常: {str(e)[:80]}")
            return None

    def _is_video(self, path: str):
        return os.path.splitext(path)[1].lower() in (".mp4", ".mov", ".m4v", ".webm", ".mkv")

    async def _transcode_video(self, path: str):
        try:
            probe = await asyncio.create_subprocess_exec(
                "ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                "-of", "csv=p=0", path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            aout, _ = await probe.communicate()
            has_audio = bool(aout.strip())
            out = os.path.join(self.tmp_dir, f"tc_{uuid.uuid4().hex}.mp4")
            cmd = ["ffmpeg", "-y", "-i", path]
            if not has_audio:
                cmd += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-map", "0:v:0", "-map", "1:a:0"]
            cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "26", "-c:a", "aac", "-b:a", "128k"]
            if not has_audio: cmd += ["-shortest"]
            cmd += ["-movflags", "+faststart", out]
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            await asyncio.wait_for(proc.communicate(), timeout=180)
            return out if (proc.returncode == 0 and os.path.exists(out)) else None
        except Exception as e:
            logger.warning(f"[media_bridge] 转码异常: {str(e)[:80]}")
            return None

    def _bsky_cdn(self, did: str, blob: dict, kind: str = "image") -> str:
        try:
            cid = blob.get("ref", {}).get("$link", "")
            mime = blob.get("mimeType", "image/jpeg")
            ext = mime.split("/")[-1]
            if kind == "video":
                return f"https://cdn.bsky.app/img/feed_fullsize/plain/{did}/{cid}@mp4"
            return f"https://cdn.bsky.app/img/feed_fullsize/plain/{did}/{cid}@{ext}"
        except Exception:
            return ""

    async def _iter_media(self, urls: list, proxy: str, text: str = ""):
        """下载所有媒体；文字、每张图、每个视频 都单独 yield（逐条发送，避免 napcat 多图问题）"""
        images = []
        videos = []
        tmp = []
        for item in urls:
            if isinstance(item, dict):
                u = item.get("url", ""); th = item.get("type", "")
            else:
                u = item; th = ""
            if not u:
                continue
            p = await self._download(u, proxy=proxy, type_hint=th)
            if not p:
                continue
            try:
                if self._is_video(p):
                    tp = await self._transcode_video(p)
                    sp = tp or p
                    videos.append(Video.fromFileSystem(sp))
                    if tp: tmp.append(tp)
                else:
                    images.append(Image.fromFileSystem(p))
            except Exception as e:
                logger.warning(f"[media_bridge] 构建媒体失败: {str(e)[:80]}")
        logger.info(f"[media_bridge] 图片 {len(images)} 张, 视频 {len(videos)} 个, 文字 {bool(text)}")
        if text:
            yield self._plain(text)
        for im in images:
            yield self._chain([im])
        for v in videos:
            yield self._chain([v])
        if not images and not videos and text:
            yield self._plain("⚠️ 媒体下载失败(可能超限或代理异常)")
        for t in tmp:
            try:
                if t and os.path.exists(t): os.remove(t)
            except Exception: pass

    def _plain(self, text):
        from astrbot.api.event import MessageEventResult
        r = MessageEventResult()
        r.chain = [Plain(text)]
        return r

    def _chain(self, comps):
        from astrbot.api.event import MessageEventResult
        r = MessageEventResult()
        r.chain = comps
        return r

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
                        yield self._plain(f"⚠️ YouTube 解析失败({r.status})"); return
                    d = await r.json()
            title = d.get("title", ""); author = d.get("author_name", ""); thumb = d.get("thumbnail_url", "")
            path = await self._ytdlp_download(url, proxy)
            if path:
                yield self._plain(f"YouTube {title} — {author}")
                yield self._chain([Video.fromFileSystem(path)])
                try: os.remove(path)
                except Exception: pass
            else:
                if thumb:
                    p = await self._download(thumb, proxy=proxy)
                    if p:
                        yield self._chain([Image.fromFileSystem(p)])
                yield self._plain(f"YouTube {title} — {author}")
        except Exception as e:
            yield self._plain(f"❌ YouTube 处理错误: {str(e)[:80]}")

    async def _ytdlp_download(self, url: str, proxy: str):
        try:
            out_tmpl = os.path.join(self.tmp_dir, f"yt_{uuid.uuid4().hex}.%(ext)s")
            cmd = ["yt-dlp", "-f", "bv*+ba/b", "--merge-output-format", "mp4", "-o", out_tmpl,
                   "--no-playlist", "--max-filesize", f"{int(self.max_video_mb)}M"]
            if proxy: cmd += ["--proxy", proxy]
            cmd.append(url)
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            _, err = await asyncio.wait_for(proc.communicate(), timeout=300)
            if proc.returncode != 0:
                logger.warning(f"[media_bridge] yt-dlp 失败: {err.decode(errors='ignore')[:120]}")
                return None
            for f in os.listdir(self.tmp_dir):
                if f.startswith("yt_") and os.path.splitext(f)[1] in (".mp4", ".mkv", ".webm"):
                    return os.path.join(self.tmp_dir, f)
            return None
        except Exception as e:
            logger.warning(f"[media_bridge] yt-dlp 异常: {str(e)[:80]}")
            return None

    # ---------- Bluesky ----------
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
                yield self._plain("⚠️ 无法解析 Bluesky 帖子链接"); return
            handle, rkey = m.group(1), m.group(2)
            async with aiohttp.ClientSession() as s:
                ru = f"https://public.api.bsky.app/xrpc/com.atproto.identity.resolveHandle?handle={handle}"
                async with s.get(ru, proxy=proxy, headers=self.headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status != 200:
                        yield self._plain("⚠️ Bluesky 用户解析失败"); return
                    did = (await r.json()).get("did", "")
                if not did:
                    yield self._plain("⚠️ 未找到用户 DID"); return
                uri = f"at://{did}/app.bsky.feed.post/{rkey}"
                pu = f"https://public.api.bsky.app/xrpc/app.bsky.feed.getPosts?uris={urllib.parse.quote(uri)}"
                async with s.get(pu, proxy=proxy, headers=self.headers, timeout=aiohttp.ClientTimeout(total=15)) as r2:
                    if r2.status != 200:
                        yield self._plain(f"⚠️ Bluesky 帖子获取失败({r2.status})"); return
                    posts = (await r2.json()).get("posts", [])
            if not posts:
                yield self._plain("⚠️ Bluesky 帖子不存在"); return
            rec = posts[0].get("record", {})
            text = (rec.get("text") or "")[:120]
            embed = rec.get("embed", {}) or {}
            et = embed.get("$type", "")
            media = []
            if et == "app.bsky.embed.images":
                for img in embed.get("images", []):
                    u = self._bsky_cdn(did, img.get("image", {}), "image")
                    if u: media.append({"url": u, "type": "image"})
            elif et == "app.bsky.embed.video":
                u = self._bsky_cdn(did, embed.get("video", {}), "video")
                if u: media.append({"url": u, "type": "video"})
            elif et == "app.bsky.embed.external":
                eu = (embed.get("external") or {}).get("uri", "")
                if eu: media.append({"url": eu, "type": ""})
            elif et == "app.bsky.embed.recordWithMedia":
                m2 = embed.get("media", {}) or {}
                if m2.get("$type") == "app.bsky.embed.images":
                    for img in m2.get("images", []):
                        u = self._bsky_cdn(did, img.get("image", {}), "image")
                        if u: media.append({"url": u, "type": "image"})
            if media:
                async for r in self._iter_media(media, proxy, f"Bluesky {text}"):
                    yield r
            else:
                yield self._plain(f"Bluesky {text}" if text else "Bluesky (无媒体内容)")
        except Exception as e:
            yield self._plain(f"❌ Bluesky 处理错误: {str(e)[:80]}")

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
            yield self._plain("⚠️ 无法解析 Twitter 链接"); return
        user, sid = m.group(1), m.group(2)
        try:
            async with aiohttp.ClientSession() as s:
                fx = f"https://api.fxtwitter.com/{user}/status/{sid}"
                async with s.get(fx, proxy=proxy, headers=self.headers, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    if r.status != 200:
                        yield self._plain(f"⚠️ Twitter 解析失败({r.status})"); return
                    d = await r.json()
            t = d.get("tweet", {})
            author = t.get("author", {}).get("screen_name", user)
            text = (t.get("text") or "")[:120]
            medias = list(t.get("media", {}).get("all", []))
            q = t.get("quote")
            if isinstance(q, dict):
                medias += list((q.get("media", {}) or {}).get("all", []))
            urls = []
            for mm in medias:
                mu = mm.get("url") or mm.get("media_url_https") or mm.get("thumbnail_url") or ""
                if not mu: continue
                for a, b in (("name=small","name=large"),("name=medium","name=large"),("name=orig","name=large")):
                    mu = mu.replace(a, b)
                urls.append({"url": mu, "type": mm.get("type", "")})
            logger.info(f"[media_bridge] Twitter 解析到 {len(urls)} 个媒体")
            if urls:
                async for r in self._iter_media(urls, proxy, f"X @{author}: {text}"):
                    yield r
            else:
                yield self._plain(f"X @{author}: {text}")
        except Exception as e:
            yield self._plain(f"❌ Twitter 处理错误: {str(e)[:80]}")

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
            yield self._chain([Image.fromFileSystem(p)])
