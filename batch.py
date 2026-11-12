"""批量归档：登录态、收藏/喜欢列表抓取、批量入队。"""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
TOOLS = BASE / "tools"
VENDOR = BASE / "vendor" / "douyin-ai-video-downloader"
ARCHIVE = Path(os.environ.get("MA_ARCHIVE") or (Path.home() / "Media" / "Archive"))
DB = ARCHIVE / "index.db"
PORT = os.environ.get("DOUYIN_PORT") or "9222"
CACHE_DIR = Path.home() / ".media-archive" / "sources"

PLATFORM_DB = {"douyin": "douyin", "xhs": "xiaohongshu", "youtube": "youtube",
               "bilibili": "bilibili", "x": "x", "instagram": "instagram", "weibo": "weibo"}
PLATFORM_NAME = {"douyin": "抖音", "xhs": "小红书"}
LOGIN_SCRIPT = {
    "douyin": VENDOR / "scripts" / "login.sh",
    "xhs": TOOLS / "login_xhs.sh",
}
LOGIN_CHECK = {
    "douyin": VENDOR / "scripts" / "check_login.js",
    "xhs": TOOLS / "check_login_xhs.js",
}


_cache_locks = {}
_cache_locks_guard = threading.Lock()


def _get_cache_lock(key):
    with _cache_locks_guard:
        if key not in _cache_locks:
            _cache_locks[key] = threading.Lock()
        return _cache_locks[key]


def _run(cmd, timeout, label=""):
    """统一封装 subprocess.run，捕获 TimeoutExpired。"""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(BASE))
    except subprocess.TimeoutExpired:
        raise RuntimeError("%s超时（%d秒）" % (label or "命令执行", timeout))


def _sanitize_url(url):
    """去除 URL 中的控制字符和换行。"""
    return re.sub(r'[\x00-\x1f\x7f]', '', (url or '').strip())


def _node():
    return shutil.which("node") or "node"


def _ytdlp():
    for cand in (shutil.which("yt-dlp"),
                 str(Path.home() / "Library/Python/3.10/bin/yt-dlp"),
                 str(Path.home() / "Library/Python/3.9/bin/yt-dlp"),
                 "/opt/homebrew/bin/yt-dlp", "/usr/local/bin/yt-dlp"):
        if cand and Path(cand).exists():
            return cand
    return "yt-dlp"


def _ytdlp_cookie_args():
    args = []
    try:
        cfgp = Path.home() / ".media-archive" / "config.json"
        cfg = json.loads(cfgp.read_text("utf-8")) if cfgp.exists() else {}
        if cfg.get("ytdlp_use_browser_cookies"):
            args += ["--cookies-from-browser", "chrome"]
    except Exception:
        pass
    return args


def detect_list_platform(url):
    u = (url or "").strip()
    if u and "://" not in u:
        u = "https://" + u
    host = u.split("://")[-1].split("/")[0].lower()
    if host.endswith("douyin.com") or host in ("v.douyin.com",):
        return "douyin"
    if host.endswith("youtube.com") or host in ("youtu.be",):
        return "youtube"
    if host.endswith("bilibili.com") or host in ("b23.tv",):
        return "bilibili"
    if host.endswith("x.com") or host.endswith("twitter.com"):
        return "x"
    if host.endswith("instagram.com"):
        return "instagram"
    if host.endswith("weibo.com") or host.endswith("weibo.cn"):
        return "weibo"
    return ""


