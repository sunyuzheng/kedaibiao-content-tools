#!/bin/bash
set -euo pipefail

# 课代表立正频道 - 本地多模态数据仓库下载脚本 (支持字幕下载)
# 频道 ID: UC_5lJHgnMP_lb_VpIiXV0hQ
# 频道 URL: https://www.youtube.com/channel/UC_5lJHgnMP_lb_VpIiXV0hQ/

CHANNEL_URL="https://www.youtube.com/channel/UC_5lJHgnMP_lb_VpIiXV0hQ/videos"
BROWSER_COOKIES="${YTDLP_COOKIES_BROWSER:-none}"

# 获取项目根目录（脚本在 tools/download/ 目录下）
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT" || exit 1
YTDLP_BIN="${PROJECT_ROOT}/.venv-podcast/bin/yt-dlp"
if [ ! -x "$YTDLP_BIN" ]; then
    YTDLP_BIN="$(command -v yt-dlp || true)"
fi

ARCHIVE_DIR="archive"
ARCHIVE_FILE="${ARCHIVE_DIR}/downloaded_history.txt"
WITH_SUBTITLES_DIR="${ARCHIVE_DIR}/有人工字幕"
WITHOUT_SUBTITLES_DIR="${ARCHIVE_DIR}/无人工字幕"

echo "=========================================="
echo "开始下载频道: 课代表立正"
echo "=========================================="
echo ""

# 检查 yt-dlp 是否安装
if [ -z "$YTDLP_BIN" ]; then
    echo "错误: yt-dlp 未安装。请按 requirements-podcast.txt 初始化项目虚拟环境"
    exit 1
fi

# 检查 ffmpeg 是否安装
if ! command -v ffmpeg &> /dev/null; then
    echo "错误: ffmpeg 未安装。请先安装: brew install ffmpeg"
    exit 1
fi

# 创建存档目录（如果不存在）
mkdir -p "${ARCHIVE_DIR}"
mkdir -p "${WITH_SUBTITLES_DIR}"
mkdir -p "${WITHOUT_SUBTITLES_DIR}"

# 并发下载参数（可以根据需要调整）
CONCURRENT_FRAGMENTS=4  # 单个视频的并发片段数

echo "下载配置:"
echo "  - 音频格式: m4a (AAC)"
echo "  - 字幕: 优先人工字幕，无则自动字幕"
echo "  - 字幕格式: SRT"
echo "  - 字幕语言: 简体中文、繁体中文、英文"
echo "  - 并发片段: $CONCURRENT_FRAGMENTS"
if [ "$BROWSER_COOKIES" = "none" ]; then
    echo "  - Cookies: 不读取浏览器（只使用匿名公开视图）"
else
    echo "  - Cookies: 从浏览器读取 ($BROWSER_COOKIES)"
fi
echo "  - 跳过会员专属视频"
echo ""

SNAPSHOT_PYTHON="${PROJECT_ROOT}/.venv-podcast/bin/python"
if [ ! -x "$SNAPSHOT_PYTHON" ]; then
  SNAPSHOT_PYTHON="$(command -v python3)"
fi
DOWNLOAD_LOG="$(mktemp -t kedaibiao-ytdlp.XXXXXX)"
NEW_URLS_TMP="$(mktemp -t kedaibiao-new-urls.XXXXXX)"
trap 'rm -f "$DOWNLOAD_LOG" "$NEW_URLS_TMP"' EXIT

if [ "${1:-}" != "--skip-listing-refresh" ]; then
  "$SNAPSHOT_PYTHON" tools/youtube/fetch_public_videos.py \
    --channel-id "UC_5lJHgnMP_lb_VpIiXV0hQ"
fi
"$SNAPSHOT_PYTHON" tools/youtube/build_incremental_download_queue.py \
  --out "$NEW_URLS_TMP" \
  --expected-channel-id "UC_5lJHgnMP_lb_VpIiXV0hQ"

YTDLP_ARGS=(
  --batch-file "${NEW_URLS_TMP}"
  --ignore-errors \
  --socket-timeout 30 \
  --retries 3 \
  --extractor-retries 3 \
  --fragment-retries 3 \
  --sleep-requests 1 \
  --format "bestaudio[ext=m4a]/bestaudio" \
  --write-description \
  --write-info-json \
  --write-thumbnail \
  --write-sub \
  --write-auto-sub \
  --sub-lang "zh,zh-Hans,zh-Hant,en.*" \
  --convert-subs srt \
  --concurrent-fragments "${CONCURRENT_FRAGMENTS}" \
  --output "${ARCHIVE_DIR}/%(upload_date)s_%(title)s_%(id)s/%(title)s.%(ext)s" \
  --download-archive "${ARCHIVE_FILE}" \
  --parse-metadata "description:(?s)(?P<meta_summary>.*)" \
  --no-playlist-reverse
)

if [ "$BROWSER_COOKIES" != "none" ]; then
  YTDLP_ARGS+=(--cookies-from-browser "$BROWSER_COOKIES")
fi

DOWNLOAD_HEALTH=0
if [ -s "$NEW_URLS_TMP" ]; then
  set +e
  "$YTDLP_BIN" "${YTDLP_ARGS[@]}" 2>&1 | tee "$DOWNLOAD_LOG"
  YTDLP_STATUS=${PIPESTATUS[0]}
  set -e
  "$SNAPSHOT_PYTHON" tools/youtube/record_public_access_denials.py \
    --log "$DOWNLOAD_LOG"
  UNEXPECTED_ERRORS="$(
    grep -E '^ERROR:' "$DOWNLOAD_LOG" \
      | grep -Eiv 'members.only|member.only|join this channel|subscriber.only|available to this channel.s members' \
      || true
  )"
  if [ -n "$UNEXPECTED_ERRORS" ]; then
    echo "错误: 新视频下载报告了非会员权限类错误："
    echo "$UNEXPECTED_ERRORS"
    DOWNLOAD_HEALTH=2
  elif [ "$YTDLP_STATUS" -ne 0 ]; then
    echo "提示: yt-dlp 仅报告了预期的会员权限错误"
  fi
else
  echo "没有新的、未分类的频道条目需要下载"
fi

echo ""
echo "=========================================="
echo "下载完成，开始分类整理..."
echo "=========================================="

# 运行 Python 脚本进行后处理分类
# 获取项目根目录（脚本在 tools/download/ 目录下）
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
ORGANIZE_SCRIPT="${PROJECT_ROOT}/tools/organize/organize_subtitles.py"

if command -v python3 &> /dev/null; then
    if [ -f "$ORGANIZE_SCRIPT" ]; then
        python3 "$ORGANIZE_SCRIPT" "${PROJECT_ROOT}/${ARCHIVE_DIR}"
    else
        echo "警告: 未找到整理脚本: $ORGANIZE_SCRIPT"
        echo "请手动运行: python3 tools/organize/organize_subtitles.py"
    fi
else
    echo "警告: 未找到 python3，跳过自动分类。"
    echo "请手动运行: python3 tools/organize/organize_subtitles.py"
fi

echo ""
echo "=========================================="
echo "完成！"
echo "=========================================="
echo "数据保存在: ${ARCHIVE_DIR}/"
echo "  - 有人工字幕: ${WITH_SUBTITLES_DIR}/"
echo "  - 无人工字幕: ${WITHOUT_SUBTITLES_DIR}/"
echo "下载历史记录: ${ARCHIVE_FILE}"
echo ""
echo "提示: 下次运行此脚本将自动跳过已下载的视频，只下载新内容。"

exit "$DOWNLOAD_HEALTH"
