# 课代表立正 · 频道内容库

YouTube 频道「课代表立正」的本地内容管理工具集——覆盖下载、转录、字幕校对、上传到 Transistor.fm 播客平台的完整流程。

---

## 两条主线工作流

### A. 新视频处理（录制 → 字幕 → 上线）

```
新视频 (.mp4/.mov)
   │
   ├─ [一键处理] python3 tools/process_video.py <video.mp4>
   │    ├─ Qwen3-ASR 转录 → <stem>.qwen.srt
   │    └─ Claude 校对  → <stem>.corrected.srt
   │
   └─ [本地界面] python3 tools/webapp/app.py
        拖拽上传 + 实时日志 + 下载结果
```

### B. YouTube 下载 → 播客上线（存量维护）

```
1. 下载    → ./tools/download/download_channel.sh
2. 对账    → python3 tools/check/check_upload_candidates.py
3. 校验    → python3 tools/check/validate_guest_data.py
4. 上传    → python3 tools/upload/upload_to_transistor_v2.py --upload-only-new
5. 排序    → python3 tools/upload/reorder_episodes_by_date.py
```

详细说明见 [docs/核心任务说明.md](docs/核心任务说明.md)。

---

## 项目结构

```
archive/          本地视频资料（音频 + 字幕 + 元数据，gitignored）
  ├── 有人工字幕/   → 可直接上传 Transistor
  ├── 无人工字幕/   → 需转录后再上传
  └── 会员视频/     → 会员专属内容
docs/             工作流文档 + 复盘
envs/             Python 虚拟环境（gitignored）
logs/             评估日志 + 候选词典（gitignored）
tools/
  ├── process_video.py      新视频一键处理（转录 + 校对）
  ├── download/             yt-dlp 下载脚本
  ├── organize/             字幕分类
  ├── transcribe/           批量转录（旧存量用，Whisper/MLX/Qwen）
  ├── correct/              字幕校对引擎（Qwen+Claude pipeline）
  ├── compare/              校对效果对比评估
  ├── check/                对账 + 校验 + 诊断（只读）
  ├── upload/               上传 + 排序 + 修复（写远端）
  ├── webapp/               本地 Web 界面（Flask）
  └── youtube/              YouTube 频道管理自动化
       ├── fetch_all_videos.py      拉取全量视频元数据
       ├── build_database.py        构建本地 SQLite 数据库
       ├── apply_patches.py         批量更新视频描述（嘉宾信息块）
       ├── classify_playlists.py    AI 分类视频（Claude Haiku）
       └── create_playlists.py      创建/填充 YouTube playlist
```

---

## 配置

```bash
cp .env.example .env   # 填入 API keys
```

```
ANTHROPIC_API_KEY=...        # Claude 校对用
TRANSISTOR_API_KEY=...       # 播客上传用
TRANSISTOR_SHOW_ID=...
```

---

## 依赖

```bash
brew install yt-dlp ffmpeg

# 字幕校对（新视频流程）
pip install mlx-qwen3-asr    # Qwen3-ASR 转录（Apple Silicon only）
pip install anthropic         # Claude 校对
pip install flask             # 本地 Web 界面

# 播客上传
pip install requests

# 旧存量转录（可选）
pip install mlx-whisper       # Apple Silicon Whisper
pip install faster-whisper    # CPU/CUDA Whisper
```

---

## 文档

| 文档 | 内容 |
|------|------|
| [docs/嘉宾索引.md](docs/嘉宾索引.md) | 嘉宾完整列表 + 每位嘉宾的 archive 视频索引 |
| [docs/网站嘉宾数据说明.md](docs/网站嘉宾数据说明.md) | `lizheng.ai/guests` 的数据流、权威来源、派生文件说明 |
| [docs/核心任务说明.md](docs/核心任务说明.md) | 播客工作流（下载→上传）完整说明 |
| [docs/项目重构复盘.md](docs/项目重构复盘.md) | 2026-03-29 Transistor 元数据大修复盘 |
| [docs/字幕校对工程复盘.md](docs/字幕校对工程复盘.md) | 2026-04 Qwen+Claude 校对 pipeline 复盘 |

---

## YouTube 频道管理

详细说明见 [tools/youtube/README.md](tools/youtube/README.md)。

主要工作流：
- **描述更新**：为嘉宾视频批量追加嘉宾信息块 → `apply_patches.py`
- **Playlist**：AI 分类 705 个公开视频（11类）→ 在 YouTube 创建/填充 → `classify_playlists.py` + `create_playlists.py`
- **数据库**：本地 SQLite（`channel.db`）供查询分析 → `build_database.py`

---

## 网站 Guests 页

如果你是在维护 `https://www.lizheng.ai/guests`：

- 嘉宾 roster / 嘉宾-视频映射的唯一权威来源是 [`guests.json`](guests.json)
- 视频标题等 metadata 的本地权威来源是 `tools/youtube/all_videos_full.json`
- 更新 `guests.json` 后先跑 `python3 tools/check/validate_guest_data.py`
- 详细说明见 [docs/网站嘉宾数据说明.md](docs/网站嘉宾数据说明.md)
