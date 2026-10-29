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
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
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
WEB = BASE / "web"
ENGINE = BASE / "engine.py"
PY = "/usr/bin/python3"

TASKS = {}
TASKS_LOCK = threading.Lock()


def db():
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    return con


def q_items(q="", platform="", author="", limit=200, offset=0):
    con = db()
    where, args = [], []
    if q:
        where.append("(title LIKE ? OR author LIKE ? OR source_url LIKE ?)")
        args += ["%%%s%%" % q] * 3
    if platform:
        where.append("platform_cn = ?")
        args.append(platform)
    if author:
        where.append("author = ?")
        args.append(author)
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
        r["files"] = list_files(r["folder"])
    return rows


def list_files(folder):
    p = Path(folder)
    if not p.exists():
        return []
    out = []
    exts = {".mp4", ".mkv", ".webm", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".m4a", ".mp3"}
    for f in sorted(p.rglob("*")):
        if f.is_file() and f.suffix.lower() in exts:
            out.append({
                "name": f.name,
                "url": "/media?p=" + urllib.parse.quote(str(f)),
                "size": f.stat().st_size,
                "kind": "video" if f.suffix.lower() in (".mp4", ".mkv", ".webm") else "image",
            })
    return out


def q_authors():
    con = db()
    rows = con.execute("""SELECT platform_cn, author, author_url, author_id, COUNT(*) n
                          FROM items GROUP BY platform_cn, author ORDER BY n DESC""").fetchall()
    con.close()
    return [dict(r) for r in rows]


def q_platforms():
    con = db()
    rows = con.execute("SELECT platform_cn, COUNT(*) n FROM items GROUP BY platform_cn ORDER BY n DESC").fetchall()
    con.close()
    return [dict(r) for r in rows]


def q_stats():
    con = db()
    row = con.execute("SELECT COUNT(*) items, SUM(file_count) files FROM items").fetchone()
    con.close()
    total = 0
    for f in ARCHIVE.rglob("*"):
        if f.is_file() and f.suffix.lower() in (".mp4", ".jpg", ".jpeg", ".png", ".webp"):
            total += f.stat().st_size
    return {"items": row["items"] or 0, "files": row["files"] or 0,
            "size": total, "platforms": len(q_platforms())}


def run_archive(url):
    tid = str(int(time.time() * 1000))
    with TASKS_LOCK:
        TASKS[tid] = {"id": tid, "url": url, "status": "running", "log": "", "folder": ""}
    def worker():
        env = dict(os.environ)
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + env.get("PATH", "")
        p = subprocess.run([PY, str(ENGINE), url], capture_output=True, text=True,
                           timeout=1200, env=env)
        out = (p.stdout or "") + (p.stderr or "")
        folder = ""
        for line in out.splitlines():
            if "归档：" in line:
                folder = line.split("归档：")[-1].split("（")[0].strip()
        with TASKS_LOCK:
            TASKS[tid].update({
                "status": "done" if p.returncode == 0 else "error",
                "log": out[-3000:], "folder": folder,
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
                    limit=int((qs.get("limit") or ["200"])[0]),
                    offset=int((qs.get("offset") or ["0"])[0])))
            if u.path == "/api/authors":
                return self._json(q_authors())
            if u.path == "/api/platforms":
                return self._json(q_platforms())
            if u.path == "/api/stats":
                return self._json(q_stats())
            if u.path.startswith("/api/task/"):
                tid = u.path.rsplit("/", 1)[-1]
                with TASKS_LOCK:
                    return self._json(TASKS.get(tid, {"status": "unknown"}))
            if u.path == "/media":
                return self._media((qs.get("p") or [""])[0])
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            return self._json({"error": str(e)}, 500)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        try:
            if u.path == "/api/archive":
                url = (body.get("url") or "").strip()
                m = re.search(r"https?://\S+", url)
                if not m:
                    return self._json({"error": "没找到链接"}, 400)
                tid = run_archive(m.group(0))
                return self._json({"task": tid})
            if u.path == "/api/reveal":
                p = body.get("path") or ""
                if p and Path(p).exists():
                    subprocess.run(["open", "-R", p])
                    return self._json({"ok": True})
                return self._json({"error": "路径不存在"}, 400)
            return self._json({"error": "not found"}, 404)
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
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = int(m.group(2))
                code = 206
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
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
                self.wfile.write(chunk)
                left -= len(chunk)


def main():
    port = 8787
    for a in sys.argv[1:]:
        if a.startswith("--port="):
            port = int(a.split("=", 1)[1])
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("media-archive Web 端：http://127.0.0.1:%d" % port)
    print("归档目录：%s" % ARCHIVE)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
