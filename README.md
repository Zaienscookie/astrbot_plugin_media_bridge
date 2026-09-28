# astrbot_plugin_media_bridge

AstrBot 媒体解析插件：解析 **YouTube / Bluesky / Twitter(X) / GIF / 图片** 媒体链接，返回直链。支持**手动配置代理**（如本地 clash docker）与**画质选择**。

## 功能
- 🎬 **YouTube**：标题/作者/直链（oEmbed API）
- 📘 **Bluesky**：帖子文字 + 图片/外部链接（官方 API）
- 🐦 **Twitter/X**：多级 fallback（fxtwitter → syndication → OG抓取）
- 🖼️ **GIF/图片**：直接返回原图链接

## 配置（_conf_schema.json）
- **proxy**：代理地址（默认 `http://127.0.0.1:7890`，可指向本地 clash），按平台开关
- **quality**：画质（YouTube: max/720p/1080p，Twitter: hd/orig，Bluesky: fullsize/thumb）——能高清就高清
- **max_video_mb**：视频最大大小

## 代理说明
本机无公网直连国外媒体平台时，需配置代理。示例（clash docker）：
```
http://127.0.0.1:7890
```

## 安装
1. 下载插件目录放入 `data/plugins/`
2. 重启 AstrBot（或重载插件）
3. 在 AstrBot 管理面板配置代理/画质
