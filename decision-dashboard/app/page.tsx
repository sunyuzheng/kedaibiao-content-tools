"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import state from "./podcast-state.local.json";

type Tab = "overview" | "publish" | "blocked" | "metadata" | "system";
type DecisionStatus = "do_next" | "defer" | "investigate";
type DecisionEntry = { status?: DecisionStatus; note?: string };
type DecisionMap = Record<string, DecisionEntry>;

type PublishItem = (typeof state.publishItems)[number];
type BlockedItem = (typeof state.blockedItems)[number];
type MetadataItem = (typeof state.metadataGaps)[number];

const STORAGE_KEY = `kedaibiao-podcast-decisions:${state.plan.planHash}`;

const tabs: Array<{ id: Tab; label: string; count?: number }> = [
  { id: "overview", label: "总览" },
  { id: "publish", label: "待发布", count: state.summary.publishReady },
  { id: "blocked", label: "Blocked", count: state.summary.publishBlocked },
  { id: "metadata", label: "描述缺口", count: state.summary.metadataGaps },
  { id: "system", label: "系统健康" },
];

const reasonLabels: Record<string, string> = {
  youtube_candidate_not_verified: "YouTube 尚未验证",
  multiple_remote_drafts: "存在多个远端 draft",
  missing_description: "缺少 description",
  missing_transcript: "缺少 transcript",
};

const sourceLabels: Record<string, string> = {
  local_corrected: "本地精校字幕",
  timed_unknown: "来源未确认字幕",
  missing: "无字幕",
};

const actionLabels: Record<string, string> = {
  update_draft_then_publish: "复用并修复现有 draft",
  create_draft_then_publish: "创建新 draft",
};

const missingTranscriptArtifacts =
  state.summary.remotePublished - state.summary.transcriptArtifacts;
const transcriptCoveragePercent = state.summary.remotePublished
  ? Math.round(
      (state.summary.transcriptArtifacts / state.summary.remotePublished) * 1000,
    ) / 10
  : 0;

function formatDate(value?: string | null) {
  if (!value) return "未知日期";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  }).format(new Date(value));
}

function formatDateTime(value?: string | null) {
  if (!value) return "未知时间";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "America/Los_Angeles",
  }).format(new Date(value));
}

function compactHash(value?: string | null) {
  if (!value) return "—";
  return `${value.slice(0, 9)}…${value.slice(-7)}`;
}

