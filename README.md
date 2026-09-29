# astrbot_plugin_media_bridge

AstrBot 媒体解析插件：**解析并下载** YouTube / Bluesky / Twitter(X) / GIF / 图片，**直接发送媒体**（视频/图片）。支持**手动配置代理**、**画质选择**、**群/用户白名单**。

## ✨ 功能
- 🎬 **YouTube**：yt-dlp 下载最高画质视频并发视频
- 📘 **Bluesky**：API 解析 → 下载图片 → 发图片
- 🐦 **Twitter/X**：fxtwitter 解析 → 下载 mp4/图片 → 发媒体
- 🖼️ **GIF/图片**：下载后直接发
- 🔒 **白名单**：开启后仅指定群/用户可用

## ⚙️ 配置
| 项 | 说明 |
|---|---|
| **whitelist.enabled** | 启用白名单 |
| **whitelist.groups** | 白名单群号（逗号分隔）|
| **whitelist.users** | 白名单用户 QQ（逗号分隔）|
| **proxy.enabled / url** | 代理开关 + 地址（默认 `http://127.0.0.1:7890`）|
| **quality** | 画质（YouTube: max；Twitter: hd/orig；Bluesky: fullsize）|
| **max_video_mb** | 视频最大下载大小（默认 50MB）|

## 🔒 白名单逻辑
- 未开启：所有人都能用
- 已开启：群号在白名单 或 用户 QQ 在白名单 才可用
- 白名单列表为空：视为全部允许（防误锁）

## 🚀 安装
1. 插件目录放入 `data/plugins/`
2. 重启/重载 AstrBot
3. 面板配置代理/画质/白名单

## 🎬 视频发送说明
QQ 发送**无音轨**视频会超时。插件发送视频前会用 **ffmpeg 转码**为标准 mp4（H.264+AAC+faststart），无音轨时自动添加静音音轨。

**依赖**：`ffmpeg`（`apt install ffmpeg`）

## 📦 依赖
- `aiohttp`（AstrBot 自带）
- `yt-dlp`（YouTube 视频下载需要）
