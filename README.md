# media-archive · 媒体归档

一个 macOS 本地工具：**把抖音 / 小红书 / X / B站 / YouTube / Instagram / 微博 / Reddit / TikTok 的视频和图文存到本地，自动按「平台 / 博主 / 日期_标题」归档，并生成可搜索的索引表。**

不依赖任何在线解析服务，所有请求从你的机器直接发出；解析、下载、整理全在本地完成。

## 它能做什么

- **三处入口**：macOS App（窗口 + 全局快捷键）、本地 Web 页「http://127.0.0.1:8787」、命令行
- **按博主自动分类**：`~/Media/Archive/抖音/杨超越/20210513_标题/video.mp4`
- **索引表**：`index.db`（SQLite）+ `索引.csv`，记录平台/博主/账号/主页/作品ID/描述/发布时间/点赞评论收藏/文件路径
- **图文与 Live Photo**：小红书的多图、Live Photo 会按 `01.jpg` + `01_live.mp4` 一一配对下载
- **去水印**：抖音取详情接口里的播放直链；小红书取 CDN 原图（1080 档）
- **自动代理**：探测本机 Clash / Surge 等常见端口，墙外站点自动走，国内直连

## 安装

```bash
git clone https://github.com/<你的用户名>/media-archive.git
cd media-archive
./install.sh
```

`install.sh` 会：检查 Chrome / Node / Python / swiftc → 缺什么用 Homebrew + pip 补上 → 拉取抖音抓取器并打补丁 → 生成配置 → 编译安装 App → 配置 Web 服务常驻。

装完：

```bash
# 打开 App（窗口里有粘贴框、平台/博主筛选、视频预览）
open /Applications/MediaArchive.app

# 或用命令行
python3 ~/bin/media-archive/engine.py "https://v.douyin.com/xxxxx/"

# 或浏览器
open http://127.0.0.1:8787
```

## 支持平台

| 平台 | 手段 | 状态 |
|---|---|---|
| 抖音 | 真实 Chrome + DevTools 协议读详情接口直链（绕过 JS-VMP 反爬） | ✅ 1080p 无水印 + 博主信息 |
| 小红书 | 短链展开 + 页面内嵌 `__INITIAL_STATE__` 解析（移动端 UA） | ✅ 多图 / Live Photo / 视频 |
| B站 / YouTube / X / Instagram / 微博 / Reddit / TikTok | [yt-dlp](https://github.com/yt-dlp/yt-dlp) | ✅ 需 ffmpeg 合并音视频 |
| 其它 1800+ 站点 | yt-dlp 通用兜底 | ✅ 尽力而为 |
| Telegram | MTProto（Telethon，需自备 api_id/api_hash） | ⚠️ 需运行 `tg_setup.py` 配置 |

## 设置

App 里点「设置…」（或 `⌘,`）：

- **存放位置**：任意目录，改后只影响新内容
- **全局快捷键**：录制任意组合键（默认 `⌥⌘D`）
- **自动监听剪贴板**：开启后复制白名单链接即自动归档
- **Web 服务状态**：查看 / 重启

配置文件 `~/.media-archive/config.json`：

```json
{
  "archive_dir": "/Users/you/Media/Archive",
  "hotkey": { "keyCode": 2, "modifiers": ["command", "option"], "display": "⌥⌘D" },
  "auto_watch": false,
  "web_port": 8787
}
```

## 为什么不用「纯 HTTP 解析」

2026 年实测：抖音对无 Cookie 的程序化请求启用 JS-VMP 反爬（`a_bogus` 签名），分享页 SSR 里的 `videoInfoRes` / `play_addr` 已被移除，老接口 `iteminfo` 返回 0 字节，yt-dlp 自带的 Douyin 提取器也因缺签名算法报 `Fresh cookies are needed`。

所以抖音改成让**真实浏览器**自己去请求详情接口，我们通过 Chrome DevTools 协议把响应体读出来——不伪造签名，只旁观页面自己拿到的数据。

小红书则是版本差异问题：桌面 UA 会被 302 到登录页，必须用移动端 UA；数据结构也从 `note.noteDetailMap` 迁移到 `noteData.data.noteData`。

## 目录结构

```
media-archive/
├── install.sh          # 一键安装
├── engine.py           # 核心：平台识别 / 解析 / 下载 / 归档 / 索引
├── server.py           # 本地 Web 服务（标准库，无第三方依赖）
├── web/index.html      # Web 前端（单文件）
├── app/main.swift      # macOS App（SwiftUI + AppKit + WebKit）
├── patches/            # 给抖音抓取器打的补丁
└── tg_setup.py         # Telegram MTProto 登录（可选）
```

## 依赖与致谢

- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — 通用站点解析引擎
- [douyin-ai-video-downloader](https://github.com/Awake118/douyin-ai-video-downloader)（MIT）— 抖音 CDP 抓取，本项目在其上补充了元信息导出与 macOS bash 3.2 兼容修复
- ffmpeg — 音视频合并

## 免责声明

仅供**个人学习、研究和个人内容备份**使用。下载他人作品请遵守各平台用户协议与著作权法，不得二次传播或商用。作者不对使用本工具产生的任何后果负责。

## License

MIT（本项目代码）。第三方组件遵循其自身许可证。