def flat_list(platform, url, limit=100):
    """YouTube/B站：yt-dlp flat playlist，一页拿一批，不滚动页面。"""
    url = _sanitize_url(url)
    cmd = [_ytdlp(), "-J", "--flat-playlist", "--playlist-end", str(limit),
           "--no-warnings", url] + _ytdlp_cookie_args()
    r = _run(cmd, timeout=180, label="%s flat_list" % platform)
    if r.returncode != 0 or not r.stdout.strip():
        tail = ((r.stderr or "") + (r.stdout or ""))[-400:].strip()
        if "412" in tail or "blocked" in tail.lower():
            raise RuntimeError("%s 拒绝访问（412）：需要登录态或换时间再试；可在设置里打开「允许 yt-dlp 使用本机 Chrome 登录态」" % platform)
        if "login" in tail.lower() or "sign in" in tail.lower():
            raise RuntimeError("%s 需要登录：请在设置里打开 yt-dlp 本机 Chrome 登录态，或先在 App 浏览器登录" % platform)
        raise RuntimeError("%s 列表读取失败：%s" % (platform, tail[:300]))
    try:
        data = json.loads(r.stdout)
    except Exception:
        raise RuntimeError("%s 列表返回非 JSON" % platform)
    entries = data.get("entries") or []
    if not entries and data.get("id"):
        entries = [data]
    items = []
    for e in entries:
        eid = str(e.get("id") or "")
        if not eid:
            continue
        u = e.get("webpage_url") or e.get("url") or ""
        if platform == "youtube" and not (u or "").startswith("http"):
            u = "https://www.youtube.com/watch?v=" + eid
        if platform == "bilibili" and not (u or "").startswith("http"):
            u = "https://www.bilibili.com/video/" + eid
        thumbs = e.get("thumbnails") or []
        cover = ""
        if thumbs:
            cover = thumbs[-1].get("url") or ""
        if not cover:
            cover = e.get("thumbnail") or ""
        items.append({"id": eid, "url": u, "cover": cover,
                      "title": (e.get("title") or eid).strip(),
                      "type": "视频"})
    return items


def login_state(platform):
    """查 Chrome 里是否已登录某平台。返回 False / True / 昵称字符串。"""
    script = LOGIN_CHECK.get(platform)
    if not script or not script.exists():
        return False
    try:
        r = subprocess.run([_node(), str(script), PORT], capture_output=True,
                           text=True, timeout=15, cwd=str(BASE))
    except subprocess.TimeoutExpired:
        print("[warn] 登录检查超时：%s" % platform, file=sys.stderr)
        return False
    except FileNotFoundError:
        print("[warn] node 未找到，无法检查登录态", file=sys.stderr)
        return False
    except Exception as e:
        print("[warn] 登录检查异常：%s" % e, file=sys.stderr)
        return False
    first = (r.stdout or "").strip().splitlines()[0:1]
    if not first or not first[0].startswith("logged_in"):
        return False
    parts = first[0].split("\t", 1)
    return parts[1] if len(parts) > 1 and parts[1] else True


def login_script(platform):
    s = LOGIN_SCRIPT.get(platform)
    return str(s) if s and s.exists() else ""


def _cache_path(platform, kind, url=None):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    suffix = ""
    if url:
        suffix = "_" + hashlib.md5(url.encode()).hexdigest()[:8]
    return CACHE_DIR / ("%s_%s%s.json" % (platform, kind, suffix))


def _archived_ids(platform):
    try:
        con = sqlite3.connect(str(DB))
        rows = con.execute("SELECT item_id FROM items WHERE platform=?",
                           (PLATFORM_DB.get(platform, platform),)).fetchall()
        con.close()
        return {str(r[0]) for r in rows}
    except Exception as e:
        print("[warn] 查询已归档 ID 失败：%s" % e, file=sys.stderr)
        return set()


