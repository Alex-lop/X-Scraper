const $ = (selector) => document.querySelector(selector);
const form = $("#collection-form");
let preview = null;
let activeJobId = null;
let pollTimer = null;
let postsOffset = 0;

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({ error: { message: `Request failed (${response.status})` } }));
  if (!response.ok) throw new Error(data.error?.message || `Request failed (${response.status})`);
  return data;
}

function payload() {
  const values = new FormData(form);
  return {
    sourceType: values.get("sourceType"), sourceValue: values.get("sourceValue"),
    maxPosts: Number(values.get("maxPosts")), startDate: values.get("startDate") || null,
    endDate: values.get("endDate") || null, includeReplies: values.has("includeReplies"),
    mediaOnly: values.has("mediaOnly"),
  };
}

function banner(element, message) { element.textContent = message || ""; element.hidden = !message; }
function number(value) { return new Intl.NumberFormat().format(Number(value || 0)); }
function money(value) { return new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", minimumFractionDigits: 3 }).format(value); }

async function loadConnection() {
  try {
    const data = await api("/api/connection");
    const offline = data.demoMode === "offline";
    $("#connection-status").textContent = offline ? "Offline demo" : data.valid ? "X API configured" : "Connection required";
    $("#connection-message").textContent = data.message;
    $("#connection-card").classList.toggle("ready", data.valid || offline);
    $("#setup-card").hidden = data.valid || offline;
    $("#preview-button").disabled = !data.valid || offline;
  } catch (error) { $("#connection-message").textContent = error.message; }
}

form.addEventListener("change", () => { preview = null; $("#preview-card").hidden = true; });
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("#preview-button");
  button.disabled = true;
  try {
    preview = await api("/api/collections/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload()) });
    $("#preview-query").textContent = preview.compiledIntent.query;
    $("#preview-window").textContent = `${new Date(preview.compiledIntent.startTime).toLocaleString()} — ${new Date(preview.compiledIntent.endTime).toLocaleString()}`;
    $("#preview-reads").textContent = `${number(preview.maximumPostReads)} Posts`;
    $("#preview-cost").textContent = money(preview.estimatedPostReadUsd);
    $("#cache-badge").textContent = preview.cacheAvailable ? "Exact cache available" : "Paid API read";
    $("#preview-card").hidden = false;
    form.hidden = true;
  } catch (error) { alert(error.message); }
  finally { button.disabled = false; }
});

