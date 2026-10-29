#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Telegram 一次性配置 + 登录（MTProto，用于下载频道里的视频/图片）

准备：
  1. 浏览器打开 https://my.telegram.org → 用手机号登录
  2. 进 "API development tools"，填个应用名，拿到 api_id 和 api_hash

然后运行：
  /usr/bin/python3 ~/bin/media-archive/tg_setup.py

会依次问：api_id、api_hash、手机号（+86…）、Telegram 里收到的验证码（有两步验证再输密码）。
登录成功后 session 存到 ~/.media-archive/tg.session，之后归档 Telegram 链接就不需要再登录。
想注销：Telegram App → 设置 → 设备 → 终止该会话，并删掉 ~/.media-archive/tg.session*
"""
import json
import sys
from pathlib import Path

HOME = Path.home()
TG_DIR = HOME / ".media-archive"
CFG = TG_DIR / "tg.json"
SESSION = TG_DIR / "tg"

try:
    from telethon.sync import TelegramClient
except ImportError:
    print("缺少 telethon，先安装：\n  /usr/bin/python3 -m pip install --user telethon")
    sys.exit(1)


def load_cfg():
    if CFG.exists():
        try:
            return json.loads(CFG.read_text("utf-8"))
        except Exception:
            pass
    return {}


def main():
    TG_DIR.mkdir(parents=True, exist_ok=True)
    cfg = load_cfg()
    print("=== Telegram 配置 ===")
    print("没有 api_id/api_hash？去 https://my.telegram.org → API development tools 拿\n")
    api_id = cfg.get("api_id") or input("api_id（纯数字）: ").strip()
    api_hash = cfg.get("api_hash") or input("api_hash: ").strip()
    if not str(api_id).isdigit() or len(str(api_hash)) < 20:
        print("api_id / api_hash 格式不对，退出")
        return 1
    CFG.write_text(json.dumps({"api_id": int(api_id), "api_hash": api_hash},
                              ensure_ascii=False, indent=2), "utf-8")
    print("已写入 %s" % CFG)

    print("\n=== 登录（首次会要验证码）===")
    client = TelegramClient(str(SESSION), int(api_id), api_hash)
    client.connect()
    if client.is_user_authorized():
        me = client.get_me()
        print("已登录：%s (@%s)" % (getattr(me, "first_name", ""), getattr(me, "username", "")))
    else:
        phone = input("手机号（含国家码，如 +8613800000000）: ").strip()
        client.send_code_request(phone)
        code = input("Telegram 里收到的验证码: ").strip()
        try:
            client.sign_in(phone, code)
        except Exception as e:
            if "password" in str(e).lower() or "SessionPasswordNeeded" in type(e).__name__:
                pwd = input("两步验证密码: ").strip()
                client.sign_in(password=pwd)
            else:
                raise

    me = client.get_me()
    print("登录成功：%s (@%s)  id=%s" % (
        getattr(me, "first_name", ""), getattr(me, "username", ""), getattr(me, "id", "")))

    # 快速自检：列一个公开频道最近消息，确认能读
    print("\n=== 自检：读取公开频道 @no7nosese 最近 3 条 ===")
    try:
        ent = client.get_entity("no7nosese")
        print("频道：%s（%d 订阅）" % (getattr(ent, "title", ""), getattr(ent, "participants_count", 0) or 0))
        for m in client.get_messages(ent, limit=3):
            kind = "有媒体" if m.media else "纯文字"
            print("  #%s %s | %s | %s" % (
                m.id, m.date.strftime("%Y-%m-%d %H:%M"), kind,
                (m.message or "").replace("\n", " ")[:40]))
    except Exception as e:
        print("自检失败（不影响使用，可能是频道名写错）：%s" % e)

    client.disconnect()
    print("\n完成。现在可以：/usr/bin/python3 ~/bin/media-archive/engine.py \"https://t.me/频道/消息号\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
