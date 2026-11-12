#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Live Photo 合成

Apple 实况照片 = 一张图 + 一段 MOV，通过同一个 UUID 关联：
  视频：QuickTime 元数据 com.apple.quicktime.content.identifier = <UUID>
  图片：EXIF ContentIdentifier                          = <UUID>
两者文件名相同（01.jpg / 01.mov），放进「照片」App 就是实况照片。

用法：
  livephoto.py <图片> <视频> [输出目录]
  livephoto.py --dir <素材目录>          # 批量：把 01.jpg + 01_live.mp4 合成 01.jpg + 01.mov
"""
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
EXIFTOOL = shutil.which("exiftool") or "/opt/homebrew/bin/exiftool"


def have_tools():
    return Path(FFMPEG).exists(), Path(EXIFTOOL).exists()


def image_size(p):
    try:
        out = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(p)],
                             capture_output=True, text=True, timeout=30).stdout
        w = int(re.search(r"pixelWidth:\s*(\d+)", out).group(1))
        h = int(re.search(r"pixelHeight:\s*(\d+)", out).group(1))
        return w, h
    except Exception:
        return 0, 0


def make_pair(image: Path, video: Path, out_dir: Path = None, stem: str = None):
    """把「图 + 视频」合成一对合规的 Live Photo

    要点（缺一个苹果就不认）：
      1. 视频尺寸必须与图片一致
      2. 视频 SAR 必须是 1:1（否则播放时被拉伸）
      3. 视频里必须有 mdta/com.apple.quicktime.still-image-time 元数据轨
         —— 这一条只能由 AVFoundation 写，ffmpeg 的 -metadata 写的是文件级标签，不算
    """
    out_dir = Path(out_dir or image.parent)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = stem or image.stem
    image = Path(image)

    # 1) 视频按图片尺寸重编码（cover 裁剪 + 强制 SAR=1）
    w, h = image_size(image)
    aligned = out_dir / ("._align_%s_%s.mp4" % (stem, uuid.uuid4().hex[:8]))
    vf = "setsar=1"
    if w and h:
        vf = ("scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,setsar=1"
              % (w, h, w, h))
    r = subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(video),
                        "-vf", vf, "-c:v", "libx264", "-crf", "20",
                        "-pix_fmt", "yuv420p", "-an", str(aligned)],
                       capture_output=True, text=True, timeout=900)
    if r.returncode != 0 or not aligned.exists():
        raise RuntimeError("视频重编码失败：%s" % (r.stderr or "")[-300:])

    # 2) 用 AVFoundation 版工具封装（写元数据轨 + 图片标识）
    lpmake = Path(__file__).resolve().parent / "tools" / "lpmake"
    mov = out_dir / (stem + ".mov")
    img = out_dir / (stem + image.suffix.lower())
    if lpmake.exists():
        r2 = subprocess.run([str(lpmake), str(image), str(aligned), str(out_dir)],
                            capture_output=True, text=True, timeout=600)
        aligned.unlink(missing_ok=True)
        if r2.returncode != 0:
            raise RuntimeError("lpmake 失败：%s" % (r2.stderr or r2.stdout)[-300:])
    else:
        # 兜底：老办法（可能不被相册接受，仅保证文件能生成）
        aligned.unlink(missing_ok=True)
        uid = str(uuid.uuid4()).upper()
        subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(video),
                        "-c", "copy", "-movflags", "+faststart+use_metadata_tags",
                        "-metadata", "com.apple.quicktime.content.identifier=" + uid,
                        "-metadata", "com.apple.quicktime.still-image-time=0",
                        "-f", "mov", str(mov)], check=True, timeout=300)
        if img.resolve() != image.resolve():
            shutil.copy2(str(image), str(img))
        subprocess.run([EXIFTOOL, "-ContentIdentifier=" + uid, "-overwrite_original", str(img)],
                       check=True, capture_output=True, timeout=120)
        return img, mov

    if img.resolve() != image.resolve():
        shutil.copy2(str(image), str(img))
    return img, mov


def verify(img: Path, mov: Path):
    """读回两边标识，确认一致"""
    def read_id(p, tag):
        for t in (tag, "Keys:" + tag):
            try:
                out = subprocess.run([EXIFTOOL, "-s3", "-" + t, str(p)],
                                     capture_output=True, text=True, timeout=30).stdout.strip()
                if out:
                    return out.splitlines()[0]
            except Exception:
                pass
        return ""
    a = read_id(img, "ContentIdentifier")
    b = read_id(mov, "ContentIdentifier")
    return a, b, bool(a) and a == b


def convert_dir(folder: Path, keep_originals=True):
    """把目录里 xx.jpg + xx_live.mp4 全部合成实况照片"""
    folder = Path(folder)
    if not Path(EXIFTOOL).exists():
        raise RuntimeError("缺少 exiftool：brew install exiftool")
    pairs = []
    for img in sorted(folder.glob("*.jpg")):
        stem = img.stem
        for cand in (folder / (stem + "_live.mp4"), folder / (stem + ".mp4")):
            if cand.exists() and not stem.endswith("_live"):
                pairs.append((img, cand))
                break
    if not pairs:
        return []
    results = []
    for img, vid in pairs:
        stem = img.stem
        try:
            ni, nv = make_pair(img, vid, folder, stem)
            uid_img, uid_mov, ok = verify(ni, nv)
            results.append({"stem": stem, "image": str(ni), "video": str(nv),
                            "id_image": uid_img, "id_video": uid_mov, "ok": ok})
            if not keep_originals:
                try:
                    vid.unlink()
                except Exception:
                    pass
        except Exception as e:
            results.append({"stem": stem, "error": str(e), "ok": False})
    return results


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 0
    if args[0] == "--dir" and len(args) > 1:
        res = convert_dir(Path(args[1]))
        for r in res:
            print(r)
        return 0
    if len(args) < 2:
        print(__doc__)
        return 2
    img, mov = make_pair(Path(args[0]), Path(args[1]),
                         Path(args[2]) if len(args) > 2 else None)
    a, b, ok = verify(img, mov)
    print("图片:", img)
    print("视频:", mov)
    print("标识:", a, "|", b, "| 一致:", ok)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
