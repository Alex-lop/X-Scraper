import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const [source, appSource, htmlSource, cssSource] = await Promise.all([
  "analysis.js", "app.js", "index.html", "styles.css",
].map((name) => readFile(new URL(`../xworkbench/static/${name}`, import.meta.url), "utf8")));
const analysis = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);

const posts = [
  {
    post_id: "1", text: "First launch", author_username: "alex", created_at: "2026-08-03T10:00:00Z",
    language: "en", like_count: 4, reply_count: 1, repost_count: 2, quote_count: 0,
    bookmark_count: null, is_reply: false, is_repost: false, is_quote: false, has_media: true,
  },
  {
    post_id: "2", text: "A useful reply", author_username: "sam", created_at: "2026-08-04T10:00:00Z",
    language: "en", like_count: 1, reply_count: 0, repost_count: 0, quote_count: 0,
    bookmark_count: null, is_reply: true, is_repost: false, is_quote: false,
  },
  {
    post_id: "3", text: "Repost", author_username: "alex", created_at: "2026-08-04T12:00:00-04:00",
    language: null, like_count: 10, reply_count: 2, repost_count: 1, quote_count: 1,
    bookmark_count: null, is_reply: false, is_repost: true, is_quote: false,
  },
];

test("filters and sorts the local snapshot", () => {
  assert.deepEqual(analysis.filterAndSortPosts(posts, { text: "USEFUL" }).map((post) => post.post_id), ["2"]);
  assert.deepEqual(analysis.filterAndSortPosts(posts, { author: "alex", type: "media" }).map((post) => post.post_id), ["1"]);
  assert.deepEqual(analysis.filterAndSortPosts(posts, { language: "unknown" }).map((post) => post.post_id), ["3"]);
  assert.deepEqual(analysis.filterAndSortPosts(posts, { sort: "oldest" }).map((post) => post.post_id), ["1", "2", "3"]);
  assert.deepEqual(analysis.filterAndSortPosts(posts, { sort: "engagement" }).map((post) => post.post_id), ["3", "1", "2"]);
});

test("preserves missing metrics instead of manufacturing zeroes", () => {
  assert.equal(analysis.engagement(posts[0]), 7);
  assert.deepEqual(analysis.engagementDetails(posts[0]).missing, ["bookmark_count"]);
  assert.equal(analysis.formatMetric(null), "Missing");
  assert.equal(analysis.formatMetric("0"), "Missing");
  assert.equal(analysis.formatMetric(0), "0");
  assert.equal(analysis.engagement({ like_count: null }), null);
});

test("classifies unknown types honestly and summarizes the local snapshot", () => {
  assert.deepEqual(analysis.postTypes({}), ["unclassified"]);
  assert.deepEqual(analysis.postTypes(posts[0]), ["original", "media"]);
  const summary = analysis.summarizePosts([...posts, { post_id: "4" }]);
  assert.equal(summary.total, 4);
  assert.equal(summary.types.unclassified, 1);
  assert.equal(summary.postsWithMissingMetrics, 4);
  assert.deepEqual(summary.dailyVolume.map(({ count }) => count), [1, 2]);
});

test("compares bounded snapshots without treating missing metrics as zero", () => {
  const before = [
    { post_id: "same", snapshot_position: 0, like_count: 2, view_count: null },
    { post_id: "old", snapshot_position: 1, like_count: 9 },
  ];
  const after = [
    { post_id: "same", snapshot_position: 1, like_count: 5, view_count: 20 },
    { post_id: "new", snapshot_position: 0, like_count: 1 },
  ];
  const comparison = analysis.compareSnapshotPosts(before, after, {
    beforeSnapshotId: "snapshot-a", afterSnapshotId: "snapshot-b", partial: true,
  });

  assert.deepEqual(comparison.newlyObserved.map((post) => post.post_id), ["new"]);
  assert.deepEqual(comparison.reobserved.map((post) => post.post_id), ["same"]);
  assert.deepEqual(comparison.notObservedInLatest.map((post) => post.post_id), ["old"]);
  assert.deepEqual(comparison.engagementDeltas[0].fields.like_count, { before: 2, after: 5, delta: 3 });
  assert.equal(comparison.engagementDeltas[0].fields.view_count, undefined);
  assert.deepEqual(comparison.sample, { beforeCount: 2, afterCount: 2 });
  assert.equal(comparison.partial, true);
});

test("orders server-approved batch items deterministically", () => {
  const manifest = {
    items: [
      { sourceId: "later", expectedQueueOrder: 2 },
      { sourceId: "first-b", expectedQueueOrder: 1 },
      { sourceId: "first-a", expectedQueueOrder: 1 },
    ],
  };
  assert.deepEqual(
    analysis.orderedBatchItems(manifest).map((item) => item.sourceId),
    ["first-a", "first-b", "later"],
  );
  assert.deepEqual(analysis.orderedBatchItems({}), []);
});

test("keeps the product loop CSP-safe, keyboard-native, and motion-safe", () => {
  for (const unsafe of ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"]) {
    assert.equal(appSource.includes(unsafe), false);
  }
  for (const label of ["Sources", "Capture", "Changes", "Evidence", "Connect an agent"]) {
    assert.match(htmlSource, new RegExp(`>${label}<`));
  }
  assert.match(htmlSource, /href="#sources" aria-current="page"/);
  assert.match(htmlSource, /id="compare-error"[^>]*role="alert"/);
  assert.match(htmlSource, /id="compare-status"[^>]*aria-live="polite"/);
  assert.match(htmlSource, /id="batch-source-options"/);
  assert.match(htmlSource, /id="batch-preview-rows"/);
  assert.match(htmlSource, /id="batch-confirm-error"[^>]*role="alert"/);
  assert.match(appSource, /Username or exact X profile URL/);
  assert.match(appSource, /provenance\.sourceKind \|\| job\.request\?\.sourceType/);
  assert.doesNotMatch(appSource, /`Browser Home ·/);
  assert.match(appSource, /MCP test passed during demo startup/);
  assert.match(appSource, /\/api\/batches\/preview/);
  assert.match(appSource, /\/api\/progress\?after=/);
  assert.equal(/<(?:script|link)[^>]+https?:\/\//i.test(htmlSource), false);
  assert.equal(htmlSource.includes("<img"), false);
  assert.equal(appSource.includes('behavior: "smooth"'), false);
  assert.equal(cssSource.includes("scroll-behavior: smooth"), false);
  assert.match(cssSource, /prefers-reduced-motion: reduce/);
});
