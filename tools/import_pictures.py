#!/usr/bin/env python3
# 把本地图片/视频目录整理进归档：平台=本地（默认 ~/Pictures，环境变量 MA_IMPORT_SRC 可改）
# 规则：<源>/照片/<博主>/ 下每个压缩包=一个作品（解压后入库），每个子目录=一个作品，
#       散落的媒体文件=一个文件一个作品；其他顶层目录（证件照/screeshot）同名博主。
# 入库成功后压缩包进废纸篓。Photos Library.photoslibrary 跳过（系统保护）。
import os, sys, json, shutil, zipfile, urllib.request

SRC = os.environ.get("MA_IMPORT_SRC") or os.path.expanduser("~/Pictures")
API = "http://127.0.0.1:8787"
TRASH = os.path.expanduser("~/.Trash")
MEDIA = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".gif", ".bmp",
         ".mp4", ".mov", ".m4v", ".mkv", ".avi"}
SKIP_DIR = {"Photos Library.photoslibrary"}
STAGE = ".__extract__"


def media_files(root):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".") and d != "__MACOSX" and not d.endswith(STAGE)]
        for fn in filenames:
            if fn.startswith("."):
                continue
            if os.path.splitext(fn)[1].lower() in MEDIA:
                out.append(os.path.join(dirpath, fn))
    return sorted(out)


def unzip(zip_path, dest):
    os.makedirs(dest, exist_ok=True)
    real = os.path.realpath(dest)
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            name = info.filename
            if name.startswith("__MACOSX/"):
                continue
            if not (info.flag_bits & 0x800):
                try:
                    name = name.encode("cp437").decode("gbk")
                except Exception:
                    try:
                        name = name.encode("cp437").decode("utf-8")
                    except Exception:
                        pass
            name = name.replace("\\", "/").lstrip("/")
            if not name or os.path.basename(name).startswith("."):
                continue
            target = os.path.normpath(os.path.join(real, name))
            if not (target == real or target.startswith(real + os.sep)):
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(info) as s, open(target, "wb") as o:
                shutil.copyfileobj(s, o)


def author_for(parent):
    rel = os.path.relpath(parent, SRC)
    parts = [] if rel == "." else rel.split(os.sep)
    if not parts:
        return "本地"
    if parts[0] == "照片":
        return parts[1] if len(parts) > 1 else "照片"
    return parts[0]


def dir_groups(base, author):
    groups = []
    for entry in sorted(os.listdir(base)):
        if entry.startswith(".") or entry.endswith(STAGE):
            continue
        p = os.path.join(base, entry)
        if os.path.isdir(p):
            files = media_files(p)
            if files:
                groups.append({"author": author, "title": entry, "files": files,
                               "zip": None, "stage": None})
        elif os.path.splitext(entry)[1].lower() in MEDIA:
            groups.append({"author": author, "title": os.path.splitext(entry)[0],
                           "files": [p], "zip": None, "stage": None})
    return groups


def post(groups):
    payload = {"move": True,
               "groups": [{"author": g["author"], "title": g["title"], "files": g["files"]}
                          for g in groups]}
    req = urllib.request.Request(
        API + "/api/import_local",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=1800).read().decode("utf-8"))


def trash(p):
    base = os.path.basename(p)
    dst = os.path.join(TRASH, base)
    i = 1
    while os.path.exists(dst):
        dst = os.path.join(TRASH, "%s_%d" % (base, i))
        i += 1
    shutil.move(p, dst)


def main():
    # 0) 平台「本地」
    req = urllib.request.Request(
        API + "/api/platforms/add",
        data=json.dumps({"key": "local", "name": "本地"}).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    print("platform:", json.loads(urllib.request.urlopen(req).read().decode("utf-8")).get("ok"))

    groups = []
    # 1) 压缩包：解压到同级 staging，整包一个作品
    zips = []
    for dirpath, dirnames, filenames in os.walk(SRC):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR and not d.endswith(STAGE)]
        for fn in filenames:
            if fn.lower().endswith(".zip"):
                zips.append(os.path.join(dirpath, fn))
    print("zips:", len(zips))
    for zp in sorted(zips):
        parent = os.path.dirname(zp)
        stem = os.path.splitext(os.path.basename(zp))[0]
        stage = os.path.join(parent, stem[:40] + STAGE)
        try:
            unzip(zp, stage)
        except Exception as e:
            print("  解压失败:", os.path.basename(zp), e)
            shutil.rmtree(stage, ignore_errors=True)
            continue
        files = media_files(stage)
        if not files:
            print("  空包:", os.path.basename(zp))
            shutil.rmtree(stage, ignore_errors=True)
            continue
        groups.append({"author": author_for(parent), "title": stem,
                       "files": files, "zip": zp, "stage": stage})

    # 2) 目录 / 散文件
    photo = os.path.join(SRC, "照片")
    if os.path.isdir(photo):
        for entry in sorted(os.listdir(photo)):
            p = os.path.join(photo, entry)
            if entry.startswith(".") or not os.path.isdir(p):
                continue
            groups += dir_groups(p, entry)
        # 照片/ 下散落的媒体
        for entry in sorted(os.listdir(photo)):
            p = os.path.join(photo, entry)
            if os.path.isfile(p) and os.path.splitext(entry)[1].lower() in MEDIA:
                groups.append({"author": "照片", "title": os.path.splitext(entry)[0],
                               "files": [p], "zip": None, "stage": None})
    for entry in sorted(os.listdir(SRC)):
        p = os.path.join(SRC, entry)
        if entry.startswith(".") or entry in SKIP_DIR or not os.path.isdir(p):
            continue
        if entry == "照片":
            continue
        groups += dir_groups(p, entry)

    print("groups:", len(groups), "files:", sum(len(g["files"]) for g in groups))

    # 3) 分批入库
    B = 30
    done = 0
    for i in range(0, len(groups), B):
        batch = groups[i:i + B]
        try:
            r = post(batch)
        except Exception as e:
            print("批次失败:", i, e)
            continue
        failed_titles = {f.get("title") for f in (r.get("failed") or [])}
        done += r.get("done", 0)
        for g in batch:
            if g["zip"] and g["title"] not in failed_titles:
                try:
                    trash(g["zip"])
                    shutil.rmtree(g["stage"], ignore_errors=True)
                except Exception as e:
                    print("  清理失败:", g["zip"], e)
        print("  批次 %d-%d: done=%d failed=%d" % (i, i + len(batch) - 1,
                                                  r.get("done", 0), len(failed_titles)))
        for f in (r.get("failed") or [])[:5]:
            print("    fail:", f)
    print("TOTAL done:", done, "/", len(groups))


if __name__ == "__main__":
    main()
