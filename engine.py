#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
media-archive 统一归档引擎

    engine.py <url>              # 解析并归档
    engine.py --clip             # 读取剪贴板里的链接
    engine.py --dry-run <url>    # 只解析，不下载
    engine.py --list [N]         # 列出最近归档
    engine.py --open             # 打开归档目录

归档结构：~/Media/Archive/{平台}/{博主}/{日期}_{标题}/
索引：~/Media/Archive/index.db（SQLite）+ 索引.csv
"""
import csv
import gzip
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

HOME = Path.home()
BASE = HOME / "bin" / "media-archive"
CFG_FILE = HOME / ".media-archive" / "config.json"


def load_cfg():
    try:
        return json.loads(CFG_FILE.read_text("utf-8"))
    except Exception:
        return {}


CFG = load_cfg()
ARCHIVE = Path(CFG.get("archive_dir") or (HOME / "Media" / "Archive"))
DB = ARCHIVE / "index.db"
CSV_OUT = ARCHIVE / "索引.csv"
DOUYIN_VENDOR = HOME / "bin" / "douyin-collect" / "vendor" / "douyin-ai-video-downloader"
YTDLP = shutil.which("yt-dlp") or str(HOME / "Library/Python/3.9/bin/yt-dlp")
FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
TG_DIR = HOME / ".media-archive"
TG_CFG = TG_DIR / "tg.json"
TG_SESSION = TG_DIR / "tg"

UA_DESK = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")
UA_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")

URL_RE = re.compile(r"https?://[^\s\"'<>，。、）)】\u3000]+")

PLATFORMS = [
    ("douyin", ["v.douyin.com", "douyin.com", "iesdouyin.com"], "抖音"),
    ("xiaohongshu", ["xhslink.com", "xhslink.cn", "xiaohongshu.com"], "小红书"),
    ("x", ["x.com", "twitter.com", "t.co"], "X"),
    ("bilibili", ["bilibili.com", "b23.tv", "bili2233.cn"], "B站"),
    ("youtube", ["youtube.com", "youtu.be"], "YouTube"),
    ("instagram", ["instagram.com", "instagr.am"], "Instagram"),
    ("weibo", ["weibo.com", "weibo.cn", "t.cn"], "微博"),
    ("reddit", ["reddit.com", "redd.it"], "Reddit"),
    ("telegram", ["t.me", "telegram.me", "telegram.org"], "Telegram"),
    ("tiktok", ["tiktok.com", "vm.tiktok.com"], "TikTok"),
]
NEED_PROXY = {"x", "youtube", "instagram", "reddit", "telegram", "tiktok"}


def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


# ---------------------------------------------------------------- 代理 / 网络
def detect_proxy():
    for port in (7897, 7890, 7891, 1087, 1080, 6152):
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.4)
            s.close()
            return "http://127.0.0.1:%d" % port
        except OSError:
            continue
    return None


PROXY = detect_proxy()


def http_get(url, ua=UA_DESK, timeout=25, proxy=None, referer=None):
    handlers = []
    px = proxy if proxy is not None else PROXY
    if px:
        handlers.append(urllib.request.ProxyHandler({"http": px, "https": px}))
    op = urllib.request.build_opener(*handlers)
    headers = {"User-Agent": ua, "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with op.open(req, timeout=timeout) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return raw, r.geturl()


def http_text(url, **kw):
    raw, final = http_get(url, **kw)
    return raw.decode("utf-8", "ignore"), final


def download(url, dest, ua=UA_DESK, referer=None, tries=3, timeout=120):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    for i in range(tries):
        try:
            raw, _ = http_get(url, ua=ua, referer=referer, timeout=timeout)
            dest.write_bytes(raw)
            if dest.stat().st_size > 512:
                return True
        except Exception as e:
            log("下载重试 %d/%d %s" % (i + 1, tries, e))
            time.sleep(1 + i)
    return False


# ---------------------------------------------------------------- 工具
def detect_platform(url):
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    for key, domains, cn in PLATFORMS:
        for d in domains:
            if host == d or host.endswith("." + d):
                return key, cn
    return "generic", "通用"


def pick_url(text):
    """从文案里挑出第一个"能识别平台"的链接，其次退回第一个链接"""
    found = []
    for m in URL_RE.finditer(text or ""):
        u = m.group(0).rstrip('.,);]，。、）】')
        found.append(u)
        if detect_platform(u)[0] != "generic":
            return u
    return found[0] if found else None


def safe(s, limit=40):
    s = re.sub(r'[\\/:*?"<>|\r\n\t#]', " ", s or "")
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit].strip() or "untitled"


def to_ts(v):
    try:
        v = int(v)
        if v > 10 ** 12:
            v //= 1000
        return v if v > 0 else 0
    except Exception:
        return 0


def empty_result(platform, cn, url):
    return {"platform": platform, "platform_cn": cn, "source_url": url,
            "author": "", "author_id": "", "author_url": "", "title": "", "desc": "",
            "aweme_id": "", "type": "", "created_ts": 0, "stats": {},
            "files": [], "extra": {}}


# ---------------------------------------------------------------- 抖音
def parse_douyin(url, wait=90, headed=False):
    res = empty_result("douyin", "抖音", url)
    script = DOUYIN_VENDOR / "scripts" / "get_video.sh"
    if not script.exists():
        raise RuntimeError("缺少抖音抓取器：%s" % script)
    meta_path = Path("/tmp/douyin_meta.json")
    try:
        meta_path.unlink()
    except FileNotFoundError:
        pass
    name = "ma_%s" % datetime.now().strftime("%Y%m%d_%H%M%S")
    cmd = ["/bin/bash", str(script), url, name, "--wait=%d" % wait]
    if headed:
        cmd.append("--headed")
    log("抖音：启动 Chrome CDP 抓取（30~120 秒）…")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900,
                          cwd=str(DOUYIN_VENDOR))
    files = sorted((DOUYIN_VENDOR / "downloads").glob("%s*" % name))
    if not files:
        tail = ((proc.stdout or "") + (proc.stderr or ""))[-600:]
        raise RuntimeError("抖音抓取失败：%s" % tail)
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text("utf-8"))
        except Exception:
            pass
    st = meta.get("statistics") or {}
    res.update({
        "author": meta.get("nickname", ""),
        "author_id": meta.get("unique_id", ""),
        "author_url": meta.get("home", ""),
        "title": meta.get("desc", ""),
        "desc": meta.get("desc", ""),
        "aweme_id": str(meta.get("aweme_id", "")),
        "type": "图文" if meta.get("type") == "图文" else "视频",
        "created_ts": to_ts(meta.get("create_time")),
        "stats": {"digg": st.get("digg_count"), "comment": st.get("comment_count"),
                  "collect": st.get("collect_count"), "share": st.get("share_count")},
        "files": [str(f) for f in files],
    })
    return res


# ---------------------------------------------------------------- 小红书
XHS_STATE_RE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>", re.S)


def parse_xhs(url):
    res = empty_result("xiaohongshu", "小红书", url)
    final = url
    try:
        _, final = http_get(url, ua=UA_MOBILE, timeout=20)
    except Exception as e:
        log("小红书短链展开失败：%s" % e)
    note_id = ""
    m = re.search(r"/(?:explore|discovery/item|item)/([0-9a-f]{16,32})", final)
    if m:
        note_id = m.group(1)
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(final).query)
    token = (qs.get("xsec_token") or [""])[0]
    source = (qs.get("xsec_source") or ["pc_share"])[0]
    if not note_id:
        raise RuntimeError("没能从小红书链接解析出笔记 ID：%s" % final)

    page = "https://www.xiaohongshu.com/discovery/item/%s?xsec_token=%s&xsec_source=%s" % (
        note_id, token, source)
    # 注意：桌面 UA 会被重定向到登录页，移动 UA 才返回完整 SSR 数据
    html, _ = http_text(page, ua=UA_MOBILE, referer="https://www.xiaohongshu.com/")
    m = XHS_STATE_RE.search(html)
    if not m:
        raise RuntimeError("小红书页面没有 __INITIAL_STATE__（可能需登录或触发风控）")
    raw = m.group(1).replace("undefined", "null")
    try:
        state = json.loads(raw)
    except Exception as e:
        raise RuntimeError("小红书状态 JSON 解析失败：%s" % e)

    note = ((((state.get("noteData") or {}).get("data") or {}).get("noteData")) or {})
    if not note:
        detail = ((state.get("note") or {}).get("noteDetailMap") or {}).get(note_id) or {}
        note = detail.get("note") or {}
    if not note:
        # 兜底：遍历找带 imageList / video 的节点
        def walk(n, d=0):
            if d > 8 or not isinstance(n, (dict, list)):
                return None
            if isinstance(n, dict) and ("imageList" in n or "video" in n) and "noteId" in n:
                return n
            it = n.values() if isinstance(n, dict) else n
            for v in it:
                r = walk(v, d + 1)
                if r:
                    return r
            return None
        note = walk(state) or {}
    if not note:
        raise RuntimeError("小红书笔记数据为空（可能被风控或笔记已删）")

    user = note.get("user") or {}
    uid = user.get("userId") or ""
    nick = user.get("nickName") or user.get("nickname") or user.get("nick_name") or ""
    media = []
    for im in note.get("imageList") or []:
        u = ""
        best = 0
        for info in im.get("infoList") or []:
            iu = info.get("url") or ""
            if not iu:
                continue
            if info.get("imageScene") == "H5_DTL":
                u = iu
                break
            if not u:
                u = iu
        if not u:
            u = im.get("urlDefault") or im.get("urlPre") or im.get("url") or ""
        live_url = ""
        for codec in ("h264", "h265", "h266", "av1"):
            lst = ((im.get("stream") or {}).get(codec)) or []
            if lst and lst[0].get("masterUrl"):
                live_url = lst[0]["masterUrl"]
                break
        if u:
            media.append({"image": u, "live": live_url})
    imgs = [m["image"] for m in media]
    video = ""
    vs = (((note.get("video") or {}).get("media") or {}).get("stream") or {})
    for codec in ("h264", "h265", "av1"):
        for s in vs.get(codec) or []:
            u = s.get("masterUrl") or ""
            if u:
                video = u
                break
        if video:
            break
    if not video:
        video = ((note.get("video") or {}).get("consumer") or {}).get("originVideoKey", "")

    res.update({
        "author": nick,
        "author_id": uid,
        "author_url": "https://www.xiaohongshu.com/user/profile/%s" % uid if uid else "",
        "title": (note.get("title") or note.get("desc") or "").strip()[:120],
        "desc": (note.get("desc") or "").strip(),
        "aweme_id": note_id,
        "type": "视频" if video else "图文",
        "created_ts": to_ts(note.get("time")),
        "stats": {"digg": (note.get("interactInfo") or {}).get("likedCount"),
                  "collect": (note.get("interactInfo") or {}).get("collectedCount")},
        "extra": {"images": imgs, "video": video, "media": media},
    })
    return res


# ---------------------------------------------------------------- Telegram
def parse_telegram(url):
    """t.me/频道/消息号 —— 走 MTProto（网页预览常被频道关闭，yt-dlp 也取不到）"""
    res = empty_result("telegram", "Telegram", url)
    m = re.search(r"t\.me/(?:s/)?([A-Za-z0-9_]{3,})/(\d+)", url)
    if not m:
        raise RuntimeError("不是单条帖子链接（需要 t.me/频道名/消息号）")
    channel, mid = m.group(1), int(m.group(2))
    if not TG_CFG.exists():
        raise RuntimeError(
            "Telegram 需要一次性配置：\n"
            "  1. 打开 https://my.telegram.org → API development tools → 拿 api_id / api_hash\n"
            "  2. 写入 %s：{\"api_id\": 123456, \"api_hash\": \"...\"}\n"
            "  3. 运行 /usr/bin/python3 ~/bin/media-archive/tg_setup.py 完成登录" % TG_CFG)
    try:
        from telethon.sync import TelegramClient
    except ImportError:
        raise RuntimeError("缺少 telethon：/usr/bin/python3 -m pip install --user telethon")
    cfg = json.loads(TG_CFG.read_text("utf-8"))
    client = TelegramClient(str(TG_SESSION), int(cfg["api_id"]), str(cfg["api_hash"]))
    client.connect()
    try:
        if not client.is_user_authorized():
            raise RuntimeError("Telegram 未登录：运行 /usr/bin/python3 ~/bin/media-archive/tg_setup.py")
        ent = client.get_entity(channel)
        msg = client.get_messages(ent, ids=mid)
        if not msg:
            raise RuntimeError("取不到该消息（已删除 / 频道不可访问）")
        msgs = [msg]
        if getattr(msg, "grouped_id", None):
            around = client.get_messages(ent, ids=list(range(max(1, mid - 12), mid + 13)))
            msgs = [x for x in around if x and getattr(x, "grouped_id", None) == msg.grouped_id] or [msg]
        tmp = Path(tempfile.mkdtemp(prefix="tg_"))
        files = []
        for x in msgs:
            if x.media:
                f = client.download_media(x, file=str(tmp) + "/")
                if f:
                    files.append(str(f))
        if not files:
            raise RuntimeError("这条消息没有可下载的媒体（纯文字？）")
        title = (getattr(msg, "message", "") or "").strip().splitlines()
        title = (title[0][:120] if title else "") or ("%s #%d" % (channel, mid))
        res.update({
            "author": getattr(ent, "title", "") or channel,
            "author_id": str(getattr(ent, "id", "") or channel),
            "author_url": "https://t.me/%s" % channel,
            "title": title,
            "desc": getattr(msg, "message", "") or "",
            "aweme_id": "%s/%d" % (channel, mid),
            "type": "视频" if any(
                (m2.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm")) for m2 in map(Path, files)) else "图文",
            "created_ts": to_ts(getattr(msg, "date", None).timestamp()) if getattr(msg, "date", None) else 0,
            "files": files,
        })
        return res
    finally:
        client.disconnect()


# ---------------------------------------------------------------- yt-dlp 通用
def ytdlp_json(url, proxy=True):
    cmd = [YTDLP, "-J", "--no-warnings", "--no-playlist", url]
    if proxy and PROXY:
        cmd += ["--proxy", PROXY]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if not p.stdout.strip():
        raise RuntimeError("yt-dlp 解析失败：%s" % (p.stderr or "")[-400:])
    return json.loads(p.stdout)


def parse_generic(url):
    key, cn = detect_platform(url)
    res = empty_result(key, cn, url)
    data = ytdlp_json(url)
    ts = to_ts(data.get("timestamp"))
    if not ts and data.get("upload_date"):
        try:
            ts = int(datetime.strptime(str(data["upload_date"]), "%Y%m%d").timestamp())
        except Exception:
            ts = 0
    author_url = data.get("uploader_url") or data.get("channel_url") or ""
    if key == "bilibili" and not author_url and data.get("uploader_id"):
        author_url = "https://space.bilibili.com/%s" % data["uploader_id"]
    if key == "x" and not author_url and data.get("uploader_id"):
        author_url = "https://x.com/%s" % data["uploader_id"]
    res.update({
        "author": data.get("uploader") or data.get("channel") or data.get("uploader_id") or "",
        "author_id": data.get("uploader_id") or data.get("channel_id") or "",
        "author_url": author_url,
        "title": (data.get("title") or data.get("description") or "").strip()[:120],
        "desc": data.get("description") or "",
        "aweme_id": str(data.get("id") or ""),
        "type": "视频",
        "created_ts": ts,
        "stats": {"digg": data.get("like_count"), "comment": data.get("comment_count"),
                  "collect": None, "share": None},
        "extra": {"extractor": data.get("extractor_key"), "web": data.get("webpage_url")},
    })
    return res


def download_generic(res, folder):
    cmd = [YTDLP, "--no-warnings", "--no-playlist",
           "-o", str(folder / "%(title).80B [%(id)s].%(ext)s"), res["source_url"]]
    if PROXY:
        cmd += ["--proxy", PROXY]
    if os.path.exists(FFMPEG):
        cmd += ["--ffmpeg-location", FFMPEG]
    log("yt-dlp 下载中…")
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    files = [str(f) for f in folder.glob("*") if f.is_file()
             and f.suffix.lower() in (".mp4", ".mkv", ".webm", ".m4a", ".mp3", ".jpg", ".jpeg", ".png", ".webp", ".gif")]
    if not files:
        raise RuntimeError("yt-dlp 下载失败：%s" % ((p.stderr or "")[-500:]))
    return files


# ---------------------------------------------------------------- 索引
def db_init():
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB))
    con.execute("""CREATE TABLE IF NOT EXISTS items(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        platform TEXT, platform_cn TEXT, author TEXT, author_id TEXT, author_url TEXT,
        title TEXT, item_id TEXT, type TEXT, created_ts INTEGER, saved_ts INTEGER,
        file_count INTEGER, folder TEXT, source_url TEXT,
        digg INTEGER, comment INTEGER, collect INTEGER, share INTEGER,
        UNIQUE(platform, item_id))""")
    con.commit()
    return con


def db_upsert(con, r, folder, file_count):
    con.execute("""INSERT OR REPLACE INTO items
        (platform, platform_cn, author, author_id, author_url, title, item_id, type,
         created_ts, saved_ts, file_count, folder, source_url, digg, comment, collect, share)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (r["platform"], r["platform_cn"], r["author"], r["author_id"], r["author_url"],
                 r["title"], r["aweme_id"], r["type"], r["created_ts"], int(time.time()),
                 file_count, str(folder), r["source_url"],
                 r["stats"].get("digg"), r["stats"].get("comment"),
                 r["stats"].get("collect"), r["stats"].get("share")))
    con.commit()


