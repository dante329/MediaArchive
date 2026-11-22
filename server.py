#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""media-archive Web 端：本地网页管理归档内容

    python3 server.py            # 默认 http://127.0.0.1:8787
    python3 server.py --port 9000
"""
import errno
import asyncio
import json
import mimetypes
import os
import queue
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
PID_DIR = HOME / ".media-archive"


def pid_file_for(port):
    """按端口分文件：同时跑多个实例（比如临时起一个调试端口）时不会互相踩"""
    return PID_DIR / ("server-%d.pid" % port)
SERVER_STARTED = time.time()


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

# ---------- 解析历史（成功/失败留痕，供信号灯窗口查看） ----------
HISTORY_FILE = HOME / ".media-archive" / "history.json"
HISTORY = []
HISTORY_LOCK = threading.Lock()
HISTORY_MAX = 400
_LOCAL_SEQ = [0]
_LOCAL_SEQ_LOCK = threading.Lock()


def next_local_item_id():
    with _LOCAL_SEQ_LOCK:
        _LOCAL_SEQ[0] += 1
        return "local_%d_%d" % (int(time.time() * 1000), _LOCAL_SEQ[0])


def _history_load():
    try:
        if HISTORY_FILE.exists():
            data = json.loads(HISTORY_FILE.read_text("utf-8"))
            if isinstance(data, list):
                HISTORY.extend(data[-HISTORY_MAX:])
    except Exception:
        pass


def history_append(rec):
    with HISTORY_LOCK:
        HISTORY.append(rec)
        while len(HISTORY) > HISTORY_MAX:
            HISTORY.pop(0)
        try:
            HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            HISTORY_FILE.write_text(json.dumps(HISTORY, ensure_ascii=False), "utf-8")
        except Exception:
            pass


_history_load()


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
    _hi, _ha, _hp, hc = _hidden_sets()
    out = []
    for r in rows:
        if r["id"] in hc:
            continue
        d = dict(r)
        d["items"] = [dict(x) for x in con.execute("""
            SELECT i.id, i.title, i.author
            FROM collection_items ci JOIN items i ON i.id = ci.item_id
            WHERE ci.collection_id = ?
            ORDER BY ci.added_ts DESC, i.id DESC""", (d["id"],)).fetchall()]
        out.append(d)
    con.close()
    return out


HIDDEN_FILE = HOME / ".media-archive" / "hidden.json"
_HIDDEN_LOCK = threading.Lock()


def load_hidden():
    """App 内隐藏名单：作品 / 博主 / 平台 / 分类。只是不展示，数据都还在。"""
    try:
        d = json.loads(HIDDEN_FILE.read_text("utf-8"))
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    return {"items": [], "authors": [], "platforms": [], "collections": []}


def save_hidden(h):
    HIDDEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _HIDDEN_LOCK:
        tmp = HIDDEN_FILE.with_name(HIDDEN_FILE.name + ".tmp")
        tmp.write_text(json.dumps(h, ensure_ascii=False), "utf-8")
        os.replace(str(tmp), str(HIDDEN_FILE))


def _hidden_sets():
    h = load_hidden()
    def _ints(v):
        out = set()
        for x in (v or []):
            try:
                out.add(int(x))
            except Exception:
                pass
        return out
    return (_ints(h.get("items")),
            {str(x) for x in (h.get("authors") or [])},
            {str(x) for x in (h.get("platforms") or [])},
            _ints(h.get("collections")))


def _hidden_where(con_kind):
    """返回 (where 片段列表, 参数列表)，把隐藏的东西从 SQL 里排掉。"""
    hi, ha, hp, hc = _hidden_sets()
    where, args = [], []
    if con_kind in ("items", "stats"):
        if hi:
            where.append("id NOT IN (" + ",".join("?" * len(hi)) + ")")
            args += sorted(hi)
        if ha:
            where.append("(CASE WHEN author_id IS NULL OR author_id='' THEN author ELSE author_id END)"
                         " NOT IN (" + ",".join("?" * len(ha)) + ")")
            args += sorted(ha)
        if hp:
            where.append("platform NOT IN (" + ",".join("?" * len(hp)) + ")")
            args += sorted(hp)
    return where, args


# ---------- 拼音索引（搜索博主/平台名支持拼音、首字母、大小写不敏感） ----------
try:
    from pypinyin import lazy_pinyin as _lazy_py, Style as _PyStyle

    def _py_full(s):
        return "".join(_lazy_py(s or "")).lower()

    def _py_ini(s):
        return "".join(_lazy_py(s or "", style=_PyStyle.FIRST_LETTER)).lower()
except Exception:
    def _py_full(s):
        return ""

    def _py_ini(s):
        return ""

PINYIN_CACHE = {}
PINYIN_LOCK = threading.Lock()


def pinyin_of(s):
    s = (s or "").strip()
    if not s:
        return ("", "")
    with PINYIN_LOCK:
        v = PINYIN_CACHE.get(s)
    if v is None:
        v = (_py_full(s), _py_ini(s))
        with PINYIN_LOCK:
            PINYIN_CACHE[s] = v
    return v


def q_items(q="", platform="", author="", limit=200, offset=0, collection=None, author_key=""):
    try:
        # 行模式要拿全量作品分组，上限放宽到 6000（普通翻页仍走默认 200）
        limit = max(1, min(6000, int(limit)))
    except Exception:
        limit = 200
    try:
        offset = max(0, int(offset))
    except Exception:
        offset = 0
    con = db()
    where, args = _hidden_where("items")
    if q:
        q_escaped = q.replace("%", "\\%").replace("_", "\\_")
        parts = ["title LIKE ? ESCAPE '\\'", "author LIKE ? ESCAPE '\\'",
                 "source_url LIKE ? ESCAPE '\\'", "platform_cn LIKE ? ESCAPE '\\'"]
        args += ["%%%s%%" % q_escaped] * 4
        # 博主名 / 平台名额外支持拼音、首字母、大小写不敏感
        ql = q.strip().lower()
        try:
            con0 = db()
            names = [r[0] for r in con0.execute("SELECT DISTINCT author FROM items")]
            plats = [r[0] for r in con0.execute("SELECT DISTINCT platform_cn FROM items")]
            con0.close()
        except Exception:
            names, plats = [], []
        ma = [n for n in names if n and (ql in pinyin_of(n)[0] or ql in pinyin_of(n)[1])]
        mp = [p for p in plats
              if p and (ql in p.lower() or ql in pinyin_of(p)[0] or ql in pinyin_of(p)[1])]
        if ma:
            parts.append("author IN (%s)" % ",".join("?" * len(ma)))
            args += ma
        if mp:
            parts.append("platform_cn IN (%s)" % ",".join("?" * len(mp)))
            args += mp
        where.append("(" + " OR ".join(parts) + ")")
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
        # 注意：这里是只读的，任何「找回被挪走的目录」都走显式的 /api/rescan
        fc = r.get("file_count") or 0
        folder = r.get("folder") or ""
        folder_exists = bool(folder) and Path(folder).exists()
        if fc > 0 and folder_exists:
            # 列表不需要完整 files（省 N+1 scandir），但卡片缩略图至少要一个图片和一个视频 URL。
            # 只扫一次目录，找到第一个 image 和第一个 video 就停，比 list_files 快得多。
            r["files"] = _first_media_thumbs(folder)
            r["missing"] = False
            r["empty"] = False
        else:
            r["files"] = list_files(folder)
            # empty = 目录在但还没放文件（刚新建的作品）；missing = 目录整个不见了
            r["empty"] = folder_exists and not r["files"]
            r["missing"] = (not folder_exists) or (len(r["files"]) == 0)
        r["folder_exists"] = folder_exists
        r["author_key"] = (r.get("author_id") or "").strip() or (r.get("author") or "")
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


def _first_media_thumbs(folder):
    """只取第一个图片和第一个视频，用于列表卡片缩略图。比 list_files 快得多。"""
    p = Path(folder)
    if not p.exists():
        return []
    img_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic"}
    vid_exts = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
    # 实况伴生视频（01_live.mp4 / 01.mov）不算「这个作品的视频」，
    # 规则要和前端 renderItems 里的 /_live|^\d+\.mov$/ 保持一致，
    # 否则实况图文的卡片会拿伴生小视频当封面，真正的视频反而被挤掉。
    live_re = re.compile(r"_live$|^\d+\.mov$", re.I)
    first_img = None
    first_vid = None
    first_live = None
    try:
        entries = sorted(os.scandir(p), key=lambda e: e.name)
    except Exception:
        return []
    for entry in entries:
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
            name = entry.name
            low = name.lower()
            if low.endswith(".poster.jpg") or low.endswith(".web.mp4"):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in img_exts and ext not in vid_exts:
                continue
            st = entry.stat()
            rec = {
                "name": name,
                "url": "/media?v=%d&p=%s" % (int(st.st_mtime),
                                              urllib.parse.quote(str(p / name))),
                "size": st.st_size,
                "kind": "video" if ext in vid_exts else "image",
            }
            if ext in vid_exts:
                stem = os.path.splitext(name)[0]
                if live_re.search(stem) or live_re.search(name):
                    if first_live is None:
                        first_live = rec
                elif first_vid is None:
                    first_vid = rec
            elif ext in img_exts and first_img is None:
                first_img = rec
            if first_img and (first_vid or first_live):
                break
        except Exception:
            continue
    if first_vid is None:
        first_vid = first_live
    out = []
    if first_img:
        out.append(first_img)
    if first_vid:
        out.append(first_vid)
    return out


# =======================================================
#  归属 / 目录结构 / 文件上传
#
#  归档目录固定三层：<归档根>/<平台中文名>/<博主>/<作品目录>
#  作品的归属以数据库为准，目录结构只是它的物理体现。
#  所有「改归属」的操作都是显式的（用户点了按钮），浏览列表绝不写库。
# =======================================================
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".bmp", ".tif", ".tiff"}
VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi"}
AUDIO_EXT = {".m4a", ".mp3", ".wav", ".aac", ".flac"}
MEDIA_EXT = IMAGE_EXT | VIDEO_EXT | AUDIO_EXT
# 扫描目录时要跳过的：工具缓存目录 + 系统垃圾目录
SKIP_DIRS = {"_extras", ".git", ".Trashes", ".Spotlight-V100", ".fseventsd"}

_PLAT_TABLES = {"ts": 0.0, "k2cn": {}, "cn2k": {}}


def safe_dir_name(s, limit=30):
    """目录名清洗，规则跟 engine.safe 保持一致（避免两套命名）"""
    s = re.sub(r'[\\/:*?"<>|\r\n\t#]', " ", s or "")
    s = re.sub(r"\s+", " ", s).strip()[:limit].strip()
    while len(s.encode("utf-8")) > 200:
        s = s[:-1]
    return s.strip() or "untitled"


def safe_file_name(name):
    """上传文件名清洗：去掉路径和危险字符，保留扩展名"""
    name = os.path.basename(str(name or "")).strip()
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name)
    name = name.lstrip(".").strip()
    return name[:120] or ("file_%d" % int(time.time()))


def unique_dest(path):
    """目标已存在就加序号，绝不覆盖用户已有的文件"""
    p = Path(path)
    if not p.exists():
        return p
    stem, suf = p.stem, p.suffix
    for i in range(1, 500):
        cand = p.with_name("%s_%d%s" % (stem, i, suf))
        if not cand.exists():
            return cand
    return p.with_name("%s_%d%s" % (stem, int(time.time()), suf))


def platform_tables():
    """平台 key ↔ 中文名（内置 + 用户自定义 + 用户改名 + DB 里出现过的）"""
    now = time.time()
    if now - _PLAT_TABLES["ts"] < 30 and _PLAT_TABLES["k2cn"]:
        return _PLAT_TABLES["k2cn"], _PLAT_TABLES["cn2k"]
    k2cn, cn2k = {}, {}
    try:
        import engine
        for row in getattr(engine, "PLATFORMS", []):
            k2cn[row[0]] = row[2]
            cn2k.setdefault(row[2], row[0])
    except Exception:
        pass
    cfgdir = HOME / ".media-archive"
    try:
        custom = json.loads((cfgdir / "custom_platforms.json").read_text("utf-8"))
        for p in (custom if isinstance(custom, list) else []):
            if isinstance(p, dict) and p.get("key") and p.get("name"):
                k2cn[p["key"]] = p["name"]
                cn2k.setdefault(p["name"], p["key"])
    except Exception:
        pass
    try:
        names = json.loads((cfgdir / "platform_names.json").read_text("utf-8"))
        for k, v in (names or {}).items():
            if k and v:
                k2cn[k] = v
                cn2k.setdefault(v, k)
    except Exception:
        pass
    try:
        con = db()
        for r in con.execute("SELECT DISTINCT platform, platform_cn FROM items"):
            if r["platform"] and r["platform_cn"]:
                k2cn.setdefault(r["platform"], r["platform_cn"])
                cn2k.setdefault(r["platform_cn"], r["platform"])
        con.close()
    except Exception:
        pass
    k2cn.setdefault("manual", "手动")
    cn2k.setdefault("手动", "manual")
    _PLAT_TABLES.update({"ts": now, "k2cn": k2cn, "cn2k": cn2k})
    return k2cn, cn2k


def platform_cn_for(key):
    return platform_tables()[0].get(key or "", "手动")


def platform_key_for(cn):
    return platform_tables()[1].get(cn or "", "")


def author_key_of(row):
    """侧栏分组用的 key：有稳定 ID 用 ID，没有就用名字（跟 q_authors 一致）"""
    aid = (row["author_id"] if "author_id" in row.keys() else "") or ""
    return aid.strip() or (row["author"] or "")


def resolve_author_identity(con, name, platform=""):
    """按博主名（+平台）取出这个博主的稳定身份。

    新建作品 / 搬作品过来时都用它，否则侧栏会裂成两个同名博主，
    按 author_key 过滤也匹配不上。platform 为空时不限平台。
    """
    name = (name or "").strip()
    out = {"author": name, "author_id": "", "author_url": "",
           "platform": (platform or "").strip(), "platform_cn": ""}
    if not name:
        out["platform"] = out["platform"] or "manual"
        out["platform_cn"] = platform_cn_for(out["platform"])
        return out
    sql = ("SELECT author_id, author_url, platform, platform_cn FROM items"
           " WHERE author=?")
    args = [name]
    if out["platform"]:
        sql += " AND platform=?"
        args.append(out["platform"])
    sql += (" ORDER BY (CASE WHEN author_id IS NULL OR author_id='' THEN 1 ELSE 0 END),"
            " saved_ts DESC LIMIT 1")
    try:
        r = con.execute(sql, args).fetchone()
    except Exception:
        r = None
    if r:
        out["author_id"] = r["author_id"] or ""
        out["author_url"] = r["author_url"] or ""
        if not out["platform"]:
            out["platform"] = r["platform"] or ""
        out["platform_cn"] = r["platform_cn"] or ""
    if not out["platform"]:
        out["platform"] = platform_key_for(out["platform_cn"]) or "manual"
    if not out["platform_cn"]:
        out["platform_cn"] = platform_cn_for(out["platform"])
    return out


def author_dir_for(platform_cn, author):
    """某个博主对应的归档目录：<根>/<平台中文名>/<博主>"""
    return ARCHIVE / safe_dir_name(platform_cn or "手动", 30) / safe_dir_name(author or "未知博主", 30)


def path_identity(folder):
    """从作品目录路径反推「平台 / 博主」（用户在 Finder 里挪过目录时以此为准）"""
    try:
        rel = Path(folder).resolve().relative_to(ARCHIVE.resolve())
    except Exception:
        return None
    parts = rel.parts
    if len(parts) < 3:
        return None
    return {"platform_cn": parts[0], "author": parts[1],
            "platform": platform_key_for(parts[0])}


def refresh_item_media(con, item_id, folder):
    """按目录里的实际文件刷新 file_count 和类型。

    类型规则：只有音频 → 音频；有视频且没图片 → 视频；其余 → 图文。
    目录为空时保留原类型（用户刚建的空作品不该被改成图文）。
    """
    files = list_files(folder)
    kinds = set(f["kind"] for f in files)
    typ = None
    if files:
        if kinds == {"audio"}:
            typ = "音频"
        elif "video" in kinds and "image" not in kinds:
            typ = "视频"
        else:
            typ = "图文"
    fields = {"file_count": len(files)}
    if typ:
        fields["type"] = typ
    sets = ",".join("%s=?" % k for k in fields)
    con.execute("UPDATE items SET %s WHERE id=?" % sets,
                list(fields.values()) + [item_id])
    return {"file_count": len(files), "type": typ, "files": files}


def set_item_identity(con, item_id, ident, folder=None):
    """把一条记录的归属改成 ident（可选同时改目录），返回被改的字段"""
    fields = {"author": ident.get("author") or "",
              "author_id": ident.get("author_id") or "",
              "author_url": ident.get("author_url") or "",
              "platform": ident.get("platform") or "manual",
              "platform_cn": ident.get("platform_cn") or "手动"}
    if folder is not None:
        fields["folder"] = str(folder)
    sets = ",".join("%s=?" % k for k in fields)
    con.execute("UPDATE items SET %s WHERE id=?" % sets,
                list(fields.values()) + [item_id])
    return fields


def _set_fields(con, item_id, fields):
    if not fields:
        return
    sets = ",".join("%s=?" % k for k in fields)
    con.execute("UPDATE items SET %s WHERE id=?" % sets,
                list(fields.values()) + [item_id])


def scan_folder_index():
    """{作品目录名: [实际路径]}。只扫「平台/博主/作品」这三层，很便宜。"""
    m = {}
    try:
        plats = list(os.scandir(str(ARCHIVE)))
    except Exception as e:
        print("[warn] 扫描归档目录失败：%s" % e, file=sys.stderr)
        return m
    for plat in plats:
        if not plat.is_dir() or plat.name in SKIP_DIRS:
            continue
        try:
            authors = list(os.scandir(plat.path))
        except Exception:
            continue
        for au in authors:
            if not au.is_dir() or au.name in SKIP_DIRS:
                continue
            try:
                items = list(os.scandir(au.path))
            except Exception:
                continue
            for it in items:
                if it.is_dir() and it.name not in SKIP_DIRS:
                    m.setdefault(it.name, []).append(Path(it.path))
    return m


def claimed_folders(con, exclude_id=None):
    """数据库里已经有主的目录集合；重定位时绝不能指过去，否则两条记录共用一个文件夹"""
    out = set()
    try:
        rows = con.execute("SELECT id, folder FROM items").fetchall()
    except Exception:
        return out
    for a in rows:
        if exclude_id is not None and a["id"] == exclude_id:
            continue                      # 自己原来占的位置要让出来
        f = a["folder"] or ""
        if not f:
            continue
        try:
            out.add(str(Path(f).resolve()))
        except Exception:
            out.add(f)
    return out


def locate_folder(name, index=None, exclude=None):
    """按目录名定位作品文件夹，返回 (路径 或 None, 原因)。

    原因用于告诉用户为什么没自动修好：
      ""          → 找到了，唯一匹配
      "dup_name"  → 归档里有多个同名目录，不敢猜
      "claimed"   → 唯一的同名目录已经是别的作品的，不能抢
      "not_found" → 归档里根本没有这个目录名
    """
    if not name:
        return None, "not_found"
    idx = index if index is not None else scan_folder_index()
    skip = exclude or set()
    all_cands, free = [], []
    for p in (idx.get(name) or []):
        if not p.exists():
            continue
        all_cands.append(p)
        try:
            taken = str(p.resolve()) in skip
        except Exception:
            taken = str(p) in skip
        if not taken:
            free.append(p)
    if not all_cands:
        return None, "not_found"
    if len(free) == 1:
        return free[0], ""
    if not free:
        return None, "claimed"
    return None, "dup_name"


def find_folder_by_name(name, index=None, exclude=None):
    """按目录名找回作品文件夹；只在「全库唯一且没被别人占用」时才认"""
    return locate_folder(name, index, exclude)[0]


def relocate_row(con, row, index=None, claimed=None):
    """记录里的目录不见了 → 按目录名唯一匹配找回来。返回 (新路径 或 None, 原因)。

    找到后按新位置回填平台/博主（目录结构就是归属），并沿用目标博主的稳定 ID。
    claimed: 已被其它记录占用的目录集合，避免两条记录指到同一个文件夹。
    """
    old = row["folder"] or ""
    if not old or Path(old).exists():
        return None, ""
    new, reason = locate_folder(Path(old).name, index, claimed)
    if not new:
        return None, reason or "not_found"
    ident = path_identity(new)
    fields = {"folder": str(new)}
    if ident:
        other = resolve_author_identity(con, ident["author"], ident["platform"] or "")
        fields.update({"author": ident["author"],
                       "platform_cn": ident["platform_cn"],
                       "author_id": other["author_id"],
                       "author_url": other["author_url"]})
        if ident["platform"]:
            fields["platform"] = ident["platform"]
    files = list_files(new)
    fields["file_count"] = len(files)
    _set_fields(con, row["id"], fields)
    if claimed is not None:
        try:
            claimed.add(str(new.resolve()))
        except Exception:
            claimed.add(str(new))
    return new, ""


def repair_index(only_id=None):
    """显式修复：把「目录不见了」的记录找回来 + 刷新文件数/类型。

    只在用户点按钮时跑，不在浏览列表时跑（浏览是只读的）。
    修不好的会在 details 里说明原因，前端据此提示用户手动指路。
    """
    REASON_CN = {"dup_name": "归档里有多个同名目录，不敢猜",
                 "claimed": "同名目录已经是别的作品的，不能抢",
                 "not_found": "归档里找不到这个目录"}
    con = db()
    stats = {"scanned": 0, "relocated": 0, "refreshed": 0, "still_missing": 0,
             "ambiguous": 0, "blocked": 0, "ids": [], "details": []}
    changed = False
    try:
        if only_id is not None:
            rows = con.execute("SELECT * FROM items WHERE id=?", (int(only_id),)).fetchall()
        else:
            rows = con.execute("SELECT * FROM items").fetchall()
        claimed = claimed_folders(con)
        index = scan_folder_index()
        for r in rows:
            stats["scanned"] += 1
            folder = r["folder"] or ""
            if folder and Path(folder).exists():
                # 目录在：只把文件数/类型刷新一下（目录被外部改过也能对上）
                before = r["file_count"] or 0
                info = refresh_item_media(con, r["id"], folder)
                if info["file_count"] != before:
                    changed = True
                    stats["refreshed"] += 1
                continue
            if not folder:
                stats["still_missing"] += 1
                stats["details"].append({"id": r["id"], "title": r["title"],
                                         "reason": "记录里没有目录"})
                continue
            # 自己原来占的位置先让出来，否则永远匹配不上
            try:
                claimed.discard(str(Path(folder).resolve()))
            except Exception:
                claimed.discard(folder)
            new, reason = relocate_row(con, r, index, claimed)
            if new:
                changed = True
                stats["relocated"] += 1
                stats["ids"].append(r["id"])
            else:
                stats["still_missing"] += 1
                if reason == "dup_name":
                    stats["ambiguous"] += 1
                elif reason == "claimed":
                    stats["blocked"] += 1
                stats["details"].append({
                    "id": r["id"], "title": r["title"], "folder": folder,
                    "reason": REASON_CN.get(reason, reason)})
        if changed:
            con.commit()
    finally:
        con.close()
    _PLAT_TABLES["ts"] = 0.0
    if changed:
        try:
            export_csv()
        except Exception:
            pass
    return stats


def cleanup_empty_dir(d, depth=0):
    """博主/平台目录空了就顺手清掉，别在归档里留空壳（最多往上清两层）"""
    try:
        d = Path(d)
        if depth > 2 or not d.exists() or not d.is_dir():
            return
        if d.resolve() == ARCHIVE.resolve():
            return
        left = [x for x in d.iterdir() if x.name != ".DS_Store"]
        if left:
            return
        for junk in d.iterdir():
            try:
                junk.unlink()
            except Exception:
                pass
        d.rmdir()
        cleanup_empty_dir(d.parent, depth + 1)
    except Exception:
        pass


def move_item_folder(row, ident, mode="move"):
    """把作品目录搬到目标博主目录下（move）或复制一份（copy）。

    返回新目录 Path；mode="copy" 时源目录保留。目录名冲突自动加序号，绝不覆盖。
    """
    src = Path(row["folder"] or "")
    dest_dir = author_dir_for(ident.get("platform_cn"), ident.get("author"))
    dest_dir.mkdir(parents=True, exist_ok=True)
    if not src.exists():
        # 源目录已经不在了：只把记录指到新位置（后面 rescan 还能按名字找回文件）
        return dest_dir / (Path(row["folder"] or "item").name)
    if mode == "copy":
        dest = unique_dest(dest_dir / src.name)
        shutil.copytree(str(src), str(dest), symlinks=True,
                        ignore=shutil.ignore_patterns(*SKIP_DIRS))
        return dest
    if src.parent.resolve() == dest_dir.resolve():
        return src                      # 已经在这个博主下了，不用搬
    dest = unique_dest(dest_dir / src.name)
    shutil.move(str(src), str(dest))
    cleanup_empty_dir(src.parent)
    return dest


def author_group_rows(con, key="", name="", platform=""):
    """取出侧栏「一个博主」对应的全部记录。

    key 优先（稳定 ID，没有 ID 的组 key 就是名字）；platform 用来区分同名不同平台。
    """
    key = (key or "").strip()
    name = (name or "").strip()
    if not key and not name:
        return []
    sql = ("SELECT * FROM items WHERE (CASE WHEN author_id IS NULL OR author_id=''"
           " THEN author ELSE author_id END) = ?")
    args = [key or name]
    if platform:
        sql += " AND platform=?"
        args.append(platform)
    return con.execute(sql, args).fetchall()


def find_author_conflicts(con, name, exclude_key="", exclude_platform=""):
    """重名检查：目标名字是否已经被「另一个博主」占用。

    返回 None 表示没冲突；否则返回冲突方的信息，前端据此提示合并。
    """
    name = (name or "").strip()
    if not name:
        return None
    rows = con.execute("SELECT * FROM items WHERE author=?", (name,)).fetchall()
    groups = {}
    for r in rows:
        k = (r["platform"] or "", author_key_of(r))
        groups.setdefault(k, []).append(r)
    # 手工建但还没有作品的博主也算占用
    customs = []
    try:
        con.execute("""CREATE TABLE IF NOT EXISTS custom_authors(
            name TEXT, platform TEXT, created_ts INTEGER,
            PRIMARY KEY(name, platform))""")
        for c in con.execute("SELECT name, platform FROM custom_authors WHERE name=?",
                             (name,)).fetchall():
            customs.append(c)
    except Exception:
        pass
    hit = None
    for (plat, key), rs in groups.items():
        if key == (exclude_key or "").strip() and plat == (exclude_platform or ""):
            continue                      # 就是它自己，不算冲突
        hit = {"name": name, "platform": plat, "author_key": key,
               "items": len(rs),
               "platforms": sorted({x["platform_cn"] or "" for x in rs} - {""}),
               "author_id": next((x["author_id"] for x in rs if x["author_id"]), ""),
               "author_url": next((x["author_url"] for x in rs if x["author_url"]), "")}
        break
    if hit:
        return hit
    for c in customs:
        if (c["platform"] or "") == (exclude_platform or "") and name == (exclude_key or "").strip():
            continue
        return {"name": name, "platform": c["platform"] or "", "author_key": name,
                "items": 0, "platforms": [], "author_id": "", "author_url": "",
                "empty_custom": True}
    return None


def rename_author_group(con, old, new, key="", platform=""):
    """重命名一个博主组：改记录 + 把磁盘上的博主目录一起改名。

    只动 key/platform 圈定的那一组，同名不同平台的另一个博主不受影响。
    """
    rows = author_group_rows(con, key, old, platform)
    if not rows:
        return {"count": 0, "moved": 0}
    ident = resolve_author_identity(con, new, platform)
    ident["author"] = new
    moved = 0
    for r in rows:
        fields = {"author": new}
        # 稳定 ID 沿用原来那一组的（改名不换人）
        if r["author_id"]:
            fields["author_id"] = r["author_id"]
            fields["author_url"] = r["author_url"] or ident["author_url"]
        if not platform:
            fields["platform"] = r["platform"] or ident["platform"]
            fields["platform_cn"] = r["platform_cn"] or ident["platform_cn"]
        else:
            fields["platform"] = platform
            fields["platform_cn"] = platform_cn_for(platform)
        old_folder = r["folder"] or ""
        if old_folder and Path(old_folder).exists():
            try:
                dest_dir = author_dir_for(fields.get("platform_cn") or r["platform_cn"], new)
                dest_dir.mkdir(parents=True, exist_ok=True)
                if Path(old_folder).parent.resolve() != dest_dir.resolve():
                    dest = unique_dest(dest_dir / Path(old_folder).name)
                    shutil.move(old_folder, str(dest))
                    cleanup_empty_dir(Path(old_folder).parent)
                    fields["folder"] = str(dest)
                    moved += 1
            except Exception as e:
                print("[warn] 改名搬目录失败 #%s：%s" % (r["id"], e), file=sys.stderr)
        _set_fields(con, r["id"], fields)
    try:
        con.execute("UPDATE custom_authors SET name=? WHERE name=?", (new, old))
    except Exception:
        pass
    return {"count": len(rows), "moved": moved}


def merge_author_group(con, old, target, key="", platform="", target_key="",
                       target_platform=""):
    """合并：把 old 这一组整体并进已存在的 target（记录 + 目录 + 稳定 ID 全统一）"""
    rows = author_group_rows(con, key, old, platform)
    if not rows:
        return {"count": 0, "moved": 0}
    trows = author_group_rows(con, target_key, target, target_platform)
    ident = resolve_author_identity(con, target, target_platform)
    if trows:
        ident["author_id"] = next((x["author_id"] for x in trows if x["author_id"]),
                                  ident["author_id"])
        ident["author_url"] = next((x["author_url"] for x in trows if x["author_url"]),
                                   ident["author_url"])
        ident["platform"] = target_platform or trows[0]["platform"] or ident["platform"]
        ident["platform_cn"] = (trows[0]["platform_cn"]
                                or platform_cn_for(ident["platform"]))
    ident["author"] = target
    moved = 0
    for r in rows:
        fields = {"author": target,
                  "author_id": ident["author_id"],
                  "author_url": ident["author_url"]}
        # 目标组没有稳定 ID 时，保留各行自己的平台，别把不同平台的作品混成一堆
        if ident["author_id"]:
            fields["platform"] = ident["platform"]
            fields["platform_cn"] = ident["platform_cn"]
        old_folder = r["folder"] or ""
        if old_folder and Path(old_folder).exists():
            try:
                pcn = fields.get("platform_cn") or r["platform_cn"]
                dest_dir = author_dir_for(pcn, target)
                dest_dir.mkdir(parents=True, exist_ok=True)
                if Path(old_folder).parent.resolve() != dest_dir.resolve():
                    dest = unique_dest(dest_dir / Path(old_folder).name)
                    shutil.move(old_folder, str(dest))
                    cleanup_empty_dir(Path(old_folder).parent)
                    fields["folder"] = str(dest)
                    moved += 1
            except Exception as e:
                print("[warn] 合并搬目录失败 #%s：%s" % (r["id"], e), file=sys.stderr)
        _set_fields(con, r["id"], fields)
    try:
        con.execute("DELETE FROM custom_authors WHERE name=?", (old,))
    except Exception:
        pass
    return {"count": len(rows), "moved": moved}


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
    pin_list = list(load_pins().get("authors") or [])
    pin_set = set(pin_list)
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
    _hi, ha, hp, _hc = _hidden_sets()
    for d in out:
        ak = d.get("author_key") or ""
        an = d.get("author") or ""
        d["pinned"] = ak in pin_set or an in pin_set
        d.pop("saved_ts", None)
    # 隐藏过的博主 / 被隐藏平台下的博主都不再出现
    out = [d for d in out
           if d.get("author_key") not in ha and d.get("author") not in ha
           and d.get("platform") not in hp]
    # 已经有作品的博主名字：手工建的空壳不再重复出现（否则侧栏会裂成两个同名）
    seen_keys = {d["author_key"] for d in out}
    seen_names = {(d["author"] or "") for d in out}
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
        nm = e["name"] or ""
        if not nm or nm in seen_names or nm in seen_keys:
            continue
        pf = e["platform"] or "manual"
        out.append({"author": nm, "platform": pf,
                    "platform_cn": platform_cn_for(pf),
                    "author_url": "", "author_id": "", "author_key": nm, "n": 0,
                    "pinned": nm in pin_set})
        seen_names.add(nm)
        seen_keys.add(nm)
    # 手工建的空壳博主同样遵守隐藏名单
    out = [d for d in out
           if d.get("author_key") not in ha and d.get("author") not in ha
           and d.get("platform") not in hp]
    # 按置顶顺序排：pin_list 索引越小越靠前，未置顶的按作品数降序
    pin_idx_map = {k: i for i, k in enumerate(pin_list)}
    def _pin_sort_key(x):
        ak = x.get("author_key") or ""
        an = x.get("author") or ""
        pi = pin_idx_map.get(ak, pin_idx_map.get(an, 999999))
        return (0 if pi < 999999 else 1, pi, -x["n"])
    out.sort(key=_pin_sort_key)
    return out


def q_platforms():
    con = db()
    rows = con.execute("SELECT platform_cn, platform, COUNT(*) n FROM items GROUP BY platform_cn, platform ORDER BY n DESC").fetchall()
    con.close()
    pins = set(load_pins().get("platforms") or [])
    _hi, _ha, hp, _hc = _hidden_sets()
    out = []
    for r in rows:
        d = dict(r)
        if d["platform"] in hp:
            continue
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
    hw, hargs = _hidden_where("stats")
    sql = "SELECT COUNT(*) items, SUM(file_count) files FROM items"
    if hw:
        sql += " WHERE " + " AND ".join(hw)
    row = con.execute(sql, hargs).fetchone()
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
    """清掉抖音下载器的 Chrome（只匹配下载器自己的 profile）

    注意：这是一个「会影响所有在跑的抖音任务」的重操作 —— 常驻 Chrome 是所有
    抖音任务共用的，杀掉它等于把每个任务页面内 API 的 CDP 连接一起掐断。
    只在确认 CDP 已经不可用时才调用（见 ensure_douyin_chrome）。
    """
    try:
        subprocess.run(["pkill", "-f", "douyin-ai-video-downloader/.chrome-profile"],
                       capture_output=True, timeout=15)
    except Exception as e:
        print("[warn] 清理残留 Chrome 失败：%s" % e, file=sys.stderr)


def douyin_cdp_up(timeout=2.0):
    """CDP 端口上是不是真有一个能应答的 Chrome"""
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:9222/json/version",
                                    timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _chrome_version():
    """读取本机 Chrome 实际版本号，避免 UA 和真实版本不一致被反爬检测"""
    try:
        out = subprocess.check_output(
            ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "--version"],
            timeout=5, stderr=subprocess.DEVNULL).decode().strip()
        # 输出格式: "Google Chrome 157.0.8103.78" 或 "Chromium 120.0.0.0"
        m = re.search(r"(\d+\.\d+\.\d+\.\d+)", out)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "157.0.0.0"  # fallback


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
    ver = _chrome_version()
    ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/%s Safari/537.36" % ver)
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


def ensure_douyin_chrome():
    """保证 CDP Chrome 可用，但不去动一个健康的实例。

    只有「进程还在、CDP 却不应答」的僵尸状态才需要 pkill 重来：健康的 Chrome 是
    所有抖音任务共用的，无脑杀掉会让正在解析的任务全部失败（这正是抖音爬取
    失败率居高不下的原因）。
    """
    with DOUYIN_LOCK:
        if douyin_cdp_up():
            return True
        cleanup_stale_downloader()
        time.sleep(0.5)
        return start_douyin_chrome()


def _active_douyin_tasks(exclude=None):
    """正在跑的抖音任务（exclude 用于把自己排除掉）"""
    with TASKS_LOCK:
        return [t for t in TASKS.values()
                if t.get("kind") == "archive" and t.get("active")
                and t.get("id") != exclude
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
                _login_cache_set(platform, batch.login_state(platform) or True)
            else:
                _login_set(state="error", message="登录窗口已关闭，但未检测到登录态")
                _login_cache_set(platform, False)
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


# ---------------------------------------------------------------- Telegram 一次性配置
# 归档 t.me 链接走 MTProto，需要 api_id/api_hash（~/.media-archive/tg.json）+ 登录会话
# （~/.media-archive/tg.session）。以前只能跑 tg_setup.py，这里把整套流程搬到 App 设置里。
TG_DIR = HOME / ".media-archive"
TG_CFG = TG_DIR / "tg.json"
TG_SESSION = TG_DIR / "tg"
TG_SETUP = BASE / "tg_setup.py"
# 登录状态机：idle → connecting → sending_code → waiting_code → (waiting_password) → done / error
TG_LOGIN = {"state": "idle", "message": "", "phone": "", "needs_password": False,
            "user": "", "error": "", "started": 0, "cancel": False}
TG_LOGIN_LOCK = threading.Lock()
TG_INPUT = queue.Queue()          # App 提交的验证码 / 两步密码 / 取消
TG_LIVE = {"checking": False, "checked_at": 0, "authorized": None, "user": "", "error": ""}
TG_LIVE_LOCK = threading.Lock()
_TELETHON_VER = None


def _tg_set(**kw):
    with TG_LOGIN_LOCK:
        TG_LOGIN.update(kw)


def _tg_get(key, default=None):
    with TG_LOGIN_LOCK:
        return TG_LOGIN.get(key, default)


def _tg_live_set(**kw):
    with TG_LIVE_LOCK:
        TG_LIVE.update(kw)


def telethon_version():
    """探测 telethon 是否装了（结果缓存，避免每次状态查询都导入一遍）"""
    global _TELETHON_VER
    if _TELETHON_VER is None:
        try:
            import telethon
            _TELETHON_VER = getattr(telethon, "__version__", "installed") or "installed"
        except ImportError:
            _TELETHON_VER = ""
    return _TELETHON_VER


def _tg_own_loop():
    """telethon 的 sync 客户端用的是「当前线程的 event loop」，工作线程里默认没有，
    必须自己建一个（tg_setup.py 跑在主线程，所以命令行一直没暴露这个问题）"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop


