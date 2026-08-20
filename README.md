# 课代表立正 · 频道内容库

YouTube 频道「课代表立正」的本地内容管理工具集——覆盖下载、转录、字幕校对、上传到 Transistor.fm 播客平台的完整流程。

---

## 两条主线工作流

### 可复用开源版：YouTube to Podcast

`packages/youtube-to-podcast/` 是从本项目现役流程抽出的通用、可安装 CLI。
它面向任意公开视频频道与 Transistor show，默认只做增量 draft，并通过不可变
plan + approval hash 阻止误发和历史补档变成新发布。这也是课代表立正把真实生产
基础设施打磨成熟后，再开放给更多人检查、改造和拥有的一次实践。

安装、配置和安全边界见
[packages/youtube-to-podcast/README.md](packages/youtube-to-podcast/README.md)。

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
1. 发现    → .venv-podcast/bin/python tools/youtube/fetch_public_videos.py
2. 下载    → ./tools/download/download_channel.sh（只处理未见过的增量条目）
3. 计划    → 候选发现 plan → bounded candidate verification → 最终 plan
4. 审批    → 检查 logs/podcast_sync/plans/latest.md 与 approval hash
5. 执行    → .venv-podcast/bin/python tools/upload/apply_podcast_sync_plan.py ...
6. 质检    → .venv-podcast/bin/python tools/check/check_upload_quality.py --n 10
7. 重排    → 使用最终 publish payload 中已审核的完整投影
```

播客专用 show notes 的版本化权威来源是 `podcast_show_notes/<video_id>.txt`；archive
目录下的 `*.podcast-description.txt` 仅作兼容回退。它优先于 YouTube `.description`，
并以独立 description approval hash 更新既有 episode；不会把历史单集重新创建或发布。

详细说明见 [docs/核心任务说明.md](docs/核心任务说明.md)。

本地自动化：MacBook 上已通过 launchd 注册每周日 09:15 运行的同步任务
`com.sunyuzheng.kedaibiao-podcast-sync`，入口是
`tools/automation/sync_podcast.py`。定时任务只刷新、下载、对账并生成审批计划，
不会自行修改或发布 Transistor。每次运行会先尝试把项目内 yt-dlp 更新到 PyPI
最新预发布/nightly；网络或索引故障时继续使用仍可执行的本地已验证版本。
日志在 `logs/podcast_sync/`。配置 `RESEND_API_KEY`、`RESEND_FROM_EMAIL` 与
`PODCAST_SYNC_EMAIL_TO` 后，每次周任务会发送一封幂等摘要邮件；失败、待审核和
健康状态都会报告。邮件发送是 best-effort，Resend 暂时不可用不会反过来令同步任务
失败。

---

## 项目结构

```
archive/          本地视频资料（音频 + 字幕 + 元数据，gitignored）
  ├── 有人工字幕/   → 历史物理目录，不是业务状态
  ├── 无人工字幕/   → 历史物理目录，不是业务状态
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
  ├── podcast/              manifest、字幕转换、Transistor client 共享实现
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
RESEND_API_KEY=...           # 可选：周任务邮件提醒
RESEND_FROM_EMAIL=...
PODCAST_SYNC_EMAIL_TO=...
```

---

## 依赖

```bash
brew install yt-dlp ffmpeg

# 字幕校对（新视频流程）
pip install mlx-qwen3-asr    # Qwen3-ASR 转录（Apple Silicon only）
pip install anthropic         # Claude 校对
pip install flask             # 本地 Web 界面

# 播客同步（独立、锁定依赖）
python3 -m venv .venv-podcast
.venv-podcast/bin/python -m pip install -r requirements-podcast.txt

# 旧存量转录（可选）
pip install mlx-whisper       # Apple Silicon Whisper
pip install faster-whisper    # CPU/CUDA Whisper
```

---

## 文档

| 文档 | 内容 |
|------|------|
| [AGENTS.md](AGENTS.md) | AI agent 进入本项目时必须遵守的本地规则 |
| [docs/媒体库维护规则.md](docs/媒体库维护规则.md) | 媒体库 canonical manifest、字幕状态、Transistor 同步规则 |
| [docs/播客自动化审计与运行手册.md](docs/播客自动化审计与运行手册.md) | 现役/归档索引、审计结论、审批门槛、SRT 与运行手册 |
| [docs/嘉宾索引.md](docs/嘉宾索引.md) | 嘉宾完整列表 + 每位嘉宾的 archive 视频索引 |
| [docs/网站嘉宾维护手册.md](docs/网站嘉宾维护手册.md) | Guest 功能维护手册：source of truth、更新顺序、验收清单 |
| [docs/网站嘉宾数据说明.md](docs/网站嘉宾数据说明.md) | `lizheng.ai/guests` 的数据流、权威来源、派生文件说明 |
| [docs/核心任务说明.md](docs/核心任务说明.md) | 播客工作流（下载→上传）完整说明 |
| [docs/项目重构复盘.md](docs/项目重构复盘.md) | 2026-03-29 Transistor 元数据大修复盘 |
| [docs/字幕校对工程复盘.md](docs/字幕校对工程复盘.md) | 2026-04 Qwen+Claude 校对 pipeline 复盘 |
| [docs/YouTube Playlist 重构复盘.md](docs/YouTube%20Playlist%20重构复盘.md) | 2026-04 11类 playlist 体系重建：分类逻辑、执行过程、经验总结 |

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
- Guest 页面视频 metadata 的权威来源是 [`guest_video_metadata.json`](guest_video_metadata.json)
- `tools/youtube/all_videos_full.json` 是更大的本地全量视频元数据表，用来生成上面的 guest metadata
- 更新 guest 视频 metadata 时先跑 `python3 tools/youtube/build_guest_video_metadata.py`
- 低频刷新时可直接跑 `./tools/youtube/refresh_guest_page_data.sh`
- 更新 `guests.json` 或 guest 相关视频 metadata 后先跑 `python3 tools/check/validate_guest_data.py`
- GitHub 上也会在 push / PR 时自动跑 `Validate Guest Data`
- 详细说明见 [docs/网站嘉宾数据说明.md](docs/网站嘉宾数据说明.md)
