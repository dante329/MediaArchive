# 媒体归档 · MediaArchive

**中文** | [English](#mediaarchive)

**把你收藏的短视频、图文、实况照片存到自己的硬盘上，按平台、博主整理好，还支持自定义分类。**

一个 macOS 本地应用。粘贴一条链接，或者直接复制链接（自动监听剪贴板），它会从 20+ 个平台把内容抓下来、按「平台 / 博主 / 日期_标题」建好目录、写进索引表。支持批量导入抖音喜欢/收藏列表、博主主页作品、YouTube/B站播放列表。

所有解析和下载都从你自己的机器直接发出，不经过任何第三方服务。

---

## 为什么需要它

| 场景 | 问题 | 这个工具做什么 |
|---|---|---|
| 收藏夹越攒越多 | 平台内容会下架，博主会删号 | 抓成本地文件，永久保留 |
| 想批量备份喜欢列表 | 一个个复制链接太慢 | 一键读取抖音喜欢/收藏，勾选批量归档 |
| 实况照片 | 右键保存只有一张静图 | 还原成真正的 Live Photo，可导入相册 |
| 想训练/微调本地 AI | 缺少高质量中文多模态语料 | 归档即语料——视频、图文、元数据全部结构化存储在本地，可直接喂给本地大模型做 RAG、微调、数据分析 |

---

## 功能

### 一、一键归档（20+ 个平台）

抖音 · 小红书 · X · B站 · YouTube · Instagram · 微博 · Reddit · Telegram · TikTok · Threads · Niconico · Vimeo · Dailymotion · Streamable · Newgrounds · Tumblr · Pinterest · VK · OK.ru · Twitch · SoundCloud · Apple Podcasts · 微信公众号

**四处入口，同一个内核：**

```
macOS App 窗口    → 粘贴链接 / 回车
全局快捷键 ⌘】     → 任何 App 里按下，归档剪贴板
复制链路自动监听   → 连快捷键都不用（可配置监听哪些平台）
本地网页           → http://127.0.0.1:8787
命令行             → python3 engine.py "链接"
```

**存下来的结构：**

```
~/Media/Archive/
└── <平台>/
    └── <博主>/
        └── <日期>_<标题>_<id>/
            ├── video.mp4          去水印原画质
            ├── .poster.jpg        封面缩略图
            ├── meta.json          原始元数据
            └── 来源.txt           可读的来源说明
```

- **去水印**：抖音走详情接口拿播放直链，小红书取 1080 档 CDN 原图
- **自动代理**：探测本机 Clash / Surge 端口，墙外站点自动走，国内直连
- **重复检测**：已归档过的会提示「已存在」，不重复占空间
- **目录防碰撞**：目录名末尾追加作品 ID 后缀，同标题不同作品不会互相覆盖

### 二、批量归档

不只是单条链接——支持整批导入：

| 入口 | 说明 |
|---|---|
| **抖音喜欢** | 登录抖音后，一键读取全部喜欢列表 |
| **抖音收藏** | 同上，读取收藏夹 |
| **博主主页** | 粘贴博主主页链接，读取全部公开作品 |
| **播放列表** | YouTube 频道/播放列表、B站合集/分P |
| **批量链接** | 粘贴一整段分享文案，自动识别所有链接 |

批量面板支持：搜索过滤、只看未归档、全选未归档、勾选后一键归档。

### 三、实况照片完整还原

各平台的实况照片，在这个工具里有完整支持。

**关键认知：Live Photo 不是一个文件格式。** 它是照片 App 里的一条记录，由一个图片资源 + 一个配对视频 + 共享的 UUID 组成。文件系统层面永远是 `01.jpg` + `01.mov` 两个文件。

流程：

```
下载配对（01.jpg + 01_live.mp4）
   ↓
尺寸对齐（视频按图片尺寸重编码，强制 SAR=1:1）
   ↓
AVFoundation 封装（写入 mdta 元数据轨 + 双端 ContentIdentifier）
   ↓
PhotoKit 导入（PHAssetCreationRequest，photo + pairedVideo 同时添加）
   ↓
照片 App 里出现真正的实况照片
```

技术细节：`com.apple.quicktime.still-image-time` 必须是**独立元数据轨**，用 ffmpeg 的 `-metadata` 写只是文件级标签，照片 App 不认（会报 `PHPhotosErrorDomain error 3302`）。图片端的 `ContentIdentifier` 在 Apple MakerNote 里，部分平台转码过的图常常没有这段结构，工具会自动注入骨架再写。

### 四、音频提取

按 `⌘E`，从当前打开的视频里把音轨单独拿出来：

```bash
ffmpeg -i video.mp4 -vn -c:a copy audio.m4a     # 无损，秒级完成
```

失败自动降级转 mp3。文件落在作品目录里。

### 五、管理与检索

- **搜索**：标题 / 博主 / 链接，支持模糊匹配
- **筛选**：平台、博主（点卡片上的博主名直接跳转）、分类
- **分类**：自建分类，把作品归入不同分类
- **任务队列**：状态栏实时显示进度，点状态灯展开任务列表，每个任务可取消
- **批量删除**：勾选 → 删除 → 进废纸篓（不是直接抹掉）
- **清理失效记录**：一键清除数据库里文件已不存在的记录

### 六、本地 AI 语料库

归档不只是备份——它天然就是一个**结构化的多模态语料库**。

每条归档作品在磁盘上的存储结构：

```
作品目录/
├── video.mp4          原始视频/图片文件
├── .poster.jpg        封面缩略图
├── meta.json          完整元数据（标题、作者、发布时间、点赞/评论/收藏数、标签、原始链接…）
└── 来源.txt           人类可读的来源说明
```

加上 SQLite 索引表里的结构化字段（平台、博主、博主 ID、作品 ID、时间戳、互动数据），你拥有的是一份：

- **按博主/平台/时间维度组织**的中文互联网内容数据集
- **视频 + 图文 + 元数据**的多模态配对数据
- **完全离线、完全私有**——数据从未离开你的机器

**可以直接用来做什么：**

| 用途 | 怎么用 |
|---|---|
| **RAG 知识库** | 把 meta.json + 转录文本灌入向量数据库，用本地 LLM 做语义搜索和问答 |
| **微调数据集** | 标题+描述+标签天然就是 instruction-output 对，可直接构造 SFT 数据 |
| **内容分析** | SQL 查询互动数据趋势、博主产出频率、热门话题分布 |
| **推荐系统原型** | 基于收藏行为 + 内容特征训练个性化推荐模型 |
| **多模态训练** | 视频帧 + 标题 + 标签 = 图文对齐训练的现成数据 |

所有数据都是标准格式（JSON + MP4/JPG + SQLite），不需要任何专有工具就能读取和处理。归档越多，语料越丰富——这是一个会随着使用自动增长的数据资产。

### 七、原生窗口体验

详情页是**真正的 macOS 窗口**，不是一个网页弹层：

- `⌘W` 关闭、`⌘M` 最小化到 Dock、`⌃⌘F` 全屏
- 可拖到屏幕任意位置，八向拖拽调整大小
- 多个作品可以同时开多个窗口，各自独立播放
- 关闭窗口会立即停止播放

### 八、11 套主题

默认 · 深夜 · 暮光 · 薄荷 · 拿铁 · 靛蓝 · 珊瑚 · 冰川 · 极光 · 石墨 · 跟随系统

每套主题都有独立的配色方案，包括深色模式（深夜、暮光、极光、石墨）和浅色模式。石墨主题带有碳纤维编织纹理效果。

---

## 设计

### 整体架构

```
┌─────────────────────────────────────────────────┐
│  macOS App（Swift）                              │
│  ├ 主窗口      WKWebView → 本地网页               │
│  ├ 详情窗口    NSWindow × N（每个作品一个）        │
│  ├ 设置窗口    SwiftUI 原生表单                   │
│  └ 菜单栏      全局快捷键 / 归档 / 提取音频        │
└────────────────┬────────────────────────────────┘
                 │ JS 桥（WKScriptMessageHandler）
                 │ + HTTP
┌────────────────▼────────────────────────────────┐
│  Python 服务  ThreadingHTTPServer :8787          │
│  ├ engine.py      平台识别 / 解析 / 下载 / 归档    │
│  ├ batch.py       批量列表抓取 / 登录态管理        │
│  ├ livephoto.py   实况照片合成                    │
│  └ server.py      REST API + 任务队列 + 文件服务   │
└────────────────┬────────────────────────────────┘
                 │
┌────────────────▼────────────────────────────────┐
│  数据层                                          │
│  ├ ~/Media/Archive/  文件（人来读，Finder 能看）   │
│  └ index.db          SQLite WAL（程序来查）       │
└─────────────────────────────────────────────────┘
```

### 几个关键决策

**1. 为什么不做成纯网页应用**

浏览器的 div 做不到这些：拖出窗口外（会被裁剪）、最小化进 Dock、系统级快捷键、真正的全屏 Space。所以详情页用原生 `NSWindow` + `WKWebView` 组合——窗口归系统管，内容归网页管。

**2. 为什么四个入口共用一个内核**

App、网页、命令行都只是「调用方」，真正的逻辑全在 `engine.py`。加一个新平台只需要改一处，所有入口同时生效。App 的归档也走 HTTP API（而不是直接 `Process` 调脚本），这样网页的任务面板才能看到进度。

**3. 为什么数据分两层**

文件系统层是给人看的：目录结构清晰、文件名有意义、Finder 里能直接浏览，工具没了数据还在。SQLite 层是给程序查的：搜索、筛选、统计。

**4. 抖音为什么要单独处理**

抖音有 JS-VMP 反爬（`a_bogus` 签名），纯 HTTP 请求拿不到数据。方案是启动真实 Chrome（headless），用 DevTools Protocol 挂上去，读取页面自己发出的 `aweme/v1/web/aweme/detail/` 响应——让浏览器替我们算签名。其他平台用 yt-dlp 或直接解析页面数据。

**5. 为什么实况照片要自己写封装**

开源工具大多用 ffmpeg 的 `-metadata` 写标识，生成的文件在元数据上看是对的，但导入照片 App 会失败。真正合规必须用 AVFoundation 创建独立的 metadata track。这个差异只有实际导入相册才能发现，`exiftool` 两个都能读出来。

### 技术栈

| 层 | 技术 | 说明 |
|---|---|---|
| **App** | Swift + SwiftUI + AppKit | 窗口/菜单用 AppKit，设置表单用 SwiftUI |
| **网页** | 原生 HTML/CSS/JS | 没有框架，单文件 ~4100 行 |
| **容器** | WKWebView | 主窗口和详情窗口都靠它渲染 |
| **桥接** | WKScriptMessageHandler | 网页 ↔ 原生双向通信 |
| **服务** | Python 3 标准库 | `ThreadingHTTPServer`，零依赖 |
| **解析** | Chrome DevTools Protocol / yt-dlp | 抖音用 CDP，其余用 yt-dlp |
| **媒体** | ffmpeg | 转码、抽帧、抽音轨、尺寸对齐 |
| **实况** | AVFoundation + exiftool + PhotoKit | 元数据轨、图片标识、相册写入 |
| **存储** | SQLite WAL + 文件系统 | 索引与原始数据分离 |

### 代码规模

```
server.py        1896 行   REST API 与任务队列
web/index.html   4154 行   全部前端（单文件）
app/main.swift   2127 行   原生 App
engine.py        1203 行   平台解析 + 下载 + 归档
tools/*.js        612 行   抖音 API / 浏览器兜底 / 列表抓取
tools/*.swift     246 行   lpmake / OCR / 废纸篓 / 批量导入
batch.py          345 行   批量列表 + 登录态
livephoto.py      170 行   实况照片合成
─────────────────────────
约 10700 行
```

### 目录结构

```
media-archive/
├── app/main.swift          原生 App（单文件）
├── web/index.html          全部前端（单文件）
├── engine.py               平台解析 + 下载 + 归档
├── server.py               本地 HTTP 服务
├── batch.py                批量列表抓取 + 登录态管理
├── livephoto.py            实况照片合成
├── tools/
│   ├── douyin_api.js       抖音页面内 API（CDP）
│   ├── fetch_list.js       通用列表抓取
│   ├── browser_media.js    浏览器兜底抓取
│   ├── check_login_xhs.js  小红书登录态检查
│   ├── lpmake.swift        AVFoundation 实况封装
│   ├── ocr.swift           Vision OCR
│   ├── lpimport.swift      批量导入相册
│   └── trash.swift         移到废纸篓
├── vendor/                 抖音下载器
├── patches/                抖音抓取器的补丁
├── design/                 设计稿 / 主题预览
└── install.sh              一键安装
```

---

## 已知限制

| 限制 | 原因 |
|---|---|
| 抖音需要 Chrome + Node.js | 反爬签名必须由真实浏览器计算 |
| 实况照片必须在照片 App 里才是「实况」 | 它本质是相册记录，不是文件格式 |
| 微信公众号文章不支持 | 需要单独的解析器，暂未实现 |
| 只支持 macOS | App 层用了大量 AppKit / PhotoKit / Vision 原生能力 |

---

## 安装

```bash
git clone https://github.com/dante329/MediaArchive.git
cd MediaArchive
./install.sh
```

`install.sh` 会自动检查并补齐依赖：Chrome / Node / Python / Swift / ffmpeg / exiftool，拉取抖音抓取器并打补丁，生成配置，编译安装 App，注册 Web 服务常驻。

---

## 快捷键

| 快捷键 | 作用 | 范围 |
|---|---|---|
| `⌘】` | 归档剪贴板 | 全局 |
| `⇧⌘I` | 导入所有实况照片到相册 | App 内 |
| `⌘I` | 提取当前窗口作品的实况照片 | App 内 |
| `⌘E` | 提取当前窗口作品的音频 | App 内 |
| `⌘O` | 打开主窗口 / 在 Finder 显示 | App 内 |
| `⌘R` | 刷新页面 | App 内 |
| `⌘,` | 设置 | App 内 |
| `⌘W` `⌘M` `⌃⌘F` | 关闭 / 最小化 / 全屏 | 详情窗口 |

全部可在设置里重新录制。

---

## License

MIT

---
---

<a id="mediaarchive"></a>

# MediaArchive

**Save your favorite short videos, image posts, and Live Photos to your own disk — organized by platform, creator, and custom collections.**

A macOS native app. Paste a link, or just copy one (clipboard auto-watch), and it downloads content from 20+ platforms into a clean `Platform / Creator / Date_Title` directory tree with a SQLite index. Supports batch import from Douyin likes/favorites, creator homepages, and YouTube/Bilibili playlists.

All parsing and downloading happens directly from your machine — no third-party services involved.

---

## Why

| Scenario | Problem | What this tool does |
|---|---|---|
| Favorites keep piling up | Platform content gets taken down, creators delete accounts | Downloads to local files, preserved forever |
| Want to batch-backup liked lists | Copying links one by one is too slow | One-click read of Douyin likes/favorites, check & batch archive |
| Live Photos | Right-click save only gets a still image | Restores real Live Photos, importable to Photos app |
| Want to train/fine-tune local AI | Lack of high-quality Chinese multimodal corpus | Archive = corpus — videos, images, metadata all structured locally, ready for RAG, fine-tuning, data analysis |

---

## Features

### 1. One-Click Archive (20+ Platforms)

Douyin · Xiaohongshu · X · Bilibili · YouTube · Instagram · Weibo · Reddit · Telegram · TikTok · Threads · Niconico · Vimeo · Dailymotion · Streamable · Newgrounds · Tumblr · Pinterest · VK · OK.ru · Twitch · SoundCloud · Apple Podcasts · WeChat Articles

**Four entry points, one engine:**

```
macOS App window   → Paste link / Enter
Global hotkey ⌘]   → Press in any app, archives clipboard
Clipboard watch    → No hotkey needed (configurable per platform)
Local web UI       → http://127.0.0.1:8787
CLI                → python3 engine.py "url"
```

**Saved structure:**

```
~/Media/Archive/
└── <Platform>/
    └── <Creator>/
        └── <Date>_<Title>_<id>/
            ├── video.mp4          Original quality, watermark-free
            ├── .poster.jpg        Cover thumbnail
            ├── meta.json          Full metadata
            └── source.txt         Human-readable source info
```

- **Watermark removal**: Douyin uses detail API for direct playback URL; Xiaohongshu fetches 1080p CDN originals
- **Auto proxy**: Detects local Clash/Surge ports; overseas sites go through proxy, domestic direct
- **Duplicate detection**: Already archived items show "exists" prompt, no wasted space
- **Directory collision prevention**: Item ID suffix appended to directory name

### 2. Batch Archive

Not just single links — supports bulk import:

| Entry | Description |
|---|---|
| **Douyin Likes** | After login, one-click read entire liked list |
| **Douyin Favorites** | Same, reads favorites collection |
| **Creator Homepage** | Paste creator URL, reads all public works |
| **Playlists** | YouTube channels/playlists, Bilibili collections |
| **Batch Links** | Paste a block of share text, auto-extracts all URLs |

Batch panel supports: search filter, show unarchived only, select all unarchived, one-click archive selected.

### 3. Live Photo Restoration

Live Photos from various platforms are fully supported.

**Key insight: Live Photo is not a file format.** It's a record in the Photos app, consisting of an image resource + a paired video + a shared UUID. At the filesystem level it's always `01.jpg` + `01.mov`.

Pipeline:

```
Download pair (01.jpg + 01_live.mp4)
   ↓
Size alignment (re-encode video to image dimensions, force SAR=1:1)
   ↓
AVFoundation packaging (write mdta metadata track + dual ContentIdentifier)
   ↓
PhotoKit import (PHAssetCreationRequest, photo + pairedVideo added together)
   ↓
Real Live Photo appears in Photos app
```

Technical detail: `com.apple.quicktime.still-image-time` must be an **independent metadata track**. Writing via ffmpeg's `-metadata` is just a file-level tag that Photos app ignores (throws `PHPhotosErrorDomain error 3302`). The image-side `ContentIdentifier` lives in Apple MakerNote; some platforms' transcoded images lack this structure — the tool auto-injects the skeleton before writing.

### 4. Audio Extraction

Press `⌘E` to extract the audio track from the currently open video:

```bash
ffmpeg -i video.mp4 -vn -c:a copy audio.m4a     # Lossless, instant
```

Falls back to mp3 on failure. File saved in the item's directory.

### 5. Management & Search

- **Search**: Title / creator / URL, fuzzy matching
- **Filter**: Platform, creator (click creator name on card to jump), collections
- **Collections**: Create custom collections, organize items into categories
- **Task queue**: Status bar shows real-time progress, click to expand task list, each task cancellable
- **Batch delete**: Select → Delete → moves to Trash (not permanently erased)
- **Prune stale records**: One-click cleanup of DB records whose files no longer exist

### 6. Local AI Corpus

Archiving isn't just backup — it's naturally a **structured multimodal corpus**.

Each archived item on disk:

```
item_directory/
├── video.mp4          Original video/image file
├── .poster.jpg        Cover thumbnail
├── meta.json          Full metadata (title, author, publish time, likes/comments/saves, tags, source URL…)
└── source.txt         Human-readable source info
```

Combined with SQLite index fields (platform, creator, creator ID, item ID, timestamps, engagement data), you have:

- A Chinese internet content dataset **organized by creator/platform/time**
- **Video + image + metadata** multimodal paired data
- **Fully offline, fully private** — data never leaves your machine

**What you can do with it:**

| Use Case | How |
|---|---|
| **RAG knowledge base** | Ingest meta.json + transcripts into vector DB, use local LLM for semantic search & QA |
| **Fine-tuning dataset** | Title + description + tags naturally form instruction-output pairs for SFT |
| **Content analysis** | SQL queries on engagement trends, creator output frequency, topic distribution |
| **Recommender prototype** | Train personalized recommendation models on收藏 behavior + content features |
| **Multimodal training** | Video frames + titles + tags = ready-made image-text alignment data |

All data is in standard formats (JSON + MP4/JPG + SQLite), readable without any proprietary tools. The more you archive, the richer the corpus — a data asset that grows automatically with use.

### 7. Native Window Experience

Detail pages are **real macOS windows**, not web popups:

- `⌘W` close, `⌘M` minimize to Dock, `⌃⌘F` fullscreen
- Draggable anywhere, resizable from all 8 edges
- Multiple items can have multiple windows open simultaneously, each playing independently
- Closing a window immediately stops playback

### 8. 11 Themes

Default · Deep Night · Twilight · Mint · Latte · Indigo · Coral · Glacier · Aurora · Graphite · Follow System

Each theme has its own color scheme, including dark modes (Deep Night, Twilight, Aurora, Graphite) and light modes. The Graphite theme features a carbon fiber weave texture effect.

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│  macOS App (Swift)                               │
│  ├ Main window    WKWebView → local web UI       │
│  ├ Detail windows NSWindow × N (one per item)   │
│  ├ Settings       SwiftUI native form            │
│  └ Menu bar       Global hotkey / archive / audio│
└────────────────┬────────────────────────────────┘
                 │ JS bridge (WKScriptMessageHandler)
                 │ + HTTP
┌────────────────▼────────────────────────────────┐
│  Python Server  ThreadingHTTPServer :8787        │
│  ├ engine.py      Platform detect / parse / DL   │
│  ├ batch.py       Batch list fetch / login state │
│  ├ livephoto.py   Live Photo synthesis           │
│  └ server.py      REST API + task queue + files  │
└────────────────┬────────────────────────────────┘
                 │
┌────────────────▼────────────────────────────────┐
│  Data Layer                                      │
│  ├ ~/Media/Archive/  Files (human-readable)      │
│  └ index.db          SQLite WAL (machine-query)  │
└─────────────────────────────────────────────────┘
```

### Key Design Decisions

**1. Why not a pure web app?**

Browser divs can't do: drag outside window bounds (gets clipped), minimize to Dock, system-level hotkeys, real fullscreen Spaces. So detail pages use native `NSWindow` + `WKWebView` — the OS manages the window, the web manages the content.

**2. Why four entry points share one engine?**

App, web, CLI are all just "callers". The real logic lives in `engine.py`. Adding a new platform requires changing one place; all entry points benefit simultaneously. The app also archives via HTTP API (not direct `Process` calls), so the web task panel can see progress.

**3. Why two data layers?**

Filesystem layer is for humans: clear directory structure, meaningful filenames, browsable in Finder — data survives even without the tool. SQLite layer is for programs: search, filter, statistics.

**4. Why does Douyin need special handling?**

Douyin has JS-VMP anti-scraping (`a_bogus` signature). Pure HTTP requests can't get data. The solution: launch real Chrome (headless), attach via DevTools Protocol, read the page's own `aweme/v1/web/aweme/detail/` response — let the browser compute the signature for us. Other platforms use yt-dlp or direct page parsing.

**5. Why custom Live Photo packaging?**

Most open-source tools use ffmpeg's `-metadata` to write identifiers. The generated files look correct in metadata, but fail when imported to Photos app. Proper compliance requires AVFoundation to create an independent metadata track. This difference is only discoverable through actual Photos app import — `exiftool` reads both correctly.

### Tech Stack

| Layer | Technology | Notes |
|---|---|---|
| **App** | Swift + SwiftUI + AppKit | Windows/menu via AppKit, settings via SwiftUI |
| **Web** | Vanilla HTML/CSS/JS | No framework, single file ~4100 lines |
| **Container** | WKWebView | Both main and detail windows |
| **Bridge** | WKScriptMessageHandler | Web ↔ native bidirectional |
| **Server** | Python 3 stdlib | `ThreadingHTTPServer`, zero dependencies |
| **Parsing** | Chrome DevTools Protocol / yt-dlp | Douyin via CDP, others via yt-dlp |
| **Media** | ffmpeg | Transcode, thumbnails, audio extraction, size alignment |
| **Live Photo** | AVFoundation + exiftool + PhotoKit | Metadata track, image identifier, album write |
| **Storage** | SQLite WAL + filesystem | Index and raw data separated |

### Code Size

```
server.py        1896 lines  REST API & task queue
web/index.html   4154 lines  All frontend (single file)
app/main.swift   2127 lines  Native app
engine.py        1203 lines  Platform parsing + download + archive
tools/*.js        612 lines  Douyin API / browser fallback / list fetch
tools/*.swift     246 lines  lpmake / OCR / trash / batch import
batch.py          345 lines  Batch lists + login state
livephoto.py      170 lines  Live Photo synthesis
─────────────────────────
~10,700 lines
```

---

## Known Limitations

| Limitation | Reason |
|---|---|
| Douyin requires Chrome + Node.js | Anti-scraping signature must be computed by a real browser |
| Live Photos are only "live" in Photos app | It's fundamentally an album record, not a file format |
| WeChat articles not supported | Requires a separate parser, not yet implemented |
| macOS only | App layer uses extensive AppKit / PhotoKit / Vision native APIs |

---

## Installation

```bash
git clone https://github.com/dante329/MediaArchive.git
cd MediaArchive
./install.sh
```

`install.sh` auto-checks and installs dependencies: Chrome / Node / Python / Swift / ffmpeg / exiftool, pulls the Douyin downloader and applies patches, generates config, compiles and installs the app, registers the web service as a persistent daemon.

---

## Keyboard Shortcuts

| Shortcut | Action | Scope |
|---|---|---|
| `⌘]` | Archive clipboard | Global |
| `⇧⌘I` | Import all Live Photos to album | In-app |
| `⌘I` | Extract Live Photo from current item | In-app |
| `⌘E` | Extract audio from current item | In-app |
| `⌘O` | Open main window / Reveal in Finder | In-app |
| `⌘R` | Refresh page | In-app |
| `⌘,` | Settings | In-app |
| `⌘W` `⌘M` `⌃⌘F` | Close / Minimize / Fullscreen | Detail window |

All rebindable in Settings.

---

## License

MIT