def _tg_close_loop(loop):
    try:
        asyncio.set_event_loop(None)
    except Exception:
        pass
    try:
        if loop is not None and not loop.is_closed():
            loop.close()
    except Exception:
        pass


def tg_proxy():
    """MTProto 直连在国内基本连不通，复用 engine 的本地代理探测结果（7897/7890/…）"""
    try:
        import engine
        return engine.PROXY or None
    except Exception:
        return None


def tg_proxy_arg():
    """telethon 只认 dict/tuple 代理，用 engine 里那个转换（URL 字符串会 TypeError）"""
    try:
        import engine
        return engine.tg_proxy_arg()
    except Exception:
        return None


def tg_client():
    """按 tg.json 建一个 telethon 客户端（带代理，直连 MTProto 在国内基本连不通）"""
    from telethon.sync import TelegramClient
    cfg = tg_cfg_load()
    aid = int(cfg.get("api_id") or _TG_DEFAULT_API_ID)
    ahash = str(cfg.get("api_hash") or _TG_DEFAULT_API_HASH)
    return TelegramClient(str(TG_SESSION), aid, ahash,
                          proxy=tg_proxy_arg(), timeout=12,
                          connection_retries=2, request_retries=2)


def tg_cfg_load():
    try:
        d = json.loads(TG_CFG.read_text("utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


# 内置默认 api_id/api_hash（Telegram 官方允许第三方客户端使用）
# 用户不需要自己去 my.telegram.org 申请，直接输手机号+验证码就行
_TG_DEFAULT_API_ID = 2040
_TG_DEFAULT_API_HASH = "b18441a1ff607e10a989891a5462e627"


def tg_cfg_save(api_id, api_hash):
    """校验并写入 tg.json；返回错误文案（None 表示成功）"""
    api_id = str(api_id or "").strip()
    api_hash = str(api_hash or "").strip()
    # 允许留空——留空时用内置默认值
    if api_id and not api_id.isdigit():
        return "api_id 要是纯数字（在 https://my.telegram.org → API development tools 拿）"
    if api_hash and len(api_hash) < 20:
        return "api_hash 长度不对（应该是 32 位十六进制字符串）"
    old = tg_cfg_load()
    merged = dict(old)
    if api_id:
        merged["api_id"] = int(api_id)
    if api_hash:
        merged["api_hash"] = api_hash
    TG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = TG_CFG.with_suffix(".tmp")
    tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(TG_CFG)
    os.chmod(str(TG_CFG), 0o600)
    return None


def tg_session_files():
    try:
        return sorted(p.name for p in TG_DIR.glob("tg.session*"))
    except Exception:
        return []


def tg_status_public():
    """文件态 + 登录流程态 + 上一次实测结果，一次给全，App 直接渲染"""
    with TG_LOGIN_LOCK:
        login = dict(TG_LOGIN)
    with TG_LIVE_LOCK:
        live = dict(TG_LIVE)
    cfg = tg_cfg_load()
    api_id = str(cfg.get("api_id") or "")
    api_hash = str(cfg.get("api_hash") or "")
    sessions = tg_session_files()
    # 有内置默认值，即使没填也算 configured
    has_creds = bool((api_id and api_hash) or (not api_id and not api_hash))
    return {
        "configured": True,   # 始终 True，因为有内置默认值
        "has_custom_creds": bool(api_id and api_hash),
        "api_id": api_id,
        "api_hash": api_hash,          # 只监听 127.0.0.1，App 打开设置时要回填
        "api_hash_masked": (api_hash[:4] + "…" + api_hash[-4:]) if len(api_hash) > 8 else api_hash,
        "session_exists": bool(sessions),
        "session_files": sessions,
        "telethon": telethon_version(),
        "proxy": tg_proxy() or "",
        "cfg_path": str(TG_CFG),
        "session_path": str(TG_SESSION) + ".session",
        "setup_script": str(TG_SETUP) if TG_SETUP.exists() else "",
        "login": login,
        "live": live,
    }


def tg_verify_worker():
    """拿现有 session 真连一次，确认登录态还有效（App 打开设置时后台跑）"""
    client = None
    loop = None
    try:
        cfg = tg_cfg_load()
        # 有内置默认值，不再要求用户必须填 api_id/api_hash
        if not telethon_version():
            _tg_live_set(authorized=False, user="", checked_at=time.time(),
                         error="缺少 telethon")
            return
        loop = _tg_own_loop()
        client = tg_client()
        client.connect()
        if client.is_user_authorized():
            me = client.get_me()
            name = " ".join(x for x in [getattr(me, "first_name", "") or "",
                                        getattr(me, "last_name", "") or ""] if x).strip()
            uname = getattr(me, "username", "") or ""
            _tg_live_set(authorized=True, checked_at=time.time(), error="",
                         user=("%s（@%s）" % (name, uname)) if uname else (name or "已登录"))
        else:
            _tg_live_set(authorized=False, user="", checked_at=time.time(),
                         error="配置在，但会话没登录（或已失效）")
    except Exception as e:
        _tg_live_set(authorized=False, user="", checked_at=time.time(), error=str(e)[:200])
    finally:
        _tg_live_set(checking=False)
        try:
            if client:
                client.disconnect()
        except Exception:
            pass
        _tg_close_loop(loop)


def tg_login_worker(phone):
    """整个登录流程都待在这一个线程里：telethon 的 sync 客户端绑定创建它的线程，
    而且 send_code_request 拿到的 phone_code_hash 只存在内存里，换进程/换线程就接不上。"""
    client = None
    loop = None
    try:
        from telethon import errors as tg_errors
        cfg = tg_cfg_load()
        # api_id/api_hash 留空时用内置默认值，不再报错
        px = tg_proxy()
        _tg_set(state="connecting",
                message=("连接 Telegram…（代理 %s）" % px) if px else "连接 Telegram…（直连）",
                phone=phone, needs_password=False, user="", error="", started=time.time())
        loop = _tg_own_loop()
        client = tg_client()
        try:
            client.connect()
        except Exception as ce:
            raise RuntimeError("连不上 Telegram：%s%s" % (
                str(ce)[:120],
                "" if px else "（没探到本地代理，MTProto 直连大概率不通：先开代理再试）"))
        if client.is_user_authorized():
            me = client.get_me()
            _tg_set(state="done", message="这个会话本来就是登录状态",
                    user=getattr(me, "username", "") or str(getattr(me, "id", "")))
            _tg_live_set(authorized=True, user=_tg_get("user") or "已登录",
                         checked_at=time.time(), error="")
            return

        _tg_set(state="sending_code", message="正在发送验证码到 %s…" % phone)
        sent = client.send_code_request(phone)
        via = ""
        try:
            via = type(sent.type).__name__.replace("SentCodeType", "").lower()
        except Exception:
            pass
        hint = {"sms": "短信", "call": "电话", "flash_call": "来电尾号",
                "fragment_sms": "Fragment"}.get(via, "Telegram 站内")
        _tg_set(state="waiting_code",
                message="验证码已通过%s发出，去 Telegram 里看（10 分钟内有效）" % hint)

        deadline = time.time() + 600
        while True:
            if _tg_get("cancel"):
                raise RuntimeError("已取消登录")
            timeout = max(1.0, deadline - time.time())
            try:
                item = TG_INPUT.get(timeout=min(timeout, 1.0))
            except queue.Empty:
                if time.time() > deadline:
                    raise RuntimeError("10 分钟没输验证码，登录超时")
                continue
            kind = item.get("kind")
            if kind == "cancel":
                raise RuntimeError("已取消登录")
            if kind == "resend":
                _tg_set(state="sending_code", message="重新发送验证码…")
                client.send_code_request(phone)
                deadline = time.time() + 600
                _tg_set(state="waiting_code", message="验证码已重新发送")
                continue
            code = str(item.get("code") or "").strip()
            pwd = str(item.get("password") or "").strip()
            if not code and not pwd:
                _tg_set(message="验证码不能为空")
                continue
            try:
                if code:
                    client.sign_in(phone=phone, code=code)
                else:
                    client.sign_in(password=pwd)
                break
            except tg_errors.SessionPasswordNeededError:
                if not pwd:
                    _tg_set(state="waiting_password", needs_password=True,
                            message="这个号开了两步验证，再输入一次密码")
                    continue
                try:
                    client.sign_in(password=pwd)
                    break
                except tg_errors.PasswordHashInvalidError:
                    _tg_set(state="waiting_password", needs_password=True,
                            message="两步验证密码不对，重新输入")
                    continue
            except tg_errors.PhoneCodeInvalidError:
                _tg_set(state="waiting_code", message="验证码不对，重新输入")
                continue
            except tg_errors.PhoneCodeExpiredError:
                raise RuntimeError("验证码过期了，点「重新发送」再来一次")
            except tg_errors.FloodWaitError as e:
                raise RuntimeError("触发 Telegram 限流，%d 秒后再试" % getattr(e, "seconds", 60))

        me = client.get_me()
        name = " ".join(x for x in [getattr(me, "first_name", "") or "",
                                    getattr(me, "last_name", "") or ""] if x).strip()
        uname = getattr(me, "username", "") or ""
        shown = ("%s（@%s）" % (name, uname)) if uname else (name or str(getattr(me, "id", "")))
        _tg_set(state="done", message="登录成功，会话已保存到 %s.session" % TG_SESSION,
                user=shown, needs_password=False)
        _tg_live_set(authorized=True, user=shown, checked_at=time.time(), error="")
    except Exception as e:
        cancelled = bool(_tg_get("cancel")) or "已取消" in str(e)
        _tg_set(state=("cancelled" if cancelled else "error"),
                message=("已取消登录" if cancelled else str(e)[:200]),
                error=("" if cancelled else str(e)[:200]), needs_password=False)
    finally:
        try:
            if client:
                client.disconnect()
        except Exception:
            pass
        _tg_close_loop(loop)


def tg_login_busy():
    return _tg_get("state") in ("connecting", "sending_code", "waiting_code", "waiting_password")


def tg_logout():
    """删掉本地会话文件；账号侧的会话要自己去 Telegram → 设置 → 设备里终止"""
    removed = []
    for p in TG_DIR.glob("tg.session*"):
        try:
            p.unlink()
            removed.append(p.name)
        except Exception:
            pass
    _tg_set(state="idle", message="", phone="", needs_password=False, user="", error="")
    _tg_live_set(authorized=False, user="", checked_at=time.time(), error="")
    return removed


LOGIN_CACHE_FILE = HOME / ".media-archive" / "login_cache.json"
_LOGIN_CACHE_LOCK = threading.Lock()


def _login_cache_load():
    try:
        d = json.loads(LOGIN_CACHE_FILE.read_text("utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _login_cache_save(c):
    LOGIN_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _LOGIN_CACHE_LOCK:
        LOGIN_CACHE_FILE.write_text(json.dumps(c, ensure_ascii=False), "utf-8")


def _login_cache_set(platform, st):
    c = _login_cache_load()
    c[platform] = {"ts": time.time(),
                   "nick": st if isinstance(st, str) else (True if st else False)}
    _login_cache_save(c)


def login_state_fast(platform, max_age=90):
    """登录态快速版：磁盘缓存优先，避免每次开批量归档都等 node 去连 CDP。

    - 缓存新鲜（< max_age 秒）：直接返回；
    - 缓存过期：先把旧值返回（界面立刻显示「已登录·昵称」），后台静默刷新；
    - 没有缓存：同步查一次并落盘（只有第一次会慢）。
    """
    c = _login_cache_load().get(platform)
    now = time.time()
    if c and now - c.get("ts", 0) <= max_age:
        return c.get("nick") or True

    def _bg():
        try:
            _login_cache_set(platform, batch.login_state(platform))
        except Exception:
            pass

    if c:
        threading.Thread(target=_bg, daemon=True).start()
        return c.get("nick") or True
    st = batch.login_state(platform)
    _login_cache_set(platform, st)
    return st


def scope_from_qs(qs):
    if (qs.get("collection") or [""])[0]:
        return {"type": "collection", "value": (qs.get("collection") or [""])[0]}
    if (qs.get("author") or [""])[0]:
        return {"type": "author", "value": (qs.get("author") or [""])[0]}
    if (qs.get("platform") or [""])[0]:
        return {"type": "platform", "value": (qs.get("platform") or [""])[0]}
    return {"type": "all"}


def apply_archive_targets(item_id, targets):
    """批量归档时指定的归属：平台 / 博主 / 多个分类，归档完成后一次性套用。"""
    if not targets or not item_id:
        return
    try:
        con = db()
        fields = {}
        ak = str(targets.get("author_key") or "").strip()
        if ak:
            row = con.execute(
                "SELECT author, author_id, platform, platform_cn FROM items "
                "WHERE (CASE WHEN author_id IS NULL OR author_id='' THEN author ELSE author_id END)=? "
                "LIMIT 1", (ak,)).fetchone()
            if row:
                fields.update({"author": row["author"],
                               "author_id": (row["author_id"] or ak),
                               "platform": row["platform"],
                               "platform_cn": row["platform_cn"]})
            else:
                crow = con.execute(
                    "SELECT name, platform FROM custom_authors WHERE name=? LIMIT 1",
                    (ak,)).fetchone()
                pf = (crow["platform"] if crow else "") or str(targets.get("platform") or "") or "manual"
                fields.update({"author": ak, "author_id": ak,
                               "platform": pf, "platform_cn": platform_cn_for(pf)})
        pk = str(targets.get("platform") or "").strip()
        if pk and not ak:
            prow = con.execute(
                "SELECT platform, platform_cn FROM items WHERE platform=? LIMIT 1",
                (pk,)).fetchone()
            cn = prow["platform_cn"] if prow else platform_cn_for(pk)
            fields.update({"platform": pk, "platform_cn": cn})
        if fields:
            sets = ",".join(k + "=?" for k in fields)
            con.execute("UPDATE items SET " + sets + " WHERE id=?",
                        list(fields.values()) + [int(item_id)])
        ensure_collections()
        for c in (targets.get("collections") or []):
            cid, name = None, None
            if isinstance(c, dict):
                cid, name = c.get("id"), c.get("name")
            else:
                cid = c
            if not cid and name:
                con.execute("INSERT OR IGNORE INTO collections(name, created_ts) VALUES(?, ?)",
                            (name, int(time.time())))
                r = con.execute("SELECT id FROM collections WHERE name=?", (name,)).fetchone()
                cid = r["id"] if r else None
            if cid:
                con.execute(
                    "INSERT OR IGNORE INTO collection_items(collection_id, item_id, added_ts) "
                    "VALUES(?,?,?)", (int(cid), int(item_id), int(time.time())))
        con.commit()
        con.close()
    except Exception as e:
        print("[warn] 套用归档归属失败：%s" % e, file=sys.stderr)


def run_archive(url, targets=None):
    tid = next_task_id()
    is_douyin = ("douyin" in url) or ("iesdouyin" in url)
    with TASKS_LOCK:
        TASKS[tid] = {"id": tid, "url": url, "status": "running", "log": "",
                      "folder": "", "started": time.time(), "kind": "archive",
                      "queued": True, "active": False, "targets": targets or None}
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
                # 兜底：只有在 CDP 确实挂了、并且没有别的抖音任务在跑时才重启 Chrome。
                # 常驻 Chrome 是所有抖音任务共用的，一个任务超时就把整个浏览器杀掉，
                # 会连带把另外几个正在解析的任务全部弄失败。
                if is_douyin:
                    try:
                        if not douyin_cdp_up():
                            if not _active_douyin_tasks(exclude=tid):
                                ensure_douyin_chrome()
                            else:
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
            try:
                ensure_douyin_chrome()
            except Exception:
                pass
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
            # 套用「归档到指定分类」里选的平台 / 博主 / 收藏夹
            if item_info and targets:
                apply_archive_targets(item_info["id"], targets)
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
        # 留痕：成功/失败都进历史，供信号灯窗口查看
        _cur = cur or {}
        _it = item_info if isinstance(item_info, dict) else {}
        history_append({
            "ts": time.time(), "url": url, "status": _cur.get("status") or "error",
            "title": _it.get("title", "") or "", "platform": _it.get("platform", "") or "",
            "error": _cur.get("error_reason", "") or "",
            "folder": folder or "",
            "duplicate": bool(_cur.get("duplicate")),
            "skipped": bool(_cur.get("skipped")),
        })
    threading.Thread(target=worker, daemon=True).start()
    return tid


class Handler(BaseHTTPRequestHandler):
    server_version = "MediaArchive/1.0"
    MAX_UPLOAD = 8 * 1024 ** 3          # 单个文件上限 8GB

    def log_message(self, fmt, *args):
        pass

    def _upload_files(self, u):
        """把用户选的照片/视频写进作品目录。

        请求体就是文件原始字节（不做 multipart 解析），一个请求一个文件，
        边收边写盘，几个 G 的视频也不会把内存吃满。
            POST /api/item_files?id=<作品id>&name=<文件名>
        """
        qs = urllib.parse.parse_qs(u.query)
        iid = (qs.get("id") or [""])[0]
        raw_name = (qs.get("name") or [""])[0]
        if not iid:
            return self._json({"ok": False, "error": "缺少作品 id"}, 400)
        total = int(self.headers.get("Content-Length") or 0)
        if total <= 0:
            return self._json({"ok": False, "error": "空文件"}, 400)
        if total > self.MAX_UPLOAD:
            return self._json({"ok": False, "error": "文件太大（上限 8GB）"}, 413)
        con = db()
        row = con.execute("SELECT * FROM items WHERE id=?", (int(iid),)).fetchone()
        if not row:
            con.close()
            return self._json({"ok": False, "error": "作品不存在"}, 404)
        folder = Path(row["folder"] or "")
        # 目录被挪走/删掉了：先按名字找回来，找不回来就在归属目录下重建
        if not folder.exists():
            found = find_folder_by_name(folder.name,
                                        exclude=claimed_folders(con, row["id"]))
            if found:
                folder = found
                _set_fields(con, row["id"], {"folder": str(folder)})
            else:
                try:
                    folder.mkdir(parents=True, exist_ok=True)
                except Exception as e:
                    con.close()
                    return self._json({"ok": False, "error": "建目录失败：%s" % e}, 500)
        try:
            folder.resolve().relative_to(ARCHIVE.resolve())
        except Exception:
            con.close()
            return self._json({"ok": False, "error": "只能写进归档目录里面"}, 403)
        name = safe_file_name(raw_name)
        ext = os.path.splitext(name)[1].lower()
        if ext and ext not in MEDIA_EXT and ext not in {".txt", ".json", ".srt", ".vtt"}:
            # 不是媒体文件也收（字幕/说明），但明确告诉前端类型没变
            pass
        target = unique_dest(folder / name)
        tmp = target.with_name(target.name + ".part")
        got = 0
        try:
            with open(str(tmp), "wb") as f:
                left = total
                while left > 0:
                    chunk = self.rfile.read(min(1024 * 1024, left))
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    left -= len(chunk)
            if got != total:
                tmp.unlink(missing_ok=True)
                con.close()
                return self._json({"ok": False,
                                   "error": "上传中断（只收到 %d/%d 字节）" % (got, total)}, 400)
            tmp.replace(target)
        except Exception as e:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            con.close()
            return self._json({"ok": False, "error": "写入失败：%s" % e}, 500)
        info = refresh_item_media(con, row["id"], folder)
        con.commit()
        con.close()
        return self._json({"ok": True, "name": target.name, "path": str(target),
                           "size": got, "file_count": info["file_count"],
                           "type": info["type"],
                           "kind": ("video" if ext in VIDEO_EXT
                                    else "audio" if ext in AUDIO_EXT
                                    else "image" if ext in IMAGE_EXT else "other"),
                           "files": info["files"]})

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
            if u.path == "/api/ping":
                # 轻量存活探针：新实例用它判断「端口上是不是自己人」，
                # 前端/桌面 App 也可以用它做健康检查（比 /api/stats 便宜得多）
                return self._json({"app": "media-archive", "ok": True,
                                   "pid": os.getpid(),
                                   "started": SERVER_STARTED,
                                   "version": 2})
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
                    "arrow_switch_works": bool(c.get("arrow_switch_works", True)),
                })

            if u.path == "/api/tg/status":
                # ?live=1 时后台真连一次 Telegram 验证会话（不阻塞这次响应）
                want_live = (urllib.parse.parse_qs(u.query).get("live", ["0"])[0] == "1")
                if want_live:
                    with TG_LIVE_LOCK:
                        checking = TG_LIVE.get("checking")
                        stale = (time.time() - (TG_LIVE.get("checked_at") or 0)) > 30
                    if not checking and stale and not tg_login_busy():
                        _tg_live_set(checking=True)
                        threading.Thread(target=tg_verify_worker, daemon=True).start()
                return self._json({"ok": True, "tg": tg_status_public()})

            if u.path == "/api/suggest":
                ql = (urllib.parse.parse_qs(u.query).get("q", [""])[0] or "").strip().lower()
                if not ql:
                    return self._json({"ok": True, "authors": [], "platforms": []})
                con = db()
                arows = con.execute("SELECT author, platform_cn, COUNT(*) n "
                                    "FROM items GROUP BY author, platform_cn").fetchall()
                prows = con.execute("SELECT platform_cn, COUNT(*) n "
                                    "FROM items GROUP BY platform_cn").fetchall()
                con.close()

                def _score(name):
                    if not name:
                        return None
                    f, i = pinyin_of(name)
                    nl = name.lower()
                    if nl.startswith(ql) or f.startswith(ql) or i.startswith(ql):
                        return 0
                    if ql in nl or ql in f or ql in i:
                        return 1
                    return None

                auth, plat = [], []
                for r in arows:
                    s = _score(r["author"])
                    if s is not None:
                        auth.append((s, -r["n"], {"name": r["author"],
                                                  "platform_cn": r["platform_cn"] or "",
                                                  "n": r["n"]}))
                for r in prows:
                    s = _score(r["platform_cn"])
                    if s is not None:
                        plat.append((s, -r["n"], {"name": r["platform_cn"], "n": r["n"]}))
                auth.sort(key=lambda x: (x[0], x[1]))
                plat.sort(key=lambda x: (x[0], x[1]))
                return self._json({"ok": True,
                                   "authors": [a[2] for a in auth[:8]],
                                   "platforms": [p[2] for p in plat[:4]]})

            if u.path == "/api/history":
                with HISTORY_LOCK:
                    items = list(reversed(HISTORY))   # 新的在前
                return self._json({"ok": True, "items": items})

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
                return self._json(items)
            if u.path == "/api/stats":
                return self._json(q_stats())
            if u.path == "/api/collections":
                return self._json(q_collections())
            if u.path == "/api/hidden":
                return self._json({"ok": True, **load_hidden()})
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
                    st = login_state_fast(p)
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
                if not login_state_fast(platform):
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
                folder_exists = bool(d.get("folder")) and Path(d["folder"]).exists()
                d["folder_exists"] = folder_exists
                d["empty"] = folder_exists and not d["files"]
                d["missing"] = (not folder_exists) or (len(d["files"]) == 0)
                d["author_key"] = (d.get("author_id") or "").strip() or (d.get("author") or "")
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
        # 文件上传：请求体是原始字节，不能走下面的 JSON 解析
        if u.path in ("/api/item_files", "/api/upload"):
            return self._upload_files(u)
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
                # 按「这一组」删：同名不同平台的另一个博主不能被误删
                akey = (body.get("author_key") or "").strip() or name
                aplat = (body.get("platform") or "").strip()
                con = db()
                rows = author_group_rows(con, akey, name, aplat)
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
                ids = [r["id"] for r in rows]
                n = 0
                if ids:
                    qs = ",".join("?" * len(ids))
                    con.execute("DELETE FROM collection_items WHERE item_id IN (%s)" % qs, ids)
                    try:
                        con.execute("DELETE FROM analysis WHERE item_id IN (%s)" % qs, ids)
                    except Exception:
                        pass
                    n = con.execute("DELETE FROM items WHERE id IN (%s)" % qs, ids).rowcount
                try:
                    if aplat:
                        con.execute("DELETE FROM custom_authors WHERE name=? AND platform=?",
                                    (name, aplat))
                    else:
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
                removing = (u.path == "/api/collection/remove")
                # 目标分类：可以一次给多个名字（names），也兼容老的单个 name / id
                names_in = body.get("names")
                if not isinstance(names_in, list):
                    names_in = []
                names_in = [str(x).strip()[:40] for x in names_in if str(x).strip()]
                one = (body.get("name") or "").strip()[:40]
                if one and one not in names_in:
                    names_in.append(one)
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
                cids = []
                if u.path == "/api/collection/add_by_name" or names_in:
                    if not names_in:
                        con.close()
                        return self._json({"ok": False, "error": "缺少分类名"})
                    for nm in names_in:
                        if not removing:
                            con.execute("INSERT OR IGNORE INTO collections(name, created_ts)"
                                        " VALUES(?,?)", (nm, int(time.time())))
                        row = con.execute("SELECT id FROM collections WHERE name=?",
                                          (nm,)).fetchone()
                        if row and row["id"] not in cids:
                            cids.append(row["id"])
                else:
                    try:
                        cids.append(int(cid))
                    except Exception:
                        con.close()
                        return self._json({"ok": False, "error": "缺少分类"})
                if not cids:
                    con.close()
                    return self._json({"ok": False, "error": "分类不存在"})
                valid = []
                if ids:
                    qs = ",".join("?" * len(ids))
                    valid = [r["id"] for r in con.execute(
                        "SELECT id FROM items WHERE id IN (%s)" % qs, ids).fetchall()]
                results, total_changed = [], 0
                for c in cids:
                    row = con.execute("SELECT name FROM collections WHERE id=?",
                                      (c,)).fetchone()
                    if not row:
                        continue
                    changed = 0
                    for iid in valid:
                        if removing:
                            cur = con.execute(
                                "DELETE FROM collection_items WHERE collection_id=? AND item_id=?",
                                (c, iid))
                        else:
                            cur = con.execute(
                                "INSERT OR IGNORE INTO collection_items"
                                "(collection_id,item_id,added_ts) VALUES(?,?,?)",
                                (c, iid, int(time.time())))
                        changed += max(0, cur.rowcount)
                    n = con.execute("SELECT COUNT(*) AS n FROM collection_items"
                                    " WHERE collection_id=?", (c,)).fetchone()["n"]
                    total_changed += changed
                    results.append({"id": c, "name": row["name"],
                                    "changed": changed, "count": n})
                con.commit()
                con.close()
                first = results[0] if results else {"id": None, "name": "", "count": 0}
                return self._json({"ok": bool(results), "id": first["id"],
                                   "name": first["name"], "count": first["count"],
                                   "changed": total_changed,
                                   "names": [r["name"] for r in results],
                                   "results": results})

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
                on = body.get("pinned")   # True/False 或 "top"
                action = (body.get("action") or "").strip()  # "top" = 移到最前
                if kind not in ("authors", "platforms") or not key:
                    return self._json({"ok": False, "error": "参数不完整"})
                pins = load_pins()
                arr = list(pins.get(kind) or [])
                if action == "top" or on == "top":
                    # 移到最前：先移除再插到头部
                    arr = [x for x in arr if x != key]
                    arr.insert(0, key)
                elif on and on is not False:
                    # 置顶（如果不在列表里就加到头部）
                    if key not in arr:
                        arr.insert(0, key)
                else:
                    # 取消置顶
                    arr = [x for x in arr if x != key]
                pins[kind] = arr
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
                typ = (body.get("type") or "").strip()
                if typ not in ("图文", "视频", "音频"):
                    typ = ""            # 空着：等文件传上来后按实际内容定
                req_author = (body.get("author") or "").strip()
                con = db()
                if req_author and req_author != "我的作品":
                    # 在某个博主标签下新建 → 归这个博主，并沿用它的平台和稳定作者 ID，
                    # 否则侧栏会裂成两个同名博主，按 author_key 过滤也匹配不上
                    ident = resolve_author_identity(
                        con, req_author,
                        (body.get("platform") or "").strip())
                    # 前端明确带过来的身份优先（刚建的空博主 DB 里还查不到）
                    if (body.get("author_id") or "").strip():
                        ident["author_id"] = body["author_id"].strip()
                    if (body.get("author_url") or "").strip():
                        ident["author_url"] = body["author_url"].strip()
                    if (body.get("platform") or "").strip():
                        ident["platform"] = body["platform"].strip()
                        ident["platform_cn"] = (body.get("platform_cn") or "").strip() \
                            or platform_cn_for(ident["platform"])
                else:
                    # 只有「全部」下新建才归「我的作品」
                    ident = {"author": "我的作品", "author_id": "", "author_url": "",
                             "platform": "manual", "platform_cn": "手动"}
                pf = ident["platform"] or "manual"
                pcn = ident["platform_cn"] or platform_cn_for(pf)
                safe = re.sub(r'[\\/:*?"<>|]', "_", title)[:50]
                safe = safe + "_" + str(int(time.time() * 1000))[-6:]
                stamp = time.strftime("%Y%m%d")
                folder = unique_dest(author_dir_for(pcn, ident["author"])
                                     / ("%s_%s" % (stamp, safe)))
                folder.mkdir(parents=True, exist_ok=True)
                cur = con.execute("""INSERT INTO items
                    (platform, platform_cn, author, author_id, author_url, title, item_id, type,
                     created_ts, saved_ts, file_count, folder, source_url)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (pf, pcn, ident["author"], ident["author_id"], ident["author_url"],
                     title, "manual_%d" % int(time.time()*1000), typ or "图文",
                     int(time.time()), int(time.time()), 0, str(folder), ""))
                con.commit()
                new_id = cur.lastrowid
                # 记到 custom_authors，这样作品为 0 时博主也留在侧栏
                try:
                    con.execute("""CREATE TABLE IF NOT EXISTS custom_authors(
                        name TEXT, platform TEXT, created_ts INTEGER,
                        PRIMARY KEY(name, platform))""")
                    con.execute("INSERT OR IGNORE INTO custom_authors(name, platform, created_ts)"
                                " VALUES (?,?,?)",
                                (ident["author"], pf, int(time.time())))
                    con.commit()
                except Exception:
                    pass
                con.close()
                _PLAT_TABLES["ts"] = 0.0
                try:
                    export_csv()
                except Exception:
                    pass
                return self._json({"ok": True, "id": new_id, "folder": str(folder),
                                   "author": ident["author"], "platform": pf,
                                   "platform_cn": pcn, "type": typ or "图文",
                                   "author_id": ident["author_id"],
                                   "author_url": ident["author_url"],
                                   "author_key": ident["author_id"] or ident["author"]})

            if u.path == "/api/rename_author":
                old = (body.get("old") or "").strip()
                new = (body.get("new") or "").strip()
                if not old or not new:
                    return self._json({"ok": False, "error": "名字不能为空"})
                # 精确圈定要改的那一组：同名博主（手动建的 + 平台抓到的）不能一起改
                src_key = (body.get("source_author_key") or body.get("author_key")
                           or "").strip() or old
                src_plat = (body.get("source_platform") or body.get("platform") or "").strip()
                on_conflict = (body.get("on_conflict") or "").strip()
                con = db()
                if new != old and on_conflict != "merge":
                    conflict = find_author_conflicts(con, new, src_key, src_plat)
                    if conflict:
                        con.close()
                        # 不默默新建第二个同名博主：把冲突详情交给前端，让用户选合并还是换名
                        return self._json({
                            "ok": False, "error": "already_exists",
                            "message": "已经有博主叫「%s」了" % new,
                            "conflict": conflict})
                try:
                    if on_conflict == "merge" and new != old:
                        tgt_key = (body.get("target_author_key") or "").strip() or new
                        tgt_plat = (body.get("target_platform") or "").strip()
                        res = merge_author_group(con, old, new, src_key, src_plat,
                                                 tgt_key, tgt_plat)
                        merged = True
                    else:
                        res = rename_author_group(con, old, new, src_key, src_plat)
                        merged = False
                    con.commit()
                except Exception as e:
                    con.rollback()
                    con.close()
                    return self._json({"ok": False, "error": str(e)})
                con.close()
                _PLAT_TABLES["ts"] = 0.0
                pins = load_pins()
                alist = pins.get("authors") or []
                if old in alist:
                    if new in alist:
                        pins["authors"] = [x for x in alist if x != old]
                    else:
                        pins["authors"] = [new if x == old else x for x in alist]
                    save_pins(pins)
                try:
                    export_csv()
                except Exception:
                    pass
                ident = {"author": new}
                return self._json({"ok": True, "count": res["count"],
                                   "moved": res["moved"], "merged": merged,
                                   "author": new,
                                   "target_author_id": (body.get("target_author_key")
                                                        or "") if merged else ""})

            # ---------- 把作品挪到 / 复制到另一个博主 ----------
            if u.path == "/api/move_to_author":
                raw_ids = (body.get("item_ids") or body.get("ids")
                           or ([body["id"]] if body.get("id") else []))
                if not isinstance(raw_ids, list):
                    return self._json({"ok": False, "error": "参数不完整"})
                ids = []
                for x in raw_ids:
                    try:
                        ids.append(int(x))
                    except Exception:
                        pass
                target = (body.get("author") or "").strip()
                if not ids or not target:
                    return self._json({"ok": False, "error": "缺少作品或博主"})
                mode = "copy" if (body.get("mode") or "").strip() == "copy" else "move"
                con = db()
                ident = resolve_author_identity(con, target,
                                                (body.get("platform") or "").strip())
                if (body.get("author_id") or "").strip():
                    ident["author_id"] = body["author_id"].strip()
                if (body.get("author_url") or "").strip():
                    ident["author_url"] = body["author_url"].strip()
                if (body.get("platform") or "").strip():
                    ident["platform"] = body["platform"].strip()
                if (body.get("platform_cn") or "").strip():
                    ident["platform_cn"] = body["platform_cn"].strip()
                ident["platform"] = ident["platform"] or "manual"
                ident["platform_cn"] = ident["platform_cn"] or platform_cn_for(ident["platform"])
                done, failed, new_ids, notes = 0, [], [], []
                qs = ",".join("?" * len(ids))
                rows = con.execute("SELECT * FROM items WHERE id IN (%s)" % qs, ids).fetchall()
                for r in rows:
                    try:
                        new_folder = move_item_folder(r, ident, mode)
                        if mode == "copy":
                            # 复制：新插一条记录，原记录原封不动
                            info = refresh_item_media(con, r["id"], r["folder"])
                            cur = con.execute("""INSERT INTO items
                                (platform, platform_cn, author, author_id, author_url,
                                 title, item_id, type, created_ts, saved_ts,
                                 file_count, folder, source_url,
                                 digg, comment, collect, share)
                                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (ident["platform"], ident["platform_cn"], ident["author"],
                                 ident["author_id"], ident["author_url"], r["title"],
                                 "%s_c%d" % (r["item_id"], int(time.time() * 1000)),
                                 r["type"] or info.get("type") or "图文",
                                 r["created_ts"] or int(time.time()), int(time.time()),
                                 info["file_count"], str(new_folder), r["source_url"] or "",
                                 r["digg"] or 0, r["comment"] or 0,
                                 r["collect"] or 0, r["share"] or 0))
                            nid = cur.lastrowid
                            new_ids.append(nid)
                            # 分类关系也带过去
                            try:
                                for c in con.execute(
                                        "SELECT collection_id FROM collection_items WHERE item_id=?",
                                        (r["id"],)).fetchall():
                                    con.execute("INSERT OR IGNORE INTO collection_items"
                                                "(collection_id,item_id,added_ts) VALUES(?,?,?)",
                                                (c["collection_id"], nid, int(time.time())))
                            except Exception:
                                pass
                        else:
                            set_item_identity(con, r["id"], ident, new_folder)
                            refresh_item_media(con, r["id"], new_folder)
                            new_ids.append(r["id"])
                        done += 1
                    except Exception as e:
                        failed.append({"id": r["id"], "error": str(e)})
                        notes.append("%s：%s" % (r["title"] or r["id"], e))
                con.commit()
                con.close()
                _PLAT_TABLES["ts"] = 0.0
                try:
                    export_csv()
                except Exception:
                    pass
                return self._json({"ok": not failed, "done": done, "failed": failed,
                                   "ids": new_ids, "mode": mode,
                                   "author": ident["author"],
                                   "platform": ident["platform"],
                                   "platform_cn": ident["platform_cn"],
                                   "folder": str(author_dir_for(ident["platform_cn"],
                                                                ident["author"])),
                                   "notes": notes})

            # ---------- 重新扫描归档目录：找回被挪走的作品 ----------
            if u.path == "/api/rescan":
                only = body.get("id")
                try:
                    stats = repair_index(only_id=int(only) if only else None)
                except Exception as e:
                    return self._json({"ok": False, "error": str(e)})
                stats["ok"] = True
                return self._json(stats)

            # ---------- 手动给一条作品指个新目录 ----------
            if u.path == "/api/item_relocate":
                iid = body.get("id")
                raw = (body.get("path") or "").strip()
                if not iid or not raw:
                    return self._json({"ok": False, "error": "参数不完整"})
                p = Path(raw).expanduser()
                if not p.exists():
                    return self._json({"ok": False, "error": "目录不存在"})
                if not p.is_dir():
                    return self._json({"ok": False, "error": "这不是一个目录"})
                if not resolve_under_archive(p):
                    return self._json({"ok": False, "error": "只能指向归档目录里面"})
                con = db()
                row = con.execute("SELECT * FROM items WHERE id=?", (int(iid),)).fetchone()
                if not row:
                    con.close()
                    return self._json({"ok": False, "error": "作品不存在"})
                ident = path_identity(p)
                info = refresh_item_media(con, row["id"], p)
                fields = {"folder": str(p), "file_count": info["file_count"]}
                if info["type"]:
                    fields["type"] = info["type"]
                if ident:
                    other = resolve_author_identity(con, ident["author"],
                                                    ident["platform"] or "")
                    fields.update({"author": ident["author"],
                                   "platform_cn": ident["platform_cn"],
                                   "author_id": other["author_id"],
                                   "author_url": other["author_url"]})
                    if ident["platform"]:
                        fields["platform"] = ident["platform"]
                _set_fields(con, row["id"], fields)
                con.commit()
                con.close()
                _PLAT_TABLES["ts"] = 0.0
                try:
                    export_csv()
                except Exception:
                    pass
                return self._json({"ok": True, "folder": str(p),
                                   "files": info["file_count"], "type": info["type"],
                                   "author": fields.get("author")})

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
            if u.path == "/api/delete_file":
                # 从某条作品里移除单个文件（移到废纸篓），更新 file_count
                item_id = body.get("item_id")
                rel_path = body.get("path") or ""
                if not item_id or not rel_path:
                    return self._json({"ok": False, "error": "参数不对"}, 400)
                con = db()
                row = con.execute("SELECT folder, file_count FROM items WHERE id=?", (item_id,)).fetchone()
                if not row:
                    con.close()
                    return self._json({"ok": False, "error": "记录不存在"}, 404)
                folder = Path(row["folder"])
                try:
                    folder.resolve().relative_to(ARCHIVE.resolve())
                except ValueError:
                    con.close()
                    return self._json({"ok": False, "error": "路径不在归档目录内"})
                # rel_path 可能是 /api/media?p=... 格式，提取真实路径
                from urllib.parse import urlparse, parse_qs, unquote
                parsed = urlparse(rel_path)
                qs = parse_qs(parsed.query)
                real_rel = (qs.get("p") or [rel_path])[0]
                real_rel = unquote(real_rel)
                target = (folder / real_rel).resolve()
                try:
                    target.relative_to(folder.resolve())
                except ValueError:
                    con.close()
                    return self._json({"ok": False, "error": "路径逃逸"})
                if not target.exists():
                    con.close()
                    return self._json({"ok": False, "error": "文件不存在"})
                if not move_to_trash(target):
                    con.close()
                    return self._json({"ok": False, "error": "移入废纸篓失败"})
                new_count = max(0, (row["file_count"] or 1) - 1)
                con.execute("UPDATE items SET file_count=? WHERE id=?", (new_count, item_id))
                con.commit()
                con.close()
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
                rows = con.execute("SELECT * FROM items").fetchall()
                index = scan_folder_index()
                claimed = claimed_folders(con)
                removed, rescued, skipped = 0, 0, 0
                for row in rows:
                    # 手工建的作品（含刚建还没放文件的空作品）一律不碰
                    iid = str(row["item_id"] or "")
                    if (row["platform"] or "") == "manual" or iid.startswith(
                            ("manual_", "adopted_")):
                        skipped += 1
                        continue
                    if row["saved_ts"] and (time.time() - row["saved_ts"]) < 3600:
                        skipped += 1          # 刚归档的，可能还在写文件
                        continue
                    pth = Path(row["folder"] or "")
                    if pth.exists():
                        # 目录在：只有真的一个文件都没有才算失效
                        try:
                            if any(pth.iterdir()):
                                continue
                        except Exception:
                            continue
                    else:
                        # 目录不见了：先试着按名字找回来，找得回就别删
                        found, _why = relocate_row(con, row, index, claimed)
                        if found:
                            rescued += 1
                            continue
                    con.execute("DELETE FROM collection_items WHERE item_id=?", (row["id"],))
                    try:
                        con.execute("DELETE FROM analysis WHERE item_id=?", (row["id"],))
                    except Exception:
                        pass
                    con.execute("DELETE FROM items WHERE id=?", (row["id"],))
                    removed += 1
                con.commit()
                con.close()
                _PLAT_TABLES["ts"] = 0.0
                try:
                    export_csv()
                except Exception:
                    pass
                return self._json({"ok": True, "removed": removed,
                                   "rescued": rescued, "skipped": skipped})
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
            if u.path == "/api/import_local":
                # 本地目录批量导入：每组 = 一个作品（博主 + 标题 + 文件列表）
                groups = body.get("groups") or []
                move = bool(body.get("move", True))
                if not isinstance(groups, list) or not groups:
                    return self._json({"ok": False, "error": "缺少 groups"}, 400)
                con = db()
                done, failed = 0, []
                stamp = time.strftime("%Y%m%d")
                vid_ext = {".mp4", ".mov", ".m4v", ".mkv", ".avi"}
                for g in groups:
                    author = (g.get("author") or "").strip() or "未知博主"
                    title = (g.get("title") or "").strip() or "未命名"
                    files = [f for f in (g.get("files") or [])
                             if isinstance(f, str) and os.path.isfile(f)]
                    if not files:
                        failed.append({"title": title, "error": "没有有效文件"})
                        continue
                    try:
                        safe = re.sub(r'[\\/:*?"<>|]', "_", title)[:50]
                        safe = safe + "_" + str(int(time.time() * 1000))[-6:]
                        folder = unique_dest(author_dir_for("本地", author)
                                             / ("%s_%s" % (stamp, safe)))
                        folder.mkdir(parents=True, exist_ok=True)
                        cts = 0
                        vids = 0
                        for i, f in enumerate(files):
                            dst = folder / os.path.basename(f)
                            if dst.exists():
                                dst = folder / ("%d_%s" % (i, os.path.basename(f)))
                            if move:
                                shutil.move(f, str(dst))
                            else:
                                shutil.copy2(f, str(dst))
                            if os.path.splitext(f)[1].lower() in vid_ext:
                                vids += 1
                            if not cts:
                                try:
                                    cts = int(os.path.getmtime(f))
                                except Exception:
                                    pass
                        con.execute("""INSERT INTO items
                            (platform, platform_cn, author, author_id, author_url, title,
                             item_id, type, created_ts, saved_ts, file_count, folder, source_url)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            ("local", "本地", author, "", "", title,
                             next_local_item_id(),
                             "视频" if vids else "图文",
                             cts or int(time.time()), int(time.time()),
                             len(files), str(folder), ""))
                        try:
                            con.execute("""CREATE TABLE IF NOT EXISTS custom_authors(
                                name TEXT, platform TEXT, created_ts INTEGER,
                                PRIMARY KEY(name, platform))""")
                            con.execute("INSERT OR IGNORE INTO custom_authors(name, platform, created_ts)"
                                        " VALUES (?,?,?)", (author, "local", int(time.time())))
                        except Exception:
                            pass
                        done += 1
                    except Exception as e:
                        failed.append({"title": title, "error": str(e)[:120]})
                con.commit()
                con.close()
                _PLAT_TABLES["ts"] = 0.0
                return self._json({"ok": True, "done": done, "failed": failed})

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
            # ---------------- Telegram 一次性配置 / 登录 ----------------
            if u.path == "/api/tg/config":
                err = tg_cfg_save(body.get("api_id"), body.get("api_hash"))
                if err:
                    return self._json({"ok": False, "error": err}, 400)
                _tg_live_set(checked_at=0, authorized=None, user="", error="")   # 配置变了，重测
                return self._json({"ok": True, "message": "已写入 %s" % TG_CFG,
                                   "tg": tg_status_public()})

            if u.path == "/api/tg/send_code":
                phone = re.sub(r"[^\d+]", "", str(body.get("phone") or ""))
                if not phone.startswith("+"):
                    phone = "+" + phone.lstrip("+")
                if len(re.sub(r"\D", "", phone)) < 8:
                    return self._json({"ok": False, "error": "手机号要含国家码，例如 +8613800000000"}, 400)
                if body.get("api_id") or body.get("api_hash"):
                    err = tg_cfg_save(body.get("api_id") or tg_cfg_load().get("api_id"),
                                      body.get("api_hash") or tg_cfg_load().get("api_hash"))
                    if err:
                        return self._json({"ok": False, "error": err}, 400)
                cfg = tg_cfg_load()
                # api_id/api_hash 留空用内置默认值，不再拦截
                if not telethon_version():
                    return self._json({"ok": False, "error":
                                       "缺少 telethon：/usr/bin/python3 -m pip install --user telethon"}, 400)
                if tg_login_busy():
                    return self._json({"ok": False, "error": "登录进行中",
                                       "tg": tg_status_public()})
                while not TG_INPUT.empty():          # 清掉上一轮残留的输入
                    try:
                        TG_INPUT.get_nowait()
                    except queue.Empty:
                        break
                _tg_set(state="connecting", message="准备登录…", phone=phone, cancel=False,
                        needs_password=False, user="", error="")
                threading.Thread(target=tg_login_worker, args=(phone,), daemon=True).start()
                return self._json({"ok": True, "tg": tg_status_public()})

            if u.path == "/api/tg/code":
                if not tg_login_busy():
                    return self._json({"ok": False, "error": "当前没有进行中的登录",
                                       "tg": tg_status_public()})
                if body.get("resend"):
                    TG_INPUT.put({"kind": "resend"})
                else:
                    TG_INPUT.put({"kind": "submit",
                                  "code": str(body.get("code") or "").strip(),
                                  "password": str(body.get("password") or "")})
                return self._json({"ok": True, "tg": tg_status_public()})

            if u.path == "/api/tg/cancel":
                _tg_set(cancel=True)
                TG_INPUT.put({"kind": "cancel"})
                return self._json({"ok": True, "tg": tg_status_public()})

            if u.path == "/api/tg/logout":
                _tg_set(cancel=True)
                TG_INPUT.put({"kind": "cancel"})
                removed = tg_logout()
                return self._json({"ok": True, "removed": removed,
                                   "message": ("已删除本地会话：%s" % ", ".join(removed)) if removed
                                   else "本地没有会话文件。账号侧的会话去 Telegram → 设置 → 设备 里终止",
                                   "tg": tg_status_public()})

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
            if u.path == "/api/source/logout":
                platform = (body.get("platform") or "").strip()
                if platform not in ("douyin", "xhs"):
                    return self._json({"ok": False, "error": "不支持的平台"}, 400)
                # 停掉 headless Chrome，清掉 profile 里的 cookies，再重启
                cleanup_stale_downloader()
                time.sleep(1.0)
                profile = BASE / "vendor" / "douyin-ai-video-downloader" / ".chrome-profile"
                for fname in ("Cookies", "Cookies-journal", "Login Data", "Login Data-journal",
                              "Web Data", "Web Data-journal"):
                    f = profile / "Default" / fname
                    try:
                        if f.exists():
                            f.unlink()
                    except Exception:
                        pass
                # 清 Local Storage / Session Storage
                for dname in ("Local Storage", "Session Storage"):
                    d = profile / "Default" / dname
                    try:
                        if d.exists():
                            shutil.rmtree(str(d), ignore_errors=True)
                    except Exception:
                        pass
                try:
                    start_douyin_chrome()
                except Exception:
                    pass
                _login_cache_set(platform, False)   # 退出后立刻失效，别闪「已登录」
                return self._json({"ok": True, "message": "已退出登录"})
            if u.path == "/api/hide":
                kind = (body.get("kind") or "").strip()
                hidden = bool(body.get("hidden", True))
                keys = body.get("keys")
                if keys is None:
                    keys = [body.get("key")]
                keys = [k for k in keys if k not in (None, "")]
                if kind == "all" and not hidden:
                    save_hidden({"items": [], "authors": [], "platforms": [], "collections": []})
                    return self._json({"ok": True})
                if kind not in ("item", "author", "platform", "collection") or not keys:
                    return self._json({"ok": False, "error": "参数不对"}, 400)
                field = {"item": "items", "author": "authors",
                         "platform": "platforms", "collection": "collections"}[kind]
                h = load_hidden()
                cur = list(h.get(field) or [])
                if kind in ("item", "collection"):
                    norm = []
                    for k in keys:
                        try:
                            norm.append(int(k))
                        except Exception:
                            pass
                    keys = norm
                else:
                    keys = [str(k) for k in keys]
                if hidden:
                    for k in keys:
                        if k not in cur:
                            cur.append(k)
                else:
                    cur = [x for x in cur if x not in keys]
                h[field] = cur
                save_hidden(h)
                return self._json({"ok": True, "hidden": h})
            if u.path == "/api/archive_batch":
                raw = body.get("urls") or body.get("text") or ""
                if isinstance(raw, list):
                    raw = "\n".join(str(x) for x in raw)
                urls = batch.normalize_urls(raw)[:200]
                if not urls:
                    return self._json({"ok": False, "error": "没有解析到链接"})
                targets = body.get("targets") or None
                ids = [run_archive(u, targets) for u in urls]
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
    """历史遗留：早期手工作品被统一写成「图文」。

    类型现在由目录里的实际文件决定（见 refresh_item_media），不再一刀切改回去，
    否则用户刚选的「视频」每次重启都会被抹掉。空目录保持原样。
    """
    try:
        con = db()
        rows = con.execute("SELECT id, folder FROM items WHERE platform='manual'"
                           " OR item_id LIKE 'manual_%'").fetchall()
        n = 0
        for r in rows:
            info = refresh_item_media(con, r["id"], r["folder"] or "")
            if info["type"]:
                n += 1
        con.commit()
        con.close()
        return n
    except Exception:
        return 0


def _listener_pid(port):
    """占用 TCP 端口的进程 PID；查不到返回 None"""
    lsof = shutil.which("lsof") or "/usr/sbin/lsof"
    try:
        r = subprocess.run([lsof, "-ti", "tcp:%d" % port, "-sTCP:LISTEN"],
                           capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    for tok in (r.stdout or "").split():
        try:
            pid = int(tok)
        except ValueError:
            continue
        if pid and pid != os.getpid():
            return pid
    return None


def _is_our_server(port):
    """端口上应答的是不是 media-archive 自己的 server（而不是别的程序）"""
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:%d/api/ping" % port, timeout=2) as r:
            d = json.loads(r.read().decode("utf-8", "ignore"))
        return d.get("app") == "media-archive"
    except Exception:
        return False


def _evict_pid(pid, grace=8.0):
    """礼貌地请旧实例让位：SIGTERM → 等 → SIGKILL"""
    try:
        os.kill(pid, signal.SIGTERM)
    except Exception:
        return True
    deadline = time.time() + grace
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.25)
    try:
        os.kill(pid, signal.SIGKILL)
    except Exception:
        pass
    time.sleep(0.5)
    try:
        os.kill(pid, 0)
    except OSError:
        return True
    return False


def acquire_port(port, timeout=30.0):
    """先绑定端口，再谈别的。

    这一步必须在任何有副作用的初始化之前完成：以前的顺序是
    「杀 Chrome → 起 Chrome → 绑端口」，端口被占时进程直接崩掉，
    但 Chrome 已经被杀过一遍了。launchd 的 KeepAlive 又会每隔约 10 秒
    把这个流程重跑一次 —— 于是常驻 Chrome 被反复杀死重启，
    所有正在跑的抖音任务的 CDP 连接被掐断，表现就是「抖音爬取失败率极高」。

    返回 (httpd, 失败原因)。失败时不会留下任何副作用。
    """
    deadline = time.time() + timeout
    while True:
        try:
            return ThreadingHTTPServer(("127.0.0.1", port), Handler), ""
        except OSError as e:
            if e.errno not in (errno.EADDRINUSE, errno.EADDRNOTAVAIL, 48):
                raise
            if not _is_our_server(port):
                squatter = _listener_pid(port)
                return None, ("端口 %d 被其它程序占用%s，media-archive 没有启动"
                              % (port, "（PID %s）" % squatter if squatter else ""))
            # 端口上是上一代 media-archive（孤儿实例）：接管它，
            # 让 launchd 管的这一份当家，否则会无限「拉起→撞端口→退出」。
            squatter = _listener_pid(port)
            if squatter:
                print("接管端口 %d：结束旧实例 PID %d" % (port, squatter), file=sys.stderr)
                _evict_pid(squatter)
            if time.time() >= deadline:
                return None, "端口 %d 一直被旧实例占用，接管超时" % port
            time.sleep(0.4)


def _write_pidfile(pid_file):
    try:
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(os.getpid()), "utf-8")
    except Exception:
        pass


def _clear_pidfile(pid_file):
    """只删自己写的那份：别的实例接管过之后，这里已经不是自己的 PID 了"""
    try:
        if pid_file.exists() and pid_file.read_text("utf-8").strip() == str(os.getpid()):
            pid_file.unlink()
    except Exception:
        pass


def main():
    port = 8787
    for a in sys.argv[1:]:
        if a.startswith("--port="):
            port = int(a.split("=", 1)[1])

    # 1) 先抢端口。抢不到就安静退出，绝不碰 Chrome / 数据库。
    srv, why = acquire_port(port)
    if srv is None:
        print(why, file=sys.stderr)
        return 1

    # 2) 端口到手了，才做初始化
    pid_file = pid_file_for(port)
    _write_pidfile(pid_file)

    def _shutdown(signum, frame):
        # launchctl kickstart -k / 接管旧实例时能干净退出，别留下半截状态
        threading.Thread(target=srv.shutdown, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _shutdown)
        except Exception:
            pass

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
    # 3) 常驻 Chrome：健康就复用，只有僵尸状态才清理重启
    try:
        ensure_douyin_chrome()
    except Exception as e:
        print("[warn] 抖音 Chrome 启动失败：%s" % e, file=sys.stderr)
    # 4) 后台预热登录态缓存：开批量归档时立刻能显示「已登录·昵称」，不用等 node 连 CDP
    def _warm_login_cache():
        for p in ("douyin", "xhs"):
            try:
                login_state_fast(p, max_age=120)
            except Exception:
                pass
    threading.Thread(target=_warm_login_cache, daemon=True).start()

    print("media-archive Web 端：http://127.0.0.1:%d" % port)
    print("归档目录：%s" % ARCHIVE)
    try:
        srv.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        try:
            srv.server_close()
        except Exception:
            pass
        _clear_pidfile(pid_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
