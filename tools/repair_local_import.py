#!/usr/bin/env python3
# 修复导入中断留下的烂尾：
# 1) ARCHIVE/本地/ 下没有 DB 记录的孤儿目录 → 补登记为作品
# 2) 内容已入库的 zip → 进废纸篓，并清掉同级 staging 目录
import os, re, json, shutil, sqlite3, time, urllib.request

SRC = os.environ.get("MA_IMPORT_SRC") or os.path.expanduser("~/Pictures")
API = os.environ.get("MA_API") or "http://127.0.0.1:8787"


def _archive_dir():
    try:
        j = json.loads(urllib.request.urlopen(API + "/api/config", timeout=5).read())
        return j.get("archive_dir") or os.path.expanduser("~/Media/Archive")
    except Exception:
        return os.path.expanduser("~/Media/Archive")


ARCHIVE = os.environ.get("MA_ARCHIVE") or _archive_dir()
DB = os.path.join(ARCHIVE, "index.db")
TRASH = os.path.expanduser("~/.Trash")
STAGE = ".__extract__"
MEDIA = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".gif", ".bmp",
         ".mp4", ".mov", ".m4v", ".mkv", ".avi"}
VID = {".mp4", ".mov", ".m4v", ".mkv", ".avi"}


def sanitize(t):
    return re.sub(r'[\\/:*?"<>|]', "_", t)[:50]


def trash(p):
    base = os.path.basename(p)
    dst = os.path.join(TRASH, base)
    i = 1
    while os.path.exists(dst):
        dst = os.path.join(TRASH, "%s_%d" % (base, i))
        i += 1
    shutil.move(p, dst)


def main():
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    owned = {r["folder"] for r in con.execute(
        "SELECT folder FROM items WHERE platform='local'")}
    titles = {}
    for r in con.execute("SELECT author, title FROM items WHERE platform='local'"):
        titles.setdefault(r["author"], set()).add(r["title"])

    # 1) 孤儿目录补登记
    fixed = 0
    local_root = os.path.join(ARCHIVE, "本地")
    seq = 0
    if os.path.isdir(local_root):
        for author in sorted(os.listdir(local_root)):
            adir = os.path.join(local_root, author)
            if not os.path.isdir(adir):
                continue
            for fn in sorted(os.listdir(adir)):
                fpath = os.path.join(adir, fn)
                if fpath in owned or not os.path.isdir(fpath):
                    continue
                files = [os.path.join(dp, f) for dp, _, fs in os.walk(fpath)
                         for f in fs if os.path.splitext(f)[1].lower() in MEDIA]
                if not files:
                    continue
                m = re.match(r"^\d{8}_(.*)_\d{6}$", fn)
                title = m.group(1) if m else fn
                cts = min(int(os.path.getmtime(f)) for f in files)
                vids = sum(1 for f in files if os.path.splitext(f)[1].lower() in VID)
                seq += 1
                con.execute("""INSERT INTO items
                    (platform, platform_cn, author, author_id, author_url, title,
                     item_id, type, created_ts, saved_ts, file_count, folder, source_url)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    ("local", "本地", author, "", "", title,
                     "localr_%d_%d" % (int(time.time() * 1000), seq),
                     "视频" if vids else "图文", cts, int(time.time()),
                     len(files), fpath, ""))
                titles.setdefault(author, set()).add(title)
                fixed += 1
    con.commit()
    print("孤儿目录补登记:", fixed)

    # 2) 已入库的 zip 进废纸篓 + 清 staging
    trashed = 0
    for dirpath, dirnames, filenames in os.walk(SRC):
        dirnames[:] = [d for d in dirnames
                       if d != "Photos Library.photoslibrary" and not d.endswith(STAGE)]
        for fn in filenames:
            if not fn.lower().endswith(".zip"):
                continue
            zp = os.path.join(dirpath, fn)
            stem = os.path.splitext(fn)[0]
            rel = os.path.relpath(dirpath, SRC).split(os.sep)
            author = (rel[1] if len(rel) > 1 else rel[0]) if rel and rel[0] == "照片" \
                else (rel[0] if rel != ["."] else "本地")
            if sanitize(stem) in titles.get(author, set()):
                try:
                    trash(zp)
                    shutil.rmtree(os.path.join(dirpath, stem[:40] + STAGE),
                                  ignore_errors=True)
                    trashed += 1
                except Exception as e:
                    print("  清理失败:", zp, e)
    # 残留空 staging 一并清掉
    for dirpath, dirnames, filenames in os.walk(SRC):
        for d in list(dirnames):
            if d.endswith(STAGE):
                shutil.rmtree(os.path.join(dirpath, d), ignore_errors=True)
    con.close()
    print("zip 进废纸篓:", trashed)


if __name__ == "__main__":
    main()
