#!/usr/bin/env bash
# media-archive 一键安装（macOS）
# 用法： git clone <仓库地址> && cd media-archive && ./install.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="$HOME/bin/media-archive"
DOUYIN_REPO="https://github.com/Awake118/douyin-ai-video-downloader.git"

info() { printf "\033[1;34m==>\033[0m %s\n" "$*"; }
ok()   { printf "\033[1;32m  ✓\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m  !\033[0m %s\n" "$*"; }
die()  { printf "\033[1;31m  ✗ %s\033[0m\n" "$*" >&2; exit 1; }

[[ "$(uname)" == "Darwin" ]] || die "只支持 macOS"

info "检查环境"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
[[ -x "$CHROME" ]] && ok "Google Chrome" || warn "没找到 Chrome —— 抖音抓取需要它，请先安装"

PY_BIN=""
for c in python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1; then PY_BIN="$(command -v "$c")"; break; fi
done
[[ -n "$PY_BIN" ]] && ok "python: $($PY_BIN -V 2>&1)" || die "需要 python3"

command -v node >/dev/null && ok "node $(node -v)" || warn "没找到 node —— 抖音抓取需要：brew install node"

if command -v swiftc >/dev/null; then
  ok "swiftc 可用"
else
  warn "没找到 swiftc（编译菜单栏/窗口 App 需要）：xcode-select --install"
fi

# ---------- 依赖 ----------
info "检查外部工具"
if ! command -v ffmpeg >/dev/null; then
  if command -v brew >/dev/null; then
    warn "安装 ffmpeg（B站/YouTube 合并音视频需要，可能要几分钟）"
    brew install ffmpeg || warn "ffmpeg 安装失败，其它平台仍可用"
  else
    warn "没有 ffmpeg 也没有 Homebrew，B站/YouTube 会下载失败（其余平台正常）"
  fi
else
  ok "ffmpeg"
fi

YTDLP="$(command -v yt-dlp || true)"
if [[ -z "$YTDLP" ]]; then
  warn "安装 yt-dlp（X/YouTube/B站/IG/微博/Reddit 等 1800+ 站点的解析引擎）"
  "$PY_BIN" -m pip install --user -q yt-dlp || warn "yt-dlp 安装失败"
  YTDLP="$HOME/Library/Python/$( "$PY_BIN" -c 'import sys;print("%d.%d"%sys.version_info[:2])' )/bin/yt-dlp"
fi
[[ -x "$YTDLP" ]] && ok "yt-dlp: $("$YTDLP" --version 2>/dev/null | head -1)" || warn "yt-dlp 不可用，通用平台将失败"

# ---------- 安装本体 ----------
info "安装到 $TARGET"
mkdir -p "$TARGET"
if [[ "$REPO_ROOT" != "$TARGET" ]]; then
  rsync -a --exclude '.git' --exclude 'vendor' --exclude '.venv' "$REPO_ROOT/" "$TARGET/"
  ok "文件已复制"
else
  ok "已在目标目录"
fi
mkdir -p "$TARGET/web" "$TARGET/app" "$TARGET/patches"

# ---------- 抖音抓取器 ----------
info "准备抖音抓取器（上游开源项目 + 本地补丁）"
VENDOR="$TARGET/vendor/douyin-ai-video-downloader"
PATCH="$TARGET/patches/douyin-meta.patch"
mkdir -p "$TARGET/vendor"

if [[ -d "$VENDOR" ]]; then
  ok "已存在，跳过下载"
else
  # 1) git clone，带低速兜底：20 秒内低于 1KB/s 就放弃，避免国内直连卡死
  if git -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=20 \
       clone --depth 1 "$DOUYIN_REPO" "$VENDOR" 2>/dev/null; then
    ok "git clone 完成"
  else
    rm -rf "$VENDOR"
    warn "git clone 失败或过慢，改用压缩包下载"
    TGZ_URL="https://codeload.github.com/Awake118/douyin-ai-video-downloader/tar.gz/refs/heads/main"
    TMPTGZ="$(mktemp -t dyarchive).tgz"
    if curl -fL --max-time 120 --retry 2 -o "$TMPTGZ" "$TGZ_URL"; then
      tar xzf "$TMPTGZ" -C "$TARGET/vendor"
      mv "$TARGET/vendor/douyin-ai-video-downloader-main" "$VENDOR"
      rm -f "$TMPTGZ"
      ok "压缩包下载完成"
    else
      rm -f "$TMPTGZ"
      die "拉取失败。若在国内网络，先设代理再重试：
       export https_proxy=http://127.0.0.1:7897
       ./install.sh"
    fi
  fi
