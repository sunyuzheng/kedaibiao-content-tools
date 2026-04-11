#!/bin/bash
# 转录队列：先跑无字幕视频，完成后自动跑对比转录（错题本数据集）

PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$PROJECT_ROOT" || exit 1

PYTHON="/tmp/qwen_env/bin/python3"
LOG_DIR="logs"
mkdir -p "$LOG_DIR"

BATCH1_PID=55172
BATCH1_LOG="$LOG_DIR/transcribe_qwen_run.log"
BATCH2_LOG="$LOG_DIR/transcribe_qwen_compare.log"

echo "======================================================"
echo "转录队列启动"
echo "  Batch 1 (无字幕转录) PID: $BATCH1_PID"
echo "  Batch 2 (对比转录)   将在 Batch 1 完成后自动启动"
echo "======================================================"

# 等待 Batch 1 完成
echo ""
echo "[队列] 等待 Batch 1 (PID $BATCH1_PID) 完成..."
while kill -0 "$BATCH1_PID" 2>/dev/null; do
    DONE=$(grep -c "成功=" "$BATCH1_LOG" 2>/dev/null || echo 0)
    echo "[队列] Batch 1 进行中... 已完成约 $DONE 个文件 ($(date '+%H:%M'))"
    sleep 300  # 每5分钟汇报一次
done

echo ""
echo "[队列] Batch 1 完成！$(date)"
echo "[队列] 启动 Batch 2：对比转录（错题本数据集）..."
echo ""

# 启动 Batch 2
"$PYTHON" -u tools/transcribe/batch_transcribe_qwen_compare.py \
    >> "$BATCH2_LOG" 2>&1

echo ""
echo "[队列] Batch 2 完成！$(date)"
echo "[队列] 全部转录任务完成。"
echo ""
echo "查看结果："
echo "  Batch 1 日志: $BATCH1_LOG"
echo "  Batch 2 日志: $BATCH2_LOG"
echo "  进度文件:      $LOG_DIR/transcribe_progress_qwen.json"
echo "  对比进度:      $LOG_DIR/transcribe_progress_qwen_compare.json"
