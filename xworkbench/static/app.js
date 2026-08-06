import {
  authorKey,
  engagement,
  engagementDetails,
  filterAndSortPosts,
  formatMetric,
  postTypes,
  summarizePosts,
} from "./analysis.js";

const $ = (selector) => document.querySelector(selector);
const form = $("#collection-form");
const activeStatuses = new Set(["queued", "running", "waiting"]);
const terminalStatuses = new Set(["succeeded", "failed", "cancelled", "interrupted", "partial"]);
const integerFormat = new Intl.NumberFormat();
const moneyFormat = new Intl.NumberFormat(undefined, {
  style: "currency", currency: "USD", minimumFractionDigits: 3, maximumFractionDigits: 6,
});

let preview = null;
let previewTimer = null;
let activeJobId = null;
let activeJob = null;
let pollTimer = null;
let allPosts = [];
let offlineDemo = false;
let providerConnections = {};

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (response.status === 204) return null;
  const data = await response.json().catch(() => ({ error: { message: `Request failed (${response.status})` } }));
  if (!response.ok) throw new Error(data.error?.message || `Request failed (${response.status})`);
  return data;
}

function node(tag, text = "", className = "") {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== "") element.textContent = text;
  return element;
}

function banner(element, message) {
  element.textContent = message || "";
  element.hidden = !message;
}

function count(value) {
  return value !== null && value !== undefined && Number.isFinite(Number(value))
    ? integerFormat.format(Number(value))
    : "—";
}

function money(value) {
  return value !== null && value !== undefined && Number.isFinite(Number(value))
    ? moneyFormat.format(Number(value))
    : "—";
}

function utc(value, fallback = "Not available") {
  if (!value) return fallback;
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf())
    ? String(value)
    : parsed.toLocaleString(undefined, { timeZone: "UTC", timeZoneName: "short" });
}

function titleCase(value) {
  return String(value || "").replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function safeUrl(value) {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : null;
  } catch {
    return null;
  }
}

function postAnchor(postId) {
  return `post-${String(postId).replace(/[^A-Za-z0-9_-]/g, "-")}`;
}

function authorLabel(post) {
  if (post.author_username) return `@${post.author_username}`;
  if (post.author_id) return `Author ${post.author_id}`;
  return "Unknown author";
}

function setStage(stage) {
  for (let index = 1; index <= 3; index += 1) {
    const item = $(`#step-${index}`);
    item.classList.toggle("current", index === stage);
    item.classList.toggle("complete", index < stage);
  }
}

function setOfflineDemo() {
  offlineDemo = true;
  $("#connection-card").classList.add("ready");
  $("#connection-status").textContent = "Synthetic offline demo";
  $("#connection-message").textContent = "Local demo evidence only; no X request or API cost.";
  $("#setup-card").hidden = true;
  $("#demo-card").hidden = false;
  $("#preview-button").disabled = true;
}

function selectedProvider() {
  return new FormData(form).get("provider") || "playwright_browser";
}

function providerReady(provider) {
  const connection = providerConnections[provider]?.connection || {};
  return connection.ready === true || connection.valid === true || ["ready", "configured"].includes(connection.status);
}

function requestPayload() {
  const data = new FormData(form);
  const provider = data.get("provider");
  if (provider === "playwright_browser") {
    return {
      provider,
      sourceType: "home",
      maxPosts: Number($("#browser-max-posts").value),
    };
  }
  return {
    provider,
    searchMode: data.get("searchMode"),
    sourceType: data.get("sourceType"),
    sourceValue: data.get("sourceValue"),
    maxPosts: Number($("#api-max-posts").value),
    startDate: data.get("startDate") || null,
    endDate: data.get("endDate") || null,
    includeReplies: data.has("includeReplies"),
    mediaOnly: data.has("mediaOnly"),
  };
}

