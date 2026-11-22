#!/usr/bin/env python3
# 把「本地」平台每个博主的所有作品合并成一个作品：
# 文件全部搬进该博主的一个目录，DB 只留一行（title=全部照片）。
import os, re, json, shutil, sqlite3, time, urllib.request

API = os.environ.get("MA_API") or "http://127.0.0.1:8787"
VID = {".mp4", ".mov", ".m4v", ".mkv", ".avi"}


def archive_dir():
    try:
        j = json.loads(urllib.request.urlopen(API + "/api/config", timeout=5).read())
        return j.get("archive_dir") or os.path.expanduser("~/Media/Archive")
    except Exception:
        return os.path.expanduser("~/Media/Archive")


def main():
    ARCHIVE = archive_dir()
    DB = os.path.join(ARCHIVE, "index.db")
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT id, author, folder, created_ts, type FROM items "
                       "WHERE platform='local' ORDER BY author, id").fetchall()
    by_author = {}
    for r in rows:
        by_author.setdefault(r["author"], []).append(r)
    stamp = time.strftime("%Y%m%d")
    merged = 0
    for author, items in by_author.items():
        # 目标目录：该博主下统一一个
        adir = os.path.join(ARCHIVE, "本地", author)
        target = os.path.join(adir, "%s_全部照片_%d" % (stamp, int(time.time() * 1000) % 1000000))
        os.makedirs(target, exist_ok=True)
        total, vids, cts = 0, 0, 0
        seq = 0
        for r in items:
            src = r["folder"]
            if not src or not os.path.isdir(src):
                continue
            for dp, _, fs in os.walk(src):
                for f in sorted(fs):
                    if f.startswith("."):
                        continue
                    seq += 1
                    dst = os.path.join(target, f)
                    if os.path.exists(dst):
                        dst = os.path.join(target, "%d_%s" % (seq, f))
                    shutil.move(os.path.join(dp, f), dst)
                    total += 1
                    if os.path.splitext(f)[1].lower() in VID:
                        vids += 1
            shutil.rmtree(src, ignore_errors=True)
            if not cts or (r["created_ts"] and r["created_ts"] < cts):
                cts = r["created_ts"] or cts
        keep = items[0]
        con.execute("UPDATE items SET folder=?, file_count=?, type=?, created_ts=?, "
                    "saved_ts=?, title='全部照片' WHERE id=?",
                    (target, total, "视频" if vids else "图文",
                     cts or int(time.time()), int(time.time()), keep["id"]))
        if len(items) > 1:
            ids = [r["id"] for r in items[1:]]
            qs = ",".join("?" * len(ids))
            con.execute("DELETE FROM items WHERE id IN (%s)" % qs, ids)
            for t in ("collection_items", "analysis"):
                try:
                    con.execute("DELETE FROM %s WHERE item_id IN (%s)" % (t, qs), ids)
                except Exception:
                    pass
        merged += 1
        print("  %-12s → 1 作品 / %d 文件" % (author, total))
    con.commit()
    con.close()
    print("合并完成：", merged, "个博主")


if __name__ == "__main__":
    main()