$("#edit-button").addEventListener("click", () => { form.hidden = false; $("#preview-card").hidden = true; });
$("#confirm-button").addEventListener("click", async () => {
  if (!preview) return;
  const button = $("#confirm-button");
  button.disabled = true;
  try {
    const data = await api("/api/jobs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...preview.request, compiledRequest: preview.compiledRequest, confirmPaidRead: true, forceRefresh: $("#force-refresh").checked }) });
    activeJobId = data.jobId;
    postsOffset = 0;
    $("#posts-list").replaceChildren();
    await pollJob();
  } catch (error) { alert(error.message); }
  finally { button.disabled = false; }
});

function postCard(post) {
  const article = document.createElement("article"); article.className = "post";
  const top = document.createElement("div"); top.className = "post-top";
  const author = document.createElement("strong"); author.textContent = `@${post.author_username}`;
  const when = document.createElement("time"); when.textContent = post.created_at ? new Date(post.created_at).toLocaleString() : "Unknown time";
  top.append(author, when);
  const text = document.createElement("p"); text.textContent = post.text;
  const chips = document.createElement("div"); chips.className = "chips";
  const types = [post.is_reply && "reply", post.is_retweet && "repost", post.is_quote && "quote", post.has_media && "media"].filter(Boolean);
  for (const type of types.length ? types : ["post"]) { const chip = document.createElement("span"); chip.textContent = type; chips.append(chip); }
  const media = document.createElement("div"); media.className = "media";
  for (const item of post.media || []) if (item.url) { const image = document.createElement("img"); image.src = item.url; image.alt = item.altText || `${item.type || "Post"} media`; image.loading = "lazy"; media.append(image); }
  const foot = document.createElement("div"); foot.className = "post-foot";
  foot.textContent = `♥ ${number(post.like_count)}  ↩ ${number(post.reply_count)}  ↻ ${number(post.retweet_count)}  ❝ ${number(post.quote_count)}`;
  const link = document.createElement("a"); link.href = post.url; link.target = "_blank"; link.rel = "noreferrer"; link.textContent = "Open original ↗"; foot.append(" · ", link);
  article.append(top, text, chips); if (media.children.length) article.append(media); article.append(foot); return article;
}

async function loadPosts(reset = false) {
  if (!activeJobId) return;
  if (reset) { postsOffset = 0; $("#posts-list").replaceChildren(); }
  const data = await api(`/api/jobs/${activeJobId}/posts?limit=50&offset=${postsOffset}`);
  for (const post of data.posts) $("#posts-list").append(postCard(post));
  postsOffset += data.posts.length;
  $("#result-count").textContent = `${number(data.pagination.total)} posts`;
  $("#load-more").hidden = data.pagination.nextOffset === null;
  if (!data.pagination.total) $("#posts-list").innerHTML = '<p class="empty">No collected posts yet.</p>';
}

function setJob(job) {
  $("#active-panel").hidden = false;
  $("#job-title").textContent = `${job.request.sourceType}: ${job.request.sourceValue}`;
  $("#job-status").textContent = job.status;
  $("#job-status").className = `pill ${job.status}`;
  $("#collected-count").textContent = `${number(job.collectedCount)} / ${number(job.targetCount)}`;
  $("#read-count").textContent = number(job.readCount);
  $("#rate-limit").textContent = job.rateLimit.remaining ?? "—";
  $("#job-cost").textContent = money(job.cost.estimatedPostReadUsd);
  const percent = Math.min(100, job.targetCount ? job.collectedCount / job.targetCount * 100 : 0);
  $("#progress-bar").style.width = `${percent}%`;
  let copy = `${number(job.collectedCount)} collected from ${number(job.readCount)} maximum-billable observations so far.`;
  if (job.status === "waiting" && job.retryAt) copy = `Rate limited. Automatic retry ${new Date(job.retryAt).toLocaleTimeString()} (${Math.max(0, Math.ceil((new Date(job.retryAt) - Date.now()) / 1000))}s).`;
  $("#progress-copy").textContent = copy;
  banner($("#error-banner"), job.status === "waiting" ? "" : job.error?.message);
  banner($("#warning-banner"), [...(job.isPartial ? ["Partial results are preserved."] : []), ...(job.warnings || [])].join(" "));
  $("#cancel-button").hidden = !["queued", "running", "waiting"].includes(job.status);
  $("#resume-button").hidden = !(["cancelled", "interrupted", "partial"].includes(job.status) || (job.status === "failed" && job.error?.retryable));
  $("#json-export").href = `/api/jobs/${job.id}/export?format=json`;
  $("#csv-export").href = `/api/jobs/${job.id}/export?format=csv`;
}

async function pollJob() {
  if (!activeJobId) return;
  window.clearTimeout(pollTimer);
  try {
    const job = await api(`/api/jobs/${activeJobId}`); setJob(job); await loadPosts(true);
    if (["queued", "running", "waiting"].includes(job.status)) pollTimer = window.setTimeout(pollJob, 1500);
    else await loadHistory();
  } catch (error) { banner($("#error-banner"), error.message); pollTimer = window.setTimeout(pollJob, 3000); }
}

async function openJob(id) { activeJobId = id; await pollJob(); $("#active-panel").scrollIntoView({ behavior: "smooth" }); }
async function loadHistory() {
  try {
    const data = await api("/api/jobs?limit=25"); const list = $("#history-list"); list.replaceChildren();
    if (!data.jobs.length) { list.innerHTML = '<p class="muted">No collections yet.</p>'; return; }
    for (const job of data.jobs) { const button = document.createElement("button"); button.className = "history-item"; const title = document.createElement("strong"); title.textContent = `${job.request.sourceType}: ${job.request.sourceValue}`; const detail = document.createElement("small"); detail.textContent = `${new Date(job.createdAt).toLocaleString()} · ${job.collectedCount}/${job.targetCount}`; const status = document.createElement("span"); status.className = `pill ${job.status}`; status.textContent = job.status; button.append(title, detail, status); button.addEventListener("click", () => openJob(job.id)); list.append(button); }
  } catch (error) { $("#history-list").textContent = error.message; }
}

$("#cancel-button").addEventListener("click", async () => { await api(`/api/jobs/${activeJobId}`, { method: "DELETE" }); await pollJob(); });
$("#resume-button").addEventListener("click", async () => { await api(`/api/jobs/${activeJobId}/resume`, { method: "POST" }); await pollJob(); });
$("#load-more").addEventListener("click", () => loadPosts(false));
$("#refresh-history").addEventListener("click", loadHistory);
form.addEventListener("change", (event) => { if (event.target.name === "sourceType") { const search = event.target.value === "search"; $("#source-label").textContent = search ? "Search query" : "Username or profile URL"; $("#source-value").placeholder = search ? "AI agents lang:en" : "@OpenAI"; } });
loadConnection(); loadHistory();