function updateRequestHelp() {
  const provider = selectedProvider();
  const browser = provider === "playwright_browser";
  $("#browser-options").hidden = !browser;
  $("#api-options").hidden = browser;
  for (const control of $("#browser-options").querySelectorAll("input, select, textarea, button")) control.disabled = !browser;
  for (const control of $("#api-options").querySelectorAll("input, select, textarea, button")) control.disabled = browser;
  const data = new FormData(form);
  $("#source-value").required = !browser;
  $("#setup-card").hidden = !browser || providerReady(provider);
  $("#api-setup-card").hidden = browser || providerReady(provider);
  $("#preview-button").textContent = browser ? "Review browser capture" : "Preview exact read and cost";
  $("#preview-button").disabled = offlineDemo || !providerReady(provider);
  if (!offlineDemo) {
    const connection = providerConnections[provider]?.connection || {};
    const ready = providerReady(provider);
    $("#connection-card").classList.toggle("ready", ready);
    $("#connection-status").textContent = browser
      ? `Browser session: ${titleCase(connection.status || "unavailable")}`
      : `Official X API: ${titleCase(connection.status || "unavailable")}`;
    $("#connection-message").textContent = connection.message || (browser
      ? "Run xworkbench auth to sign in manually."
      : "Run xworkbench configure to add an optional token.");
  }
  if (browser) {
    $("#scope-badge").textContent = "Browser · Home";
    return;
  }
  const archive = data.get("searchMode") === "fullArchive";
  const search = data.get("sourceType") === "search";
  $("#scope-badge").textContent = archive ? "Full archive · bounded" : "Recent · 7 days";
  $("#start-date").required = archive;
  $("#end-date").required = archive;
  $("#date-feedback").textContent = archive
    ? "Required. Full archive uses the inclusive UTC start and end dates shown in the preview."
    : "Optional. Recent mode uses a rolling seven-day window; a boundary date may be clamped to the exact cutoff.";
  $("#source-label").textContent = search ? "Search query" : "Username or profile URL";
  $("#source-value").placeholder = search ? "AI agents lang:en" : "@OpenAI";
  const help = $("#query-help");
  if (search) {
    const link = node("a", "X search operators");
    link.href = "https://docs.x.com/x-api/posts/search/integrate/build-a-query";
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    help.replaceChildren("Use supported ", link, "; reply and media filters are appended automatically.");
  } else {
    help.replaceChildren("Profiles compile to an exact from: search.");
  }
}

async function loadConnection() {
  try {
    const data = await api("/api/connection");
    if (data.demoMode === "offline" || data.synthetic === true) {
      setOfflineDemo();
      return;
    }
    providerConnections = data.providers || {};
    const browser = providerConnections.playwright_browser?.connection || {};
    const ready = providerReady("playwright_browser");
    $("#connection-card").classList.toggle("ready", ready);
    $("#connection-status").textContent = `Browser session: ${titleCase(browser.status || "unavailable")}`;
    $("#connection-message").textContent = browser.message || "Run xworkbench auth to sign in manually.";
    updateRequestHelp();
  } catch (error) {
    $("#connection-status").textContent = "Local application unavailable";
    $("#connection-message").textContent = error.message;
  }
}

function expirePreview() {
  if (!preview) return;
  $("#confirm-button").disabled = true;
  banner($("#confirm-error"), "This five-minute preview expired. Edit or preview the request again before confirming.");
}

function renderPreview() {
  const browser = preview.provider === "playwright_browser";
  $("#browser-preview").hidden = !browser;
  $("#api-preview").hidden = browser;
  $("#api-billing").hidden = browser;
  $("#preview-eyebrow").textContent = browser ? "Human-approved capture" : "Five-minute preflight";
  $("#preview-title").textContent = browser ? "Review browser capture" : "Review the exact paid read";
  $("#preview-badge").textContent = browser ? "Local · bounded" : "Expires in 5 minutes";
  $("#confirm-button").textContent = browser ? "Start browser capture" : "Confirm paid read";
  if (browser) {
    const intent = preview.captureIntent || preview.executionPlan;
    $("#browser-preview-source").textContent = intent.sourceUrl || "https://x.com/home";
    $("#browser-preview-target").textContent = `${count(intent.targetPosts)} visible Posts maximum`;
    $("#browser-preview-session").textContent = titleCase(providerConnections.playwright_browser?.connection?.status || "unavailable");
    $("#confirm-button").disabled = !providerReady("playwright_browser");
    banner($("#confirm-error"), "");
    clearTimeout(previewTimer);
    return;
  }
  const intent = preview.compiledIntent;
  const estimate = preview.costEstimate;
  const prices = estimate.unitPricesUsd;
  const queryLimit = intent.searchMode === "fullArchive" ? 1024 : 512;
  $("#preview-scope").textContent = intent.searchMode === "fullArchive" ? "Full archive" : "Recent (rolling seven days)";
  $("#preview-endpoint").textContent = intent.endpoint;
  $("#preview-query").textContent = intent.query;
  $("#query-count").textContent = `${count(intent.compiledLength)} / ${count(queryLimit)} characters`;
  $("#query-meter").max = queryLimit;
  $("#query-meter").value = intent.compiledLength;
  $("#preview-window").textContent = `${utc(intent.startTime)} — ${utc(intent.endTime)}`;
  $("#preview-expiry").textContent = utc(intent.expiresAt);
  $("#preview-post-cost").textContent = `Up to ${count(estimate.maximumPostResources)} × ${money(prices.post)} = ${money(estimate.maximumPostListPriceUsd)} list price`;
  $("#preview-user-cost").textContent = preview.request.sourceType === "profile"
    ? "Not requested; the profile handle supplies the author"
    : `${money(prices.user)} per returned User resource; total varies`;
  $("#preview-media-cost").textContent = `${money(prices.media)} per returned Media resource; total varies`;
  $("#billing-note").textContent = `${estimate.note} Pricing as of ${estimate.pricingAsOf}.`;
  $("#pricing-link").href = safeUrl(estimate.pricingUrl) || "https://docs.x.com/x-api/getting-started/pricing";
  $("#confirm-button").disabled = false;
  banner($("#confirm-error"), "");
  clearTimeout(previewTimer);
  previewTimer = setTimeout(expirePreview, Math.max(0, new Date(intent.expiresAt) - Date.now()));
}

