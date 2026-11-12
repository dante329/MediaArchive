#!/usr/bin/env bash
# 让下载用的 Chrome 登录小红书（同一 profile），登录态长期保存。
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENDOR="$ROOT/vendor/douyin-ai-video-downloader"
PROFILE="$VENDOR/.chrome-profile"
PORT="${CDP_PORT:-9222}"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
WAIT_MIN="${WAIT_MIN:-10}"
CHECK="$ROOT/tools/check_login_xhs.js"

command -v node >/dev/null || { echo "需要 node" >&2; exit 1; }
[ -x "$CHROME" ] || { echo "找不到 Google Chrome" >&2; exit 1; }
cdp_up() { curl -sS --max-time 2 "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1; }
login_state() { node "$CHECK" "$PORT" 2>/dev/null | head -1; }

if [ "$(login_state)" = "logged_in" ]; then
  echo "已是登录状态，无需重复扫码。"; exit 0
fi
if cdp_up; then
  echo "端口 $PORT 上已有 Chrome 在跑但未登录小红书，请先关闭它。" >&2; exit 1
fi

mkdir -p "$PROFILE"
echo "启动 Chrome（独立 profile）..."
open -na "Google Chrome" --args \
  --remote-debugging-port="$PORT" --remote-allow-origins='*' \
  --user-data-dir="$PROFILE" \
  --no-first-run --no-default-browser-check \
  --window-size=1200,900 \
  --new-window "https://www.xiaohongshu.com/"
for _ in $(seq 1 40); do cdp_up && break; sleep 1; done
cdp_up || { echo "Chrome 没起来" >&2; exit 1; }

echo "======================================================"
echo " 窗口已弹出，请在小红书页面点「登录」→ 扫码"
echo " 最多等待 ${WAIT_MIN} 分钟 ..."
echo "======================================================"
OK=0
for _ in $(seq 1 $((WAIT_MIN * 6))); do
  if [ "$(login_state)" = "logged_in" ]; then OK=1; break; fi
  sleep 10
done
if [ "$OK" = "1" ]; then
  echo "✓ 登录成功"
  node "$ROOT/vendor/douyin-ai-video-downloader/scripts/close_browser.js" "$PORT" >/dev/null 2>&1 || true
  sleep 3
else
  echo "✗ 超时未检测到登录" >&2; exit 1
fi
