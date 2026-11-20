// Page flow: connect -> load playlist -> edit -> render -> download.
import { api } from "./api.js";
import { Editor, formatDuration } from "./editor.js";

const $ = (id) => document.getElementById(id);
const card = document.querySelector(".card");
const views = ["upload", "loading", "editor", "processing", "done"];

function showView(name) {
  views.forEach((v) => $(`view-${v}`).classList.toggle("active", v === name));
  card.classList.toggle("wide", name === "editor");
  try { sessionStorage.setItem("popslop_view", name); } catch {}
}

function showError(msg) {
  $("upload-error").textContent = msg;
  $("upload-error").classList.add("show");
}
const clearError = () => $("upload-error").classList.remove("show");

// ------------------------------------------------------------ wake server --
// Free Render instances sleep when idle and take ~20-50s to wake. Ping on page
// load so it's (usually) awake by the time someone clicks a button.
(async function wakeServer() {
  const hint = $("wake-hint");
  const slow = setTimeout(() => { hint.textContent = "Waking up the server… first load can take ~30 seconds."; hint.style.display = "block"; }, 2500);
  try {
    await api.health();
    hint.style.display = "none";
  } catch {
    hint.textContent = "Can't reach the server right now. Try again in a minute.";
    hint.style.display = "block";
  } finally {
    clearTimeout(slow);
  }
})();

// ------------------------------------------------------------------ login --
let sessionId = null;
(function readSession() {
  const params = new URLSearchParams(window.location.search);
  const sid = params.get("session_id");
  if (sid) {
    sessionId = sid;
    try { sessionStorage.setItem("seam_session_id", sid); } catch {}
    params.delete("session_id");
    const rest = params.toString();
    window.history.replaceState({}, document.title, window.location.pathname + (rest ? `?${rest}` : ""));
  } else {
    try { sessionId = sessionStorage.getItem("seam_session_id"); } catch {}
  }
  if (sessionId) markConnected();
})();

function markConnected(connected = true) {
  $("connect-btn").style.display = connected ? "none" : "block";
  $("load-btn").style.display = connected ? "block" : "none";
  $("connected-hint").style.display = connected ? "block" : "none";
}

$("connect-btn").addEventListener("click", () => { window.location.href = api.loginUrl(); });

// ----------------------------------------------------------------- editor --
const editor = new Editor({
  list: $("track-list"),
  summary: $("editor-summary"),
  sortSelect: $("sort-select"),
  reshuffleBtn: $("reshuffle-btn"),
  search: $("track-search"),
  clipInput: $("clip-duration-input"),
  clipValue: $("clip-duration-value"),
  fadeInput: $("fade-duration-input"),
  fadeValue: $("fade-duration-value"),
  renderBtn: $("render-btn"),
  toast: $("toast"),
});

let projectId = null;

$("load-btn").addEventListener("click", async () => {
  clearError();
  const url = $("playlist-url").value.trim();
  if (!sessionId) return showError("Connect Spotify first.");
  if (!url) return showError("Paste a playlist link first.");

  $("load-btn").disabled = true;
  try {
    const p = await api.createProject(url, sessionId);
    projectId = p.project_id;
    try { sessionStorage.setItem("popslop_project", projectId); } catch {}
    showView("loading");
    await waitForProject(projectId);
  } catch (e) {
    if (e.status === 401) {
      try { sessionStorage.removeItem("seam_session_id"); } catch {}
      sessionId = null;
      markConnected(false);
    }
    showView("upload");
    showError(e.message || "Something went wrong loading the playlist.");
  } finally {
    $("load-btn").disabled = false;
  }
});

async function waitForProject(pid) {
  while (true) {
    const p = await api.getProject(pid);
    $("loading-status").textContent = p.message;
    $("loading-fill").style.width = `${p.progress}%`;
    if (p.status === "ready") {
      editor.load(p);
      showView("editor");
      return;
    }
    if (p.status === "error") throw new Error(p.message);
    await new Promise((r) => setTimeout(r, 1200));
  }
}

$("back-btn").addEventListener("click", () => { editor.stop(); showView("upload"); });

// ----------------------------------------------------------------- render --
let pollTimer = null;

$("render-btn").addEventListener("click", async () => {
  editor.stop();
  $("render-btn").disabled = true;
  try {
    const job = await api.render(projectId, editor.renderPayload(), editor.fade);
    showView("processing");
    $("progress-fill").style.width = "0%";
    $("processing-estimate-hint").textContent = job.estimated_duration_sec
      ? `Mix length: ${formatDuration(job.estimated_duration_sec)}` : "";
    pollJob(job.job_id);
  } catch (e) {
    editor.toast(e.message);
  } finally {
    $("render-btn").disabled = false;
  }
});

const STATUS_LABELS = {
  queued: "Waiting in line…",
  fetching_clips: "Downloading clips…",
  stitching: "Crossfading and encoding…",
};

function pollJob(jobId) {
  clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    try {
      const job = await api.getJob(jobId);
      $("processing-sub").textContent = STATUS_LABELS[job.status] || "Working on it…";
      $("status-line").textContent = job.message || "";
      $("progress-fill").style.width = `${job.progress || 0}%`;
      if (job.status === "done") {
        clearInterval(pollTimer);
        renderDone(job);
      } else if (job.status === "error") {
        clearInterval(pollTimer);
        showView("editor");
        editor.toast(job.message || "The render failed.");
      }
    } catch {
      clearInterval(pollTimer);
      showView("editor");
      editor.toast("Lost connection while checking the render.");
    }
  }, 1500);
}

function renderDone(job) {
  showView("done");
  $("download-link").href = api.downloadUrl(job.download_url);
  const length = job.estimated_duration_sec ? ` Length: ~${formatDuration(job.estimated_duration_sec)}.` : "";
  $("done-sub").textContent = `${job.matched_tracks} clips made it into the mix.${length}`;
  const skipped = job.unmatched_tracks || [];
  $("done-unmatched-wrap").style.display = skipped.length ? "block" : "none";
  $("done-unmatched-list").textContent = `Skipped (download failed): ${skipped.join(", ")}`;
}

$("edit-again-btn").addEventListener("click", () => showView("editor"));
$("restart-btn").addEventListener("click", () => {
  $("playlist-url").value = "";
  clearError();
  showView("upload");
});

// ---------------------------------------------- resume after a refresh ----
(async function resume() {
  let pid, view;
  try { pid = sessionStorage.getItem("popslop_project"); view = sessionStorage.getItem("popslop_view"); } catch {}
  if (!pid || !["editor", "done", "processing", "loading"].includes(view)) return;
  try {
    const p = await api.getProject(pid);
    projectId = pid;
    if (p.status === "ready") { editor.load(p); showView("editor"); }
    else if (p.status === "resolving") { showView("loading"); await waitForProject(pid); }
  } catch { /* project expired on the server: stay on the start screen */ }
})();
