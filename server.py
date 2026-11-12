#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""media-archive Web 端：本地网页管理归档内容

    python3 server.py            # 默认 http://127.0.0.1:8787
    python3 server.py --port 9000
"""
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import signal
import time
import urllib.parse
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import batch
from engine import export_csv

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
WEB = BASE / "web"
ENGINE = BASE / "engine.py"
NODE = shutil.which("node") or "/opt/homebrew/bin/node"
sys.path.insert(0, str(BASE))
PY = "/usr/bin/python3"
FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"

def move_to_trash(path):
    """移到废纸篓（调用编译好的 ma-trash，走系统 API，可在废纸篓恢复）"""
    tool = BASE / "tools" / "ma-trash"
    if tool.exists():
        try:
            r = subprocess.run([str(tool), str(path)], capture_output=True, timeout=30)
            if r.returncode == 0:
                return True
            print("[warn] ma-trash 返回 %d：%s" % (r.returncode, (r.stderr or b"").decode()[:120]), file=sys.stderr)
        except Exception as e:
            print("[warn] ma-trash 异常：%s" % e, file=sys.stderr)
    # 兜底：直接放进 ~/.Trash
    try:
        trash = HOME / ".Trash"
        trash.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = trash / ("%s %s" % (path.name, stamp))
        shutil.move(str(path), str(dest))
        return True
    except Exception as e:
        print("[warn] 移入废纸篓失败：%s" % e, file=sys.stderr)
        return False


def find_bin(name):
    p = shutil.which(name)
    if p:
        return p
    for c in ("/opt/homebrew/bin/%s" % name, "/usr/local/bin/%s" % name,
              str(HOME / "Library/Python/3.9/bin" / name)):
        if Path(c).exists():
            return c
    return None


TASKS = {}
TRANSCODE_LOCKS = {}                      # 同一个实况视频只转一次
TRANSCODE_GUARD = threading.Lock()


def task_public(t):
    """TASKS 里存了 Popen 句柄，返回给前端时要剔除"""
    skip = {"proc"}
    return {k: v for k, v in t.items() if k not in skip}
GLOBAL_SEM = threading.Semaphore(3)      # 同时最多 3 个任务
DOUYIN_LOCK = threading.Lock()           # 抖音 CDP Chrome 启动/检查互斥锁
TASK_TIMEOUT = 900                       # 普通平台单任务上限（秒）
DOUYIN_TIMEOUT = 360                     # 抖音单任务上限：卡住也最多占 6 分钟
TASK_SEQ = 0
TASK_SEQ_LOCK = threading.Lock()


def next_task_id():
    global TASK_SEQ
    with TASK_SEQ_LOCK:
        TASK_SEQ += 1
        return "%d-%d" % (int(time.time() * 1000), TASK_SEQ)
TASKS_LOCK = threading.Lock()
QUEUE_PAUSED = threading.Event()          # 登录/维护时暂停新任务
LOGIN_STATE = {"platform": "", "state": "idle", "message": "", "started": 0, "proc": None, "cancel": False}
LOGIN_LOCK = threading.Lock()


def db():
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def ensure_collections():
    """手动分类（收藏夹等）：分类本身 + 分类里的作品。"""
    con = db()
    con.execute("""CREATE TABLE IF NOT EXISTS collections(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        created_ts INTEGER,
        pinned INTEGER DEFAULT 0)""")
    # 兼容旧表：补 pinned 列
    try:
        con.execute("ALTER TABLE collections ADD COLUMN pinned INTEGER DEFAULT 0")
    except Exception:
        pass
    con.execute("""CREATE TABLE IF NOT EXISTS collection_items(
        collection_id INTEGER NOT NULL,
        item_id INTEGER NOT NULL,
        added_ts INTEGER,
        PRIMARY KEY(collection_id, item_id))""")
    con.commit()
    con.close()


def q_collections():
    ensure_collections()
    con = db()
    rows = con.execute("""
        SELECT c.id, c.name, c.pinned, COUNT(ci.item_id) as n
        FROM collections c
        LEFT JOIN collection_items ci ON c.id = ci.collection_id
        GROUP BY c.id
        ORDER BY c.pinned DESC, c.name
    """).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["items"] = [dict(x) for x in con.execute("""
            SELECT i.id, i.title, i.author
            FROM collection_items ci JOIN items i ON i.id = ci.item_id
            WHERE ci.collection_id = ?
            ORDER BY ci.added_ts DESC, i.id DESC""", (d["id"],)).fetchall()]
        out.append(d)
    con.close()
    return out


def q_items(q="", platform="", author="", limit=200, offset=0, collection=None, author_key=""):
    try:
        limit = max(1, min(500, int(limit)))
    except Exception:
        limit = 200
    try:
        offset = max(0, int(offset))
    except Exception:
        offset = 0
    con = db()
    where, args = [], []
    if q:
        q_escaped = q.replace("%", "\\%").replace("_", "\\_")
        where.append("(title LIKE ? ESCAPE '\\' OR author LIKE ? ESCAPE '\\' OR source_url LIKE ? ESCAPE '\\')")
        args += ["%%%s%%" % q_escaped] * 3
    if platform:
        where.append("platform_cn = ?")
        args.append(platform)
    if author_key:
        where.append("(CASE WHEN author_id IS NULL OR author_id='' THEN author ELSE author_id END) = ?")
        args.append(author_key)
    elif author:
        where.append("author = ?")
        args.append(author)
    if collection:
        ensure_collections()
        try:
            cid = int(collection)
        except Exception:
            cid = 0
        where.append("id IN (SELECT item_id FROM collection_items WHERE collection_id = ?)")
        args.append(cid)
    sql = "SELECT * FROM items"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY saved_ts DESC LIMIT ? OFFSET ?"
    args += [limit, offset]
    rows = [dict(r) for r in con.execute(sql, args)]
    con.close()
    for r in rows:
        r["saved"] = datetime.fromtimestamp(r["saved_ts"]).strftime("%Y-%m-%d %H:%M") if r["saved_ts"] else ""
        r["created"] = datetime.fromtimestamp(r["created_ts"]).strftime("%Y-%m-%d %H:%M") if r["created_ts"] else ""
        # 使用 DB 中的 file_count 避免 N+1 scandir；仅当 file_count 不可靠时才回退扫描
        fc = r.get("file_count") or 0
        folder_exists = Path(r["folder"]).exists()
        if fc > 0 and folder_exists:
            r["files"] = []
            r["missing"] = False
        else:
            r["files"] = list_files(r["folder"])
            r["missing"] = (not folder_exists) or (len(r["files"]) == 0)
    return rows


LIVEPHOTO = BASE / "livephoto.py"


def prepare_live_folder(folder):
    """把 01.jpg + 01_live.mp4 合成 PhotoKit 认的 01.mov。

    App 端的导入只认「同名 .jpg + .mov」，小红书实况是 _live.mp4，
    所以导入前统一在这里补一次；已经转过就直接复用。
    """
    p = Path(folder).resolve()
    try:
        p.relative_to(ARCHIVE.resolve())
    except Exception:
        return {"ok": False, "error": "forbidden"}
    if not p.is_dir():
        return {"ok": False, "error": "missing"}
    if not Path(LIVEPHOTO).exists():
        return {"ok": False, "error": "缺少 livephoto.py"}

    need = []
    for img in sorted(p.glob("*.jpg")):
        if img.with_suffix(".mov").exists():
            continue
        for cand in (p / (img.stem + "_live.mp4"), p / (img.stem + ".mp4")):
            if cand.exists():
                need.append((img, cand))
                break

    if not need:
        movs = sorted(f.name for f in p.glob("*.mov"))
        return {"ok": True, "pairs": len(movs), "prepared": 0, "movs": movs}

    py = "/opt/homebrew/bin/python3.10"
    if not Path(py).exists():
        py = sys.executable
    env = dict(os.environ)
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + env.get("PATH", "")
    try:
        r = subprocess.run([py, str(LIVEPHOTO), "--dir", str(p)],
                           capture_output=True, text=True, timeout=600, env=env)
    except Exception as e:
        return {"ok": False, "error": "转换失败：%s" % e}

    movs = sorted(f.name for f in p.glob("*.mov"))
    if not movs:
        log = ((r.stdout or "") + (r.stderr or ""))[-900:]
        return {"ok": False, "error": "转换失败", "log": log}
    return {"ok": True, "pairs": len(movs), "prepared": len(need),
            "movs": movs}


def resolve_under_archive(p):
    """只允许访问归档目录内的真实路径（防目录穿越/任意文件读取）"""
    try:
        rp = Path(p).resolve()
        rp.relative_to(ARCHIVE.resolve())
        return rp
    except Exception:
        return None


THUMB_SEM = threading.Semaphore(3)
THUMB_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif"}


def thumb_path(src, w):
    d = src.parent / "_extras" / "thumbs"
    d.mkdir(parents=True, exist_ok=True)
    try:
        mt = int(src.stat().st_mtime)
    except Exception:
        mt = 0
    return d / ("%s_%d_%d.jpg" % (src.stem, w, mt))


def make_thumb(src, out, w):
    try:
        subprocess.run(["/usr/bin/sips", "-Z", str(w), "-s", "format", "jpeg",
                        "-s", "formatOptions", "80", str(src), "--out", str(out)],
                       capture_output=True, timeout=30)
    except Exception:
        pass
    if out.exists() and out.stat().st_size > 0:
        return True
    try:
        subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(src),
                        "-vf", "scale='min(%d,iw)':-2" % w, "-frames:v", "1",
                        "-q:v", "4", str(out)], capture_output=True, timeout=30)
    except Exception:
        pass
    return out.exists() and out.stat().st_size > 0


def make_poster(src, out):
    try:
        subprocess.run([FFMPEG, "-y", "-v", "error", "-ss", "0.3", "-i", str(src),
                        "-vframes", "1", "-vf", "scale=480:-2", "-q:v", "4", str(out)],
                       capture_output=True, timeout=60)
    except Exception:
        pass
    return out.exists() and out.stat().st_size > 0


def origin_allowed(value):
    """本机页面（任意端口）放行；其它站点一律拒绝（防 CSRF）"""
    if not value:
        return True
    try:
        h = urllib.parse.urlparse(value)
        host = (h.hostname or "").lower()
        return h.scheme in ("http", "https") and host in ("127.0.0.1", "localhost", "::1")
    except Exception:
        return False


_STATS_CACHE = {"ts": 0.0, "data": None}


def list_files(folder):
    p = Path(folder)
    if not p.exists():
        return []
    out = []
    exts = {".mp4", ".mkv", ".webm", ".mov", ".m4v",
            ".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic",
            ".m4a", ".mp3", ".wav", ".aac", ".flac"}
    VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
    AUDIO_EXT = {".m4a", ".mp3", ".wav", ".aac", ".flac"}
    try:
        entries = list(os.scandir(p))
    except Exception:
        return []
    # _extras 是工具生成的缓存目录；作品文件都在条目根目录
    for entry in sorted(entries, key=lambda e: e.name):
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
            name = entry.name
            if name.lower().endswith(".poster.jpg") or name.lower().endswith(".web.mp4"):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in exts:
                continue
            st = entry.stat()
            out.append({
                "name": name,
                "url": "/media?v=%d&p=%s" % (int(st.st_mtime),
                                              urllib.parse.quote(str(p / name))),
                "size": st.st_size,
                "kind": ("video" if ext in VIDEO_EXT
                         else "audio" if ext in AUDIO_EXT
                         else "image"),
            })
        except Exception:
            continue
    return out


PINS_FILE = HOME / ".media-archive" / "pins.json"
ORDER_FILE = HOME / ".media-archive" / "order.json"


def load_order():
    try:
        return json.loads(ORDER_FILE.read_text("utf-8")) if ORDER_FILE.exists() else {}
    except Exception:
        return {}


def save_order(o):
    try:
        ORDER_FILE.parent.mkdir(parents=True, exist_ok=True)
        ORDER_FILE.write_text(json.dumps(o, ensure_ascii=False, indent=2), "utf-8")
    except Exception:
        pass


def load_pins():
    try:
        return json.loads(PINS_FILE.read_text("utf-8")) if PINS_FILE.exists() else {}
    except Exception:
        return {}


def save_pins(p):
    try:
        PINS_FILE.parent.mkdir(parents=True, exist_ok=True)
        PINS_FILE.write_text(json.dumps(p, ensure_ascii=False, indent=2), "utf-8")
    except Exception:
        pass


AUTHOR_URL_ID_RES = {
    "douyin": re.compile(r"douyin\.com/user/([^/?#]+)"),
    "xiaohongshu": re.compile(r"xiaohongshu\.com/user/profile/([^/?#]+)"),
    "bilibili": re.compile(r"space\.bilibili\.com/(\d+)"),
    "telegram": re.compile(r"t\.me/([^/?#]+)"),
}


def backfill_author_ids():
    """老数据补齐稳定博主 ID：抖音用 sec_uid（URL /user/<id>），不用会变的短 ID"""
    con = db()
    rows = con.execute("SELECT id, platform, author_url, author_id FROM items WHERE author_id IS NULL OR author_id = ''").fetchall()
    n = 0
    for r in rows:
        rx = AUTHOR_URL_ID_RES.get(r["platform"])
        if not rx:
            continue
        m = rx.search(r["author_url"] or "")
        if not m:
            continue
        aid = m.group(1)
        if r["author_id"] != aid:
            con.execute("UPDATE items SET author_id=? WHERE id=?", (aid, r["id"]))
            n += 1
    con.commit()
    con.close()
    return n


def canonicalize_authors():
    """同一博主（按稳定 ID）以最新作品的名字为准，历史作品同步改名"""
    con = db()
    rows = con.execute("""SELECT platform, author_id, author FROM items
                          WHERE author_id IS NOT NULL AND author_id<>''
                          ORDER BY saved_ts DESC""").fetchall()
    latest = {}
    for r in rows:
        k = (r["platform"], r["author_id"])
        name = (r["author"] or "").strip()
        if k not in latest and name and name != "未知博主":
            latest[k] = name
    n = 0
    for (platform, aid), name in latest.items():
        cur = con.execute("UPDATE items SET author=? WHERE platform=? AND author_id=? AND author<>?",
                          (name, platform, aid, name))
        n += cur.rowcount
    con.commit()
    con.close()
    return n


def q_authors():
    con = db()
    rows = con.execute("SELECT platform, platform_cn, author, author_url, author_id, saved_ts FROM items").fetchall()
    con.close()
    pins = set(load_pins().get("authors") or [])
    groups = {}
    for r in rows:
        aid = (r["author_id"] or "").strip()
        key = aid or (r["author"] or "")
        k = (r["platform"], key)
        g = groups.get(k)
        if not g:
            groups[k] = {"platform": r["platform"], "platform_cn": r["platform_cn"],
                         "author": r["author"], "author_url": r["author_url"],
                         "author_id": aid, "author_key": key, "n": 1,
                         "saved_ts": r["saved_ts"]}
        else:
            g["n"] += 1
            if (r["saved_ts"] or 0) >= (g["saved_ts"] or 0):
                g["author"] = r["author"]
                g["author_url"] = r["author_url"]
                g["author_id"] = aid
                g["saved_ts"] = r["saved_ts"]
    out = list(groups.values())
    for d in out:
        d["pinned"] = d["author"] in pins
        d.pop("saved_ts", None)
    seen = {d["author_key"] for d in out}
    # 手工新建的博主（还没有作品）
    try:
        con2 = db()
        con2.execute("""CREATE TABLE IF NOT EXISTS custom_authors(
            name TEXT, platform TEXT, created_ts INTEGER, PRIMARY KEY(name, platform))""")
        extra = con2.execute("SELECT name, platform FROM custom_authors").fetchall()
        con2.close()
    except Exception:
        extra = []
    for e in extra:
        if e["name"] in seen:
            continue
        out.append({"author": e["name"], "platform": e["platform"], "platform_cn": "",
                    "author_url": "", "author_id": "", "author_key": e["name"], "n": 0,
                    "pinned": e["name"] in pins})
        seen.add(e["name"])
    order = (load_order().get("authors") or [])
    if order:
        idx = {name: i for i, name in enumerate(order)}
        out.sort(key=lambda x: (0 if x["pinned"] else 1, idx.get(x["author"], 9999), -x["n"]))
    else:
        out.sort(key=lambda x: (0 if x["pinned"] else 1, -x["n"]))
    return out


def q_platforms():
    con = db()
    rows = con.execute("SELECT platform_cn, platform, COUNT(*) n FROM items GROUP BY platform_cn, platform ORDER BY n DESC").fetchall()
    con.close()
    pins = set(load_pins().get("platforms") or [])
    out = []
    for r in rows:
        d = dict(r)
        d["pinned"] = d["platform"] in pins
        out.append(d)
    order = (load_order().get("platforms") or [])
    if order:
        idx = {name: i for i, name in enumerate(order)}
        out.sort(key=lambda x: (0 if x["pinned"] else 1, idx.get(x["platform"], 9999), -x["n"]))
    else:
        out.sort(key=lambda x: (0 if x["pinned"] else 1, -x["n"]))
    return out


_STATS_LOCK = threading.Lock()


def _stats_walk_size():
    """后台线程：遍历归档目录计算总大小，结果写入缓存。"""
    total = 0
    try:
        for root, dirs, names in os.walk(str(ARCHIVE)):
            dirs[:] = [d for d in dirs if d != "_extras"]
            for n in names:
                if os.path.splitext(n)[1].lower() in (".mp4", ".jpg", ".jpeg", ".png", ".webp"):
                    try:
                        total += os.path.getsize(os.path.join(root, n))
                    except Exception:
                        pass
    except Exception as e:
        print("[warn] 统计目录大小失败：%s" % e, file=sys.stderr)
    with _STATS_LOCK:
        _STATS_CACHE["_size"] = total
        _STATS_CACHE["_size_ts"] = time.time()


def q_stats():
    now = time.time()
    # 快速路径：DB 计数 + 缓存的磁盘大小（不阻塞）
    con = db()
    row = con.execute("SELECT COUNT(*) items, SUM(file_count) files FROM items").fetchone()
    con.close()
    with _STATS_LOCK:
        total = _STATS_CACHE.get("_size", 0)
        size_ts = _STATS_CACHE.get("_size_ts", 0)
    # 磁盘大小超过 5 分钟没更新就后台刷新
    if now - size_ts > 300:
        threading.Thread(target=_stats_walk_size, daemon=True).start()
    data = {"items": row["items"] or 0, "files": row["files"] or 0,
            "size": total, "platforms": len(q_platforms())}
    return data


def cleanup_stale_downloader():
    """清掉上次遗留的抖音下载器 Chrome（只匹配下载器自己的 profile）"""
    try:
        subprocess.run(["pkill", "-f", "douyin-ai-video-downloader/.chrome-profile"],
                       capture_output=True, timeout=15)
    except Exception as e:
        print("[warn] 清理残留 Chrome 失败：%s" % e, file=sys.stderr)


def douyin_cdp_up():
    try:
        import urllib.request
        urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=1.0)
        return True
    except Exception:
        return False


def start_douyin_chrome():
    """启动一个常驻的下载器 Chrome；所有抖音任务共用它，各自开标签页并行"""
    if douyin_cdp_up():
        return True
    chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if not Path(chrome).exists():
        return False
    profile = BASE / "vendor" / "douyin-ai-video-downloader" / ".chrome-profile"
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(str(profile), 0o700)
    except Exception:
        pass
    ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")
    args = [chrome, "--headless=new", "--remote-debugging-port=9222",
            "--remote-allow-origins=*", "--user-data-dir=" + str(profile),
            "--no-first-run", "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled", "--mute-audio",
            "--autoplay-policy=no-user-gesture-required", "--window-size=1280,900",
            "--user-agent=" + ua, "about:blank"]
    try:
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except Exception:
        return False
    for _ in range(60):
        if douyin_cdp_up():
            return True
        time.sleep(0.5)
    return False


def _active_douyin_tasks():
    with TASKS_LOCK:
        return [t for t in TASKS.values()
                if t.get("kind") == "archive" and t.get("active")
                and "douyin" in (t.get("url") or "")]


def _login_set(**kw):
    with LOGIN_LOCK:
        LOGIN_STATE.update(kw)


def _login_get(key, default=None):
    with LOGIN_LOCK:
        return LOGIN_STATE.get(key, default)


def login_status_public():
    with LOGIN_LOCK:
        return {k: v for k, v in LOGIN_STATE.items() if k not in ("proc",)}


def login_worker(platform):
    ok = False
    try:
        _login_set(platform=platform, state="waiting_tasks", started=time.time(), cancel=False,
                   message="等待正在下载的抖音任务结束…")
        for _ in range(600):
            if _login_get("cancel"):
                raise RuntimeError("已取消登录")
            if not _active_douyin_tasks():
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("还有抖音任务在跑，稍后再登录")

        # 先确认是否已经登录（headless 在跑时能直接判断），避免窗口弹一下就关
        try:
            start_douyin_chrome()
            if batch.login_state(platform):
                _login_set(state="done", message="已登录，无需重复扫码", proc=None)
                return
        except Exception:
            pass

        # 停掉常驻 headless Chrome，把 profile 让给有窗口登录
        cleanup_stale_downloader()
        time.sleep(1.0)
        script = batch.login_script(platform)
        if not script:
            raise RuntimeError("缺少登录脚本")
        _login_set(state="waiting_scan",
                   message="请在弹出的 Chrome 窗口扫码登录（最多 10 分钟）")
        proc = subprocess.Popen(["/bin/bash", script], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                start_new_session=True, cwd=str(BASE))
        _login_set(proc=proc)
        try:
            out = proc.communicate(timeout=900)[0] or ""
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
            out = "登录超时"
        if _login_get("cancel"):
            _login_set(state="cancelled", message="已取消登录")
            cleanup_stale_downloader()
        elif proc.returncode == 0:
            # 登录脚本会关掉有窗口 Chrome；先重启 headless 再校验，否则永远 no_cdp
            cleanup_stale_downloader()
            try:
                start_douyin_chrome()
            except Exception:
                pass
            time.sleep(1.5)
            if batch.login_state(platform):
                ok = True
                _login_set(state="done", message="登录成功")
            else:
                _login_set(state="error", message="登录窗口已关闭，但未检测到登录态")
                cleanup_stale_downloader()
        else:
            tail = (out.strip().splitlines() or ["登录失败"])[-1]
            _login_set(state="error", message=tail[:160])
            cleanup_stale_downloader()
        _login_set(proc=None)
    except Exception as e:
        _login_set(state=("cancelled" if _login_get("cancel") else "error"),
                   message=str(e)[:160], proc=None)
    finally:
        try:
            start_douyin_chrome()
        except Exception:
            pass
        QUEUE_PAUSED.clear()


def scope_from_qs(qs):
    if (qs.get("collection") or [""])[0]:
        return {"type": "collection", "value": (qs.get("collection") or [""])[0]}
    if (qs.get("author") or [""])[0]:
        return {"type": "author", "value": (qs.get("author") or [""])[0]}
    if (qs.get("platform") or [""])[0]:
        return {"type": "platform", "value": (qs.get("platform") or [""])[0]}
    return {"type": "all"}


def run_archive(url):
    tid = next_task_id()
    is_douyin = ("douyin" in url) or ("iesdouyin" in url)
    with TASKS_LOCK:
        TASKS[tid] = {"id": tid, "url": url, "status": "running", "log": "",
                      "folder": "", "started": time.time(), "kind": "archive",
                      "queued": True, "active": False}
    prog_file = HOME / ".media-archive" / "progress" / (tid + ".json")
    prog_file.parent.mkdir(parents=True, exist_ok=True)

    def worker():
        env = dict(os.environ)
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + env.get("PATH", "")
        env["MA_PROGRESS_FILE"] = str(prog_file)
        env["MA_TASK_ID"] = tid
        try:
            if load_cfg().get("ytdlp_use_browser_cookies"):
                env["MA_YTDLP_COOKIES_BROWSER"] = "chrome"
        except Exception:
            pass
        if is_douyin:
            env["DOUYIN_WS"] = "/tmp/douyin-ws-" + tid
            env["DOUYIN_OUT"] = str(BASE / "vendor" / "douyin-ai-video-downloader" / "downloads" / tid)
            env["DOUYIN_META"] = env["DOUYIN_WS"] + "/douyin_meta.json"
            env["DOUYIN_PORT"] = "9222"

        def task_cancelled():
            with TASKS_LOCK:
                return (TASKS.get(tid) or {}).get("status") == "cancelled"

        def mark(**kw):
            with TASKS_LOCK:
                t = TASKS.get(tid)
                if t:
                    t.update(kw)

        def run_engine():
            if task_cancelled():
                return 0, "CANCELLED"
            # start_new_session=True：让整个进程树（bash/node/Chrome）属于同一个组，
            # 取消时能一次杀干净，不留孤儿占住 CDP 端口
            proc = subprocess.Popen([PY, str(ENGINE), url],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, env=env, start_new_session=True)
            mark(proc=proc, queued=False, active=True)
            timeout = DOUYIN_TIMEOUT if is_douyin else TASK_TIMEOUT
            try:
                out, _ = proc.communicate(timeout=timeout)
                return proc.returncode, (out or "")
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    proc.kill()
                # 兜底清理可能残留的 Chrome
                if is_douyin:
                    try:
                        cleanup_stale_downloader()
                        time.sleep(0.5)
                        start_douyin_chrome()
                    except Exception:
                        pass
                return -1, "任务超过 %d 秒没有完成，已终止" % timeout
            finally:
                mark(active=False)

        # 登录/维护时暂停新任务（已在跑的不打断）
        while QUEUE_PAUSED.is_set() and not task_cancelled():
            time.sleep(0.3)
        # 抖音任务兜底：确保 CDP Chrome 活着（中途挂了也能自愈）
        if is_douyin:
            with DOUYIN_LOCK:
                if not douyin_cdp_up():
                    start_douyin_chrome()
        # 抖音也走全局并发：所有任务共用一个 Chrome（多标签页），
        # 每个任务有自己的临时目录 / 下载目录，互不干扰。
        with GLOBAL_SEM:
            mark(queued=False)
            rc, out = run_engine()
        p = type("R", (), {"returncode": rc, "stdout": out, "stderr": ""})()
        folder = ""
        for line in out.splitlines():
            if "归档：" in line:
                folder = line.split("归档：")[-1].split("（")[0].strip()
            elif line.startswith("EXIST_SKIP\t"):
                pass
        item_info = None
        if p.returncode == 0:
            try:
                con = sqlite3.connect(str(DB))
                con.row_factory = sqlite3.Row
                row = con.execute("SELECT id, title, author, platform_cn FROM items ORDER BY saved_ts DESC LIMIT 1").fetchone()
                if row:
                    item_info = {"id": row["id"], "title": row["title"],
                                 "author": row["author"], "platform": row["platform_cn"]}
                con.close()
            except Exception:
                pass
        try:
            prog_file.unlink(missing_ok=True)
        except Exception:
            pass
        # 清理抖音临时工作目录
        douyin_ws = env.get("DOUYIN_WS", "")
        if douyin_ws:
            try:
                shutil.rmtree(douyin_ws, ignore_errors=True)
            except Exception:
                pass
        with TASKS_LOCK:
            cur = TASKS.get(tid) or {}
            if cur.get("status") == "cancelled":
                # 用户主动取消：保持 cancelled，不要覆盖成 error
                cur.update({"finished": time.time(), "log": out[-3000:],
                            "item": item_info, "folder": folder})
            else:
                cur.update({
                    "status": "done" if p.returncode == 0 else "error",
                    "finished": time.time(),
                    "log": out[-3000:], "folder": folder,
                    "item": item_info,
                    "duplicate": "EXIST_SKIP" in out,
                    "error_reason": ("" if p.returncode == 0 else
                                     next((ln.split("失败：")[-1].strip()
                                           for ln in reversed((out or "").splitlines())
                                           if "失败" in ln or "错误" in ln), "未知错误")[:80]),
                    "skipped": "EXIST_SKIP" in out,
                })
    threading.Thread(target=worker, daemon=True).start()
    return tid


class Handler(BaseHTTPRequestHandler):
    server_version = "MediaArchive/1.0"

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, path):
        data = Path(path).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                return self._html(WEB / "index.html")
            if u.path == "/api/items":
                return self._json(q_items(
                    q=(qs.get("q") or [""])[0],
                    platform=(qs.get("platform") or [""])[0],
                    author=(qs.get("author") or [""])[0],
                    author_key=(qs.get("author_key") or [""])[0],
                    limit=int((qs.get("limit") or ["200"])[0]),
                    offset=int((qs.get("offset") or ["0"])[0]),
                    collection=(qs.get("collection") or [""])[0] or None))
            if u.path == "/api/authors":
                return self._json(q_authors())
            if u.path == "/api/platforms":
                return self._json(q_platforms())
            if u.path == "/api/health":
                ytdlp = shutil.which("yt-dlp") or str(HOME / "Library/Python/3.9/bin/yt-dlp")
                douyin_vendor = (BASE / "vendor" / "douyin-ai-video-downloader").exists()
                douyin_cdp = douyin_cdp_up()
                return self._json({
                    "ok": ENGINE.exists(),
                    "engine": ENGINE.exists(),
                    "ytdlp": Path(ytdlp).exists(),
                    "ffmpeg": bool(find_bin("ffmpeg")),
                    "douyin": douyin_vendor,
                    "douyin_cdp": douyin_cdp,
                    "archive": str(ARCHIVE),
                    "archive_writable": os.access(str(ARCHIVE), os.W_OK),
                })
            if u.path == "/api/config":
                c = load_cfg()
                return self._json({
                    "theme": c.get("theme", "auto"),
                    "archive_dir": c.get("archive_dir") or str(ARCHIVE),
                    "livephoto": c.get("livephoto") or {},
                })

            if u.path == "/api/tasks":
                with TASKS_LOCK:
                    now_ts = time.time()
                    # 完成超过 2 分钟的任务彻底丢弃，避免 TASKS 无限增长
                    for _k in [k for k, v in TASKS.items()
                               if v.get("finished") and now_ts - v["finished"] > 120]:
                        TASKS.pop(_k, None)
                    items = [task_public(v) for v in TASKS.values()]
                # 附加实时下载进度
                for t in items:
                    if t.get("status") != "running":
                        continue
                    pf = HOME / ".media-archive" / "progress" / (str(t.get("id")) + ".json")
                    try:
                        if pf.exists():
                            t["progress"] = json.loads(pf.read_text("utf-8"))
                    except Exception:
                        pass
                items.sort(key=lambda x: x.get("started", 0), reverse=True)
                return self._json(items[:60])
            if u.path == "/api/stats":
                return self._json(q_stats())
            if u.path == "/api/collections":
                return self._json(q_collections())
            if u.path == "/api/collection/ids":
                cid = (qs.get("id") or [""])[0]
                ensure_collections()
                try:
                    cid = int(cid)
                except Exception:
                    return self._json({"ok": False, "error": "缺少分类"})
                con = db()
                ids = [r["item_id"] for r in con.execute(
                    "SELECT item_id FROM collection_items WHERE collection_id=?", (cid,)).fetchall()]
                con.close()
                return self._json({"ok": True, "ids": ids})
            if u.path == "/api/live_mp4":
                # 实况伴生视频兜底：浏览器解不了（HEVC / .mov / 特殊封装）时，
                # 用 ffmpeg 转一份 H.264 MP4 放 _extras 里，之后直接复用。
                from urllib.parse import parse_qs
                qs = parse_qs(u.query)
                src = Path((qs.get("p") or [""])[0]).resolve()
                try:
                    src.relative_to(ARCHIVE.resolve())
                except Exception:
                    return self._json({"ok": False, "error": "forbidden"}, 403)
                if not src.exists():
                    return self._json({"ok": False, "error": "missing"}, 404)
                out_dir = src.parent / "_extras"
                out_dir.mkdir(parents=True, exist_ok=True)
                out = out_dir / (src.stem + ".web.mp4")
                with TRANSCODE_GUARD:
                    lock = TRANSCODE_LOCKS.setdefault(str(out), threading.Lock())
                with lock:
                    if not out.exists() or out.stat().st_size == 0:
                        try:
                            r = subprocess.run(
                                [FFMPEG, "-y", "-v", "error", "-i", str(src),
                                 "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
                                 "-pix_fmt", "yuv420p", "-an",
                                 "-movflags", "+faststart", str(out)],
                                capture_output=True, timeout=180)
                        except Exception as e:
                            return self._json({"ok": False, "error": str(e)})
                        if r.returncode != 0 or not out.exists():
                            err = (r.stderr or b"").decode("utf-8", "ignore")[-300:]
                            return self._json({"ok": False, "error": err or "转码失败"})
                return self._json({"ok": True,
                                   "url": "/media?p=" + urllib.parse.quote(str(out))})
            if u.path == "/api/poster":
                from urllib.parse import parse_qs
                qs = parse_qs(u.query)
                vp = (qs.get("p") or [""])[0]
                src = resolve_under_archive(vp)
                if not src or not src.is_file():
                    self.send_error(404)
                    return
                poster_dir = src.parent / "_extras"
                poster_dir.mkdir(parents=True, exist_ok=True)
                poster = poster_dir / (src.stem + ".poster.jpg")
                if not poster.exists():
                    try:
                        subprocess.run(
                            [FFMPEG, "-y", "-v", "error", "-ss", "0.3", "-i", str(src),
                             "-vframes", "1", "-vf", "scale=480:-2", "-q:v", "4", str(poster)],
                            capture_output=True, timeout=60)
                    except Exception:
                        pass
                if poster.exists():
                    data = poster.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "max-age=86400")
                    self.end_headers()
                    self.wfile.write(data)
                    return
                # 抽帧失败：宁可 404，也不要把整个视频当图片塞给网格
                self.send_error(404)
                return
            if u.path == "/api/source/status":
                from urllib.parse import parse_qs
                qs = parse_qs(u.query)
                only = (qs.get("platform") or [""])[0]
                plats = [only] if only in ("douyin", "xhs") else ["douyin", "xhs"]
                out = {"ok": True}
                for p in plats:
                    st = batch.login_state(p)
                    if isinstance(st, str):
                        out[p] = True
                        out[p + "_nickname"] = st
                    else:
                        out[p] = bool(st)
                out["login"] = login_status_public()
                return self._json(out)
            if u.path == "/api/source/login/status":
                return self._json({"ok": True, "login": login_status_public()})
            if u.path == "/api/source/items":
                from urllib.parse import parse_qs
                qs = parse_qs(u.query)
                platform = (qs.get("platform") or [""])[0]
                kind = (qs.get("kind") or ["fav"])[0]
                target = (qs.get("url") or [""])[0] or None
                refresh = (qs.get("refresh") or [""])[0] in ("1", "true", "yes")
                try:
                    limit = int((qs.get("limit") or ["0"])[0] or 0)
                except Exception:
                    limit = 0
                if kind == "list":
                    if not target:
                        return self._json({"ok": False, "error": "缺少列表链接"}, 400)
                    try:
                        data = batch.fetch_items(platform or "auto", "list", url=target, refresh=refresh, limit=limit)
                        return self._json({"ok": True, "logged_in": True, **data})
                    except Exception as e:
                        return self._json({"ok": False, "error": str(e)}, 500)
                if platform not in ("douyin", "xhs"):
                    return self._json({"ok": False, "error": "不支持的平台"}, 400)
                if kind not in ("fav", "like"):
                    return self._json({"ok": False, "error": "不支持的列表类型"}, 400)
                if not batch.login_state(platform):
                    return self._json({"ok": False, "logged_in": False, "items": [],
                                       "error": "未登录，请先扫码登录"})
                try:
                    data = batch.fetch_items(platform, kind, url=target, refresh=refresh, limit=limit)
                    return self._json({"ok": True, "logged_in": True, **data})
                except Exception as e:
                    return self._json({"ok": False, "error": str(e)}, 500)
            if u.path == "/api/thumb":
                from urllib.parse import parse_qs
                qs = parse_qs(u.query)
                vp = (qs.get("p") or [""])[0]
                try:
                    w = int((qs.get("w") or ["480"])[0] or 480)
                except Exception:
                    w = 480
                w = max(160, min(960, w))
                src = resolve_under_archive(vp)
                if not src or not src.is_file() or src.suffix.lower() not in THUMB_EXTS:
                    self.send_error(404)
                    return
                out = thumb_path(src, w)
                if not out.exists():
                    with THUMB_SEM:
                        if not out.exists():
                            make_thumb(src, out, w)
                if out.exists():
                    data = out.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "private, max-age=604800, immutable")
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self.send_error(404)
                return
            if u.path == "/api/item":
                from urllib.parse import parse_qs
                qs = parse_qs(u.query)
                iid = (qs.get("id") or [""])[0]
                con = db()
                con.row_factory = sqlite3.Row
                row = con.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
                con.close()
                if not row:
                    return self._json({"error": "not found"})
                d = dict(row)
                d["files"] = list_files(d.get("folder"))
                d["missing"] = (not Path(d.get("folder") or "/nonexistent").exists()) or (len(d["files"]) == 0)
                return self._json(d)
            if u.path == "/api/item_folder_by_key":
                from urllib.parse import parse_qs
                qs = parse_qs(u.query)
                pf = (qs.get("platform") or [""])[0]
                ik = (qs.get("item_id") or [""])[0]
                con = db()
                row = con.execute("SELECT folder, id FROM items WHERE platform=? AND item_id=?",
                                  (pf, ik)).fetchone()
                con.close()
                return self._json({"folder": row["folder"] if row else "",
                                   "id": row["id"] if row else 0})
            if u.path == "/api/item_folder":
                from urllib.parse import parse_qs
                qs = parse_qs(u.query)
                iid = (qs.get("id") or [""])[0]
                con = db()
                row = con.execute("SELECT folder FROM items WHERE id=?", (iid,)).fetchone()
                con.close()
                return self._json({"folder": row["folder"] if row else ""})
            if u.path.startswith("/api/task/"):
                tid = u.path.rsplit("/", 1)[-1]
                with TASKS_LOCK:
                    t = TASKS.get(tid)
                return self._json(task_public(t) if t else {"status": "unknown"})
            if u.path == "/media":
                return self._media((qs.get("p") or [""])[0])
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            return self._json({"error": str(e)}, 500)

    def do_HEAD(self):
        """WKWebView 探测媒体时会先发 HEAD，不支持会导致视频不播放"""
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        try:
            if u.path == "/media":
                p = Path((qs.get("p") or [""])[0]).resolve()
                try:
                    p.relative_to(ARCHIVE.resolve())
                except Exception:
                    self.send_response(403); self.end_headers(); return
                if not p.exists():
                    self.send_response(404); self.end_headers(); return
                ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "private, max-age=86400, immutable")
                self.send_header("Content-Length", str(p.stat().st_size))
                self.end_headers()
                return
            if u.path in ("/", "/index.html"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                return
            self.send_response(200); self.end_headers()
        except Exception:
            try: self.send_response(500); self.end_headers()
            except Exception: pass

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        # CSRF：只接受本机页面的写操作（curl 等不带 Origin 的本地调用放行）
        origin = self.headers.get("Origin") or ""
        referer = self.headers.get("Referer") or ""
        if (origin and not origin_allowed(origin)) or (not origin and referer and not origin_allowed(referer)):
            return self._json({"ok": False, "error": "forbidden"}, 403)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except Exception:
            return self._json({"ok": False, "error": "bad request"}, 400)
        try:
            if u.path == "/api/archive":
                url = (body.get("url") or "").strip()
                m = re.search(r"https?://\S+", url)
                if not m:
                    return self._json({"error": "没找到链接"}, 400)
                tid = run_archive(m.group(0))
                return self._json({"ok": True, "task": tid})
            if u.path == "/api/delete_author":
                name = (body.get("author") or "").strip()
                if not name:
                    return self._json({"ok": False, "error": "缺少博主名"})
                con = db()
                _ar = con.execute("SELECT author_id FROM items WHERE author=? AND author_id<>'' LIMIT 1", (name,)).fetchone()
                _aid = _ar["author_id"] if _ar else ""
                if _aid:
                    rows = con.execute("SELECT id, folder FROM items WHERE author_id=?", (_aid,)).fetchall()
                else:
                    rows = con.execute("SELECT id, folder FROM items WHERE author=?", (name,)).fetchall()
                trashed, failed = 0, []
                for r in rows:
                    fol = Path(r["folder"] or "")
                    if fol.exists():
                        if move_to_trash(fol):
                            trashed += 1
                        else:
                            failed.append(r["folder"])
                    else:
                        trashed += 1          # 文件已不在，直接清记录
                # 作品都移走后，博主目录如果空了也一起清掉
                author_dirs = set()
                for r in rows:
                    try:
                        author_dirs.add(Path(r["folder"]).parent)
                    except Exception:
                        pass
                for ad in author_dirs:
                    try:
                        if ad.exists():
                            left = [x for x in ad.iterdir() if x.name != ".DS_Store"]
                            if not left:
                                move_to_trash(ad)
                    except Exception:
                        pass
                ids = [str(r["id"]) for r in rows]
                if ids:
                    con.execute("DELETE FROM analysis WHERE item_id IN (%s)" % ",".join(ids))
                if _aid:
                    n = con.execute("DELETE FROM items WHERE author_id=?", (_aid,)).rowcount
                else:
                    n = con.execute("DELETE FROM items WHERE author=?", (name,)).rowcount
                try:
                    con.execute("DELETE FROM custom_authors WHERE name=?", (name,))
                except Exception:
                    pass
                con.commit()
                con.close()
                pins = load_pins()
                if name in (pins.get("authors") or []):
                    pins["authors"] = [x for x in pins["authors"] if x != name]
                    save_pins(pins)
                try:
                    export_csv()
                except Exception:
                    pass
                return self._json({"ok": True, "records": n, "trashed": trashed,
                                   "failed": failed})

            if u.path == "/api/create_author":
                name = (body.get("name") or "").strip()
                pf = (body.get("platform") or "douyin").strip()
                if not name:
                    return self._json({"ok": False, "error": "名字不能为空"})
                # 存到独立表里，这样作品为 0 也能出现在侧栏
                con = db()
                con.execute("""CREATE TABLE IF NOT EXISTS custom_authors(
                    name TEXT, platform TEXT, created_ts INTEGER,
                    PRIMARY KEY(name, platform))""")
                con.execute("INSERT OR IGNORE INTO custom_authors(name, platform, created_ts) VALUES (?,?,?)",
                            (name, pf, int(time.time())))
                con.commit()
                con.close()
                return self._json({"ok": True})

            # ---------- 手动分类（收藏夹等） ----------
            if u.path == "/api/collection/create":
                name = (body.get("name") or "").strip()[:40]
                if not name:
                    return self._json({"ok": False, "error": "名字不能为空"})
                ensure_collections()
                con = db()
                try:
                    con.execute("INSERT INTO collections(name, created_ts) VALUES(?,?)",
                                (name, int(time.time())))
                    con.commit()
                except sqlite3.IntegrityError:
                    con.close()
                    return self._json({"ok": False, "error": "已有同名分类"})
                row = con.execute("SELECT id FROM collections WHERE name=?", (name,)).fetchone()
                con.close()
                return self._json({"ok": True, "id": row["id"], "name": name})

            if u.path == "/api/collection/rename":
                cid = body.get("id")
                name = (body.get("name") or "").strip()[:40]
                if not cid or not name:
                    return self._json({"ok": False, "error": "参数不完整"})
                ensure_collections()
                con = db()
                try:
                    con.execute("UPDATE collections SET name=? WHERE id=?", (name, int(cid)))
                    con.commit()
                except sqlite3.IntegrityError:
                    con.close()
                    return self._json({"ok": False, "error": "已有同名分类"})
                con.close()
                return self._json({"ok": True, "name": name})

            if u.path == "/api/collection/delete":
                cid = body.get("id")
                if not cid:
                    return self._json({"ok": False, "error": "缺少分类"})
                ensure_collections()
                con = db()
                con.execute("DELETE FROM collection_items WHERE collection_id=?", (int(cid),))
                con.execute("DELETE FROM collections WHERE id=?", (int(cid),))
                con.commit()
                con.close()
                return self._json({"ok": True})

            if u.path in ("/api/collection/add", "/api/collection/add_by_name",
                          "/api/collection/remove"):
                ensure_collections()
                name = (body.get("name") or "").strip()[:40]
                cid = body.get("id")
                raw_ids = body.get("item_ids") or []
                if not isinstance(raw_ids, list):
                    return self._json({"ok": False, "error": "参数不完整"})
                ids = []
                for x in raw_ids:
                    try:
                        ids.append(int(x))
                    except Exception:
                        pass
                con = db()
                if u.path == "/api/collection/add_by_name":
                    if not name:
                        con.close()
                        return self._json({"ok": False, "error": "缺少分类名"})
                    con.execute("INSERT OR IGNORE INTO collections(name, created_ts) VALUES(?,?)",
                                (name, int(time.time())))
                    row = con.execute("SELECT id FROM collections WHERE name=?", (name,)).fetchone()
                    if not row:
                        con.close()
                        return self._json({"ok": False, "error": "创建分类失败"})
                    cid = row["id"]
                try:
                    cid = int(cid)
                except Exception:
                    con.close()
                    return self._json({"ok": False, "error": "缺少分类"})
                row = con.execute("SELECT name FROM collections WHERE id=?", (cid,)).fetchone()
                if not row:
                    con.close()
                    return self._json({"ok": False, "error": "分类不存在"})
                cname = row["name"]
                valid = []
                if ids:
                    qs = ",".join("?" * len(ids))
                    valid = [r["id"] for r in con.execute(
                        "SELECT id FROM items WHERE id IN (%s)" % qs, ids).fetchall()]
                changed = 0
                if u.path == "/api/collection/remove":
                    for iid in valid:
                        cur = con.execute(
                            "DELETE FROM collection_items WHERE collection_id=? AND item_id=?",
                            (cid, iid))
                        changed += max(0, cur.rowcount)
                else:
                    for iid in valid:
                        cur = con.execute(
                            "INSERT OR IGNORE INTO collection_items(collection_id,item_id,added_ts) VALUES(?,?,?)",
                            (cid, iid, int(time.time())))
                        changed += max(0, cur.rowcount)
                n = con.execute("SELECT COUNT(*) AS n FROM collection_items WHERE collection_id=?",
                                (cid,)).fetchone()["n"]
                con.commit()
                con.close()
                return self._json({"ok": True, "id": cid, "name": cname,
                                   "changed": changed, "count": n})

            if u.path == "/api/reorder":
                kind = (body.get("kind") or "").strip()
                keys = body.get("keys") or []
                if kind not in ("authors", "platforms") or not isinstance(keys, list):
                    return self._json({"ok": False, "error": "参数不完整"})
                o = load_order()
                o[kind] = [str(k) for k in keys]
                save_order(o)
                return self._json({"ok": True})

            if u.path == "/api/pin":
                kind = (body.get("kind") or "").strip()      # author / platform
                key = (body.get("key") or "").strip()
                on = bool(body.get("pinned"))
                if kind not in ("authors", "platforms") or not key:
                    return self._json({"ok": False, "error": "参数不完整"})
                pins = load_pins()
                arr = set(pins.get(kind) or [])
                if on:
                    arr.add(key)
                else:
                    arr.discard(key)
                pins[kind] = sorted(arr)
                save_pins(pins)
                return self._json({"ok": True, "pins": pins})

            if u.path == "/api/rename_title":
                iid = body.get("item_id")
                t = (body.get("title") or "").strip()
                if not iid or not t:
                    return self._json({"ok": False, "error": "参数不完整"})
                con = db()
                n = con.execute("UPDATE items SET title=? WHERE id=?", (t, iid)).rowcount
                con.commit()
                con.close()
                try:
                    export_csv()
                except Exception:
                    pass
                return self._json({"ok": True, "count": n})

            if u.path == "/api/create_item":
                title = (body.get("title") or "").strip()
                if not title:
                    return self._json({"ok": False, "error": "标题不能为空"})
                author = (body.get("author") or "").strip() or "我的作品"
                pf = (body.get("platform") or "").strip()
                pcn = (body.get("platform_cn") or "").strip()
                if not pf:
                    pf = "manual"
                if not pcn:
                    pcn = {"douyin": "抖音", "xiaohongshu": "小红书", "bilibili": "B站",
                           "youtube": "YouTube", "x": "X", "instagram": "Instagram",
                           "weibo": "微博", "telegram": "Telegram", "tiktok": "TikTok"}.get(pf, "手动")
                typ = (body.get("type") or "图文").strip()
                if typ not in ("图文", "视频", "音频"):
                    typ = "图文"
                safe = re.sub(r'[\\/:*?"<>|]', "_", title)[:50]
                safe = safe + "_" + str(int(time.time() * 1000))[-6:]
                stamp = time.strftime("%Y%m%d")
                folder = ARCHIVE / pcn / author / ("%s_%s" % (stamp, safe))
                folder.mkdir(parents=True, exist_ok=True)
                con = db()
                cur = con.execute("""INSERT INTO items
                    (platform, platform_cn, author, author_id, author_url, title, item_id, type,
                     created_ts, saved_ts, file_count, folder, source_url)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (pf, pcn, author,
                     (body.get("author_id") or "").strip(),
                     (body.get("author_url") or "").strip(), title,
                     "manual_%d" % int(time.time()*1000), typ,
                     int(time.time()), int(time.time()), 0, str(folder), ""))
                con.commit()
                new_id = cur.lastrowid
                con.close()
                return self._json({"ok": True, "id": new_id, "folder": str(folder),
                                   "author": author, "platform": pf, "platform_cn": pcn})

            if u.path == "/api/rename_author":
                old = (body.get("old") or "").strip()
                new = (body.get("new") or "").strip()
                if not old or not new:
                    return self._json({"ok": False, "error": "名字不能为空"})
                con = db()
                row = con.execute("SELECT author_id FROM items WHERE author=? AND author_id<>'' LIMIT 1", (old,)).fetchone()
                aid = row["author_id"] if row else ""
                if aid:
                    # 以稳定 ID 为准：改名会影响这个博主的全部作品
                    n = con.execute("UPDATE items SET author=? WHERE author_id=?", (new, aid)).rowcount
                else:
                    n = con.execute("UPDATE items SET author=? WHERE author=?", (new, old)).rowcount
                try:
                    con.execute("UPDATE custom_authors SET name=? WHERE name=?", (new, old))
                except Exception:
                    pass
                con.commit()
                con.close()
                pins = load_pins()
                if old in (pins.get("authors") or []):
                    pins["authors"] = [new if x == old else x for x in pins["authors"]]
                    save_pins(pins)
                try:
                    export_csv()
                except Exception:
                    pass
                return self._json({"ok": True, "count": n})

            if u.path == "/api/rename_platform":
                pf = (body.get("platform") or "").strip()
                new = (body.get("name") or "").strip()
                if not pf or not new:
                    return self._json({"ok": False, "error": "参数不完整"})
                con = db()
                n = con.execute("UPDATE items SET platform_cn=? WHERE platform=?", (new, pf)).rowcount
                con.commit()
                con.close()
                # 自定义平台名持久化，后续新归档也用这个名字
                cfgp = HOME / ".media-archive" / "platform_names.json"
                try:
                    names = json.loads(cfgp.read_text("utf-8")) if cfgp.exists() else {}
                except Exception:
                    names = {}
                names[pf] = new
                cfgp.write_text(json.dumps(names, ensure_ascii=False, indent=2), "utf-8")
                return self._json({"ok": True, "count": n})

            if u.path == "/api/platforms/add":
                key = (body.get("key") or "").strip().lower()
                name = (body.get("name") or "").strip()
                hosts = [h.strip().lower() for h in (body.get("hosts") or []) if h.strip()]
                if not key or not name:
                    return self._json({"ok": False, "error": "key 和名称都要填"})
                cfgp = HOME / ".media-archive" / "custom_platforms.json"
                try:
                    plats = json.loads(cfgp.read_text("utf-8")) if cfgp.exists() else []
                except Exception:
                    plats = []
                plats = [x for x in plats if x.get("key") != key]
                plats.append({"key": key, "name": name, "hosts": hosts})
                cfgp.write_text(json.dumps(plats, ensure_ascii=False, indent=2), "utf-8")
                return self._json({"ok": True, "platforms": plats})

            if u.path == "/api/delete":
                item_id = body.get("id") or body.get("item_id")   # 兼容两种字段名
                remove_files = bool(body.get("remove_files", True))
                con = db()
                row = con.execute("SELECT folder FROM items WHERE id=?", (item_id,)).fetchone()
                if not row:
                    con.close()
                    return self._json({"error": "记录不存在"}, 404)
                folder = Path(row["folder"])
                if remove_files:
                    try:
                        folder.resolve().relative_to(ARCHIVE.resolve())
                        if folder.exists():
                            if not move_to_trash(folder):
                                con.close()
                                return self._json({"ok": False, "error": "文件移入废纸篓失败，记录已保留"})
                    except ValueError:
                        con.close()
                        return self._json({"ok": False, "error": "路径不在归档目录内"})
                    except Exception as e:
                        con.close()
                        return self._json({"ok": False, "error": "删除文件失败：%s" % str(e)[:80]})
                con.execute("DELETE FROM collection_items WHERE item_id=?", (item_id,))
                try:
                    con.execute("DELETE FROM analysis WHERE item_id=?", (item_id,))
                except Exception:
                    pass
                con.execute("DELETE FROM items WHERE id=?", (item_id,))
                con.commit()
                con.close()
                try:
                    export_csv()
                except Exception:
                    pass
                return self._json({"ok": True})
            if u.path == "/api/delete_batch":
                ids = body.get("ids") or []
                remove_files = bool(body.get("remove_files", True))
                con = db()
                done, failed = 0, []
                for item_id in ids:
                    row = con.execute("SELECT folder FROM items WHERE id=?", (item_id,)).fetchone()
                    if not row:
                        failed.append(item_id)
                        continue
                    folder = Path(row["folder"])
                    if remove_files:
                        try:
                            folder.resolve().relative_to(ARCHIVE.resolve())
                            if folder.exists():
                                move_to_trash(folder)
                        except Exception:
                            pass
                    con.execute("DELETE FROM collection_items WHERE item_id=?", (item_id,))
                    try:
                        con.execute("DELETE FROM analysis WHERE item_id=?", (item_id,))
                    except Exception:
                        pass
                    con.execute("DELETE FROM items WHERE id=?", (item_id,))
                    done += 1
                con.commit()
                con.close()
                try:
                    export_csv()
                except Exception:
                    pass
                return self._json({"ok": True, "deleted": done, "failed": failed})
            if u.path == "/api/prune":
                con = db()
                rows = con.execute("SELECT id, folder, platform, saved_ts FROM items").fetchall()
                removed = 0
                for row in rows:
                    pth = Path(row["folder"])
                    # 保护手动条目和刚创建的条目（1小时内）
                    if row["platform"] == "manual":
                        continue
                    if row["saved_ts"] and (time.time() - row["saved_ts"]) < 3600:
                        continue
                    empty = (not pth.exists()) or (not any(pth.iterdir()))
                    if empty:
                        con.execute("DELETE FROM collection_items WHERE item_id=?", (row["id"],))
                        try:
                            con.execute("DELETE FROM analysis WHERE item_id=?", (row["id"],))
                        except Exception:
                            pass
                        con.execute("DELETE FROM items WHERE id=?", (row["id"],))
                        removed += 1
                con.commit()
                con.close()
                try:
                    export_csv()
                except Exception:
                    pass
                return self._json({"ok": True, "removed": removed})
            if u.path == "/api/check_dir":
                raw = (body.get("path") or "").strip()
                if not raw:
                    return self._json({"ok": False, "error": "请填写路径"})
                pth = Path(raw).expanduser()
                if not pth.exists():
                    return self._json({"ok": False, "error": "目录不存在"})
                if not pth.is_dir():
                    return self._json({"ok": False, "error": "这不是一个目录"})
                if not os.access(str(pth), os.W_OK):
                    return self._json({"ok": False, "error": "没有写入权限"})
                cnt, size = 0, 0
                try:
                    for f in pth.rglob("*"):
                        if f.is_file():
                            cnt += 1
                            size += f.stat().st_size
                            if cnt > 50000:
                                break
                except Exception:
                    pass
                return self._json({"ok": True, "file_count": cnt, "size": size})
            if u.path == "/api/config":
                cur = load_cfg()
                for k, v in body.items():
                    if isinstance(v, dict) and isinstance(cur.get(k), dict):
                        cur[k].update(v)
                    else:
                        cur[k] = v
                CFG_FILE.parent.mkdir(parents=True, exist_ok=True)
                CFG_FILE.write_text(json.dumps(cur, ensure_ascii=False, indent=2), "utf-8")
                try:
                    os.chmod(str(CFG_FILE), 0o600)
                except Exception:
                    pass
                return self._json({"ok": True, "config": cur})
            if u.path.startswith("/api/task/") and u.path.endswith("/cancel"):
                tid = u.path.split("/")[3]
                with TASKS_LOCK:
                    t = TASKS.get(tid)
                    proc = t.get("proc") if t else None
                    if not t:
                        return self._json({"ok": True, "gone": True})
                    if t.get("status") != "running":
                        # 已经结束了（完成/失败/取消），不要再改状态
                        return self._json({"ok": True, "already": t.get("status")})
                    t["status"] = "cancelled"
                    t["finished"] = time.time()
                if proc:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                        time.sleep(0.4)
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except Exception:
                        try:
                            proc.terminate()
                        except Exception:
                            pass
                return self._json({"ok": True})
            if u.path == "/api/prepare_live":
                folder = (body.get("folder") or "").strip()
                if not folder:
                    return self._json({"ok": False, "error": "缺少目录"})
                return self._json(prepare_live_folder(folder))
            if u.path == "/api/import_live":
                iid = body.get("item_id")
                con = db()
                row = con.execute("SELECT folder, title FROM items WHERE id=?", (iid,)).fetchone()
                con.close()
                if not row:
                    return self._json({"ok": False, "error": "记录不存在"})
                prep = prepare_live_folder(row["folder"])
                if not prep.get("ok"):
                    return self._json({"ok": False, "error": "实况配对准备失败", "detail": prep})
                qdir = HOME / ".media-archive"
                qdir.mkdir(parents=True, exist_ok=True)
                (qdir / "import_queue.json").write_text(
                    json.dumps({"folder": row["folder"], "title": row["title"] or "",
                                "ts": time.time()}, ensure_ascii=False), "utf-8")
                return self._json({"ok": True, "dir": row["folder"]})
            if u.path == "/api/extract_audio":
                iid = body.get("item_id")
                con = db()
                row = con.execute("SELECT folder FROM items WHERE id=?", (iid,)).fetchone()
                con.close()
                if not row:
                    return self._json({"ok": False, "error": "记录不存在"})
                folder = Path(row["folder"])
                if not folder.exists():
                    return self._json({"ok": False, "error": "文件夹不存在"})
                vids = [f for f in sorted(folder.iterdir())
                        if f.is_file() and f.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov", ".m4v")
                        and "_live" not in f.stem]
                if not vids:
                    return self._json({"ok": False, "error": "这条没有视频文件"})
                video = vids[0]
                # 1) 优先无损复制音轨到 m4a
                ex_dir = folder / "_extras"
                ex_dir.mkdir(parents=True, exist_ok=True)
                out = ex_dir / "audio.m4a"
                r = subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(video),
                                    "-vn", "-c:a", "copy", str(out)],
                                   capture_output=True, text=True, timeout=300)
                if r.returncode != 0 or not out.exists() or out.stat().st_size < 1024:
                    # 2) 退回转码 mp3
                    out = ex_dir / "audio.mp3"
                    r = subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(video),
                                        "-vn", "-c:a", "libmp3lame", "-q:a", "2", str(out)],
                                       capture_output=True, text=True, timeout=600)
                if r.returncode != 0 or not out.exists() or out.stat().st_size < 1024:
                    return self._json({"ok": False, "error": "提取失败（视频可能没有音轨）",
                                       "log": (r.stderr or "")[-400:]})
                return self._json({"ok": True, "file": out.name,
                                   "path": str(out),
                                   "size": out.stat().st_size})
            if u.path == "/api/source/login":
                platform = (body.get("platform") or "").strip()
                if platform not in ("douyin", "xhs"):
                    return self._json({"ok": False, "error": "不支持的平台"}, 400)
                try:
                    if batch.login_state(platform):
                        return self._json({"ok": False, "already": True,
                                           "message": "已登录，无需重复扫码",
                                           "login": login_status_public()})
                except Exception:
                    pass
                with LOGIN_LOCK:
                    st = LOGIN_STATE.get("state")
                if st in ("waiting_tasks", "waiting_scan"):
                    return self._json({"ok": False, "error": "登录进行中",
                                       "login": login_status_public()})
                QUEUE_PAUSED.set()
                _login_set(platform=platform, state="waiting_tasks", started=time.time(),
                           cancel=False, message="准备登录…")
                threading.Thread(target=login_worker, args=(platform,), daemon=True).start()
                return self._json({"ok": True, "login": login_status_public()})
            if u.path == "/api/source/login/cancel":
                _login_set(cancel=True)
                with LOGIN_LOCK:
                    proc = LOGIN_STATE.get("proc")
                if proc:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                return self._json({"ok": True, "login": login_status_public()})
            if u.path == "/api/archive_batch":
                raw = body.get("urls") or body.get("text") or ""
                if isinstance(raw, list):
                    raw = "\n".join(str(x) for x in raw)
                urls = batch.normalize_urls(raw)[:200]
                if not urls:
                    return self._json({"ok": False, "error": "没有解析到链接"})
                ids = [run_archive(u) for u in urls]
                return self._json({"ok": True, "count": len(ids), "task_ids": ids})
            if u.path == "/api/reveal":
                rp = resolve_under_archive(body.get("path") or "")
                if rp and rp.exists():
                    subprocess.run(["open", "-R", str(rp)])
                    return self._json({"ok": True})
                return self._json({"error": "路径不存在"}, 400)
            return self._json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:
            return self._json({"error": str(e)}, 500)

    def _media(self, p):
        path = Path(p).resolve()
        try:
            path.relative_to(ARCHIVE.resolve())
        except Exception:
            return self._json({"error": "forbidden"}, 403)
        if not path.exists():
            return self._json({"error": "missing"}, 404)
        size = path.stat().st_size
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        code = 200
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                a, b = m.group(1), m.group(2)
                if a:
                    start = int(a)
                    if b:
                        end = int(b)
                elif b:
                    # bytes=-N：最后 N 字节
                    n = int(b)
                    start = max(0, size - n) if n > 0 else 0
                    end = size - 1
                if start < 0:
                    start = 0
                if end >= size:
                    end = size - 1
                if start >= size or start > end:
                    # 越界/非法区间：按 RFC 返回 416，别把 Content-Length 写成负数
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                code = 206
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "private, max-age=86400, immutable")
        self.send_header("Content-Length", str(end - start + 1))
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        with open(str(path), "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(65536, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                left -= len(chunk)


def _migrate_manual_types():
    try:
        con = db()
        con.execute("UPDATE items SET type='图文' WHERE platform='manual' AND type='视频'")
        con.commit()
        con.close()
    except Exception:
        pass


def main():
    port = 8787
    for a in sys.argv[1:]:
        if a.startswith("--port="):
            port = int(a.split("=", 1)[1])
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    _migrate_manual_types()
    try:
        ensure_collections()
    except Exception:
        pass
    try:
        backfill_author_ids()
        canonicalize_authors()
    except Exception as e:
        print("作者 ID 归一化失败：%s" % e)
    cleanup_stale_downloader()
    try:
        start_douyin_chrome()
    except Exception:
        pass
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("media-archive Web 端：http://127.0.0.1:%d" % port)
    print("归档目录：%s" % ARCHIVE)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
