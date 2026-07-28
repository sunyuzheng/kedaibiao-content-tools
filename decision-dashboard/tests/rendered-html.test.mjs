import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const root = new URL("../", import.meta.url);

async function readJson(path) {
  return JSON.parse(await readFile(new URL(path, root), "utf8"));
}

test("dashboard snapshot is internally consistent", async () => {
  const state = await readJson("app/podcast-state.local.json");

  assert.equal(state.showId, "71709");
  assert.equal(
    state.summary.transcriptArtifacts + state.transcriptExceptions.length,
    state.summary.remotePublished,
    "transcript artifacts plus explicit exceptions should cover every published episode",
  );
  assert.equal(
    state.transcriptExceptions.length,
    state.summary.remotePublished - state.summary.transcriptArtifacts,
  );
  assert.ok(
    state.transcriptExceptions.every(
      (item) => item.episodeId && item.videoId && item.title,
    ),
  );
  assert.equal(state.publishItems.length, state.summary.publishReady);
  assert.equal(state.blockedItems.length, state.summary.publishBlocked);
  assert.equal(state.metadataGaps.length, state.summary.metadataGaps);
  assert.equal(
    state.metadataGaps.filter((item) => item.source.chars > 0).length,
    state.summary.metadataRecoverable,
  );
  assert.equal(
    state.publishItems.filter((item) => item.warnings.length > 0).length,
    Object.values(state.summary.publishWarningCounts).reduce(
      (total, count) => total + count,
      0,
    ),
  );
  assert.equal(state.publishOutcome.completed, true);
  assert.equal(
    state.publishOutcome.published,
    state.publishOutcome.createdDrafts + state.publishOutcome.repairedDrafts,
  );
  assert.ok(state.publishOutcome.reordered >= 0);
});

test("dashboard keeps review local and exposes an export", async () => {
  const [page, layout, packageJson, viteConfig, livePlugin] = await Promise.all([
    readFile(new URL("app/page.tsx", root), "utf8"),
    readFile(new URL("app/layout.tsx", root), "utf8"),
    readFile(new URL("package.json", root), "utf8"),
    readFile(new URL("vite.config.ts", root), "utf8"),
    readFile(new URL("build/podcast-live-data-plugin.ts", root), "utf8"),
  ]);

  assert.match(page, /window\.localStorage/);
  assert.match(page, /downloadDecisions/);
  assert.match(page, /重新读取实时状态/);
  assert.match(page, /这不是发布批准/);
  assert.match(layout, /podcast-decision-dashboard-og\.png/);
  assert.match(packageJson, /"refresh-data"/);
  assert.match(viteConfig, /podcastLiveData/);
  assert.match(livePlugin, /generate-dashboard-data\.py/);
  assert.match(livePlugin, /request\.method !== "POST"/);
  assert.doesNotMatch(packageJson, /site-creator-vinext-starter/);
});
