import {
  authorKey,
  engagement,
  engagementDetails,
  filterAndSortPosts,
  formatMetric,
  compareSnapshotPosts,
  orderedBatchItems,
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
let snapshotCatalog = [];
let savedSourceCatalog = [];
let batchPreview = null;
let activeBatch = null;
let progressSequence = 0;
let progressTimer = null;
let batchCompletedCount = 0;
const batchJobStatuses = new Map();

function savedNames() {
  try {
    const value = JSON.parse(localStorage.getItem("xworkbench-display-names") || "{}");
    return value && typeof value === "object" && !Array.isArray(value) ? value : {};
  } catch {
    return {};
  }
}

const localNames = savedNames();

function persistNames() {
  try {
    localStorage.setItem("xworkbench-display-names", JSON.stringify(localNames));
  } catch {
    // Display labels are optional; capture identity remains the server-issued snapshot ID.
  }
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (response.status === 204) return null;
  const data = await response.json().catch(() => ({ error: { message: `Request failed (${response.status})` } }));
  if (!response.ok) {
    const error = new Error(data.error?.message || `Request failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
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
  const section = stage < 3 ? "capture" : "evidence";
  for (const link of document.querySelectorAll(".product-nav a")) {
    if (link.getAttribute("href") === `#${section}`) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
}

function setOfflineDemo() {
  offlineDemo = true;
  $("#connection-card").classList.add("ready");
  $("#connection-status").textContent = "Synthetic offline demo";
  $("#connection-message").textContent = "Local demo evidence only; no X request or API cost.";
  $("#setup-card").hidden = true;
  $("#demo-card").hidden = false;
  $("#agent-test-result").textContent =
    "MCP test passed during demo startup: direct local comparison succeeded with 12 read-only tools.";
  $("#preview-button").disabled = true;
  $("#batch-preview-button").disabled = true;
}

function selectedProvider() {
  return new FormData(form).get("provider") || "playwright_browser";
}

function providerReady(provider) {
  const connection = providerConnections[provider]?.connection || {};
  return provider === "playwright_browser"
    ? connection.ready === true && connection.status === "verified_live"
    : connection.valid === true && connection.status === "configured";
}

function requestPayload() {
  const data = new FormData(form);
  const provider = data.get("provider");
  if (provider === "playwright_browser") {
    const sourceType = data.get("browserSourceType") || "home";
    return {
      provider,
      sourceType,
      sourceValue: sourceType === "home" ? "home" : $("#browser-source-value").value,
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
    const surface = data.get("browserSourceType") || "home";
    const sourceInput = $("#browser-source-value");
    const sourceLabel = $("#browser-source-label");
    const sourceHelp = $("#browser-source-help");
    const capabilities = providerConnections.playwright_browser?.capabilities || {};
    const supported = new Set(capabilities.sources || ["home"]);
    for (const input of form.elements.browserSourceType || []) {
      input.disabled = !supported.has(input.value);
    }
    if (!supported.has(surface)) {
      form.elements.browserSourceType.value = "home";
      return updateRequestHelp();
    }
    sourceInput.readOnly = surface === "home";
    sourceInput.required = surface !== "home";
    sourceInput.value = surface === "home" ? "home" : sourceInput.value === "home" ? "" : sourceInput.value;
    sourceLabel.textContent = surface === "home" ? "Home feed" : surface === "profile" ? "Username or exact X profile URL" : "Search query";
    sourceInput.placeholder = surface === "profile" ? "@OpenAI" : surface === "search" ? "AI agents lang:en" : "home";
    sourceHelp.textContent = surface === "home"
      ? "The destination is fixed to your signed-in Home feed."
      : supported.has(surface)
        ? `The backend will preview the exact bounded ${surface} destination before capture.`
        : `${titleCase(surface)} capture is unavailable in this backend version.`;
    $("#scope-badge").textContent = `Browser · ${titleCase(surface)}`;
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
  const sourceName = $("#source-name").value.trim();
  const snapshotName = $("#snapshot-name").value.trim();
  $("#preview-names").textContent = [
    sourceName ? `Source “${sourceName}”` : "Unnamed source",
    snapshotName ? `snapshot “${snapshotName}”` : "unnamed snapshot",
  ].join(" · ");
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
    localNames[data.jobId] = {
      source: $("#source-name").value.trim().slice(0, 100),
      snapshot: $("#snapshot-name").value.trim().slice(0, 100),
    };
    persistNames();
    clearTimeout(previewTimer);
    preview = null;
    form.hidden = false;
    $("#preview-card").hidden = true;
    await openJob(data.jobId);
    $("#stage-collect").scrollIntoView();
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
  const id = snapshotId(job);
  const source = job.source || job.request || {};
  const named = job.displayName || job.name || localNames[id]?.snapshot || source.displayName;
  if (named) return named;
  if (job.provider === "playwright_browser") {
    const surface = source.sourceType || source.surface || job.provenance?.sourceKind || "home";
    const value = source.sourceValue || source.value || source.query;
    return `${titleCase(surface)}${value && value !== "home" ? `: ${value}` : " feed"}: ${id.slice(0, 8)}`;
  }
  return `${source.sourceType || source.surface || "collection"}: ${source.sourceValue || source.value || source.query || id}`;
}

function sourceKey(value) {
  return [value.provider, value.surface || value.sourceType, value.value || value.sourceValue]
    .map((part) => String(part || "")).join("\u0000");
}

function derivedSources(jobs) {
  const sources = new Map();
  for (const job of jobs) {
    const request = job.request || {};
    const source = {
      id: sourceKey({ provider: job.provider, sourceType: request.sourceType, sourceValue: request.sourceValue }),
      displayName: localNames[job.id]?.source || `${titleCase(request.sourceType || "source")} · ${request.sourceValue || "home"}`,
      provider: job.provider,
      surface: request.sourceType,
      value: request.sourceValue,
      lastStatus: job.status,
    };
    if (!sources.has(sourceKey(source))) sources.set(sourceKey(source), source);
  }
  return [...sources.values()];
}

function chooseSource(source) {
  const provider = source.provider || "playwright_browser";
  const providerControl = [...form.querySelectorAll('[name="provider"]')]
    .find((control) => control.value === provider);
  if (!providerControl) return;
  providerControl.checked = true;
  const surface = source.surface || source.sourceType || "home";
  if (provider === "playwright_browser") {
    const surfaceControl = [...form.querySelectorAll('[name="browserSourceType"]')]
      .find((control) => control.value === surface);
    if (surfaceControl && !surfaceControl.disabled) surfaceControl.checked = true;
    $("#browser-source-value").value = source.value || source.sourceValue || source.query || "home";
  } else {
    const surfaceControl = [...form.querySelectorAll('[name="sourceType"]')]
      .find((control) => control.value === surface);
    if (surfaceControl) surfaceControl.checked = true;
    $("#source-value").value = source.value || source.sourceValue || source.query || "";
  }
  $("#source-name").value = source.displayName || source.name || "";
  updateRequestHelp();
  $("#capture").scrollIntoView();
  $("#source-name").focus();
}

function renderSources(sources, note) {
  const list = $("#source-list");
  list.replaceChildren();
  for (const source of sources) {
    const item = node("article", "", "source-card");
    item.append(
      node("strong", source.displayName || source.name || "Unnamed source"),
      node("span", `${titleCase(source.surface || source.sourceType)} · ${source.value || source.sourceValue || source.query || "home"}`),
      node("small", `${titleCase(source.provider)} · last status ${titleCase(source.lastStatus || "unknown")}`),
    );
    const button = node("button", "Capture again");
    button.type = "button";
    button.addEventListener("click", () => chooseSource(source));
    item.append(button);
    list.append(item);
  }
  if (!sources.length) list.append(node("p", "No saved sources yet. Name one in Capture.", "empty"));
  $("#sources-note").textContent = note;
}

function renderBatchSourceOptions(sources) {
  const container = $("#batch-source-options");
  const selected = new Set([...container.querySelectorAll('input[type="checkbox"]:checked')]
    .map((control) => control.value));
  container.replaceChildren();
  for (const source of sources) {
    const sourceId = String(source.sourceId || "");
    if (!sourceId) continue;
    const label = node("label", "", "batch-source-option");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.name = "batchSourceId";
    checkbox.value = sourceId;
    checkbox.checked = selected.has(sourceId);
    label.append(
      checkbox,
      node("span", source.displayName || "Unnamed source"),
      node("small", `${titleCase(source.surface)} · ${source.query || "home"}`),
    );
    container.append(label);
  }
  if (!container.children.length) {
    container.append(node("p", "Save at least two sources before building a batch.", "empty"));
  }
}

async function loadSources(fallbackJobs = snapshotCatalog) {
  try {
    const data = await api("/api/sources");
    savedSourceCatalog = Array.isArray(data.sources) ? data.sources : [];
    renderSources(savedSourceCatalog, "Loaded from the local saved-source catalog.");
    renderBatchSourceOptions(savedSourceCatalog);
  } catch {
    savedSourceCatalog = [];
    renderSources(derivedSources(fallbackJobs), "Saved-source API unavailable; showing sources derived from local snapshot history.");
    renderBatchSourceOptions([]);
  }
}

function batchPayload() {
  const selected = [...$("#batch-source-options").querySelectorAll('input[type="checkbox"]:checked')]
    .map((control) => control.value);
  const priority = Number($("#batch-priority").value);
  return {
    items: selected.map((sourceId) => {
      const source = savedSourceCatalog.find((item) => item.sourceId === sourceId);
      return {
        sourceId,
        maxPosts: Number(source?.provider === "official_x_api"
          ? $("#batch-api-posts").value : $("#batch-browser-posts").value),
        priority,
      };
    }),
    deadlineSeconds: Number($("#batch-deadline").value),
    freshnessChoice: $("#batch-freshness").value,
  };
}

function renderBatchPreview() {
  const manifest = batchPreview.manifest;
  const rows = orderedBatchItems(manifest).map((item) => {
    const row = node("tr");
    for (const value of [
      item.expectedQueueOrder,
      item.displayName,
      item.visibleDestination || item.normalizedDestination,
      item.maxPosts,
      `${item.deadlineSeconds}s · ${utc(item.deadlineAt)}`,
      item.freshnessChoice === "capture_fresh" ? "Fresh capture" : item.freshnessChoice,
      item.routeAlias,
      item.priority,
    ]) row.append(node("td", String(value ?? "—")));
    return row;
  });
  $("#batch-preview-rows").replaceChildren(...rows);
  $("#batch-preview-count").textContent = `${count(rows.length)} saved sources`;
  $("#batch-preview-note").textContent = `Maximum concurrency ${count(manifest.maxConcurrency)} globally, `
    + `${count(manifest.perSourceConcurrency)} per source, and ${count(manifest.perAuthStateConcurrency)} per auth state · `
    + `queue capacity ${count(manifest.queueCapacity)} · approval expires ${utc(manifest.expiresAt)}. `
    + manifest.queueOrderBasis;
  banner($("#batch-confirm-error"), "");
  $("#batch-confirm-button").disabled = false;
  $("#batch-form").hidden = true;
  $("#batch-preview").hidden = false;
}

function renderActiveBatch(value) {
  activeBatch = value;
  progressSequence = 0;
  batchCompletedCount = 0;
  batchJobStatuses.clear();
  clearTimeout(progressTimer);
  $("#active-batch").hidden = false;
  $("#active-batch-title").textContent = `Batch ${value.batchId.slice(0, 12)}`;
  $("#batch-status").textContent = `${count(value.admittedCount)} jobs admitted atomically. Cancelling one does not cancel the others.`;
  const jobs = value.jobIds.map((jobId, index) => {
    const item = node("div", "", "batch-job");
    const status = node("span", "Queued", "pill queued");
    batchJobStatuses.set(jobId, status);
    item.append(node("strong", `Queue item ${index + 1} · ${jobId.slice(0, 12)}`), status);
    const actions = node("div", "", "actions");
    const open = node("button", "Open capture");
    open.type = "button";
    open.addEventListener("click", async () => {
      await openJob(jobId);
      $("#stage-collect").scrollIntoView({ block: "start" });
    });
    const cancel = node("button", "Cancel this capture", "danger");
    cancel.type = "button";
    cancel.addEventListener("click", async () => {
      cancel.disabled = true;
      try {
        await api(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, {
          method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
        });
        cancel.textContent = "Cancellation requested";
        await loadHistory();
      } catch (error) {
        banner($("#batch-cancel-error"), error.message);
        cancel.disabled = false;
      }
    });
    actions.append(open, cancel);
    item.append(actions);
    return item;
  });
  $("#batch-job-list").replaceChildren(...jobs);
  banner($("#batch-cancel-error"), "");
  void pollProgress();
}

async function pollProgress() {
  if (!activeBatch) return;
  clearTimeout(progressTimer);
  try {
    const value = await api(`/api/progress?after=${progressSequence}&limit=100`);
    progressSequence = Number.isInteger(value.lastSequence)
      ? value.lastSequence : progressSequence;
    const durable = new Map((value.jobs || []).map((job) => [job.id, job.status]));
    for (const jobId of activeBatch.jobIds) {
      const event = [...(value.events || [])].reverse()
        .find((item) => item.jobId === jobId);
      const statusValue = durable.get(jobId) || event?.status || event?.type;
      const status = batchJobStatuses.get(jobId);
      if (status && statusValue) {
        status.textContent = titleCase(statusValue);
        status.className = `pill ${statusValue}`;
      }
    }
    const complete = activeBatch.jobIds.filter((jobId) =>
      terminalStatuses.has(durable.get(jobId))).length;
    $("#batch-status").textContent = `${count(complete)} of ${count(activeBatch.jobIds.length)} captures are terminal.${value.gap ? " Intermediate progress was coalesced; durable job state is shown." : ""}`;
    if (complete !== batchCompletedCount) {
      batchCompletedCount = complete;
      await loadHistory();
    }
    if (complete < activeBatch.jobIds.length) progressTimer = setTimeout(pollProgress, 1500);
  } catch (error) {
    $("#batch-status").textContent = `Progress events unavailable; durable capture history remains authoritative. ${error.message}`;
    progressTimer = setTimeout(pollProgress, 3000);
  }
}

$("#batch-form").addEventListener("change", () => {
  batchPreview = null;
  $("#batch-preview").hidden = true;
  banner($("#batch-error"), "");
});

$("#batch-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("#batch-preview-button");
  button.disabled = true;
  banner($("#batch-error"), "");
  try {
    batchPreview = await api("/api/batches/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(batchPayload()),
    });
    renderBatchPreview();
  } catch (error) {
    banner($("#batch-error"), error.message);
  } finally {
    button.disabled = offlineDemo;
  }
});

$("#batch-edit-button").addEventListener("click", () => {
  batchPreview = null;
  $("#batch-preview").hidden = true;
  $("#batch-form").hidden = false;
  $("#batch-source-options input")?.focus();
});

$("#batch-confirm-button").addEventListener("click", async () => {
  if (!batchPreview) return;
  const button = $("#batch-confirm-button");
  button.disabled = true;
  banner($("#batch-confirm-error"), "");
  try {
    const value = await api("/api/batches/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        confirm: true,
        manifest: batchPreview.manifest,
        approvalDigest: batchPreview.approvalDigest,
      }),
    });
    batchPreview = null;
    $("#batch-preview").hidden = true;
    $("#batch-form").hidden = false;
    renderActiveBatch(value);
    await loadHistory();
  } catch (error) {
    banner($("#batch-confirm-error"), error.message);
    button.disabled = false;
  }
});