form.addEventListener("change", () => {
  preview = null;
  clearTimeout(previewTimer);
  $("#preview-card").hidden = true;
  updateRequestHelp();
  banner($("#form-error"), "");
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("#preview-button");
  button.disabled = true;
  banner($("#form-error"), "");
  try {
    preview = await api("/api/collections/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(requestPayload()),
    });
    renderPreview();
    form.hidden = true;
    $("#preview-card").hidden = false;
    setStage(1);
  } catch (error) {
    banner($("#form-error"), error.message);
  } finally {
    button.disabled = offlineDemo;
  }
});

$("#edit-button").addEventListener("click", () => {
  form.hidden = false;
  $("#preview-card").hidden = true;
});

$("#confirm-button").addEventListener("click", async () => {
  if (!preview) return;
  const button = $("#confirm-button");
  button.disabled = true;
  banner($("#confirm-error"), "");
  try {
    const confirmation = preview.provider === "playwright_browser"
      ? { confirmBrowserCapture: true }
      : { confirmPaidRead: true };
    const data = await api("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...preview.request, executionPlan: preview.executionPlan, ...confirmation }),
    });
    clearTimeout(previewTimer);
    preview = null;
    form.hidden = false;
    $("#preview-card").hidden = true;
    await openJob(data.jobId);
    $("#stage-collect").scrollIntoView({ behavior: "smooth" });
  } catch (error) {
    banner($("#confirm-error"), error.message);
    button.disabled = false;
  }
});

function isSyntheticJob(job) {
  const provider = String(job.provenance?.provider || "").toLocaleLowerCase();
  return job.synthetic === true || job.completionReason === "offline_demo_seeded"
    || provider.includes("offline") || provider.includes("synthetic");
}

function jobLabel(job) {
  if (job.provider === "playwright_browser") return `Home feed: ${job.id.slice(0, 8)}`;
  return `${job.request?.sourceType || "collection"}: ${job.request?.sourceValue || job.id}`;
}

async function loadHistory({ openDemo = false } = {}) {
  const list = $("#history-list");
  try {
    const data = await api("/api/jobs?limit=25");
    const jobs = data.jobs || [];
    list.replaceChildren();
    for (const job of jobs) {
      const button = node("button", "", "history-item");
      button.type = "button";
      button.append(
        node("strong", jobLabel(job)),
        node("small", `${titleCase(job.provider || "snapshot")} · ${utc(job.createdAt)} · ${count(job.collectedCount)}/${count(job.targetCount)}`),
      );
      button.append(node("span", titleCase(job.status), `pill ${job.status || "neutral"}`));
      button.addEventListener("click", () => openJob(job.id));
      list.append(button);
    }
    if (!jobs.length) list.append(node("p", "No stored snapshots yet.", "empty"));
    const demo = jobs.find(isSyntheticJob);
    if (demo) setOfflineDemo();
    if (openDemo && demo && !activeJobId) await openJob(demo.id);
  } catch (error) {
    list.replaceChildren(node("p", error.message, "empty"));
  }
}

