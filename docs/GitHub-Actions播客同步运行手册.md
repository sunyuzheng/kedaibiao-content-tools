# GitHub Actions 播客同步运行手册

这套入口把「课代表立正」的增量同步放到 GitHub 托管的 Linux 运行器。代码留在公开仓库，状态、分类目录、当前音频和执行记录放在私有运行仓库。Mac 关机后，已启用的云端流程仍可执行。

**当前状态：GitHub 基础设施已部署，端到端同步仍被 YouTube 云端验证阻断。** 公开代码、私有运行仓库、Transistor secret 和每两小时调度均已上线；policy 仍关闭，本机 writer 未切换。2026-09-30 两次真实云端 plan 运行均停止在 YouTube 探测，未发布节目。

最新执行代码固定为 `e743ba840046fdaf7aba241fc0865aa9422311ff`；[Linux CI](https://github.com/sunyuzheng/kedaibiao-content-tools/actions/runs/36812056337) 的 122 项测试和编译通过。[实际同步试跑](https://github.com/sunyuzheng/kedaibiao-podcast-ops/actions/runs/36812144199) 在配置 Python 3.11、Deno 2.9.7 和 `yt-dlp-ejs==0.8.0` 后，仍返回 `diagnostic=bot_or_sign_in_challenge`。Transistor 读取、私有状态提交与 receipt artifact 保存成功；不能据此声称音频已上传或同步成功。

接下来由 Dot 验证其云端电脑或原始素材来源能否稳定提供音频；当前 Codex 任务再据此接入合适的素材入口。Dot 的具体任务见[交接说明](Transistor云端同步与Dot交接方案.md)。

## 组成与权威来源

| 内容 | 位置 |
| --- | --- |
| 公开源码 | `sunyuzheng/kedaibiao-content-tools` |
| 私有运行仓库 | `sunyuzheng/kedaibiao-podcast-ops`，`main` |
| 工作流模板 | `deployment/podcast-ops/.github/workflows/podcast-sync.yml` |
| Linux 入口 | `tools/automation/cloud_podcast.py` |
| 私有运行仓库中的固定代码版本 | `code-version.txt`，完整 Git commit SHA |
| 私有持久状态 | `runtime/state.json` |
| 云端切换后的分类权威目录 | 私有仓库的 `podcast_series.json` |
| 明确排除的课程、内部内容等 | 私有仓库的 `excluded-videos.json`，每项附 `basis` |
| 当前计划及素材 | 私有 Actions artifact `podcast-plan`，保留 7 天 |
| 运行回执 | `runtime/state.json.latest_receipt`；artifact `podcast-receipt` 保留 14 天 |
| 发布目的地及受众 | Transistor show `71709`；`pod.lizheng.ai`、公开 RSS 及订阅者 |

公开源码中的 `podcast_series.json` 目前仍是本机权威目录。**只有完成经批准的云端切换后**，私有仓库才成为新分类和编号的唯一写入方，公开目录成为迁移基线。不能在本机和云端各自继续分配序号。

## 工作方式

1. 每两小时的第 17 分钟扫描本频道公开列表，并读取 Transistor 的 published 和 draft。
2. 以列表中最新已发布视频为边界，只检查其前面的新条目。没有可信边界时停止；单次最多探测 10 条、最多发布 3 期。历史漏档不自动补发。
3. 匿名探测逐项确认频道、身份、公开状态及是否直播。明确会员拒绝单独排除；机器人验证、403、限流和其他提取错误会停止，不伪装成“没有更新”。
4. 缺少系列分类时，把标题、日期、简介和来源 URL 写进 `dot_actions`，结束本次运行，不先下载整批音频。
5. 分类齐全后只下载当批音频，恢复历史日期索引，冻结标题、编号、简介、音频 hash、远端前置条件与发布范围。缺字幕按现有规则提示，不阻止公开普通视频。
6. 默认仅生成计划。正式发布必须经过明确的单次批准，或启用已获授权的窄范围自动发布策略。
7. 发布前先将待执行目标提交到私有状态仓库；保存失败即停止。执行器再核对整个已发布 feed，阻止历史标题、期号或成员变化后的旧计划。
8. 上传后回读单集，运行质量检查、只读重排检查、重新生成计划并检查 RSS。RSS 或客户端刷新延迟不触发重新发布。

2026-09-30 只读盘点：532 期已发布，身份、日期索引和系列目录均完整且无重复；后台另有 112 条历史草稿。无关旧草稿隔离保留，与本次候选对应的重复草稿会阻断。迁移包只含 715 条最小历史元数据和 532 条系列记录，不包含历史音频、OAuth、cookies 或完整下载器响应。

## 三个入口

### 生成计划

```bash
gh workflow run podcast-sync.yml \
  --repo sunyuzheng/kedaibiao-podcast-ops --ref main -f mode=plan
```

等待运行完成，下载其 `podcast-receipt`。若状态为 `awaiting_approval`，下载同一 run 的 `podcast-plan`，审阅其中 `logs/cloud/plan.json`。具体公开 payload 在 `publish_actions[].local` 和 `projected_feed`；后者同时显示当前与目标标题、期号。音频和正文与文件 hash 绑定。

`needs_classification` 表示 Dot 先处理分类；`blocked` 或 `failed` 必须按回执调查，不能直接改成 publish。`no_new_episodes` 只表示本次没有符合范围的新增节目。

### 执行一份已审阅计划

停止本机发布入口、确认没有运行中的本机同步进程，再把 `publication-policy.json.local_writer_disabled` 设置为 `true`。这项开关记录实际切换，不能提前填写。

```bash
gh workflow run podcast-sync.yml \
  --repo sunyuzheng/kedaibiao-podcast-ops --ref main \
  -f mode=publish \
  -f plan_run_id=已审阅的成功运行ID \
  -f plan_hash=已审阅的64位计划hash \
  -f approval_hash=已审阅的64位发布范围hash
```

工作流只接受本私有仓库、main、指定工作流产生的成功运行及唯一未过期 artifact。它不执行 artifact 内的代码。代码 SHA、分类目录、两个 hash、文件大小/hash 或来源证据不符时停止。候选证据有效期为 **6 小时**；artifact 保留 7 天不代表 7 天内都可以发布。过期后重新生成并审阅新计划。

### 获得持续授权后的自动发布

调度运行使用 `mode=auto`，但出厂策略 `enabled: false`，所以仍只生成计划。启用前必须完成云端取音频验证、本机写入停止、首批结果核验，并取得云端长期发布范围的明确授权。

策略必须同时包含正确的 show/channel、`scope: new_public_normal_only`、`max_items: 3`、真实的 `local_writer_disabled: true`、`approved_by` 和 `approved_at`。不能把计划 hash 当作授权，也不能让 Dot 为了清除阻塞自行开启策略或扩大范围。

## 中断与重复保护

- 对 `video_id` 对账，当前 API 使用 `youtube_url`，只对合法的旧 YouTube `video_url` 做兼容读取；媒体 URL 不会被误识别成视频 ID。
- API 写入遇到超时或 5xx 不盲目重放。下一次先查看远端实际结果，已有草稿按 ID 恢复。
- 新 runner 每次重新取得未发布候选音频；“曾经下载过”不等于“已经发布”。当前批准计划通过私有 artifact 保留确切素材。
- `pending_execution` 在第一次远端写入前持久化。下次对已发布目标做精确回读；剩余目标不会被静默删除。若部分发布导致剩余条目落到增量边界后面，会明确要求恢复审阅。
- 自动流程不清理草稿、不修历史标题/编号、不回填历史 Show Notes。需要这些操作时另建具体计划。
- Actions 使用一个 show 级并发组，拒绝取消正在发布的运行；Git 状态更新不强推、不自动合并冲突。本机必须在正式切换前退出写入。

## 上线清单与剩余验证

1. 已完成：立正批准精确源码 diff、私有状态包、仓库目的地及受众；兼容修复均保持原部署范围。
2. 已完成：公开源码推送、source CI 通过；私有 `code-version.txt` 固定到已验证代码。
3. 已完成：创建 **private** 运行仓库并放入经审阅的状态包，只配置 `TRANSISTOR_API_KEY`；show 固定 `71709`，未上传 `.env`。
4. 已试跑，尚未通过：云端可发现频道列表、读取 Transistor 和保存状态；第一个候选遇到 YouTube 登录/人机验证，未完成音频下载。先解决素材来源，再取得成功的云端 plan。
5. 对具体首批计划完成批准并切换本机 writer，再执行；确认历史节目未变、重跑无重复、RSS可见或明确记为传播中。
6. 只有取得持续发布授权后才启用 policy。把分类目录权威位置、调度状态与已验证 run URL 写回交接说明。

回滚：先暂停私有工作流或将 policy 关闭，确认无运行中的云端发布，再把私有目录和最新状态拉回本机、对账后恢复本机 writer。关闭调度不会撤回已经发布的节目；不要删除状态或重建重复单集。

## 已知限制与维护

GitHub cron 可能延迟或丢弃排队事件，因此 Dot 检查最近成功时间，并在长时间无成功运行时手动触发或报告。[GitHub 官方说明](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

本次 GitHub 托管运行器实际遇到了 YouTube 登录/人机验证，目标为 `gwPfRhi4lzo`；补齐 JS 运行依赖后结果相同。后续先验证 Dot 环境或原始音频来源，不通过改变发布条件把失败伪装成成功，也不把个人浏览器 cookies 复制进 Actions。[yt-dlp 官方说明](https://github.com/yt-dlp/yt-dlp/wiki/Extractors)

依赖固定为 `requirements-cloud-podcast.txt`，不在发布时自动升级 yt-dlp。更新先跑离线测试和云端 plan，再更新私有代码 SHA。当前不迁移 MLX 字幕模型，也不接 Resend 发信；Dot 读取回执后按授权通知。

私有仓库的协作者能读取私有 artifact；不要把运行仓库改为 public，不要把音频或计划粘到公开 issue。[GitHub artifact 访问规则](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/download-workflow-artifacts)

已配置的 JavaScript 组件按 [yt-dlp 官方 EJS 文档](https://github.com/yt-dlp/yt-dlp/wiki/EJS) 安装；它解决播放器脚本执行需求，不保证 YouTube 接受托管运行器的请求。