fi

if [[ -f "$PATCH" ]]; then
  if git -C "$VENDOR" apply --check "$PATCH" 2>/dev/null; then
    git -C "$VENDOR" apply "$PATCH" && ok "已应用补丁（元信息导出 + bash 兼容修复）"
  elif patch -p1 -d "$VENDOR" --dry-run < "$PATCH" >/dev/null 2>&1; then
    patch -p1 -d "$VENDOR" < "$PATCH" >/dev/null 2>&1 && ok "已应用补丁（patch 方式）"
  else
    ok "补丁已应用或无需应用"
  fi
fi

info "准备抖音抓取器的 Python 环境"
if [[ ! -x "$VENDOR/.venv/bin/python" ]]; then
  "$PY_BIN" -m venv "$VENDOR/.venv" >/dev/null 2>&1 || die "创建 venv 失败"
fi
"$VENDOR/.venv/bin/python" -m pip install -q --upgrade pip >/dev/null 2>&1 || true
"$VENDOR/.venv/bin/python" -m pip install -q requests urllib3 >/dev/null 2>&1 || warn "装 requests 失败"
ok "venv 就绪"

# ---------- 配置 ----------
info "写入配置"
mkdir -p "$HOME/.media-archive"
CFG="$HOME/.media-archive/config.json"
if [[ -f "$CFG" ]]; then
  ok "已有配置，保留：$CFG"
else
  cat > "$CFG" <<JSON
{
  "archive_dir": "$HOME/Media/Archive",
  "hotkey": { "keyCode": 2, "modifiers": ["command", "option"], "display": "⌥⌘D" },
  "auto_watch": false,
  "web_port": 8787
}
JSON
  ok "已生成 $CFG"
fi
mkdir -p "$HOME/Media/Archive"

# ---------- App ----------
info "编译 App"
if command -v swiftc >/dev/null; then
  APP="/Applications/MediaArchive.app"
  if swiftc -O "$TARGET/app/main.swift" -o /tmp/MediaArchive.build 2>/dev/null; then
    mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
    cp /tmp/MediaArchive.build "$APP/Contents/MacOS/MediaArchive"
    cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>MediaArchive</string>
  <key>CFBundleDisplayName</key><string>媒体归档</string>
  <key>CFBundleIdentifier</key><string>com.mediaarchive.app</string>
  <key>CFBundleExecutable</key><string>MediaArchive</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSUIElement</key><false/>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppleEventsUsageDescription</key><string>读取浏览器当前标签页地址，用于一键归档当前页面</string>
</dict>
</plist>
PLIST
    codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true
    /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP" 2>/dev/null || true
    ok "已安装 $APP"
  else
    warn "App 编译失败（Xcode 命令行工具版本问题？），命令行与 Web 端不受影响"
  fi
else
  warn "跳过 App 编译（缺 swiftc）"
fi

# ---------- Web 服务 ----------
info "配置 Web 服务常驻"
PLIST="$HOME/Library/LaunchAgents/com.mediaarchive.web.plist"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.mediaarchive.web</string>
  <key>ProgramArguments</key>
  <array><string>$PY_BIN</string><string>$TARGET/server.py</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/.media-archive/web.log</string>
  <key>StandardErrorPath</key><string>$HOME/.media-archive/web.log</string>
</dict>
</plist>
PL
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST" && ok "Web 服务已常驻"
sleep 2

echo
info "安装完成"
echo "  归档目录：$HOME/Media/Archive"
echo "  Web 管理： http://127.0.0.1:8787"
echo "  命令行：   $PY_BIN $TARGET/engine.py \"<链接>\""
echo "  App：      /Applications/MediaArchive.app（首次打开如被拦，右键 → 打开）"
echo
echo "  抖音首次抓取会启动 Chrome，需要 30~120 秒；被风控时用 --headed 参数。"