function renderJob(job) {
  activeJob = job;
  const status = job.status;
  const resources = job.resourcesReturned || {};
  const terminal = terminalStatuses.has(status);
  $("#collection-empty").hidden = true;
  $("#collection-content").hidden = false;
  $("#job-title").textContent = jobLabel(job);
  $("#job-status").textContent = titleCase(status);
  $("#job-status").className = `pill ${status}`;
  $("#collected-count").textContent = `${count(job.collectedCount)} / ${count(job.targetCount)}`;
  $("#job-provider").textContent = titleCase(job.provider || job.provenance?.provider);
  const browser = job.provider === "playwright_browser";
  $("#browser-stats").hidden = !browser;
  $("#api-stats").hidden = browser || !job.cost;
  if (browser) {
    const details = job.providerDetails || {};
    $("#scan-count").textContent = count(details.scanIterations);
    $("#scroll-count").textContent = count(details.scrollIterations);
    $("#observation-time").textContent = utc(details.observedAt);
  } else if (job.cost) {
    $("#post-resource-count").textContent = count(resources.posts);
    $("#user-resource-count").textContent = count(resources.users);
    $("#media-resource-count").textContent = count(resources.media);
    $("#job-cost").textContent = money(job.cost.returnedListPriceEstimateUsd);
    $("#rate-limit").textContent = count(job.rateLimit?.remaining);
  }
  const progress = job.targetCount ? Math.min(100, job.collectedCount / job.targetCount * 100) : 0;
  $("#job-progress").value = progress;
  let progressCopy = `${count(job.collectedCount)} unique Posts stored locally.`;
  if (!browser && job.resourcesReturned) progressCopy += ` ${count(resources.posts)} Post, ${count(resources.users)} User, and ${count(resources.media)} Media resources returned.`;
  if (status === "waiting" && job.retryAt) progressCopy = `Rate limited. Automatic retry at ${utc(job.retryAt)}.`;
  $("#progress-copy").textContent = progressCopy;
  const provenance = job.provenance || {};
  $("#job-provenance").textContent = browser
    ? `${titleCase(job.provider)} ${job.providerVersion || ""} · ${titleCase(provenance.sourceKind)} · ${provenance.sourceUrl || ""}`
    : `${titleCase(job.provider || provenance.provider)} ${job.providerVersion || provenance.providerVersion || ""} · ${titleCase(provenance.searchMode)} · ${provenance.endpoint || ""} · query “${provenance.query || ""}” (${count(provenance.queryLength)} characters) · ${utc(provenance.startTime)} — ${utc(provenance.endTime)} · ${job.cost?.note || ""}`;
  banner($("#job-error"), status === "waiting" ? "" : job.error?.message);
  banner($("#job-warning"), (job.warnings || []).join(" · "));
  $("#cancel-button").hidden = !activeStatuses.has(status);
  $("#resume-button").hidden = !(
    ["cancelled", "interrupted", "partial"].includes(status)
    || (status === "failed" && job.error?.retryable)
  );
  $("#delete-button").hidden = !terminal;
  if (isSyntheticJob(job)) setOfflineDemo();
  setStage(activeStatuses.has(status) ? 2 : terminal ? 3 : 2);
}

async function openJob(jobId) {
  if (!jobId) return;
  activeJobId = jobId;
  activeJob = null;
  allPosts = [];
  resetFilters();
  clearTimeout(pollTimer);
  $("#stage-inspect").hidden = true;
  await pollJob();
}

async function pollJob() {
  if (!activeJobId) return;
  const jobId = activeJobId;
  clearTimeout(pollTimer);
  try {
    const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
    if (jobId !== activeJobId) return;
    renderJob(job);
    if (activeStatuses.has(job.status)) {
      pollTimer = setTimeout(pollJob, 1500);
      return;
    }
    await loadSnapshot(jobId, job);
    await loadHistory();
  } catch (error) {
    banner($("#job-error"), error.message);
    pollTimer = setTimeout(pollJob, 3000);
  }
}

$("#refresh-history").addEventListener("click", () => loadHistory());

$("#cancel-button").addEventListener("click", async () => {
  if (!activeJobId) return;
  try {
    await api(`/api/jobs/${encodeURIComponent(activeJobId)}/cancel`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    });
    await pollJob();
  } catch (error) {
    banner($("#job-error"), error.message);
  }
});

$("#resume-button").addEventListener("click", async () => {
  if (!activeJobId) return;
  try {
    await api(`/api/jobs/${encodeURIComponent(activeJobId)}/resume`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    });
    await pollJob();
  } catch (error) {
    banner($("#job-error"), error.message);
  }
});

