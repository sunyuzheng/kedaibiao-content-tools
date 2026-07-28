# 课代表播客决策面板

这是 YouTube → Transistor 自动化项目的本地、只读决策台。它把最新 immutable
plan、Transistor 现状、YouTube 快照和本地记录汇总成一个可筛选、可标注、可导出的网页。

## 本地使用

```bash
npm install
npm run dev
```

打开 <http://localhost:3000/>。

页面中的决定和笔记保存在当前浏览器的 `localStorage`，按 plan hash 隔离。右上角可导出
JSON 或复制摘要。网页本身不会发布节目、修改 Transistor，也不会把“倾向纳入”解释为
远端批准。

右上角的“重新读取实时状态”会调用本地只读刷新器，重新读取 Transistor 和本地证据，
然后自动更新页面。按钮不会执行发布或修改远端内容。

## 刷新数据

```bash
npm run refresh-data
```

这会以只读方式重新检查 Transistor，并读取主项目的最新 plan、transcript ledger、
YouTube snapshot、launchd 配置和 yt-dlp 版本，随后重写
`app/podcast-state.local.json`。刷新后重新打开页面即可。该本地快照已 gitignore；
公开仓库只包含不带真实节目数据的 `app/podcast-state.example.json`。首次在新 clone
中构建时会自动从 example 创建本地占位快照。

## 验证

```bash
npm test
```

测试包含生产构建，以及关键计数、条目数组和本地决策功能的静态一致性检查。

## 数据边界

- 主项目：dashboard 的父目录（`../`）
- Dashboard：`decision-dashboard/`
- 所有远端写操作仍需由主项目重新生成不可变计划，并针对精确 approval hash 单独批准。
