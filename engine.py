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
import threading
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

HOME = Path.home()
BASE = Path(__file__).resolve().parent
CFG_FILE = HOME / ".media-archive" / "config.json"


def load_cfg():
    try:
        return json.loads(CFG_FILE.read_text("utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:
        print("[warn] 配置加载失败：%s" % e, file=sys.stderr)
        return {}


CFG = load_cfg()
ARCHIVE = Path(CFG.get("archive_dir") or (HOME / "Media" / "Archive"))
DB = ARCHIVE / "index.db"
CSV_OUT = ARCHIVE / "索引.csv"
DOUYIN_VENDOR = BASE / "vendor" / "douyin-ai-video-downloader"
YTDLP = (shutil.which("yt-dlp")
         or next((p for p in [
             str(HOME / "Library/Python/3.10/bin/yt-dlp"),
             str(HOME / "Library/Python/3.9/bin/yt-dlp"),
             "/opt/homebrew/bin/yt-dlp",
             "/usr/local/bin/yt-dlp"] if os.path.exists(p)),
             str(HOME / "Library/Python/3.10/bin/yt-dlp")))
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
    ("threads", ["threads.net", "threads.com"], "Threads"),
    ("niconico", ["nicovideo.jp", "nico.ms"], "Niconico"),
    ("vimeo", ["vimeo.com"], "Vimeo"),
    ("dailymotion", ["dailymotion.com", "dai.ly"], "Dailymotion"),
    ("streamable", ["streamable.com"], "Streamable"),
    ("newgrounds", ["newgrounds.com"], "Newgrounds"),
    ("tumblr", ["tumblr.com"], "Tumblr"),
    ("pinterest", ["pinterest.com", "pin.it"], "Pinterest"),
    ("vk", ["vk.com", "vkvideo.ru"], "VK"),
    ("okru", ["ok.ru", "odnoklassniki.ru"], "OK.ru"),
    ("twitch", ["twitch.tv"], "Twitch"),
    ("soundcloud", ["soundcloud.com", "snd.sc"], "SoundCloud"),
    ("apple_podcasts", ["podcasts.apple.com"], "Apple Podcasts"),
    ("wechat", ["mp.weixin.qq.com", "weixin.qq.com"], "微信公众号"),
]
NODE = (shutil.which("node")
        or "/opt/homebrew/bin/node"
        or "/usr/local/bin/node")


def ytdlp_cookie_args():
    args = []
    browser = os.environ.get("MA_YTDLP_COOKIES_BROWSER")
    if browser:
        args += ["--cookies-from-browser", browser]
    return args


NEED_PROXY = {"x", "youtube", "instagram", "reddit", "telegram", "tiktok"}


PROGRESS_PATH = os.environ.get("MA_PROGRESS_FILE") or None
_last_progress_write = 0.0


def _report(phase, done=0, total=0, **extra):
    """把下载进度写到文件，供 server 实时读取"""
    global _last_progress_write
    if not PROGRESS_PATH:
        return
    now = time.time()
    # 非 download 阶段（connect/verify/done/error）总是立即写；download 阶段节流 500ms
    if phase == "download" and (now - _last_progress_write) < 0.5:
        return
    try:
        obj = {"phase": phase, "done": done, "total": total, "ts": now}
        obj.update(extra)
        tmp = PROGRESS_PATH + ".tmp"
        Path(tmp).write_text(json.dumps(obj), "utf-8")
        os.rename(tmp, PROGRESS_PATH)
        _last_progress_write = now
    except Exception:
        pass


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

_shared_opener = None


def _get_opener(proxy=None):
    global _shared_opener
    if _shared_opener is None or proxy:
        handlers = []
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        _shared_opener = urllib.request.build_opener(*handlers)
    return _shared_opener


def http_get(url, ua=UA_DESK, timeout=25, proxy=None, referer=None):
    px = proxy if proxy is not None else PROXY
    op = _get_opener(px)
    headers = {"User-Agent": ua, "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with op.open(req, timeout=timeout) as r:
        chunks = []
        total = 0
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > 50 * 1024 * 1024:
                raise RuntimeError("响应超过 50MB 上限")
            chunks.append(chunk)
        raw = b"".join(chunks)
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return raw, r.geturl()


def http_text(url, **kw):
    raw, final = http_get(url, **kw)
    return raw.decode("utf-8", "ignore"), final


def _verify_media(path):
    """确认下载到的文件真的能解码，而不是错误页/半截文件"""
    ext = path.suffix.lower()
    size = path.stat().st_size
    if size < 4096:
        return False, "文件过小（%d 字节）" % size
    # 检查是不是 HTML/JSON 错误页伪装成媒体
    try:
        head = path.open("rb").read(64)
        if head[:1] in (b"<", b"{") and not head.startswith(b"\xff\xd8"):
            snippet = head.decode("utf-8", "ignore")[:60]
            return False, "内容是文本不是媒体：%s" % snippet.replace("\n", " ")
    except Exception:
        pass
    if ext in (".mp4", ".mov", ".m4v", ".webm", ".mkv", ".m4a", ".mp3"):
        if not os.path.exists(FFMPEG):
            return True, ""      # 没 ffprobe 就没法验，放行
        try:
            r = subprocess.run([FFMPEG, "-v", "error", "-i", str(path),
                                "-f", "null", "-"], capture_output=True, timeout=90,
                               stdin=subprocess.DEVNULL)
            if r.returncode != 0:
                err = (r.stderr or b"").decode("utf-8", "ignore")[:120]
                return False, "解码失败：%s" % err
        except Exception as e:
            return False, "校验异常：%s" % e
    elif ext in (".jpg", ".jpeg", ".png", ".webp", ".heic"):
        try:
            from PIL import Image
            with Image.open(path) as im:
                im.verify()
        except ImportError:
            pass
        except Exception as e:
            return False, "图片损坏：%s" % e
    return True, ""


def check_disk_space(need_bytes, folder):
    """归档前确认剩余空间够"""
    if not need_bytes or need_bytes <= 0:
        return True
    try:
        st = os.statvfs(str(folder))
        free = st.f_bavail * st.f_frsize
        # 预留 3 倍（转码临时文件 + 后续操作）
        if free < need_bytes * 3:
            log("磁盘空间不足：需要约 %.1f GB，剩余 %.1f GB"
                % (need_bytes * 3 / 1073741824, free / 1073741824))
            return False
    except Exception:
        pass
    return True


def download(url, dest, ua=UA_DESK, referer=None, tries=3, timeout=120,
             expect_size=None):
    """原子下载 + 实时进度：写 .part → 分块读 → 校验 → rename"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    for i in range(tries):
        tmp = dest.with_name(dest.name + ".part")
        try:
            handlers = []
            if PROXY:
                handlers.append(urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
            op = urllib.request.build_opener(*handlers)
            headers = {"User-Agent": ua, "Accept": "*/*",
                       "Accept-Language": "zh-CN,zh;q=0.9"}
            if referer:
                headers["Referer"] = referer
            req = urllib.request.Request(url, headers=headers)
            _report("connect", 0, 0, file=dest.name)
            with op.open(req, timeout=timeout) as r:
                total = int(r.headers.get("Content-Length") or expect_size or 0)
                done = 0
                t0 = time.time()
                with tmp.open("wb") as f:
                    while True:
                        buf = r.read(256 * 1024)
                        if not buf:
                            break
                        f.write(buf)
                        done += len(buf)
                        el = max(0.05, time.time() - t0)
                        # 平均速度（更稳，避免抖动）
                        speed = done / el
                        eta = max(0, (total - done) / speed) if (total and speed > 0) else 0
                        _report("download", done, total,
                                file=dest.name, speed=speed, eta=eta)
            _report("verify", done, total, file=dest.name)
            if expect_size and total and abs(done - expect_size) > max(4096, expect_size * 0.02):
                raise RuntimeError("大小不符：期望 %d 实际 %d" % (expect_size, done))
            ok, why = _verify_media(tmp)
            if not ok:
                tmp.unlink(missing_ok=True)
                raise RuntimeError(why)
            os.replace(tmp, dest)
            _report("done", done, total, file=dest.name)
            return True
        except Exception as e:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            _report("error", 0, 0, file=dest.name, message=str(e)[:120])
            log("下载重试 %d/%d %s" % (i + 1, tries, e))
            time.sleep(min(30, 2**i * 5))
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
    s = s[:limit].strip()
    while len(s.encode("utf-8")) > 200:
        s = s[:-1]
    return s or "untitled"


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
    """抖音：页面内 API（页面 SDK 签名），直接拿详情，不再开播放器等媒体。"""
    res = empty_result("douyin", "抖音", url)
    if not NODE:
        raise RuntimeError("缺少 node，无法调用抖音页面内 API")
    out = Path("/tmp/ma-douyin-%d.json" % int(time.time() * 1000))
    cmd = [NODE, str(BASE / "tools" / "douyin_api.js"), "detail", url, str(out), "9222"]
    log("抖音：页面内 API 解析…")
    _report("parse", 0, 0, step="解析中")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise RuntimeError("抖音解析超时（页面可能要求登录/验证）")
    if not out.exists():
        raise RuntimeError("抖音解析失败：%s" % (((r.stderr or "") + (r.stdout or ""))[-400:]))
    try:
        d = json.loads(out.read_text("utf-8"))
    finally:
        try:
            out.unlink()
        except Exception:
            pass
    if not d.get("id"):
        raise RuntimeError("抖音解析没有返回作品")
    total = int(d.get("data_size") or 0)
    st = d.get("stats") or {}
    res.update({
        "author": d.get("author") or "",
        "author_id": d.get("author_id") or d.get("unique_id") or "",
        "author_url": d.get("author_url") or "",
        "title": (d.get("title") or "").strip()[:120],
        "desc": d.get("title") or "",
        "aweme_id": str(d.get("id") or ""),
        "type": d.get("type") or ("图文" if d.get("images") else "视频"),
        "created_ts": to_ts(d.get("created")),
        "stats": {"digg": st.get("digg_count"), "comment": st.get("comment_count"),
                  "collect": st.get("collect_count"), "share": st.get("share_count")},
        "extra": {"video": d.get("video") or "", "images": d.get("images") or [],
                  "cover": d.get("cover") or "", "duration": (d.get("duration") or 0) / 1000.0,
                  "video_size": total},
    })
    if total:
        _report("parse", 0, total, step="解析完成", filesize=total)
    log("解析完成：抖音 ｜ %s ｜ %s" % (res["author"] or "?", res["type"]))
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
        # 优先用 fileId 拼无水印原图；H5 系列的 URL 都带平台水印
        fid = (im.get("fileId") or "").strip()
        if not fid:
            # 老接口有时只给 H5 地址：从 .../notes_pre_post/<id>!h5_... 里抠出 fileId
            for info in im.get("infoList") or []:
                m2 = re.search(r"(notes_pre_post/[A-Za-z0-9]+)", info.get("url") or "")
                if m2:
                    fid = m2.group(1)
                    break
        if not fid:
            tr = (im.get("traceId") or "").strip()
            if tr and not tr.startswith("http"):
                fid = tr
        if fid:
            u = "https://ci.xiaohongshu.com/%s" % fid
        if not u:
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
    cmd += ytdlp_cookie_args()
    if proxy and PROXY:
        cmd += ["--proxy", PROXY]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    if p.returncode != 0:
        tail = (p.stderr or p.stdout or "")[-400:]
        hint = ""
        low = tail.lower()
        if any(kw in low for kw in ("geoblocked", "unavailable", "not available in your country")):
            hint = "（可能需要切换代理节点）"
        raise RuntimeError("yt-dlp 解析失败(returncode=%d)：%s%s" % (p.returncode, tail, hint))
    if not p.stdout.strip():
        raise RuntimeError("yt-dlp 解析失败：%s" % (p.stderr or "")[-400:])
    data = json.loads(p.stdout)
    if not isinstance(data, dict):
        raise RuntimeError("yt-dlp 解析失败：%s" % ((p.stderr or p.stdout or "")[-400:]))
    return data


def parse_browser(url):
    """浏览器兜底：yt-dlp 没有 extractor 的页面（目前主要用于 Threads）。"""
    key, cn = detect_platform(url)
    res = empty_result(key, cn, url)
    if not NODE:
        raise RuntimeError("缺少 node，无法使用浏览器兜底")
    out = Path("/tmp/ma-browser-%d.json" % int(time.time() * 1000))
    cmd = [NODE, str(BASE / "tools" / "browser_media.js"), url, str(out), "9222"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise RuntimeError("浏览器兜底超时（页面可能要求登录/正在验证）")
    if not out.exists():
        raise RuntimeError("浏览器兜底失败：%s" % (((r.stderr or "") + (r.stdout or ""))[-300:]))
    try:
        data = json.loads(out.read_text("utf-8"))
    finally:
        try:
            out.unlink()
        except Exception:
            pass
    videos = [u for u in (data.get("videos") or []) if u and not u.startswith("blob:")]
    images = [u for u in (data.get("images") or []) if u and not u.startswith("blob:")]
    og = data.get("ogImage") or ""
    if og and og not in images:
        images.insert(0, og)
    if not videos and not images:
        raise RuntimeError("浏览器兜底没有抓到视频/图片（页面可能需要登录或正在验证）")
    handle = (data.get("handle") or "").strip()
    title = (data.get("title") or "").strip()
    # Threads 的 og:title 常带后缀
    title = re.sub(r"\s*[|·-]\s*Threads\s*$", "", title, flags=re.I) or title
    m = re.search(r"/post/([0-9A-Za-z_-]+)", url)
    res.update({
        "author": handle,
        "author_id": handle,
        "author_url": ("https://www.threads.net/@%s" % handle) if handle else "",
        "title": title[:120] or url,
        "desc": (data.get("description") or "")[:1000],
        "aweme_id": (m.group(1) if m else str(abs(hash(url)) % 10 ** 12)),
        "type": ("图文" if key == "wechat" and images else ("视频" if videos else "图片")),
        "created_ts": 0,
        "stats": {},
        "extra": {"browser_media": {"videos": videos[:10], "images": images[:20]},
                  "web": data.get("canonical") or url,
                  "thumbnail": og or (images[0] if images else ""),
                  "extractor": "browser"},
    })
    return res


def download_browser_media(r, folder):
    bm = (r.get("extra") or {}).get("browser_media") or {}
    videos = bm.get("videos") or []
    images = bm.get("images") or []
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    article = r.get("platform") == "wechat"
    if videos:
        u = videos[0]
        if u.split("?")[0].lower().endswith(".m3u8"):
            dest = folder / "video.mp4"
            cmd = [YTDLP, "-f", "bv*+ba/b", "--merge-output-format", "mp4",
                   "--no-playlist", "-o", str(dest), u, "--referer", r["source_url"]] + ytdlp_cookie_args()
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if dest.exists():
                out.append(str(dest))
        else:
            dest = folder / "video.mp4"
            if download(u, dest, referer=r["source_url"]):
                out.append(str(dest))
        if not out and len(videos) > 1:
            if download(videos[1], folder / "video.mp4", referer=r["source_url"]):
                out.append(str(folder / "video.mp4"))
    if article or not out:
        base = len(out)
        for i, u in enumerate(images[:30], 1):
            name = ("%02d.jpg" % (base + i)) if article else ("%02d.jpg" % i)
            dest = folder / name
            if download(u, dest, referer=r["source_url"]):
                out.append(str(dest))
    if not out:
        for i, u in enumerate(images[:20], 1):
            dest = folder / ("%02d.jpg" % i)
            if download(u, dest, referer=r["source_url"]):
                out.append(str(dest))
    return out


def parse_generic(url):
    key, cn = detect_platform(url)
    res = empty_result(key, cn, url)
    if key == "threads":
        raise RuntimeError("Threads 暂时没有 yt-dlp extractor，需要浏览器兜底")
    data = ytdlp_json(url)
    ts = to_ts(data.get("timestamp"))
    if not ts and data.get("upload_date"):
        try:
            ts = int(datetime.strptime(str(data["upload_date"]), "%Y%m%d").timestamp())
        except Exception:
            ts = 0
    if not ts and data.get("release_date"):
        try:
            ts = int(datetime.strptime(str(data["release_date"]), "%Y%m%d").timestamp())
        except Exception:
            ts = 0
    author_url = data.get("uploader_url") or data.get("channel_url") or data.get("creator_url") or ""
    uploader_id = data.get("uploader_id") or data.get("channel_id") or ""
    if not author_url and uploader_id:
        if key == "bilibili":
            author_url = "https://space.bilibili.com/%s" % uploader_id
        elif key in ("x", "twitter"):
            author_url = "https://x.com/%s" % uploader_id
        elif key == "tiktok":
            author_url = "https://www.tiktok.com/@%s" % uploader_id
        elif key == "soundcloud":
            author_url = "https://soundcloud.com/%s" % uploader_id
    ext = (data.get("ext") or "").lower()
    vcodec = (data.get("vcodec") or "").lower()
    extractor = str(data.get("extractor_key") or "").lower()
    if ext in ("jpg", "jpeg", "png", "webp", "gif"):
        typ = "图片"
    elif vcodec == "none" or ext in ("m4a", "mp3", "opus", "ogg", "flac", "wav") or "podcast" in extractor:
        typ = "音频"
    else:
        typ = "视频"
    res.update({
        "author": data.get("uploader") or data.get("channel") or data.get("creator") or uploader_id or "",
        "author_id": data.get("channel_id") or data.get("uploader_id") or "",
        "author_url": author_url,
        "title": (data.get("title") or data.get("track") or data.get("description") or "").strip()[:120],
        "desc": data.get("description") or "",
        "aweme_id": str(data.get("id") or ""),
        "type": typ,
        "created_ts": ts,
        "stats": {"digg": data.get("like_count"), "comment": data.get("comment_count"),
                  "collect": None, "share": None},
        "extra": {"extractor": data.get("extractor_key"), "web": data.get("webpage_url"),
                  "duration": data.get("duration"), "thumbnail": data.get("thumbnail"),
                  "width": data.get("width"), "height": data.get("height")},
    })
    return res


_SIZE_UNITS = {
    "B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3, "TiB": 1024 ** 4,
    "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3, "TB": 1000 ** 4,
}


def _to_bytes(txt):
    """把 '15.23MiB' / '2.1MB' 转成字节数"""
    if not txt:
        return 0
    import re as _re
    m = _re.match(r"([\d.]+)\s*([A-Za-z]+)", txt.strip())
    if not m:
        return 0
    try:
        return int(float(m.group(1)) * _SIZE_UNITS.get(m.group(2), 1))
    except Exception:
        return 0


def _watch_ytdlp(proc):
    """逐行读 yt-dlp 输出，解析真实下载进度并上报"""
    import re as _re
    pat = _re.compile(
        r"\[download\]\s+([\d.]+)%\s+of\s+~?\s*([\d.]+\s*[A-Za-z]+)"
        r"(?:\s+at\s+([\d.]+\s*[A-Za-z]+)/s)?"
        r"(?:\s+ETA\s+([\d:]+))?")
    tail = []
    try:
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            tail.append(line)
            if len(tail) > 80:
                tail.pop(0)
            m = pat.search(line)
            if m:
                pct = float(m.group(1))
                total = _to_bytes(m.group(2))
                done = int(total * pct / 100.0)
                speed = _to_bytes(m.group(3)) if m.group(3) else 0
                eta_s = 0
                if m.group(4):
                    parts = [int(x) for x in m.group(4).split(":") if x.isdigit()]
                    for v in parts:
                        eta_s = eta_s * 60 + v
                _report("download", done, total, step="下载视频",
                        speed=speed, eta=eta_s, tool="yt-dlp")
            elif "[ExtractAudio]" in line or "[Merger]" in line or "[FixupM3u8]" in line:
                _report("parse", 0, 0, step="转码/合并")
            elif "[Destination]" in line:
                _report("parse", 0, 0, step="写入文件")
    except Exception:
        pass
    return "\n".join(tail)


def download_generic(res, folder):
    audio = res.get("type") == "音频"
    image = res.get("type") == "图片"
    fmt = "b" if image else ("ba/b" if audio else "bv*+ba/b")
    cmd = [YTDLP, "--no-warnings", "--no-playlist", "--newline", "--progress",
           "-f", fmt, "--merge-output-format", "mp4",
           "-o", str(folder / "%(title).80B [%(id)s].%(ext)s"), res["source_url"]]
    if res.get("type") in ("音频", "图片"):
        cmd += ["--write-thumbnail", "--convert-thumbnails", "jpg"]
    cmd += ytdlp_cookie_args()
    if PROXY:
        cmd += ["--proxy", PROXY]
    if os.path.exists(FFMPEG):
        cmd += ["--ffmpeg-location", FFMPEG]
    log("yt-dlp 下载中…")
    _report("parse", 0, 0, step="连接中")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    out_tail = _watch_ytdlp(proc)
    try:
        proc.wait(timeout=1800)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise RuntimeError("yt-dlp 下载超时")
    _report("verify", 0, 0, step="校验中")
    p = type("R", (), {"returncode": proc.returncode,
                       "stdout": out_tail, "stderr": ""})()
    files = [str(f) for f in folder.glob("*") if f.is_file()
             and f.suffix.lower() in (".mp4", ".mkv", ".webm", ".m4a", ".mp3", ".jpg", ".jpeg", ".png", ".webp", ".gif")]
    if not files:
        tail = (out_tail or p.stderr or "")[-500:]
        hint = ""
        low = tail.lower()
        if any(kw in low for kw in ("geoblocked", "unavailable", "not available in your country")):
            hint = "（可能需要切换代理节点）"
        raise RuntimeError("yt-dlp 下载失败：%s%s" % (tail, hint))
    return files


# ---------------------------------------------------------------- 索引
def db_init():
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB))
    con.execute("PRAGMA journal_mode=WAL")
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
    # 解析失败时不要留空，否则界面上会出现「?」这种无意义的博主
    if not (r.get("author") or "").strip():
        r["author"] = folder.parent.name if folder.parent.name else "未知博主"
    if not (r.get("title") or "").strip():
        r["title"] = "（无标题）"
    # 用 UPSERT 而不是 REPLACE：REPLACE 会先删再插，主键 id 会漂移
    # 一旦 id 变了，已打开的窗口、AI 分析关联、外部引用全部失效
    con.execute("""INSERT INTO items
        (platform, platform_cn, author, author_id, author_url, title, item_id, type,
         created_ts, saved_ts, file_count, folder, source_url, digg, comment, collect, share)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(platform, item_id) DO UPDATE SET
            platform_cn=excluded.platform_cn,
            author=excluded.author,
            author_id=excluded.author_id,
            author_url=excluded.author_url,
            title=excluded.title,
            type=excluded.type,
            created_ts=excluded.created_ts,
            saved_ts=excluded.saved_ts,
            file_count=excluded.file_count,
            folder=excluded.folder,
            source_url=excluded.source_url,
            digg=excluded.digg,
            comment=excluded.comment,
            collect=excluded.collect,
            share=excluded.share""",
                (r["platform"], r["platform_cn"], r["author"], r["author_id"], r["author_url"],
                 r["title"], r["aweme_id"], r["type"], r["created_ts"], int(time.time()),
                 file_count, str(folder), r["source_url"],
                 r["stats"].get("digg"), r["stats"].get("comment"),
                 r["stats"].get("collect"), r["stats"].get("share")))
    # 同一博主（稳定 ID）改名后，历史作品同步用最新名字
    aid = (r.get("author_id") or "").strip()
    name = (r.get("author") or "").strip()
    if aid and name and name != "未知博主":
        con.execute("UPDATE items SET author=? WHERE platform=? AND author_id=? AND author<>?",
                    (name, r["platform"], aid, name))
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
    total = len(paths)
    for i, p in enumerate(paths, 1):
        _report("download", i, total, step="保存文件", speed=0)
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
    if (r.get("extra") or {}).get("browser_media"):
        return download_browser_media(r, folder)
    if r["platform"] == "douyin":
        ex = r.get("extra") or {}
        folder.mkdir(parents=True, exist_ok=True)
        out = []
        video = ex.get("video")
        if video:
            dest = folder / "video.mp4"
            if download(video, dest, referer="https://www.douyin.com/"):
                out.append(str(dest))
        for i, u in enumerate(ex.get("images") or [], 1):
            dest = folder / ("%02d.jpg" % i)
            if download(u, dest, referer="https://www.douyin.com/"):
                out.append(str(dest))
        if out:
            return out
        return move_files(r["files"], folder) if r.get("files") else []
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
        mode = (CFG.get("livephoto") or {}).get("mode", "both")
        folder.mkdir(parents=True, exist_ok=True)
        out, pairs = [], []
        for i, m in enumerate(media, 1):
            img_path = None
            if mode in ("both", "image", "live"):
                img_path = folder / ("%02d.jpg" % i)
                if download(m["image"], img_path, referer="https://www.xiaohongshu.com/"):
                    out.append(str(img_path))
                else:
                    img_path = None
            if m.get("live") and mode in ("both", "video", "live"):
                suffix = "_live.mp4" if mode != "live" else ".mp4"
                lv = folder / ("%02d%s" % (i, suffix))
                if download(m["live"], lv, referer="https://www.xiaohongshu.com/"):
                    out.append(str(lv))
                    if mode == "live" and img_path:
                        pairs.append((img_path, lv, i))
        # 实况照片模式：合成 jpg + mov 配对，删掉原始 mp4
        if mode == "live" and pairs:
            try:
                import livephoto
                for img, vid, idx in pairs:
                    ni, nv = livephoto.make_pair(img, vid, folder, "%02d" % idx)
                    out.append(str(nv))
                    try:
                        vid.unlink()
                    except Exception:
                        pass
                    out = [x for x in out if x != str(vid)]
                log("已合成 %d 组实况照片" % len(pairs))
            except Exception as e:
                log("实况照片合成失败（保留原始 mp4）：%s" % e)
        return out
    return download_generic(r, folder)


def save(r, dry_run=False):
    log("解析完成：%s ｜ %s ｜ %s" % (r["platform_cn"], r["author"] or "?", r["type"]))
    if dry_run:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0
    folder = archive_dir(r)
    # 归档前先确认磁盘空间够（预估值按平台给个下限）
    est = 300 * 1024 * 1024
    try:
        if r["platform"] == "douyin":
            est = int((r.get("extra") or {}).get("video_size") or est)
    except Exception:
        pass
    if not check_disk_space(est, folder.parent if folder.parent.exists() else HOME):
        raise RuntimeError("磁盘空间不足，已中止归档")
    # 已存在且文件还在 → 完全不重复下载
    try:
        _c = db_init()
        _ex = _c.execute("SELECT title, folder FROM items WHERE platform=? AND item_id=?",
                         (r["platform"], r["aweme_id"])).fetchone()
        _c.close()
        if _ex:
            # db_init() 返回的 row 是 tuple，不是 dict
            old_title = (_ex[0] or r["title"])
            old_folder = Path(_ex[1] or "")
            has_files = False
            if old_folder.exists():
                has_files = any(
                    f for f in old_folder.iterdir()
                    if f.is_file() and f.suffix.lower() not in (".json", ".txt")
                    and not f.name.endswith(".poster.jpg"))
            if has_files:
                log("已存在：%s，跳过下载" % old_title)
                DUP_FLAG[0] = True
                print("EXIST_SKIP	" + old_title, flush=True)
                return str(old_folder)
            log("记录存在但文件缺失，重新下载：%s" % old_title)
    except Exception as e:
        log("已存在检查异常（继续下载）：%s" % e)
    files = download_media(r, folder)
    if not files:
        raise RuntimeError("没有下载到任何文件")
    extras = folder / "_extras"
    extras.mkdir(parents=True, exist_ok=True)
    (extras / "meta.json").write_text(json.dumps(r, ensure_ascii=False, indent=2), "utf-8")
    (extras / "来源.txt").write_text(
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
    if key in ("threads", "wechat"):
        return parse_browser(url)
    return parse_generic(url)


DUP_FLAG = [False]


def want_notify(kind):
    """kind: start / done / exists —— 由设置里的开关决定"""
    n = (CFG.get("notify") or {})
    return bool(n.get(kind, True))


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
    _proxy_display = ""
    if PROXY:
        try:
            _pu = urllib.parse.urlparse(PROXY)
            _proxy_display = "（代理 %s://%s:%s）" % (_pu.scheme, _pu.hostname or "?", _pu.port or "?")
        except Exception:
            _proxy_display = "（代理已启用）"
    log("目标：%s%s" % (url, _proxy_display))

    # 解析之前先按原始链接查一遍：命中就秒回，不用启动浏览器
    try:
        _c = db_init()
        _hit = _c.execute(
            "SELECT title, folder FROM items WHERE source_url=?", (url,)).fetchone()
        _c.close()
        if _hit:
            _f = Path(_hit[1] or "")
            _alive = _f.exists() and any(
                x for x in _f.iterdir()
                if x.is_file() and x.suffix.lower() not in (".json", ".txt")
                and not x.name.endswith(".poster.jpg"))
            if _alive:
                log("已存在：%s，直接跳过（未解析）" % (_hit[0] or "该作品"))
                print("EXIST_SKIP\t" + (_hit[0] or ""), flush=True)
                if want_notify("exists"):
                    notify("已存在", _hit[0] or "该作品")
                return 0
    except Exception as e:
        log("URL 预检异常（继续）：%s" % e)

    try:
        r = parse(url, wait=wait, headed=headed)
        out = save(r, dry_run=dry)
        if not dry and isinstance(out, str):
            # 重复归档不再弹系统通知，页面里已经有轻提示
            _dup = DUP_FLAG[0]
            if not _dup and want_notify("done"):
                notify("已归档 · %s" % r["platform_cn"],
                       "%s ／ %s" % (r["author"] or "?", r["type"]))
        return 0
    except Exception as e:
        log("失败：%s" % e)
        notify("归档失败", str(e)[:120])
        return 1


if __name__ == "__main__":
    sys.exit(main())