$("#delete-button").addEventListener("click", async () => {
  if (!activeJobId || !window.confirm("Permanently delete this terminal snapshot and all of its stored Posts? This cannot be undone.")) return;
  try {
    await api(`/api/jobs/${encodeURIComponent(activeJobId)}`, {
      method: "DELETE", headers: { "Content-Type": "application/json" }, body: "{}",
    });
    activeJobId = null;
    activeJob = null;
    allPosts = [];
    $("#collection-content").hidden = true;
    $("#collection-empty").hidden = false;
    $("#stage-inspect").hidden = true;
    setStage(1);
    await loadHistory();
  } catch (error) {
    banner($("#job-error"), error.message);
  }
});

async function loadAllPosts(jobId) {
  const posts = [];
  let offset = 0;
  while (offset !== null && posts.length < 500) {
    const page = await api(`/api/jobs/${encodeURIComponent(jobId)}/posts?limit=100&offset=${offset}`);
    posts.push(...page.posts);
    offset = page.pagination.nextOffset;
  }
  return posts;
}

function resetFilters() {
  $("#text-filter").value = "";
  $("#author-filter").value = "";
  $("#language-filter").value = "";
  $("#type-filter").value = "";
  $("#sort-filter").value = "newest";
}

function replaceOptions(select, label, options) {
  const selected = select.value;
  select.replaceChildren(new Option(label, ""));
  for (const option of options) select.add(new Option(option.label, option.value));
  if (options.some((option) => option.value === selected)) select.value = selected;
}

function populateFilters() {
  const authors = new Map();
  for (const post of allPosts) if (authorKey(post)) authors.set(authorKey(post), authorLabel(post));
  replaceOptions(
    $("#author-filter"),
    "All authors",
    [...authors].map(([value, label]) => ({ value, label })).sort((a, b) => a.label.localeCompare(b.label)),
  );
  replaceOptions(
    $("#language-filter"),
    "All languages",
    [...new Set(allPosts.map((post) => post.language || "unknown"))]
      .sort().map((value) => ({ value, label: value === "unknown" ? "Unknown" : value.toUpperCase() })),
  );
}

function rankedItem(label, detail, href = null) {
  const item = node("li");
  const name = href ? node("a", label) : node("span", label);
  if (href) name.href = href;
  item.append(name, node("strong", detail));
  return item;
}

function fillRankedList(selector, items, emptyMessage) {
  const list = $(selector);
  list.replaceChildren(...items);
  if (!items.length) list.append(rankedItem(emptyMessage, ""));
}

function renderSummary() {
  const summary = summarizePosts(allPosts);
  $("#summary-total").textContent = count(summary.total);
  $("#summary-authors").textContent = count(summary.uniqueAuthors);
  $("#summary-languages").textContent = count(summary.uniqueLanguages);
  $("#summary-timestamps").textContent = `${count(summary.timestampsAvailable)} / ${count(summary.total)}`;
  $("#type-summary").textContent = `Original ${count(summary.types.original)} · Replies ${count(summary.types.reply)} · Reposts ${count(summary.types.repost)} · Quotes ${count(summary.types.quote)} · Unclassified ${count(summary.types.unclassified)} · Media ${count(summary.types.media)}`;

  fillRankedList("#daily-volume", summary.dailyVolume.map(({ date, count: value }) => rankedItem(date, count(value))), "No dated Posts");
  fillRankedList("#language-summary", summary.languages.map(({ language, count: value }) => rankedItem(language === "unknown" ? "Unknown" : language.toUpperCase(), count(value))), "No language data");
  fillRankedList("#top-posts", summary.topPosts.map((post) => {
    const text = String(post.text || "Untitled Post").replace(/\s+/g, " ");
    const label = text.length > 72 ? `${text.slice(0, 69)}…` : text;
    return rankedItem(label, count(engagement(post)), `#${postAnchor(post.post_id)}`);
  }), "No Posts with available metrics");
  fillRankedList("#top-authors", summary.topAuthors.map((author) => {
    const source = allPosts.find((post) => authorKey(post) === author.author);
    return rankedItem(source ? authorLabel(source) : author.author, `${count(author.engagement)} · ${count(author.posts)} Posts`);
  }), "No identified authors with metrics");

  const quality = [];
  const missingAuthors = allPosts.filter((post) => !authorKey(post)).length;
  const unknownLanguages = allPosts.filter((post) => !post.language).length;
  if (missingAuthors) quality.push(`${count(missingAuthors)} Posts have no resolved author.`);
  if (unknownLanguages) quality.push(`${count(unknownLanguages)} Posts have no language value.`);
  if (summary.timestampsAvailable < summary.total) quality.push(`${count(summary.total - summary.timestampsAvailable)} Posts have no usable timestamp.`);
  if (summary.types.unclassified) quality.push(`${count(summary.types.unclassified)} Posts have incomplete type classification.`);
  if (summary.postsWithMissingMetrics) quality.push(`${count(summary.postsWithMissingMetrics)} Posts have at least one missing public metric; missing values are not treated as zero.`);
  banner($("#quality-notice"), quality.join(" "));
}