def fetch_items(platform, kind, url=None, refresh=False, max_age=600, limit=0):
    """抓收藏/喜欢列表；带磁盘缓存，并标记哪些已归档。"""
    url = _sanitize_url(url)
    if kind == "list":
        p = platform if platform and platform != "auto" else detect_list_platform(url)
        if not p:
            raise ValueError("无法识别这个链接的平台")
        cp = _cache_path(p, "list", url)
        _lk = _get_cache_lock("%s_%s_%s" % (p, "list", url or ""))
        with _lk:
            if not refresh and cp.exists() and time.time() - cp.stat().st_mtime < max_age:
                try:
                    data = json.loads(cp.read_text("utf-8"))
                    return _annotate(data, p, cached=True, limit=limit)
                except Exception:
                    pass
            if p == "douyin":
                out = cp.with_name(cp.stem + ".tmp-%d.json" % os.getpid())
                r = _run([_node(), str(TOOLS / "douyin_api.js"), "user", url, str(out), PORT],
                         timeout=240, label="抖音主页")
                if not out.exists():
                    raise RuntimeError("抖音主页读取失败：%s" % (((r.stderr or "") + (r.stdout or ""))[-400:]))
                try:
                    data = json.loads(out.read_text("utf-8"))
                finally:
                    try:
                        out.unlink()
                    except Exception:
                        pass
                data["platform"] = "douyin"; data["kind"] = "list"; data["fetched_at"] = int(time.time())
                cp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
                return _annotate(data, "douyin", cached=False, limit=limit)
            if p == "xhs":
                out = cp.with_name(cp.stem + ".tmp-%d.json" % os.getpid())
                r = _run([_node(), str(TOOLS / "fetch_list.js"), "xhs", "list", str(out), PORT, url],
                         timeout=300, label="小红书主页")
                if not out.exists():
                    raise RuntimeError("小红书主页读取失败：%s" % (((r.stderr or "") + (r.stdout or ""))[-400:]))
                try:
                    data = json.loads(out.read_text("utf-8"))
                finally:
                    try:
                        out.unlink()
                    except Exception:
                        pass
                data["platform"] = "xhs"; data["kind"] = "list"; data["fetched_at"] = int(time.time())
                cp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
                return _annotate(data, "xhs", cached=False, limit=limit)
            if p in ("youtube", "bilibili"):
                items = flat_list(p, url)
                data = {"platform": p, "kind": "list", "page": url, "items": items,
                        "fetched_at": int(time.time())}
                cp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
                return _annotate(data, p, cached=False, limit=limit)
            elif p in ("x", "instagram", "weibo"):
                out = cp.with_name(cp.stem + ".tmp-%d.json" % os.getpid())
                r = _run([_node(), str(TOOLS / "fetch_list.js"), p, "list", str(out), PORT, url],
                         timeout=300, label="%s列表" % p)
                if not out.exists():
                    raise RuntimeError("%s 列表读取失败：%s" % (p, ((r.stderr or "") + (r.stdout or ""))[-400:]))
                try:
                    data = json.loads(out.read_text("utf-8"))
                finally:
                    try:
                        out.unlink()
                    except Exception:
                        pass
                data["platform"] = p
                data["kind"] = "list"
                data["fetched_at"] = int(time.time())
                cp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
                return _annotate(data, p, cached=False, limit=limit)
            else:
                raise ValueError("这个平台暂不支持列表")
    if platform not in PLATFORM_DB:
        raise ValueError("不支持的平台")
    if kind not in ("fav", "like"):
        raise ValueError("不支持的列表类型")
    cp = _cache_path(platform, kind, url)
    _lk = _get_cache_lock("%s_%s_%s" % (platform, kind, url or ""))
    with _lk:
        if not refresh and cp.exists() and time.time() - cp.stat().st_mtime < max_age:
            try:
                data = json.loads(cp.read_text("utf-8"))
            except Exception:
                data = None
            if data and data.get("items") is not None:
                return _annotate(data, platform, cached=True, limit=limit)
        out = cp.with_name(cp.stem + ".tmp-%d.json" % os.getpid())
        if platform == "douyin" and not url and kind in ("fav", "like"):
            if kind == "fav":
                args = [_node(), str(TOOLS / "douyin_api.js"), "fav", str(out), PORT]
            else:
                args = [_node(), str(TOOLS / "douyin_api.js"), "list", "like", str(out), PORT]
            r = _run(args, timeout=240, label="抖音%s" % kind)
        else:
            cmd = [_node(), str(TOOLS / "fetch_list.js"), platform, kind, str(out), PORT]
            if url:
                cmd.append(url)
            r = _run(cmd, timeout=300, label="%s_%s" % (platform, kind))
        if not out.exists():
            tail = ((r.stdout or "") + (r.stderr or ""))[-500:]
            raise RuntimeError("读取列表失败：%s" % (tail.strip() or "没有返回数据"))
        try:
            data = json.loads(out.read_text("utf-8"))
        except Exception:
            try:
                out.unlink()
            except Exception:
                pass
            raise
        try:
            out.unlink()
        except Exception:
            pass
        data["fetched_at"] = int(time.time())
        cp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
        return _annotate(data, platform, cached=False, limit=limit)


def _annotate(data, platform, cached=False, limit=0):
    ids = _archived_ids(platform)
    items = data.get("items") or []
    if limit and limit > 0:
        items = items[:limit]
    for it in items:
        it["archived"] = str(it.get("id")) in ids
        it["platform"] = platform
    data["items"] = items
    data["cached"] = cached
    data["archived_count"] = sum(1 for it in items if it.get("archived"))
    return data


def normalize_urls(raw):
    """从粘贴的文本里抽链接，去重，保留顺序。"""
    out, seen = [], set()
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        found = re.findall(r"https?://[^\s<>\"'）)\]]+", line)
        if not found and not line.startswith("http"):
            continue
        for u in (found or [line]):
            u = u.rstrip(".,;）)]】")
            if u not in seen:
                seen.add(u)
                out.append(u)
    return out
