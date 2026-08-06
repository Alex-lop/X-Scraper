import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("../xworkbench/static/analysis.js", import.meta.url), "utf8");
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
