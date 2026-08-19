const METRIC_FIELDS = ["like_count", "reply_count", "repost_count", "quote_count", "bookmark_count"];
const COMPARABLE_METRICS = [...METRIC_FIELDS, "view_count"];

function metric(value) {
  return Number.isInteger(value) && value >= 0 ? value : null;
}

export function engagementDetails(post) {
  const metrics = Object.fromEntries(METRIC_FIELDS.map((field) => [field, metric(post[field])]));
  const available = Object.values(metrics).filter((value) => value !== null);
  return {
    metrics,
    score: available.length ? available.reduce((total, value) => total + value, 0) : null,
    available: available.length,
    missing: METRIC_FIELDS.filter((field) => metrics[field] === null),
  };
}

export function engagement(post) {
  return engagementDetails(post).score;
}

export function formatMetric(value) {
  const normalized = metric(value);
  return normalized === null ? "Missing" : String(normalized);
}

export function barPercent(value, maximum) {
  const amount = Number(value);
  const ceiling = Number(maximum);
  if (!Number.isFinite(amount) || !Number.isFinite(ceiling) || ceiling <= 0) return 0;
  return Math.max(0, Math.min(100, amount / ceiling * 100));
}

export function orderedBatchItems(manifest) {
  const items = Array.isArray(manifest?.items) ? manifest.items : [];
  return [...items].sort((left, right) => {
    const leftOrder = Number.isInteger(left?.expectedQueueOrder)
      ? left.expectedQueueOrder : Number.MAX_SAFE_INTEGER;
    const rightOrder = Number.isInteger(right?.expectedQueueOrder)
      ? right.expectedQueueOrder : Number.MAX_SAFE_INTEGER;
    return leftOrder - rightOrder || String(left?.sourceId || "").localeCompare(String(right?.sourceId || ""));
  });
}

export function authorKey(post) {
  return String(post.author_username || post.author_id || "");
}

export function postTypes(post) {
  const values = [post.is_reply, post.is_repost, post.is_quote];
  const known = values.every((value) => typeof value === "boolean" || value === 0 || value === 1);
  const types = [];
  if (known && values.every((value) => !value)) types.push("original");
  if (post.is_reply === true || post.is_reply === 1) types.push("reply");
  if (post.is_repost === true || post.is_repost === 1) types.push("repost");
  if (post.is_quote === true || post.is_quote === 1) types.push("quote");
  if (!known || !types.length) types.push("unclassified");
  if (post.has_media === true || post.has_media === 1) types.push("media");
  return types;
}

function timestamp(post) {
  const value = Date.parse(post.created_at || "");
  return Number.isNaN(value) ? null : value;
}

export function filterAndSortPosts(posts, filters = {}) {
  const query = String(filters.text || "").trim().toLocaleLowerCase();
  const filtered = posts.filter((post) =>
    (!query || String(post.text || "").toLocaleLowerCase().includes(query))
    && (!filters.author || authorKey(post) === filters.author)
    && (!filters.language || (post.language || "unknown") === filters.language)
    && (!filters.type || postTypes(post).includes(filters.type))
  );

  return filtered.sort((left, right) => {
    if (filters.sort === "engagement") {
      const a = engagement(left);
      const b = engagement(right);
      if (a === null || b === null) {
        if (a === null && b !== null) return 1;
        if (b === null && a !== null) return -1;
      }
      return (b ?? 0) - (a ?? 0) || String(left.post_id).localeCompare(String(right.post_id));
    }
    const a = timestamp(left);
    const b = timestamp(right);
    if (a === null || b === null) {
      if (a === null && b !== null) return 1;
      if (b === null && a !== null) return -1;
      return String(left.post_id).localeCompare(String(right.post_id));
    }
    return (filters.sort === "oldest" ? a - b : b - a)
      || String(left.post_id).localeCompare(String(right.post_id));
  });
}

export function summarizePosts(posts) {
  const authors = new Map();
  const languages = new Map();
  const daily = new Map();
  const types = { original: 0, reply: 0, repost: 0, quote: 0, unclassified: 0, media: 0 };
  let timestampsAvailable = 0;
  let postsWithMissingMetrics = 0;

  for (const post of posts) {
    const score = engagement(post);
    if (engagementDetails(post).missing.length) postsWithMissingMetrics += 1;
    const author = authorKey(post);
    if (author) {
      const current = authors.get(author) || { author, posts: 0, engagement: null };
      current.posts += 1;
      if (score !== null) current.engagement = (current.engagement ?? 0) + score;
      authors.set(author, current);
    }

    const language = post.language || "unknown";
    languages.set(language, (languages.get(language) || 0) + 1);
    for (const type of postTypes(post)) types[type] += 1;

    const time = timestamp(post);
    if (time !== null) {
      timestampsAvailable += 1;
      const day = new Date(time).toISOString().slice(0, 10);
      daily.set(day, (daily.get(day) || 0) + 1);
    }
  }

  return {
    total: posts.length,
    uniqueAuthors: authors.size,
    uniqueLanguages: [...languages.keys()].filter((language) => language !== "unknown").length,
    timestampsAvailable,
    postsWithMissingMetrics,
    languages: [...languages].map(([language, count]) => ({ language, count }))
      .sort((a, b) => b.count - a.count || a.language.localeCompare(b.language)),
    types,
    dailyVolume: [...daily].map(([date, count]) => ({ date, count }))
      .sort((a, b) => a.date.localeCompare(b.date)),
    topPosts: filterAndSortPosts(posts.filter((post) => engagement(post) !== null), { sort: "engagement" }).slice(0, 5),
    topAuthors: [...authors.values()].filter((author) => author.engagement !== null)
      .sort((a, b) => b.engagement - a.engagement || b.posts - a.posts || a.author.localeCompare(b.author))
      .slice(0, 5),
  };
}

function evidenceOrder(left, right) {
  const position = (post) => Number.isInteger(post.snapshot_position)
    ? post.snapshot_position
    : Number.isInteger(post.source_position) ? post.source_position : Number.MAX_SAFE_INTEGER;
  return position(left) - position(right)
    || String(left.post_id).localeCompare(String(right.post_id));
}

export function compareSnapshotPosts(beforePosts, afterPosts, options = {}) {
  const before = new Map(beforePosts.map((post) => [String(post.post_id), post]));
  const after = new Map(afterPosts.map((post) => [String(post.post_id), post]));
  const newlyObserved = [...after].filter(([id]) => !before.has(id)).map(([, post]) => post)
    .sort(evidenceOrder);
  const reobserved = [...after].filter(([id]) => before.has(id)).map(([, post]) => post)
    .sort(evidenceOrder);
  const notObservedInLatest = [...before].filter(([id]) => !after.has(id)).map(([, post]) => post)
    .sort(evidenceOrder);
  const engagementDeltas = [];
  for (const post of reobserved) {
    const earlier = before.get(String(post.post_id));
    const fields = {};
    for (const field of COMPARABLE_METRICS) {
      const first = metric(earlier[field]);
      const latest = metric(post[field]);
      if (first !== null && latest !== null) {
        fields[field] = { before: first, after: latest, delta: latest - first };
      }
    }
    if (Object.keys(fields).length) engagementDeltas.push({ post, fields });
  }
  return {
    beforeSnapshotId: String(options.beforeSnapshotId || ""),
    afterSnapshotId: String(options.afterSnapshotId || ""),
    sample: { beforeCount: before.size, afterCount: after.size },
    partial: Boolean(options.partial),
    truncated: Boolean(options.truncated),
    newlyObserved,
    reobserved,
    notObservedInLatest,
    engagementDeltas,
  };
}
