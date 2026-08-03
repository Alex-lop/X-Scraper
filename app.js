const form = document.querySelector("#job-form");
const sourceType = document.querySelector("#source-type");
const sourceLabel = document.querySelector("#source-label");
const sourceValue = document.querySelector("#source-value");
const submitButton = document.querySelector("#submit-button");
const activePanel = document.querySelector("#active-panel");
const jobTitle = document.querySelector("#job-title");
const jobStatus = document.querySelector("#job-status");
const progressBar = document.querySelector("#progress-bar");
const progressTrack = document.querySelector(".progress-track");
const progressCopy = document.querySelector("#progress-copy");
const errorBanner = document.querySelector("#error-banner");
const warningBanner = document.querySelector("#warning-banner");
const cancelButton = document.querySelector("#cancel-button");
const resumeButton = document.querySelector("#resume-button");
const resultsBody = document.querySelector("#results-body");
const historyList = document.querySelector("#history-list");

let activeJobId = null;
let pollTimer = null;

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const contentType = response.headers.get("content-type") || "";
  const data = contentType.includes("application/json")
    ? await response.json()
    : { error: { message: await response.text() || `Request failed (${response.status})` } };
  if (!response.ok) {
    const error = new Error(data.error?.message || `Request failed (${response.status})`);
    error.code = data.error?.code;
    throw error;
  }
  return data;
}

function formatNumber(value) {
  return new Intl.NumberFormat().format(Number(value || 0));
}

function updateSourceForm() {
  const search = sourceType.value === "search";
  sourceLabel.textContent = search ? "Search query" : "Username or profile URL";
  sourceValue.placeholder = search ? "AI agents lang:en" : "@OpenAI or https://x.com/OpenAI";
}

async function loadSession() {
  const status = document.querySelector("#session-status");
  const message = document.querySelector("#session-message");
  try {
    const data = await api("/api/session");
    status.textContent = data.status;
    status.className = data.valid ? "good" : "bad";
    message.textContent = data.message;
    submitButton.disabled = !data.valid;
  } catch (error) {
    status.textContent = "unavailable";
    status.className = "bad";
    message.textContent = error.message;
    submitButton.disabled = true;
  }
}

function showBanner(element, message) {
  element.textContent = message || "";
  element.hidden = !message;
}

function renderTweets(tweets) {
  resultsBody.replaceChildren();
  if (!tweets.length) {
    resultsBody.innerHTML = '<tr class="empty"><td colspan="9">No collected posts yet.</td></tr>';
  }
  for (const tweet of tweets) {
    const row = document.createElement("tr");
    const type = [tweet.is_reply && "reply", tweet.is_retweet && "repost", tweet.is_quote && "quote", tweet.has_media && "media"].filter(Boolean).join(", ") || "post";
    const sentiment = tweet.sentiment_label ? `${tweet.sentiment_label} (${Number(tweet.sentiment_score).toFixed(2)})` : "—";
    const values = [
      `@${tweet.author_username}`, tweet.text,
      tweet.created_at ? new Date(tweet.created_at).toLocaleString() : "—",
      formatNumber(tweet.like_count), formatNumber(tweet.reply_count),
      formatNumber(tweet.retweet_count), type, sentiment,
    ];
    values.forEach((value, index) => {
      const cell = document.createElement("td");
      cell.textContent = value;
      if (index === 1) cell.className = "post-text";
      row.append(cell);
    });
    const linkCell = document.createElement("td");
    const link = document.createElement("a");
    link.href = tweet.url;
    link.target = "_blank";
    link.rel = "noreferrer";
    link.textContent = "View";
    linkCell.append(link);
    row.append(linkCell);
    resultsBody.append(row);
  }
  document.querySelector("#result-count").textContent = `${tweets.length} posts`;
  document.querySelector("#tweet-count").textContent = formatNumber(tweets.length);
  for (const metric of ["like", "reply", "retweet", "quote"]) {
    const total = tweets.reduce((sum, item) => sum + Number(item[`${metric}_count`] || 0), 0);
    document.querySelector(`#${metric}-count`).textContent = formatNumber(total);
  }
}

async function loadTweets(jobId) {
  const data = await api(`/api/jobs/${jobId}/tweets?limit=500`);
  renderTweets(data.tweets || []);
}

