# Dot 接手 Transistor 内容分类与同步维护

立正已决定：**GitHub Actions 的代码、上线配置和技术验证由当前 Codex 任务负责；Dot 接手内容判断、运行检查和异常处理。** 你不需要重新实现上传器，也不需要用浏览器逐项操作 Transistor 后台。

这份文件供立正交给自己的 Dot。目标是让 YouTube 新视频在 Mac 关机后仍能同步，并保持「对话 / 立正说」各自固定编号。

## 当前交付状态

截至 2026-09-30，云端入口、私有工作流模板、状态恢复与发布校验已在本地实现并测试。**尚未推送上线，私有运行仓库、云端密钥与调度尚未创建。** 第一次收到本文件时，先检查下表中的私有仓库和 Actions 是否已存在；不存在则请立正让原 Codex 任务完成上线，不能把本文件当成“已经跑起来”的证明。

| 对象 | 固定位置 |
| --- | --- |
| 公开代码 | `sunyuzheng/kedaibiao-content-tools` |
| 私有运行仓库，待创建 | `sunyuzheng/kedaibiao-podcast-ops` |
| 固定工作流 | `podcast-sync.yml`，显示名 `Private Podcast Sync` |
| YouTube 频道 | `UC_5lJHgnMP_lb_VpIiXV0hQ`，课代表立正 |
| Transistor show | `71709` |
| 公开输出 | [pod.lizheng.ai](https://pod.lizheng.ai) · [RSS](https://feeds.transistor.fm/kedaibiao) |
| 操作依据 | 私有 `runtime/state.json`、指定运行的 `podcast-receipt` 和 `podcast-plan` |

官方文档说明，Dot 有自己的云端电脑和持续保存的工作状态，但不会继承立正 Mac 的登录。请先核实你自己的 GitHub 连接能否读取私有仓库、查看 Actions、写入经授权的分类记录和触发工作流；缺权限时只报告具体缺项。[Dot 的电脑与应用](https://learn.chatgpt.com/docs/dots/computers-and-apps)

## 你的三项工作

### 一、给新内容分类

读取 `runtime/state.json.dot_actions`。每项提供 `video_id`、标题、原始日期、简介和 YouTube URL。需要时继续查看原视频、字幕或立正提供的素材，判断：

- `dialogue`：立正与嘉宾的对谈、访谈。
- `solo`：立正独立讲述、分析或分享，对外标签为「立正说」。
- 课程、会员、内部演示、非公开内容、直播回放等：排除，不进入公开播客。

不能只靠标题出现人名就判为对话。记录一条可核查的依据，例如“简介明确写明与某嘉宾访谈，视频开头两人交谈”。不确定时保留待处理项，提出具体疑点，不猜测分类或编号。

云端切换后，私有仓库的 `podcast_series.json` 是唯一分配来源。使用固定版本源码里的命令，让程序分配下一个系列编号；同一批按原始日期、video ID 排序，依次分配：

```bash
python tools/podcast/assign_series.py \
  --catalog /实际路径/kedaibiao-podcast-ops/podcast_series.json \
  --video-id VIDEO_ID --series dialogue --date YYYYMMDD \
  --basis '已经核对的具体分类依据'
```

这条命令只修改本地目录。按立正授予你的权限，将精确分类 diff 提交到私有仓库；若没有持续写入授权，先展示 diff 等待批准。已有条目不改编号。内容需要排除时在 `excluded-videos.json` 的 `videos` 中增加该 ID 与 `basis`，例如 `{"VIDEO_ID":{"basis":"已核实是内部课程"}}`；不要改发布器来绕过规则。

可选地，为本次新视频准备 `podcast_show_notes/VIDEO_ID.txt`。沿用项目现有校验和动态标签规则，正文不凭空编造。它不是发布必需项；没有时自动使用来源简介。历史节目简介修改属于另一项工作。

### 二、检查运行、触发计划并整理回执

上线后的预设扫描是每两小时第 17 分钟（UTC），手动入口默认 plan。分类落库后，可以在已经获得触发权限的前提下运行：

```bash
gh workflow run podcast-sync.yml \
  --repo sunyuzheng/kedaibiao-podcast-ops --ref main -f mode=plan
```

读取指定 run 的结果，不能凭一句“workflow dispatched”就报告成功：

| 回执状态 | 你的下一步 |
| --- | --- |
| `needs_classification` | 处理 `dot_actions`，写入分类后重新 plan |
| `awaiting_approval` | 整理本次公开标题、编号、简介、目的地、计划与 scope hash，交给立正审阅 |
| `published_verified` | 报告实际已发布的节目与链接；RSS/客户端传播状态照回执表述 |
| `no_new_episodes` | 无需打扰立正 |
| `blocked` / `failed` | 按具体 blocker 调查，记录 run ID、受影响视频和需要的动作 |

私有 `podcast-plan` artifact 包含准确 payload 和当批素材；只在私有环境读取。计划证据 6 小时过期，不能用旧 hash 批准重新生成的内容。`publish` 要同时指定原 `plan_run_id`、`plan_hash` 和 `approval_hash`；操作说明在[运行手册](GitHub-Actions播客同步运行手册.md)。

**你不能自行开启 `publication-policy.json.enabled`、修改权限或扩大自动发布范围。** 初始策略只生成计划。若立正明确授予云端长期自动发布权限，由 Codex 完成本机 writer 停止、云端验证和策略切换后，已分类且通过严格检查的新增视频才会自动发布。hash 用来锁定内容，本身不是授权。

### 三、处理新异常与漏跑

检查 `last_checked_at`、`last_successful_check_at`、`pending_execution` 和最近 Actions 结果。建议在你已获得创建定时任务权限后，保存每两小时检查一次的任务：超过 6 小时没有成功检查、出现新的失败、发布完成或需要立正决定时才提醒；状态未变或只是无新视频时保持安静。GitHub 定时触发可能延迟，不能只监控“workflow 是否存在”。[GitHub 触发规则](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

排错时优先遵循这些边界：

- **下载失败**：区分会员拒绝、限流、机器人验证和网络问题。GitHub 云端取音频尚待上线验证；如果持续失败，提供具体运行和视频信息给 Codex，考虑原始录音来源。
- **发布超时**：先看 `pending_execution` 和远端同一 video ID 的实际状态。不要重新建一集；执行器已支持草稿恢复和已发布回读。
- **重复草稿**：只处理当前候选对应的冲突，交给 Codex 做单独清理计划。后台 112 条历史草稿不属于本次自动清理范围。
- **部分批次已发布**：保留剩余目标。落到最新发布边界之后的条目需要明确恢复计划，不能把它当成“没有更新”。
- **历史标题或期号变化**：停止，用新计划显示差异；不让日常同步顺便修正旧节目。
- **状态提交冲突或持久化失败**：先核对远端和待执行目标，再重跑；不强推、不删除状态。

## 首次接手完成标准

请在自己环境完成以下核实，再给立正一份简短结果：

1. GitHub 私有仓库访问和 Actions 读取/触发能力是否可用；缺什么具体授权。
2. 最近一次真实云端运行的 URL、状态，以及是否已证明可以取得一条音频。没有证据就写尚未验证。
3. 目前有哪些待分类视频；给出分类和依据，必要时准备精确私有目录 diff。
4. 分类与通知的持续权限是什么、是否已经建立检查任务。没有授权或任务创建失败，不声称会持续监控。
5. 遇到阻碍时指出需要 Codex 修改的具体组件；不要另建第二个上传流程。

Dot 官方支持保存定时任务，但你的账号是否具备对应服务事件、GitHub权限和云端工具，应以实际连接结果为准。[Dot 任务与记忆](https://learn.chatgpt.com/docs/dots/tasks-and-memory)