$("#cancel-batch-button").addEventListener("click", async () => {
  if (!activeBatch || !window.confirm("Cancel all queued or running captures remaining in this batch? Completed snapshots remain unchanged.")) return;
  const button = $("#cancel-batch-button");
  button.disabled = true;
  banner($("#batch-cancel-error"), "");
  try {
    const value = await api(`/api/batches/${encodeURIComponent(activeBatch.batchId)}/cancel`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: true }),
    });
    $("#batch-status").textContent = `${count(value.cancelledCount)} remaining captures cancelled. Completed snapshots were unchanged.`;
    await loadHistory();
  } catch (error) {
    banner($("#batch-cancel-error"), error.message);
    button.disabled = false;
  }
});

async function loadSnapshotCatalog(fallbackJobs) {
  try {
    const data = await api("/api/snapshots?limit=50");
    if (Array.isArray(data.snapshots)) {
      return data.snapshots.filter((snapshot) => snapshot.usable !== false
        && (snapshot.sample?.observedPosts ?? snapshot.collectedCount ?? 0) > 0);
    }
  } catch {
    // Older backends expose snapshots as terminal jobs.
  }
  return fallbackJobs.filter((job) => terminalStatuses.has(job.status) && job.collectedCount > 0);
}

async function loadHistory({ openDemo = false } = {}) {
  const list = $("#history-list");
  try {
    const data = await api("/api/jobs?limit=25");
    const jobs = data.jobs || [];
    snapshotCatalog = await loadSnapshotCatalog(jobs);
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
    refreshComparisonOptions();
    await loadSources(jobs);
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
    $("#card-count").textContent = `${count(details.visibleCards)} / ${count(details.parsedCards)}`;
    $("#duplicate-count").textContent = count(details.duplicatePostIds);
    const coverage = Object.values(details.fieldCoverage || {})
      .map((field) => Number(field?.ratio)).filter(Number.isFinite);
    $("#coverage-count").textContent = coverage.length
      ? `${Math.round(coverage.reduce((total, value) => total + value, 0) / coverage.length * 100)}% avg`
      : "—";
    $("#stop-reason").textContent = titleCase(details.stopReason || job.completionReason || "pending");
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
  $("#posts-list").replaceChildren(node("p", "Loading snapshot evidence…", "empty"));
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
    $("#posts-list").replaceChildren(node("p", "Open a terminal snapshot to inspect its Posts.", "empty"));
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

function snapshotId(snapshot) {
  return String(snapshot.id || snapshot.snapshotId || "");
}

function snapshotSource(snapshot) {
  const request = snapshot.request || snapshot.source || {};
  if (request.sourceFingerprint) return `fingerprint:${request.sourceFingerprint}`;
  return sourceKey({
    provider: snapshot.provider || request.provider,
    sourceType: request.sourceType || request.surface,
    sourceValue: request.sourceValue || request.value || request.query,
  });
}

let defaultComparedKey = "";

function refreshComparisonOptions() {
  const before = $("#before-snapshot");
  const after = $("#after-snapshot");
  const oldBefore = before.value;
  const oldAfter = after.value;
  const options = snapshotCatalog.map((snapshot) => ({
    value: snapshotId(snapshot),
    label: `${jobLabel(snapshot)} · ${utc(snapshot.capturedAt || snapshot.finishedAt || snapshot.createdAt)}`,
  })).filter((option) => option.value);
  replaceOptions(before, "Choose an earlier snapshot", options);
  replaceOptions(after, "Choose a later snapshot", options);
  if (options.some((option) => option.value === oldBefore)) before.value = oldBefore;
  if (options.some((option) => option.value === oldAfter)) after.value = oldAfter;
  if (before.value && after.value) return;

  for (let latestIndex = 0; latestIndex < snapshotCatalog.length; latestIndex += 1) {
    const latest = snapshotCatalog[latestIndex];
    const earlier = snapshotCatalog.slice(latestIndex + 1)
      .find((candidate) => snapshotSource(candidate) === snapshotSource(latest));
    if (!earlier) continue;
    before.value = snapshotId(earlier);
    after.value = snapshotId(latest);
    const key = `${before.value}:${after.value}`;
    if (key !== defaultComparedKey) {
      defaultComparedKey = key;
      void runComparison();
    }
    return;
  }
}

function comparisonPayload(value) {
  const result = value?.comparison || value;
  const listNames = ["newlyObserved", "reobserved", "notObservedInLatest", "engagementDeltas"];
  if (result && listNames.every((name) => Array.isArray(result[name]))) return result;
  if (!result || !Array.isArray(result.newlyObserved)
      || !Array.isArray(result.reobserved)
      || !Array.isArray(result.notObservedInNewerSample)) return null;
  const asPost = (evidence) => ({
    post_id: evidence?.postId,
    text: evidence?.postText?.value,
    url: evidence?.originalUrl,
    created_at: evidence?.createdAt,
    observed_at: evidence?.observedAt,
    author_id: evidence?.author?.id,
    author_username: evidence?.author?.username,
  });
  const reobserved = result.reobserved.map((item) => asPost(item.evidence));
  return {
    beforeSnapshotId: result.olderSnapshot?.snapshotId,
    afterSnapshotId: result.newerSnapshot?.snapshotId,
    sample: {
      beforeCount: result.sample?.olderPostsScanned,
      afterCount: result.sample?.newerPostsScanned,
    },
    partial: Boolean(result.partial),
    truncated: Boolean(result.truncated),
    newlyObserved: result.newlyObserved.map(asPost),
    reobserved,
    notObservedInLatest: result.notObservedInNewerSample.map(asPost),
    engagementDeltas: result.reobserved
      .filter((item) => Object.keys(item.engagementDelta || {}).length)
      .map((item) => ({
        post: asPost(item.evidence),
        fields: Object.fromEntries(Object.entries(item.engagementDelta)
          .map(([field, delta]) => [field, { delta }])),
      })),
    counts: {
      newlyObserved: result.counts?.newlyObserved,
      reobserved: result.counts?.reobserved,
      notObservedInLatest: result.counts?.notObservedInNewerSample,
    },
  };
}

function comparisonEvidence(post, snapshot, label, detail = "") {
  const article = node("article", "", "change-card");
  const heading = node("div", "", "post-top");
  heading.append(node("strong", label), node("span", `Snapshot ${snapshot.slice(0, 12)}`));
  article.append(heading);
  const text = String(post.text ?? "[Post text unavailable]");
  article.append(node("p", `External Post evidence: “${text}”`, "post-text"));
  const metadata = node("p", `Post ${post.post_id || "unknown"} · observed ${utc(post.observed_at || post.created_at)}${detail ? ` · ${detail}` : ""}`, "fine");
  article.append(metadata);
  const actions = node("div", "", "actions");
  const snapshotButton = node("button", "Open snapshot evidence");
  snapshotButton.type = "button";
  snapshotButton.addEventListener("click", async () => {
    await openJob(snapshot);
    const target = document.getElementById(postAnchor(post.post_id));
    if (target) {
      target.tabIndex = -1;
      target.focus({ preventScroll: true });
      target.scrollIntoView({ block: "start" });
    } else {
      $("#evidence").scrollIntoView({ block: "start" });
    }
  });
  actions.append(snapshotButton);
  const href = safeUrl(post.url);
  if (href) {
    const link = node("a", "Open original Post ↗", "button");
    link.href = href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    actions.append(link);
  }
  article.append(actions);
  return article;
}

function deltaLabel(fields) {
  return Object.entries(fields || {}).map(([field, values]) => {
    const sign = values.delta > 0 ? "+" : "";
    return values.before === undefined || values.after === undefined
      ? `${titleCase(field)} ${sign}${values.delta}`
      : `${titleCase(field)} ${count(values.before)} → ${count(values.after)} (${sign}${values.delta})`;
  }).join("; ");
}

function comparisonGroup(title, explanation, values, snapshot, label, detail = () => "", total = values.length) {
  const section = node("section", "", "change-group");
  section.append(node("h3", `${title} (${count(total)})`), node("p", explanation, "fine"));
  const list = node("div", "", "posts");
  list.append(...values.slice(0, 100).map((value) => {
    const post = value.post || value;
    return comparisonEvidence(post, snapshot, label, detail(value));
  }));
  if (!values.length) list.append(node("p", "No evidence in this category.", "empty"));
  if (values.length > 100) list.append(node("p", `Showing the first 100 of ${count(values.length)} evidence rows.`, "fine"));
  section.append(list);
  return section;
}

function renderComparison(comparison, origin) {
  const beforeId = String(comparison.beforeSnapshotId || $("#before-snapshot").value);
  const afterId = String(comparison.afterSnapshotId || $("#after-snapshot").value);
  const categoryCounts = comparison.counts || {};
  $("#new-count").textContent = count(categoryCounts.newlyObserved ?? comparison.newlyObserved.length);
  $("#same-count").textContent = count(categoryCounts.reobserved ?? comparison.reobserved.length);
  $("#missing-count").textContent = count(categoryCounts.notObservedInLatest ?? comparison.notObservedInLatest.length);
  $("#delta-count").textContent = count(comparison.engagementDeltas.length);
  $("#comparison-summary").hidden = false;
  const sample = comparison.sample || {};
  $("#compare-status").textContent = `${origin} Earlier sample ${count(sample.beforeCount)} Posts; later sample ${count(sample.afterCount)} Posts.${comparison.partial ? " At least one snapshot is partial." : ""}${comparison.truncated ? " Results are truncated." : ""}`;
  $("#change-results").replaceChildren(
    comparisonGroup("Newly observed", "Present in the later bounded sample and absent from the earlier sample.", comparison.newlyObserved, afterId, "Newly observed", undefined, categoryCounts.newlyObserved),
    comparisonGroup("Reobserved", "Present in both bounded samples.", comparison.reobserved, afterId, "Reobserved", undefined, categoryCounts.reobserved),
    comparisonGroup("Not observed later", "Present earlier but absent from the later sample; this does not establish deletion.", comparison.notObservedInLatest, beforeId, "Not observed in later sample", undefined, categoryCounts.notObservedInLatest),
    comparisonGroup("Comparable metric changes", "Only fields present in both observations are compared.", comparison.engagementDeltas, afterId, "Metric delta", (value) => deltaLabel(value.fields)),
  );
}

async function runComparison() {
  const button = $("#compare-button");
  const beforeId = $("#before-snapshot").value;
  const afterId = $("#after-snapshot").value;
  banner($("#compare-error"), "");
  if (!beforeId || !afterId || beforeId === afterId) {
    banner($("#compare-error"), "Choose two different snapshots.");
    return;
  }
  const beforeSnapshot = snapshotCatalog.find((item) => snapshotId(item) === beforeId);
  const afterSnapshot = snapshotCatalog.find((item) => snapshotId(item) === afterId);
  if (beforeSnapshot && afterSnapshot && snapshotSource(beforeSnapshot) !== snapshotSource(afterSnapshot)) {
    banner($("#compare-error"), "Choose snapshots from the same source.");
    return;
  }
  button.disabled = true;
  $("#compare-status").textContent = "Building bounded comparison evidence…";
  try {
    let comparison = null;
    try {
      const query = new URLSearchParams({ olderSnapshotId: beforeId, newerSnapshotId: afterId });
      comparison = comparisonPayload(await api(`/api/compare?${query}`));
    } catch {
      // Fall back to the existing bounded snapshot/Post API.
    }
    if (comparison) {
      renderComparison(comparison, "Loaded from the shared comparison service.");
    } else {
      const [beforePosts, afterPosts] = await Promise.all([
        loadAllPosts(beforeId), loadAllPosts(afterId),
      ]);
      renderComparison(compareSnapshotPosts(beforePosts, afterPosts, {
        beforeSnapshotId: beforeId,
        afterSnapshotId: afterId,
        partial: Boolean(beforeSnapshot?.isPartial || afterSnapshot?.isPartial),
        truncated: beforePosts.length >= 500 || afterPosts.length >= 500,
      }), "Shared comparison endpoint unavailable; computed locally from stored Posts.");
    }
  } catch (error) {
    banner($("#compare-error"), error.message);
    $("#compare-status").textContent = "Comparison unavailable; stored snapshots were not changed.";
  } finally {
    button.disabled = false;
  }
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
  const names = { like_count: "Likes", reply_count: "Replies", repost_count: "Reposts", quote_count: "Quotes", bookmark_count: "Bookmarks", view_count: "Views" };
  for (const [field, label] of Object.entries(names)) {
    foot.append(node("span", `${label}: ${formatMetric(field === "view_count" ? post[field] : details.metrics[field])}`));
  }
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
  $("#snapshot-status").textContent = job.isPartial ? "Partial snapshot" : titleCase(job.status);
  $("#snapshot-status").className = `pill ${job.status}`;
  $("#partial-notice").hidden = !job.isPartial;
  const provenance = job.provenance || {};
  const browserSurface = titleCase(
    provenance.sourceKind || job.request?.sourceType || "browser",
  );
  $("#snapshot-meta").textContent = job.provider === "playwright_browser"
    ? `Browser ${browserSurface} · ${provenance.sourceUrl || "local snapshot"} · captured ${utc(job.capturedAt)} · ${count(posts.length)} stored Posts`
    : `${titleCase(provenance.searchMode)} · ${provenance.endpoint || ""} · effective query “${provenance.query || ""}” · captured ${utc(job.capturedAt)} · ${count(posts.length)} stored Posts`;
  $("#json-export").href = `/api/jobs/${encodeURIComponent(jobId)}/export?format=json`;
  $("#csv-export").href = `/api/jobs/${encodeURIComponent(jobId)}/export?format=csv`;
  $("#result-count").textContent = `${count(posts.length)} Posts`;
  populateFilters();
  renderSummary();
  renderFilteredPosts();
  setStage(3);
}

$("#compare-button").addEventListener("click", runComparison);
$("#refresh-sources").addEventListener("click", () => loadSources());
for (const link of document.querySelectorAll(".product-nav a")) {
  link.addEventListener("click", () => {
    for (const item of document.querySelectorAll(".product-nav a")) item.removeAttribute("aria-current");
    link.setAttribute("aria-current", "page");
  });
}

updateRequestHelp();
await loadConnection();
await loadHistory({ openDemo: true });