function postCard(post) {
  const article = node("article", "", "post");
  article.id = postAnchor(post.post_id);
  const top = node("div", "", "post-top");
  const when = node("time", utc(post.created_at, "Timestamp missing"));
  when.dateTime = post.created_at || "";
  top.append(node("strong", authorLabel(post)), when);
  article.append(top, node("p", post.text ?? "[Post text unavailable]", "post-text"));

  const chips = node("div", "", "chips");
  for (const type of postTypes(post)) chips.append(node("span", type, "chip"));
  if (post.language) chips.append(node("span", post.language.toUpperCase(), "chip"));
  article.append(chips);

  const media = node("div", "", "media-list");
  for (const item of post.media || []) {
    const source = safeUrl(item.url || item.previewImageUrl);
    const label = item.altText || `${titleCase(item.type || "Post")} media`;
    const entry = node("span", label, "media-item");
    if (source) {
      const link = node("a", "Open media ↗");
      link.href = source;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      entry.append(" · ", link);
    }
    media.append(entry);
  }
  if (media.children.length) article.append(media);

  const details = engagementDetails(post);
  const foot = node("div", "", "post-foot");
  const names = { like_count: "Likes", reply_count: "Replies", repost_count: "Reposts", quote_count: "Quotes", bookmark_count: "Bookmarks" };
  for (const [field, label] of Object.entries(names)) foot.append(node("span", `${label}: ${formatMetric(details.metrics[field])}`));
  const href = safeUrl(post.url);
  if (href) {
    const link = node("a", "Open original ↗");
    link.href = href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    foot.append(link);
  }
  article.append(foot);
  return article;
}

function renderFilteredPosts() {
  const posts = filterAndSortPosts(allPosts, {
    text: $("#text-filter").value,
    author: $("#author-filter").value,
    language: $("#language-filter").value,
    type: $("#type-filter").value,
    sort: $("#sort-filter").value,
  });
  $("#filtered-count").textContent = `Showing ${count(posts.length)} of ${count(allPosts.length)} locally stored Posts.`;
  const list = $("#posts-list");
  list.replaceChildren(...posts.map(postCard));
  if (!posts.length) list.append(node("p", "No stored Posts match these filters.", "empty"));
}

for (const selector of ["#text-filter", "#author-filter", "#language-filter", "#type-filter", "#sort-filter"]) {
  $(selector).addEventListener("input", renderFilteredPosts);
}

async function loadSnapshot(jobId, job) {
  const posts = await loadAllPosts(jobId);
  if (jobId !== activeJobId) return;
  allPosts = posts;
  $("#stage-inspect").hidden = false;
  $("#snapshot-status").textContent = job.isPartial ? "Partial snapshot" : titleCase(job.status);
  $("#snapshot-status").className = `pill ${job.status}`;
  $("#partial-notice").hidden = !job.isPartial;
  const provenance = job.provenance || {};
  $("#snapshot-meta").textContent = job.provider === "playwright_browser"
    ? `Browser Home · ${provenance.sourceUrl || "local snapshot"} · captured ${utc(job.capturedAt)} · ${count(posts.length)} stored Posts`
    : `${titleCase(provenance.searchMode)} · ${provenance.endpoint || ""} · effective query “${provenance.query || ""}” · captured ${utc(job.capturedAt)} · ${count(posts.length)} stored Posts`;
  $("#json-export").href = `/api/jobs/${encodeURIComponent(jobId)}/export?format=json`;
  $("#csv-export").href = `/api/jobs/${encodeURIComponent(jobId)}/export?format=csv`;
  $("#result-count").textContent = `${count(posts.length)} Posts`;
  populateFilters();
  renderSummary();
  renderFilteredPosts();
  setStage(3);
}

updateRequestHelp();
await loadConnection();
await loadHistory({ openDemo: true });