function formatBytes(bytes: number) {
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function decisionKey(scope: string, id: string) {
  return `${scope}:${id}`;
}

function decisionLabel(scope: string, status: DecisionStatus) {
  if (status === "defer") return "暂缓";
  if (status === "investigate") return "需调查";
  if (scope === "publish") return "倾向纳入";
  if (scope === "blocked") return "安排处理";
  return "安排修复";
}

function statusTone(status?: DecisionStatus) {
  if (status === "do_next") return "positive";
  if (status === "defer") return "muted";
  if (status === "investigate") return "warning";
  return "plain";
}

export default function Home() {
  const [activeTab, setActiveTab] = useState<Tab>("overview");
  const [decisions, setDecisions] = useState<DecisionMap>({});
  const [expanded, setExpanded] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("all");
  const [copyState, setCopyState] = useState("复制决策摘要");
  const [refreshState, setRefreshState] = useState<
    "idle" | "refreshing" | "success" | "failed"
  >("idle");
  const decisionsLoaded = useRef(false);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      try {
        const saved = window.localStorage.getItem(STORAGE_KEY);
        decisionsLoaded.current = true;
        if (saved) setDecisions(JSON.parse(saved));
      } catch {
        decisionsLoaded.current = true;
        // A blocked localStorage should not prevent the dashboard from working.
      }
    }, 0);
    return () => window.clearTimeout(timer);
  }, []);

  useEffect(() => {
    if (!decisionsLoaded.current) return;
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(decisions));
    } catch {
      // Decisions remain available for the current session.
    }
  }, [decisions]);

  const allDecisionIds = useMemo(
    () => [
      ...state.publishItems.map((item) => decisionKey("publish", item.videoId)),
      ...state.blockedItems.map((item) => decisionKey("blocked", item.videoId)),
      ...state.metadataGaps.map((item) =>
        decisionKey("metadata", item.episodeId),
      ),
    ],
    [],
  );

  const reviewedCount = allDecisionIds.filter(
    (id) => decisions[id]?.status,
  ).length;

  function setDecision(
    scope: string,
    id: string,
    patch: DecisionEntry,
  ) {
    const key = decisionKey(scope, id);
    setDecisions((current) => ({
      ...current,
      [key]: { ...current[key], ...patch },
    }));
  }

  function switchTab(tab: Tab) {
    setActiveTab(tab);
    setSearch("");
    setFilter("all");
    setExpanded(null);
    window.setTimeout(() => {
      document.getElementById("workspace")?.scrollIntoView({
        behavior: "smooth",
        block: "start",
      });
    }, 0);
  }

  function buildDecisionExport() {
    return {
      exportedAt: new Date().toISOString(),
      sourcePlanHash: state.plan.planHash,
      sourcePublishApprovalHash: state.plan.publishApprovalHash,
      notice:
        "这是本地审查意见，不是远端执行批准。任何发布仍需重新生成并审核不可变计划。",
      decisions,
    };
  }

  function downloadDecisions() {
    const blob = new Blob(
      [JSON.stringify(buildDecisionExport(), null, 2)],
      { type: "application/json" },
    );
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `podcast-decisions-${new Date().toISOString().slice(0, 10)}.json`;
    anchor.click();
    URL.revokeObjectURL(url);
  }

  async function copySummary() {
    const selected = Object.entries(decisions).filter(
      ([, value]) => value.status,
    );
    const counts = selected.reduce<Record<string, number>>((result, [, value]) => {
      const key = value.status || "unreviewed";
      result[key] = (result[key] || 0) + 1;
      return result;
    }, {});
    const text = [
      `课代表播客审查摘要`,
      `来源 plan: ${state.plan.planHash}`,
      `已审查: ${selected.length}/${allDecisionIds.length}`,
      `安排处理: ${counts.do_next || 0}`,
      `暂缓: ${counts.defer || 0}`,
      `需调查: ${counts.investigate || 0}`,
      `注意：这不是发布批准；远端操作仍需新的 immutable plan + approval hash。`,
    ].join("\n");
    try {
      await navigator.clipboard.writeText(text);
      setCopyState("已复制");
      window.setTimeout(() => setCopyState("复制决策摘要"), 1600);
    } catch {
      setCopyState("复制失败");
    }
  }

  async function refreshLiveData() {
    if (refreshState === "refreshing") return;
    setRefreshState("refreshing");
    try {
      const response = await fetch("/__podcast-dashboard/refresh", {
        method: "POST",
        headers: { Accept: "application/json" },
      });
      const result = (await response.json()) as {
        ok?: boolean;
        error?: string;
      };
      if (!response.ok || !result.ok) {
        throw new Error(result.error || "读取失败");
      }
      setRefreshState("success");
      window.setTimeout(() => window.location.reload(), 800);
    } catch {
      setRefreshState("failed");
    }
  }

  const filterOptions = {
    publish: [
      ["all", `全部 ${state.summary.publishReady}`],
      ["clean", "无 warning"],
      ["warning", "有 warning"],
      ["update", "复用 draft"],
      ["create", "新建 draft"],
    ],
    blocked: [
      ["all", `全部 ${state.summary.publishBlocked}`],
      ["probe", "仅待重新验证"],
      ["duplicate", "含重复 draft"],
    ],
    metadata: [
      ["all", `全部 ${state.summary.metadataGaps}`],
      ["recoverable", `已有来源 ${state.summary.metadataRecoverable}`],
      ["manual", `需人工写 ${state.summary.metadataManual}`],
    ],
  } as const;

  const filteredPublish = state.publishItems.filter((item) => {
    const matchesSearch =
      `${item.title} ${item.videoId} ${item.episodeId || ""}`
        .toLowerCase()
        .includes(search.toLowerCase());
    const matchesFilter =
      filter === "all" ||
      (filter === "clean" && item.warnings.length === 0) ||
      (filter === "warning" && item.warnings.length > 0) ||
      (filter === "update" && item.action === "update_draft_then_publish") ||
      (filter === "create" && item.action === "create_draft_then_publish");
    return matchesSearch && matchesFilter;
  });

  const filteredBlocked = state.blockedItems.filter((item) => {
    const matchesSearch = `${item.title} ${item.videoId}`
      .toLowerCase()
      .includes(search.toLowerCase());
    const matchesFilter =
      filter === "all" ||
      (filter === "probe" &&
        !item.reasons.includes("multiple_remote_drafts")) ||
      (filter === "duplicate" &&
        item.reasons.includes("multiple_remote_drafts"));
    return matchesSearch && matchesFilter;
  });

  const filteredMetadata = state.metadataGaps.filter((item) => {
    const matchesSearch =
      `${item.title} ${item.videoId} ${item.episodeId}`
        .toLowerCase()
        .includes(search.toLowerCase());
    const matchesFilter =
      filter === "all" ||
      (filter === "recoverable" && item.source.chars > 0) ||
      (filter === "manual" && item.source.chars === 0);
    return matchesSearch && matchesFilter;
  });

  return (
    <main>
      <header className="topbar">
        <a className="brand" href="#top" aria-label="返回页面顶部">
          <span className="brand-mark">课</span>
          <span>
            <b>Podcast Ops</b>
            <small>Show {state.showId}</small>
          </span>
        </a>
        <div className="topbar-actions">
          <span className="local-badge">
            <i aria-hidden="true" /> 本地只读 · 最近读取{" "}
            {formatDateTime(state.generatedAt)}
          </span>
          <button
            className="refresh-button"
            disabled={refreshState === "refreshing"}
            onClick={refreshLiveData}
          >
            {refreshState === "refreshing"
              ? "正在读取…"
              : refreshState === "success"
                ? "已读取，正在更新…"
                : refreshState === "failed"
                  ? "读取失败 · 重试"
                  : "重新读取实时状态"}
          </button>
          <button className="quiet-button" onClick={copySummary}>
            {copyState}
          </button>
          <button className="primary-small" onClick={downloadDecisions}>
            导出决定
          </button>
        </div>
      </header>
      <div className="refresh-feedback" aria-live="polite">
        {refreshState === "failed"
          ? "没有修改任何远端数据。请确认本地网络和 Transistor 配置后重试。"
          : ""}
      </div>

      <section className="hero" id="top">
        <div className="hero-copy">
          <div className="eyebrow">
            实时决策台 · 最近读取 {formatDateTime(state.generatedAt)}
          </div>
          <h1>
            播客同步
            <br />
            <span>现在还缺什么？</span>
          </h1>
          <p className="hero-lede">
            发布批次和编号重排已经收尾。现在真正需要你判断的，只剩 blocked
            条目和历史描述缺口；两者都不会被系统自行发布。
          </p>
          <div className="completion-card">
            <div>
              <span className="completion-label">TRANSCRIPT COVERAGE</span>
              <strong>
                {state.summary.transcriptArtifacts}
                <small> / {state.summary.remotePublished}</small>
              </strong>
            </div>
            <div className="completion-copy">
              <b>
                {missingTranscriptArtifacts === 0
                  ? "全部完成"
                  : `${missingTranscriptArtifacts} 集按规则无字幕`}
              </b>
              <span>
                {state.summary.transcriptArtifacts} 集已有 artifact；无字幕集未阻塞发布
              </span>
            </div>
            <div
              className="completion-ring"
              aria-label={`Transcript 覆盖率 ${transcriptCoveragePercent}%`}
            >
              {transcriptCoveragePercent}%
            </div>
          </div>
        </div>

        <aside className="next-actions">
          <div className="next-actions-heading">
            <span>当前状态与下一步</span>
            <b>发布已完成</b>
          </div>
          <button className="next-action priority" onClick={() => switchTab("overview")}>
            <span className="action-index">01</span>
            <span className="action-body">
              <b>发布队列已经清空</b>
              <small>
                当前 {state.summary.remotePublished} 集已发布；新计划无需发布，也无需重编号。
              </small>
            </span>
            <span className="arrow" aria-hidden="true">↗</span>
          </button>
          <button className="next-action" onClick={() => switchTab("blocked")}>
            <span className="action-index">02</span>
            <span className="action-body">
              <b>复核 {state.summary.publishBlocked} 个 blocked</b>
              <small>
                证据不足就继续保持不发布；其中{" "}
                {state.summary.blockedReasonCounts.multiple_remote_drafts || 0}{" "}
                个还有重复 draft。
              </small>
            </span>
            <span className="arrow" aria-hidden="true">↗</span>
          </button>
          <button className="next-action" onClick={() => switchTab("metadata")}>
            <span className="action-index">03</span>
            <span className="action-body">
              <b>安排 {state.summary.metadataGaps} 个空描述</b>
              <small>
                {state.summary.metadataRecoverable} 个已有来源可回填；真正需要人工补写的是{" "}
                {state.summary.metadataManual} 个。
              </small>
            </span>
            <span className="arrow" aria-hidden="true">↗</span>
          </button>
        </aside>
      </section>

      <section className="decision-strip" aria-label="关键决策数字">
        <div>
          <span className="metric-number coral">{state.summary.publishReady}</span>
          <p><b>当前待发布</b><small>队列已清空，不需要新审批</small></p>
        </div>
        <div>
          <span className="metric-number amber">{state.summary.publishBlocked}</span>
          <p><b>暂时 blocked</b><small>其中 {state.summary.blockedReasonCounts.multiple_remote_drafts || 0} 个有重复 draft</small></p>
        </div>
        <div>
          <span className="metric-number blue">{state.summary.metadataGaps}</span>
          <p><b>历史空描述</b><small>{state.summary.metadataRecoverable} 可回填 · {state.summary.metadataManual} 手写</small></p>
        </div>
        <div>
          <span className="metric-number ink">{state.plan.projectedReorderCount}</span>
          <p><b>当前需重编号</b><small>feed 已稳定在 {state.plan.projectedPublishedCount} 集</small></p>
        </div>
      </section>

      <section className="workspace" id="workspace">
        <nav className="tabs" aria-label="决策面板区域">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              aria-pressed={activeTab === tab.id}
              className={activeTab === tab.id ? "active" : ""}
              onClick={() => setActiveTab(tab.id)}
            >
              {tab.label}
              {tab.count !== undefined && <span>{tab.count}</span>}
            </button>
          ))}
        </nav>

        <div className="review-progress">
          <div>
            <b>你的审查进度</b>
            <span>{reviewedCount} / {allDecisionIds.length} 个条目已标记</span>
          </div>
          <div className="progress-track" aria-hidden="true">
            <i style={{ width: `${(reviewedCount / allDecisionIds.length) * 100}%` }} />
          </div>
          <small>标记和笔记只保存在这个浏览器里</small>
        </div>

        {activeTab === "overview" && (
          <Overview switchTab={switchTab} />
        )}

        {(activeTab === "publish" ||
          activeTab === "blocked" ||
          activeTab === "metadata") && (
          <div className="review-view">
            <div className="view-heading">
              <div>
                <span className="section-kicker">
                  {activeTab === "publish"
                    ? "DECISION 01"
                    : activeTab === "blocked"
                      ? "DECISION 02"
                      : "DECISION 03"}
                </span>
                <h2>
                  {activeTab === "publish"
                    ? "待发布候选"
                    : activeTab === "blocked"
                      ? "Blocked 条目"
                      : "历史描述缺口"}
                </h2>
                <p>
                  {activeTab === "publish"
                    ? state.summary.publishReady === 0
                      ? "当前 immutable plan 没有发布动作，也没有重排动作；这里为空是正确状态。"
                      : "逐项审查后，再生成新的 immutable plan 和精确 approval hash。"
                    : activeTab === "blocked"
                      ? "未验证不等于不能发布；它只代表目前证据不足。重复 draft 必须先人工选 canonical。"
                      : "这批不影响 transcript 或当前 feed，可作为低风险清理单独审批。"}
                </p>
              </div>
              <div className={`scope-signal ${activeTab}`}>
                <strong>
                  {activeTab === "publish"
                    ? state.summary.publishReady
                    : activeTab === "blocked"
                      ? state.summary.publishBlocked
                      : state.summary.metadataGaps}
                </strong>
                <span>
                  {activeTab === "publish"
                    ? "READY"
                    : activeTab === "blocked"
                      ? "HOLD"
                      : "CLEANUP"}
                </span>
              </div>
            </div>

            <div className="filterbar">
              <label>
                <span className="sr-only">搜索条目</span>
                <input
                  type="search"
                  value={search}
                  onChange={(event) => setSearch(event.target.value)}
                  placeholder="搜索标题、Video ID、Episode ID…"
                />
              </label>
              <div className="filter-pills">
                {filterOptions[activeTab].map(([id, label]) => (
                  <button
                    key={id}
                    className={filter === id ? "active" : ""}
                    aria-pressed={filter === id}
                    onClick={() => setFilter(id)}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>

            <div className="item-list">
              {activeTab === "publish" &&
                filteredPublish.map((item) => (
                  <PublishRow
                    key={item.videoId}
                    item={item}
                    expanded={expanded === decisionKey("publish", item.videoId)}
                    toggle={() =>
                      setExpanded((current) =>
                        current === decisionKey("publish", item.videoId)
                          ? null
                          : decisionKey("publish", item.videoId),
                      )
                    }
                    decision={decisions[decisionKey("publish", item.videoId)]}
                    setDecision={(patch) =>
                      setDecision("publish", item.videoId, patch)
                    }
                  />
                ))}
              {activeTab === "blocked" &&
                filteredBlocked.map((item) => (
                  <BlockedRow
                    key={item.videoId}
                    item={item}
                    expanded={expanded === decisionKey("blocked", item.videoId)}
                    toggle={() =>
                      setExpanded((current) =>
                        current === decisionKey("blocked", item.videoId)
                          ? null
                          : decisionKey("blocked", item.videoId),
                      )
                    }
                    decision={decisions[decisionKey("blocked", item.videoId)]}
                    setDecision={(patch) =>
                      setDecision("blocked", item.videoId, patch)
                    }
                  />
                ))}
              {activeTab === "metadata" &&
                filteredMetadata.map((item) => (
                  <MetadataRow
                    key={item.episodeId}
                    item={item}
                    expanded={expanded === decisionKey("metadata", item.episodeId)}
                    toggle={() =>
                      setExpanded((current) =>
                        current === decisionKey("metadata", item.episodeId)
                          ? null
                          : decisionKey("metadata", item.episodeId),
                      )
                    }
                    decision={decisions[decisionKey("metadata", item.episodeId)]}
                    setDecision={(patch) =>
                      setDecision("metadata", item.episodeId, patch)
                    }
                  />
                ))}
              {((activeTab === "publish" && filteredPublish.length === 0) ||
                (activeTab === "blocked" && filteredBlocked.length === 0) ||
                (activeTab === "metadata" && filteredMetadata.length === 0)) && (
                <div className="empty-state">
                  <b>
                    {activeTab === "publish" && state.summary.publishReady === 0
                      ? "发布队列已清空"
                      : "没有符合条件的条目"}
                  </b>
                  <span>
                    {activeTab === "publish" && state.summary.publishReady === 0
                      ? "最新计划显示：待发布 0，待重排 0。"
                      : "换一个筛选条件或搜索词。"}
                  </span>
                </div>
              )}
            </div>
          </div>
        )}

        {activeTab === "system" && <SystemView />}
      </section>

      <footer>
        <div>
          <b>课代表播客决策面板</b>
          <span>本地快照 · 计划生成于 {formatDate(state.plan.generatedAt)}</span>
        </div>
        <p>
          页面里的“倾向纳入”只是审查意见。任何 Transistor 修改仍必须使用新的不可变计划和精确 approval hash。
        </p>
      </footer>
    </main>
  );
}

function Overview({ switchTab }: { switchTab: (tab: Tab) => void }) {
  const reorderPercent = Math.round(
    (state.plan.projectedReorderCount / state.plan.projectedPublishedCount) * 100,
  );
  return (
    <div className="overview-grid">
      <section className="recommendation-panel">
        <div className="section-kicker">RECOMMENDED PATH</div>
        <h2>建议你这样做</h2>
        <ol className="path-list">
          <li>
            <span>已完成</span>
            <div>
              <b>确认发布与重排结果</b>
              <p>
                最近一次执行发布 {state.publishOutcome.published} 集、重排{" "}
                {state.publishOutcome.reordered} 项；新计划已经归零。
              </p>
              <button onClick={() => switchTab("system")}>查看执行凭据</button>
            </div>
          </li>
          <li>
            <span>下一步</span>
            <div>
              <b>重新验证 {state.summary.publishBlocked} 个 blocked</b>
              <p>
                不必追求全：没有足够公开证据就保持不发布；另外{" "}
                {state.summary.blockedReasonCounts.multiple_remote_drafts || 0}{" "}
                个要先解决多个 draft，避免猜错 canonical。
              </p>
              <button onClick={() => switchTab("blocked")}>拆解 blocked</button>
            </div>
          </li>
          <li className="low-priority">
            <span>可延后</span>
            <div>
              <b>清理 {state.summary.metadataGaps} 个历史空描述</b>
              <p>
                {state.summary.metadataRecoverable} 个已有来源，可批量回填；
                {state.summary.metadataManual} 个需要人工写。它们不阻塞发布，也不会被当作新发布。
              </p>
              <button onClick={() => switchTab("metadata")}>查看清理清单</button>
            </div>
          </li>
        </ol>
      </section>

      <div className="overview-side">
        <section className="impact-card">
          <div className="section-kicker">PUBLISH IMPACT</div>
          <h3>当前计划会改变多少？</h3>
          <div className="feed-flow">
            <div><strong>{state.summary.remotePublished}</strong><span>当前发布</span></div>
            <i>＋{state.summary.publishReady}</i>
            <div className="target"><strong>{state.plan.projectedPublishedCount}</strong><span>计划后</span></div>
          </div>
          <div className="impact-bar">
            <span><b>{reorderPercent}%</b> 的 feed 编号会调整</span>
            <div><i style={{ width: `${reorderPercent}%` }} /></div>
            <small>{state.plan.projectedReorderCount} / {state.plan.projectedPublishedCount} 集</small>
          </div>
          <p className="impact-note">
            {state.plan.projectedReorderCount === 0
              ? "发布后的新计划是零变化：不会再发布，也不会再改编号。"
              : "发布和重排必须作为同一个审核对象，不能先发布、再临场修编号。"}
          </p>
        </section>

        <section className="done-card">
          <div className="section-kicker">DONE · NO ACTION</div>
          <h3>这些已经不用管</h3>
          <ul>
            <li><span>✓</span><p><b>{state.summary.transcriptArtifacts} 集 transcript</b><small>{missingTranscriptArtifacts} 集按已批准规则无本地字幕，不阻塞发布</small></p></li>
            <li><span>✓</span><p><b>最近发布批次</b><small>{state.publishOutcome.published} 集发布 · {state.publishOutcome.reordered} 项重排 · 已完成</small></p></li>
            <li><span>✓</span><p><b>每周自动对账</b><small>{state.system.scheduleLabel}，始终 plan-only</small></p></li>
            <li><span>✓</span><p><b>YouTube OAuth</b><small>最小权限 {state.system.oauthScope}，快照新鲜</small></p></li>
            <li><span>✓</span><p><b>yt-dlp</b><small>当前 nightly {state.system.ytDlpVersion}</small></p></li>
            <li><span>✓</span><p><b>现役 / 归档索引</b><small>交付材料已统一指向现役项目</small></p></li>
          </ul>
        </section>
      </div>
    </div>
  );
}

function DecisionControls({
  scope,
  decision,
  setDecision,
}: {
  scope: "publish" | "blocked" | "metadata";
  decision?: DecisionEntry;
  setDecision: (patch: DecisionEntry) => void;
}) {
  const options: DecisionStatus[] = ["do_next", "defer", "investigate"];
  return (
    <div className="decision-controls" aria-label="标记审查倾向">
      {options.map((status) => (
        <button
          key={status}
          aria-pressed={decision?.status === status}
          className={
            decision?.status === status ? `selected ${statusTone(status)}` : ""
          }
          onClick={() =>
            setDecision({
              status: decision?.status === status ? undefined : status,
            })
          }
        >
          {decisionLabel(scope, status)}
        </button>
      ))}
    </div>
  );
}

function PublishRow({
  item,
  expanded,
  toggle,
  decision,
  setDecision,
}: {
  item: PublishItem;
  expanded: boolean;
  toggle: () => void;
  decision?: DecisionEntry;
  setDecision: (patch: DecisionEntry) => void;
}) {
  return (
    <article className={`review-item ${item.warnings.length ? "has-warning" : ""}`}>
      <div className="item-topline">
        <button className="item-summary" onClick={toggle} aria-expanded={expanded}>
          <span className={`status-dot ${item.warnings.length ? "warning" : "ready"}`} />
          <span className="item-title">
            <b>{item.title}</b>
            <small>
              {item.targetNumber ? `目标 E${item.targetNumber}` : "待编号"} ·{" "}
              {actionLabels[item.action] || item.action} · {formatDate(item.publishedAt)}
            </small>
          </span>
          <span className="item-tags">
            {item.warnings.map((warning) => (
              <i key={warning} className="tag warning">
                {reasonLabels[warning] || warning}
              </i>
            ))}
            {item.warnings.length === 0 && <i className="tag clean">资料完整</i>}
            <i className="chevron" aria-hidden="true">{expanded ? "−" : "+"}</i>
          </span>
        </button>
        <DecisionControls
          scope="publish"
          decision={decision}
          setDecision={setDecision}
        />
      </div>
      {expanded && (
        <div className="item-detail">
          <div className="detail-grid">
            <div><span>Video ID</span><b>{item.videoId}</b></div>
            <div><span>Episode</span><b>{item.episodeId || "将新建"}</b></div>
            <div><span>Transcript</span><b>{item.transcriptChars.toLocaleString()} 字 · {sourceLabels[item.transcriptSource] || item.transcriptSource}</b></div>
            <div><span>Description</span><b>{item.descriptionChars.toLocaleString()} 字</b></div>
            <div><span>Audio</span><b>{formatBytes(item.audioBytes)}</b></div>
            <div><span>YouTube 验证</span><b>{item.youtubeStatus} · {formatDate(item.verifiedAt)}</b></div>
          </div>
          <div className="detail-actions">
            <a href={item.videoUrl || "#"} target="_blank" rel="noreferrer">打开 YouTube ↗</a>
            {item.episodeId && (
              <span>远端 draft {item.episodeId}</span>
            )}
          </div>
          <label className="note-field">
            <span>审查笔记</span>
            <textarea
              value={decision?.note || ""}
              onChange={(event) => setDecision({ note: event.target.value })}
              placeholder="为什么纳入、暂缓，或还要查什么？"
            />
          </label>
        </div>
      )}
    </article>
  );
}

function BlockedRow({
  item,
  expanded,
  toggle,
  decision,
  setDecision,
}: {
  item: BlockedItem;
  expanded: boolean;
  toggle: () => void;
  decision?: DecisionEntry;
  setDecision: (patch: DecisionEntry) => void;
}) {
  const duplicate = item.reasons.includes("multiple_remote_drafts");
  return (
    <article className="review-item blocked-item">
      <div className="item-topline">
        <button className="item-summary" onClick={toggle} aria-expanded={expanded}>
          <span className={`status-dot ${duplicate ? "danger" : "hold"}`} />
          <span className="item-title">
            <b>{item.title}</b>
            <small>
              {item.videoId} · {formatDate(item.publishedAt)} ·{" "}
              {duplicate ? `${item.remoteDraftCount} 个远端 draft` : "等待重新验证"}
            </small>
          </span>
          <span className="item-tags">
            {item.reasons.map((reason) => (
              <i key={reason} className={`tag ${reason === "multiple_remote_drafts" ? "danger" : "hold"}`}>
                {reasonLabels[reason] || reason}
              </i>
            ))}
            <i className="chevron" aria-hidden="true">{expanded ? "−" : "+"}</i>
          </span>
        </button>
        <DecisionControls
          scope="blocked"
          decision={decision}
          setDecision={setDecision}
        />
      </div>
      {expanded && (
        <div className="item-detail">
          <div className="recommendation-callout">
            <b>{duplicate ? "推荐：先选 canonical draft" : "推荐：交给下轮 bounded probe"}</b>
            <p>
              {duplicate
                ? `远端找到 ${item.remoteDraftCount} 个 draft（${item.remoteDraftIds.join("、")}）。先比较媒体和元数据，确认保留对象后再生成新计划。`
                : "当前唯一 blocker 是 YouTube 证据不足。不要手工绕过；等待限流恢复后只重新探测这批候选。"}
            </p>
          </div>
          <div className="detail-grid compact">
            <div><span>Transcript</span><b>{item.transcriptChars ? `${item.transcriptChars.toLocaleString()} 字` : "缺失"}</b></div>
            <div><span>来源</span><b>{sourceLabels[item.transcriptSource] || item.transcriptSource}</b></div>
            <div><span>Description</span><b>{item.descriptionChars.toLocaleString()} 字</b></div>
            <div><span>远端 drafts</span><b>{item.remoteDraftCount}</b></div>
          </div>
          <div className="detail-actions">
            <a href={item.videoUrl} target="_blank" rel="noreferrer">打开 YouTube ↗</a>
          </div>
          <label className="note-field">
            <span>审查笔记</span>
            <textarea
              value={decision?.note || ""}
              onChange={(event) => setDecision({ note: event.target.value })}
              placeholder={duplicate ? "准备保留哪个 draft？还要比较什么？" : "是否有理由优先重试？"}
            />
          </label>
        </div>
      )}
    </article>
  );
}

function MetadataRow({
  item,
  expanded,
  toggle,
  decision,
  setDecision,
}: {
  item: MetadataItem;
  expanded: boolean;
  toggle: () => void;
  decision?: DecisionEntry;
  setDecision: (patch: DecisionEntry) => void;
}) {
  const recoverable = item.source.chars > 0;
  return (
    <article className="review-item metadata-item">
      <div className="item-topline">
        <button className="item-summary" onClick={toggle} aria-expanded={expanded}>
          <span className={`status-dot ${recoverable ? "recoverable" : "manual"}`} />
          <span className="item-title">
            <b>{item.title}</b>
            <small>
              Episode {item.episodeId} · {formatDate(item.publishedAt)}
            </small>
          </span>
          <span className="item-tags">
            <i className={`tag ${recoverable ? "recoverable" : "manual"}`}>
              {recoverable ? `已有来源 · ${item.source.chars} 字` : "需要人工补写"}
            </i>
            <i className="chevron" aria-hidden="true">{expanded ? "−" : "+"}</i>
          </span>
        </button>
        <DecisionControls
          scope="metadata"
          decision={decision}
          setDecision={setDecision}
        />
      </div>
      {expanded && (
        <div className="item-detail">
          <div className="recommendation-callout">
            <b>{recoverable ? "推荐：纳入一次批量 backfill" : "推荐：低优先级人工补写"}</b>
            <p>
              {recoverable
                ? `来源是 ${item.source.kind === "youtube_snapshot" ? "最新 YouTube 快照" : "本地 description 文件"}。正式写入前仍需生成 17 集的精确 payload 和 hash。`
                : "本地文件和 YouTube 快照都没有可用正文。可以写一段简短摘要，也可以明确决定保持为空。"}
            </p>
          </div>
          {recoverable && (
            <blockquote>
              <span>来源预览</span>
              {item.source.preview}
            </blockquote>
          )}
          <div className="detail-actions">
            {item.videoUrl && <a href={item.videoUrl} target="_blank" rel="noreferrer">打开 YouTube ↗</a>}
            <span>{item.source.path || "没有现成来源"}</span>
          </div>
          <label className="note-field">
            <span>审查笔记</span>
            <textarea
              value={decision?.note || ""}
              onChange={(event) => setDecision({ note: event.target.value })}
              placeholder={recoverable ? "是否接受这个来源？" : "补写方向，或为什么保持为空？"}
            />
          </label>
        </div>
      )}
    </article>
  );
}

function SystemView() {
  const evidence = state.system.candidateEvidence;
  return (
    <div className="system-view">
      <div className="view-heading">
        <div>
          <span className="section-kicker">SYSTEM HEALTH</span>
          <h2>自动化现在是否可靠？</h2>
          <p>关键依赖都正常；所有远端内容写入仍停在明确审批边界之后。</p>
        </div>
        <div className="health-score"><strong>5/5</strong><span>核心检查正常</span></div>
      </div>

      <div className="health-grid">
        <article>
          <span className="health-icon">01</span>
          <i className="health-status">HEALTHY</i>
          <h3>每周调度</h3>
          <p>{state.system.scheduleLabel}</p>
          <small>plan-only · 不会自动发布或改 Transistor</small>
        </article>
        <article>
          <span className="health-icon">02</span>
          <i className="health-status">FRESH</i>
          <h3>YouTube 证据</h3>
          <p>{evidence.public_count} public · {evidence.unverified_count} unverified</p>
          <small>证据新鲜，72 小时后自动 fail-closed</small>
        </article>
        <article>
          <span className="health-icon">03</span>
          <i className="health-status">READ ONLY</i>
          <h3>OAuth</h3>
          <p>{state.system.oauthScope}</p>
          <small>{state.system.youtubeTotal} 条视频 · {state.system.youtubePrivacy.public} public</small>
        </article>
        <article>
          <span className="health-icon">04</span>
          <i className="health-status">NIGHTLY</i>
          <h3>yt-dlp</h3>
          <p>{state.system.ytDlpVersion}</p>
          <small>每周运行前尝试升级；失败则使用仍可执行的本地版本</small>
        </article>
        <article>
          <span className="health-icon">05</span>
          <i className="health-status">EXPECTED</i>
          <h3>Transcript</h3>
          <p>{state.transcriptOutcome.updated} updated · 0 failed</p>
          <small>{state.summary.transcriptArtifacts} / {state.summary.remotePublished} 已发布集有 artifact；{missingTranscriptArtifacts} 集按规则无字幕</small>
        </article>
      </div>

      <section className="hash-panel">
        <div>
          <span>当前只读 plan</span>
          <code>{state.plan.planHash}</code>
          <small>{state.plan.planPath}</small>
        </div>
        <div>
          <span>当前 publish approval hash</span>
          <code>{state.plan.publishApprovalHash}</code>
          <small>当前计划零写入动作，不需要批准</small>
        </div>
        <div>
          <span>已完成 publish ledger</span>
          <code>{compactHash(state.publishOutcome.planHash)}</code>
          <small>{state.publishOutcome.ledgerPath}</small>
        </div>
        <div>
          <span>已完成 transcript ledger</span>
          <code>{compactHash(state.transcriptOutcome.ledgerPath)}</code>
          <small>{state.transcriptOutcome.ledgerPath}</small>
        </div>
      </section>
    </div>
  );
}