function setActiveJob(job) {
  activePanel.hidden = false;
  activeJobId = job.id;
  jobTitle.textContent = `${job.request.sourceType}: ${job.request.sourceValue}`;
  jobStatus.textContent = job.status;
  jobStatus.className = `status ${job.status}`;
  const percent = job.targetCount ? Math.min((job.collectedCount / job.targetCount) * 100, 100) : 0;
  progressBar.style.width = `${percent}%`;
  progressTrack.setAttribute("aria-valuenow", String(Math.round(percent)));
  const completion = job.completionReason ? ` · ${job.completionReason.replaceAll("_", " ")}` : "";
  progressCopy.textContent = `${formatNumber(job.collectedCount)} of ${formatNumber(job.targetCount)} maximum posts collected${completion}`;
  showBanner(errorBanner, job.error?.message);
  const warnings = [...(job.warnings || [])];
  if (job.isPartial) warnings.unshift("This job contains partial results.");
  showBanner(warningBanner, warnings.join(" "));
  const active = ["queued", "running"].includes(job.status);
  cancelButton.hidden = !active;
  cancelButton.disabled = Boolean(job.cancelRequested);
  const resumable = ["cancelled", "interrupted", "partial"].includes(job.status)
    || (job.status === "failed" && job.error?.retryable);
  resumeButton.hidden = !resumable;
  document.querySelector("#json-export").href = `/api/jobs/${job.id}/export?format=json`;
  document.querySelector("#csv-export").href = `/api/jobs/${job.id}/export?format=csv`;
}

async function pollJob() {
  if (!activeJobId) return;
  try {
    const job = await api(`/api/jobs/${activeJobId}`);
    setActiveJob(job);
    await loadTweets(activeJobId);
    if (["queued", "running"].includes(job.status)) {
      pollTimer = window.setTimeout(pollJob, 1500);
    } else {
      submitButton.disabled = false;
      await loadHistory();
    }
  } catch (error) {
    showBanner(errorBanner, error.message);
    pollTimer = window.setTimeout(pollJob, 3000);
  }
}

async function openJob(jobId) {
  window.clearTimeout(pollTimer);
  activeJobId = jobId;
  await pollJob();
  activePanel.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function loadHistory() {
  try {
    const data = await api("/api/jobs?limit=25");
    historyList.replaceChildren();
    if (!data.jobs.length) {
      historyList.innerHTML = '<p class="muted">No collections have been run yet.</p>';
      return;
    }
    for (const job of data.jobs) {
      const button = document.createElement("button");
      button.className = "history-item";
      button.type = "button";
      button.innerHTML = `<span><strong>${job.request.sourceType}: ${escapeHtml(job.request.sourceValue)}</strong><small>${new Date(job.createdAt).toLocaleString()}</small></span><span><b class="status ${job.status}">${job.status}</b><small>${job.collectedCount}/${job.targetCount}</small></span>`;
      button.addEventListener("click", () => openJob(job.id));
      historyList.append(button);
    }
  } catch (error) {
    historyList.textContent = error.message;
  }
}

function escapeHtml(value) {
  const element = document.createElement("span");
  element.textContent = value;
  return element.innerHTML;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  submitButton.disabled = true;
  showBanner(errorBanner, "");
  const values = new FormData(form);
  const payload = {
    sourceType: values.get("sourceType"), sourceValue: values.get("sourceValue"),
    maxTweets: Number(values.get("maxTweets")), startDate: values.get("startDate") || null,
    endDate: values.get("endDate") || null, includeReplies: values.has("includeReplies"),
    mediaOnly: values.has("mediaOnly"), analyzeSentiment: values.has("analyzeSentiment"),
  };
  try {
    const data = await api("/api/jobs", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    activeJobId = data.jobId;
    await pollJob();
  } catch (error) {
    activePanel.hidden = false;
    showBanner(errorBanner, error.message);
    submitButton.disabled = false;
  }
});

cancelButton.addEventListener("click", async () => {
  if (!activeJobId) return;
  try {
    window.clearTimeout(pollTimer);
    await api(`/api/jobs/${activeJobId}`, { method: "DELETE" });
    cancelButton.disabled = true;
    await pollJob();
  } catch (error) {
    showBanner(errorBanner, error.message);
  }
});

resumeButton.addEventListener("click", async () => {
  if (!activeJobId) return;
  try {
    window.clearTimeout(pollTimer);
    resumeButton.disabled = true;
    await api(`/api/jobs/${activeJobId}/resume`, { method: "POST" });
    await pollJob();
  } catch (error) {
    showBanner(errorBanner, error.message);
  } finally {
    resumeButton.disabled = false;
  }
});

sourceType.addEventListener("change", updateSourceForm);
form.addEventListener("reset", () => window.setTimeout(updateSourceForm));
document.querySelector("#refresh-history").addEventListener("click", loadHistory);
updateSourceForm();
loadSession();
loadHistory();
