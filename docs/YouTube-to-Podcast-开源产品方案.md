# YouTube to Podcast 开源产品方案

## 定位

一句话：把公开 YouTube 频道可靠地同步成 podcast，同时保证历史修复不会被误当成
新发布。

第一版是本地 CLI，Transistor 是首个 podcast host adapter。用户自己持有 API key，
音频与字幕留在本地，不经过我们的服务器。

## 为什么不是直接开源现有脚本

现有「课代表立正」流程还承担私人媒体库、频道 OAuth、历史 manifest、邮件、launchd
和 show 级修复，直接发布会同时带来隐私泄露、硬编码和误操作风险。开源版只抽取可
复用核心：

```text
public YouTube listing
        ↓
bounded discovery + media preparation
        ↓
read-only reconciliation plan
        ↓
human reviews immutable action scope
        ↓ approval hash
Transistor draft / publish adapter
        ↓
readback verification + local ledger
```

个人项目保留为一个 downstream consumer，而不是开源软件本身。

## 0.1 已实现的产品边界

| 能力 | 0.1 行为 |
|---|---|
| 发现 | `yt-dlp` 读取公开视频列表 |
| 音频 | 下载并转为 MP3，执行前复核 SHA-256 |
| 字幕 | 下载 SRT/VTT，转为纯文本并上传 |
| 对账 | 用 episode `video_url` 中的 YouTube ID 建立稳定关联 |
| 增量 | 只处理可靠 baseline 之后的新视频 |
| 历史缺口 | 默认隔离；backfill 也只能建 draft |
| 发布顺序 | 候选按由旧到新执行，发布时间沿用 YouTube upload date |
| 审批 | exact action scope 的 approval hash |
| 执行 | 写入前复核全部本地与远端 precondition |
| 验收 | 回读 title、description、video、audio、transcript、status、date |
| 日志 | 每次 apply 独立 JSONL ledger |

明确不做：删除、去重、重排现有 episode、复用既有 draft、私密/会员视频、无人审批的
发布。

## 开源发布建议

建议最终拆为单独的公开仓库 `sunyuzheng/youtube-to-podcast`，而不是长期放在个人内容库
的子目录。理由是 issue、release、CI、依赖升级和贡献者权限都能独立管理，也不会让
个人频道文件成为安装包上下文。

建议：

- 许可证：MIT，降低个人和商业用户采用门槛。
- 发布物：GitHub Release + PyPI 包 `youtube-to-podcast`。
- 默认分支保护：PR、测试通过、至少一次 review。
- 安全：开启 Dependabot；使用 GitHub private vulnerability reporting。
- secrets：只允许环境变量或 gitignored `.env`，不提供托管 key 的 SaaS。

真正公开前需要确认名称、MIT 许可证、独立仓库归属和首个 release 的 audience。

## 自动化路线

### 0.2：每周 plan + 通知

- 跨平台 scheduler 模板（launchd、cron、GitHub Actions self-hosted）。
- 每周升级并记录 `yt-dlp` 版本，然后 doctor、plan。
- 邮件或 webhook 发送 `latest.md` 与 approval hash。
- 仍然禁止 scheduler 调用 `apply`。

### 0.3：本地 Web 审批台

- 展示新增、缺描述、缺字幕、历史缺口、远端 draft 和阻塞原因。
- 用户逐集选择 draft / publish / skip。
- UI 生成新的 exact approval scope；后端仍调用同一个 planner/executor。
- Web UI 不直接绕过 hash 与 precondition。

### 0.4：多 host adapter

抽象 podcast host interface，再增加其他支持上传 API 的平台。各 adapter 必须分别实现
list、create draft、update、publish、readback；没有可靠 readback 的 host 默认只允许
draft。

## 从当前个人流程迁移

不应立刻替换已经验证过的生产流程。先让开源版以 read-only shadow mode 对同一个
YouTube / Transistor 运行 4 周，对比候选、顺序、日期、字幕选择和阻塞项。只有连续
一致后，再让个人定时任务调用开源 planner；apply 仍保留现有人工审批。
