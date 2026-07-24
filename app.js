const form = document.querySelector("#scrape-form");
const resultsBody = document.querySelector("#results-body");
const downloadCsvButton = document.querySelector("#download-csv");
const downloadJsonButton = document.querySelector("#download-json");
const tweetCount = document.querySelector("#tweet-count");
const likeCount = document.querySelector("#like-count");
const replyCount = document.querySelector("#reply-count");
const scrollSquigglePath = document.querySelector("#scroll-squiggle-progress");
const scrollSquiggleDot = document.querySelector("#scroll-squiggle-dot");
const errorBanner = document.querySelector("#error-banner");
const submitButton = form.querySelector('button[type="submit"]');
const resultsPanelHeading = document.querySelector("#results-panel-heading");

const API_BASE = "http://localhost:5000";

let currentResults = [];

function mixColor(startColor, endColor, amount) {
  const start = startColor.match(/\w\w/g).map((channel) => parseInt(channel, 16));
  const end = endColor.match(/\w\w/g).map((channel) => parseInt(channel, 16));
  const mixed = start.map((channel, index) => Math.round(channel + (end[index] - channel) * amount));

  return `#${mixed.map((channel) => channel.toString(16).padStart(2, "0")).join("")}`;
}

function getScrollProgress() {
  const scrollableHeight = document.documentElement.scrollHeight - window.innerHeight;

  if (scrollableHeight <= 0) {
    return 1;
  }

  return Math.min(window.scrollY / scrollableHeight, 1);
}

function updateScrollSquiggle() {
  if (!scrollSquigglePath || !scrollSquiggleDot) {
    return;
  }

  const pathLength = scrollSquigglePath.getTotalLength();
  const progress = getScrollProgress();
  const currentPoint = scrollSquigglePath.getPointAtLength(pathLength * progress);
  const activeColor = mixColor("8fd3ff", "c7a0ff", progress);

  scrollSquigglePath.style.strokeDasharray = pathLength;
  scrollSquigglePath.style.strokeDashoffset = pathLength * (1 - progress);
  scrollSquiggleDot.setAttribute("cx", currentPoint.x);
  scrollSquiggleDot.setAttribute("cy", currentPoint.y);
  scrollSquiggleDot.classList.toggle("is-visible", progress > 0.02);
  scrollSquiggleDot.classList.toggle("is-complete", progress >= 0.99);
  document.documentElement.style.setProperty("--scroll-color", activeColor);
}

function showError(message) {
  if (!errorBanner) return;
  errorBanner.textContent = message;
  errorBanner.hidden = false;
}

function clearError() {
  if (!errorBanner) return;
  errorBanner.textContent = "";
  errorBanner.hidden = true;
}

function setCacheHitBadge(cacheHit, cacheAge) {
  const existingBadge = document.querySelector("#cache-badge");
  if (existingBadge) existingBadge.remove();

  if (cacheHit && resultsPanelHeading) {
    const badge = document.createElement("span");
    badge.id = "cache-badge";
    badge.className = "pill";
    badge.style.marginLeft = "0.5rem";
    badge.style.fontSize = "0.75rem";
    badge.textContent = cacheAge ? `Served from cache (${cacheAge} old)` : "Served from cache";
    resultsPanelHeading.insertAdjacentElement("afterend", badge);
  }
}

function renderResults(results) {
  resultsBody.replaceChildren();

  if (results.length === 0) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");

    row.className = "empty-row";
    cell.colSpan = 7;
    cell.textContent = "No preview data yet.";
    row.append(cell);
    resultsBody.append(row);
    return;
  }

  results.forEach((tweet) => {
    const row = document.createElement("tr");
    const values = [
      `@${tweet.username}`,
      tweet.text,
      tweet.createdAt,
      tweet.likeCount,
      tweet.replyCount,
      tweet.retweetCount,
    ];

    values.forEach((value, index) => {
      const cell = document.createElement("td");

      if (index === 1) {
        cell.className = "tweet-text";
      }

      cell.textContent = value;
      row.append(cell);
    });

    const linkCell = document.createElement("td");
    const link = document.createElement("a");

    link.href = tweet.url;
    link.target = "_blank";
    link.rel = "noreferrer";
    link.textContent = tweet.url ? "View" : "—";
    if (!tweet.url) link.removeAttribute("href");
    linkCell.append(link);
    row.append(linkCell);
    resultsBody.append(row);
  });
}

function updateSummary(results) {
  tweetCount.textContent = results.length;
  likeCount.textContent = results.reduce((total, tweet) => total + (tweet.likeCount || 0), 0);
  replyCount.textContent = results.reduce((total, tweet) => total + (tweet.replyCount || 0), 0);
}

function setDownloadState(enabled) {
  downloadCsvButton.disabled = !enabled;
  downloadJsonButton.disabled = !enabled;
}

function downloadFile(filename, content, type) {
  const blob = new Blob([content], { type });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");

  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

function escapeCsvValue(value) {
  const stringValue = String(value ?? "");
  return `"${stringValue.replaceAll('"', '""')}"`;
}

function convertToCsv(results) {
  const headers = ["username", "text", "createdAt", "likeCount", "replyCount", "retweetCount", "url", "finalLabel", "finalConfidence"];
  const rows = results.map((tweet) => headers.map((header) => escapeCsvValue(tweet[header])).join(","));

  return [headers.join(","), ...rows].join("\n");
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearError();
  setCacheHitBadge(false);

  const formData = new FormData(form);
  const payload = {
    sourceValue: formData.get("sourceValue"),
    maxTweets: Number(formData.get("maxTweets")) || 25,
    includeType: formData.get("includeType"),
    startDate: formData.get("startDate") || null,
    endDate: formData.get("endDate") || null,
  };

  submitButton.textContent = "Scraping…";
  submitButton.disabled = true;

  try {
    const response = await fetch(`${API_BASE}/api/scrape`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    const data = await response.json();

    if (!response.ok) {
      showError(data.error || `Request failed with status ${response.status}.`);
      renderResults([]);
      updateSummary([]);
      setDownloadState(false);
      return;
    }

    currentResults = data.tweets || [];
    renderResults(currentResults);
    updateSummary(currentResults);
    setDownloadState(currentResults.length > 0);
    setCacheHitBadge(data.cacheHit, data.cacheAge);
  } catch (err) {
    showError(
      "Could not reach the scraper server. Make sure server.py is running on localhost:5000."
    );
    renderResults([]);
    updateSummary([]);
    setDownloadState(false);
  } finally {
    submitButton.textContent = "Preview Results";
    submitButton.disabled = false;
  }
});

form.addEventListener("reset", () => {
  currentResults = [];
  renderResults(currentResults);
  updateSummary(currentResults);
  setDownloadState(false);
  clearError();
  setCacheHitBadge(false);
});

downloadCsvButton.addEventListener("click", () => {
  downloadFile("tweet-results.csv", convertToCsv(currentResults), "text/csv");
});

downloadJsonButton.addEventListener("click", () => {
  downloadFile("tweet-results.json", JSON.stringify(currentResults, null, 2), "application/json");
});

window.addEventListener("scroll", updateScrollSquiggle, { passive: true });
window.addEventListener("resize", updateScrollSquiggle);
updateScrollSquiggle();