def export_csv():
    if not DB.exists():
        return
    con = sqlite3.connect(str(DB))
    rows = con.execute("""SELECT saved_ts, platform_cn, author, author_id, author_url, item_id,
                                 type, title, created_ts, digg, comment, collect, share,
                                 file_count, folder, source_url FROM items
                          ORDER BY saved_ts DESC""").fetchall()
    con.close()
    head = ["保存时间", "平台", "博主", "账号", "博主主页", "作品ID", "类型", "标题",
            "发布时间", "点赞", "评论", "收藏", "分享", "文件数", "目录", "原始链接"]
    with open(str(CSV_OUT), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(head)
        for row in rows:
            out = list(row)
            out[0] = datetime.fromtimestamp(row[0]).strftime("%Y-%m-%d %H:%M:%S") if row[0] else ""
            out[8] = datetime.fromtimestamp(row[8]).strftime("%Y-%m-%d %H:%M") if row[8] else ""
            w.writerow(out)


# ---------------------------------------------------------------- 归档
def archive_dir(r):
    pdir = ARCHIVE / r["platform_cn"]
    adir = pdir / safe(r["author"] or "未知博主", 30)
    d = datetime.fromtimestamp(r["created_ts"]).strftime("%Y%m%d") if r["created_ts"] \
        else datetime.now().strftime("%Y%m%d")
    return adir / ("%s_%s" % (d, safe(r["title"] or r["aweme_id"], 40)))


def move_files(paths, folder):
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for i, p in enumerate(paths, 1):
        p = Path(p)
        ext = p.suffix or ".mp4"
        name = "video%s" % ext if p.suffix.lower() in (".mp4", ".mkv", ".webm") else \
            ("%02d%s" % (i, ext) if len(paths) > 1 else "image%s" % ext)
        target = folder / name
        if target.exists():
            target = folder / ("%s_%d%s" % (target.stem, i, ext))
        shutil.move(str(p), str(target))
        out.append(str(target))
    return out


def download_media(r, folder):
    if r["platform"] == "douyin":
        return move_files(r["files"], folder)
    if r["platform"] == "xiaohongshu":
        ex = r.get("extra") or {}
        if ex.get("video"):
            dest = folder / "video.mp4"
            folder.mkdir(parents=True, exist_ok=True)
            if not download(ex["video"], dest, referer="https://www.xiaohongshu.com/"):
                raise RuntimeError("小红书视频下载失败")
            return [str(dest)]
        media = ex.get("media") or [{"image": u, "live": ""} for u in (ex.get("images") or [])]
        if not media:
            raise RuntimeError("小红书没有可下载的图片/视频")
        folder.mkdir(parents=True, exist_ok=True)
        out = []
        for i, m in enumerate(media, 1):
            dest = folder / ("%02d.jpg" % i)
            if download(m["image"], dest, referer="https://www.xiaohongshu.com/"):
                out.append(str(dest))
            if m.get("live"):
                lv = folder / ("%02d_live.mp4" % i)
                if download(m["live"], lv, referer="https://www.xiaohongshu.com/"):
                    out.append(str(lv))
        return out
    return download_generic(r, folder)


def save(r, dry_run=False):
    log("解析完成：%s ｜ %s ｜ %s" % (r["platform_cn"], r["author"] or "?", r["type"]))
    if dry_run:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0
    folder = archive_dir(r)
    files = download_media(r, folder)
    if not files:
        raise RuntimeError("没有下载到任何文件")
    (folder / "meta.json").write_text(json.dumps(r, ensure_ascii=False, indent=2), "utf-8")
    (folder / "来源.txt").write_text(
        "平台：%s\n博主：%s\n账号：%s\n主页：%s\n作品ID：%s\n类型：%s\n标题：%s\n"
        "发布时间：%s\n原始链接：%s\n" % (
            r["platform_cn"], r["author"], r["author_id"], r["author_url"], r["aweme_id"],
            r["type"], r["title"],
            datetime.fromtimestamp(r["created_ts"]).strftime("%Y-%m-%d %H:%M") if r["created_ts"] else "",
            r["source_url"]), "utf-8")
    con = db_init()
    db_upsert(con, r, folder, len(files))
    con.close()
    export_csv()
    log("归档：%s（%d 个文件）" % (folder, len(files)))
    return str(folder)


def parse(url, wait=90, headed=False):
    key, cn = detect_platform(url)
    log("平台识别：%s（%s）" % (cn, key))
    if key == "douyin":
        return parse_douyin(url, wait=wait, headed=headed)
    if key == "xiaohongshu":
        return parse_xhs(url)
    if key == "telegram" and re.search(r"t\.me/", url):
        return parse_telegram(url)
    return parse_generic(url)


def notify(title, text):
    try:
        subprocess.run(["osascript", "-e",
                        'display notification "%s" with title "%s"'
                        % (text.replace('"', "'"), title.replace('"', "'"))],
                       check=False, timeout=10)
    except Exception:
        pass


def list_recent(n=20):
    con = db_init()
    rows = con.execute("""SELECT saved_ts, platform_cn, author, type, title, file_count, folder
                          FROM items ORDER BY saved_ts DESC LIMIT ?""", (n,)).fetchall()
    con.close()
    if not rows:
        print("还没有归档记录")
        return
    for ts, cn, author, typ, title, fc, folder in rows:
        print("%s  %-5s %-16s %-4s %-40s %d 个  %s" % (
            datetime.fromtimestamp(ts).strftime("%m-%d %H:%M"), cn, (author or "?")[:16],
            typ or "", (title or "")[:40], fc, folder))


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if args[0] == "--list":
        list_recent(int(args[1]) if len(args) > 1 else 20)
        return 0
    if args[0] == "--open":
        subprocess.run(["open", str(ARCHIVE)])
        return 0
    dry = "--dry-run" in args
    headed = "--headed" in args
    wait = 90
    for a in args:
        if a.startswith("--wait="):
            wait = int(a.split("=", 1)[1])
    text = " ".join(a for a in args if not a.startswith("--"))
    if "--clip" in args or not text.strip():
        text = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout
    url = pick_url(text)
    if not url:
        log("没找到链接")
        return 2
    log("目标：%s%s" % (url, "（代理 %s）" % PROXY if PROXY else ""))
    try:
        r = parse(url, wait=wait, headed=headed)
        out = save(r, dry_run=dry)
        if not dry and isinstance(out, str):
            notify("已归档 · %s" % r["platform_cn"], "%s ／ %s" % (r["author"] or "?", r["type"]))
        return 0
    except Exception as e:
        log("失败：%s" % e)
        notify("归档失败", str(e)[:120])
        return 1


if __name__ == "__main__":
    sys.exit(main())
