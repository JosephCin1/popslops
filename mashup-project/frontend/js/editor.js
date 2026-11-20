// The pre-render editor: reorder, trim, mute, preview-on-request, swap clip source.
import { api } from "./api.js";
import { SORTERS } from "./sorters.js";

// ---------------------------------------------------------------- helpers --
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export function formatDuration(sec) {
  if (sec == null || !isFinite(sec)) return "";
  sec = Math.round(sec);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

// Accepts "83", "83.5", "1:23", "1:02:03" -> seconds (or NaN).
export function parseTime(text) {
  const parts = String(text).trim().split(":").map(Number);
  if (!parts.length || parts.some((n) => isNaN(n) || n < 0)) return NaN;
  return parts.reduce((acc, n) => acc * 60 + n, 0);
}

const fmtStart = (s) => (s >= 60 ? formatDuration(s) : String(Math.round(s * 10) / 10));

// Same formula as the backend's estimate_duration_sec.
export function estimateMix(lengths, fade) {
  let total = 0, overlaps = 0;
  lengths.forEach((l, i) => {
    total += l;
    if (i) overlaps += Math.min(fade, lengths[i - 1] / 2, l / 2);
  });
  return Math.max(total - overlaps, 0);
}

const SOURCE_LABEL = { deezer: "Deezer", itunes: "iTunes", upload: "your file" };

// ----------------------------------------------------------------- editor --
export class Editor {
  constructor(els) {
    this.els = els; // { list, summary, sortSelect, reshuffleBtn, search, clipInput, clipValue, fadeInput, fadeValue, renderBtn }
    this.audio = new Audio();
    this.audio.preload = "none";
    this.playing = null; // { key, stopAt }
    this.swap = null;    // { tid, loading, options, error, busy }
    this.filter = "";
    this.dragIdx = null;

    this.audio.addEventListener("timeupdate", () => {
      if (this.playing?.stopAt != null && this.audio.currentTime >= this.playing.stopAt) this.stop();
    });
    this.audio.addEventListener("ended", () => this.stop());
    this.audio.addEventListener("error", () => {
      if (this.playing) { this.toast(`Couldn't play that clip.`); this.stop(); }
    });

    this.populateSorters();
    this.bindControls();
    this.bindList();
  }

  // ---------------------------------------------------------- lifecycle --
  load(project) {
    this.pid = project.project_id;
    this.maxSeconds = project.max_mix_seconds;
    this.items = project.tracks.map((t) => ({
      track: t,
      start: 0,
      length: Math.min(this.clipDefault, t.seconds || this.clipDefault),
      enabled: !!t.matched,
      custom: false,
    }));
    this.restoreDraft();
    this.swap = null;
    this.render();
  }

  get clipDefault() { return parseFloat(this.els.clipInput.value); }
  get fade() { return parseFloat(this.els.fadeInput.value); }

  renderPayload() {
    return this.items
      .filter((it) => it.enabled && it.track.matched)
      .map((it) => ({ track_id: it.track.id, start: it.start, length: it.length }));
  }

  // --------------------------------------------------------- persistence --
  // Edits survive a page refresh (same tab) as long as the server still has the project.
  saveDraft() {
    try {
      sessionStorage.setItem(`popslop_draft_${this.pid}`, JSON.stringify({
        order: this.items.map((it) => it.track.id),
        edits: Object.fromEntries(this.items.map((it) => [it.track.id, [it.start, it.length, it.enabled, it.custom]])),
        clip: this.clipDefault, fade: this.fade,
      }));
    } catch { /* storage unavailable: edits just won't survive a refresh */ }
  }

  restoreDraft() {
    let draft;
    try { draft = JSON.parse(sessionStorage.getItem(`popslop_draft_${this.pid}`) || "null"); } catch { draft = null; }
    if (!draft) return;
    const byId = new Map(this.items.map((it) => [it.track.id, it]));
    const ordered = draft.order.map((id) => byId.get(id)).filter(Boolean);
    this.items = ordered.concat(this.items.filter((it) => !draft.order.includes(it.track.id)));
    for (const it of this.items) {
      const e = draft.edits[it.track.id];
      if (e) [it.start, it.length, it.enabled, it.custom] = [e[0], e[1], e[2] && it.track.matched, e[3]];
    }
    this.setSlider(this.els.clipInput, this.els.clipValue, draft.clip, (v) => `${v}s`);
    this.setSlider(this.els.fadeInput, this.els.fadeValue, draft.fade, (v) => `${v.toFixed(1)}s`);
  }

  setSlider(input, label, value, fmt) {
    if (value == null) return;
    input.value = value;
    label.textContent = fmt(parseFloat(input.value));
  }

  // ------------------------------------------------------------ controls --
  populateSorters() {
    this.els.sortSelect.innerHTML =
      `<option value="">Arrange…</option>` + SORTERS.map((s) => `<option value="${s.key}">${esc(s.label)}</option>`).join("");
  }

  applySorter(key) {
    const sorter = SORTERS.find((s) => s.key === key);
    if (!sorter) return;
    this.items = sorter.sort([...this.items]);
    this.els.reshuffleBtn.style.display = sorter.random ? "" : "none";
    this.changed();
  }

  bindControls() {
    const { sortSelect, reshuffleBtn, search, clipInput, clipValue, fadeInput, fadeValue } = this.els;
    sortSelect.addEventListener("change", () => this.applySorter(sortSelect.value));
    reshuffleBtn.addEventListener("click", () => this.applySorter(sortSelect.value));
    search.addEventListener("input", () => { this.filter = search.value.trim().toLowerCase(); this.render(); });

    clipInput.addEventListener("input", () => {
      clipValue.textContent = `${clipInput.value}s`;
      // The slider resets every track you haven't trimmed by hand.
      for (const it of this.items) {
        if (it.custom) continue;
        it.start = 0;
        it.length = Math.min(this.clipDefault, it.track.seconds || this.clipDefault);
      }
      this.changed();
    });
    fadeInput.addEventListener("input", () => {
      fadeValue.textContent = `${parseFloat(fadeInput.value).toFixed(1)}s`;
      this.updateSummary();
      this.saveDraft();
    });
  }

  // ---------------------------------------------------------------- list --
  bindList() {
    const list = this.els.list;
    const rowIdx = (el) => Number(el.closest(".track")?.dataset.idx);

    list.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-act]");
      if (!btn) return;
      const idx = rowIdx(btn);
      const it = this.items[idx];
      switch (btn.dataset.act) {
        case "play": return this.togglePlayTrack(it);
        case "toggle": it.enabled = !it.enabled && it.track.matched; return this.changed();
        case "up": return this.move(idx, idx - 1);
        case "down": return this.move(idx, idx + 1);
        case "swap": return this.toggleSwap(it.track);
        case "alt-play": return this.togglePlayUrl(btn.dataset.url, btn.dataset.key);
        case "alt-use": return this.chooseAlternative(it, btn.dataset.key);
        case "reset": it.custom = false; it.start = 0; it.length = Math.min(this.clipDefault, it.track.seconds); return this.changed();
      }
    });

    list.addEventListener("change", (e) => {
      if (e.target.matches("input[type=file]")) {
        const it = this.items[rowIdx(e.target)];
        if (e.target.files[0]) this.uploadFor(it, e.target.files[0]);
        return;
      }
      const field = e.target.dataset.field;
      if (!field) return;
      const it = this.items[rowIdx(e.target)];
      const val = parseTime(e.target.value);
      const max = it.track.seconds || 30;
      if (isNaN(val)) { e.target.classList.add("invalid"); return; }
      if (field === "start") it.start = Math.max(0, Math.min(val, max - 1));
      if (field === "length") it.length = Math.max(1, val);
      it.length = Math.min(it.length, max - it.start);
      it.custom = true;
      this.changed();
    });

    // Drag to reorder (handle only). Touch screens use the ↑ ↓ buttons.
    list.addEventListener("mousedown", (e) => {
      const row = e.target.closest(".track");
      if (row) row.draggable = !!e.target.closest(".handle");
    });
    list.addEventListener("dragstart", (e) => {
      const row = e.target.closest(".track");
      this.dragIdx = Number(row.dataset.idx);
      row.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", row.dataset.idx);
    });
    list.addEventListener("dragover", (e) => {
      const row = e.target.closest(".track");
      if (!row || this.dragIdx == null) return;
      e.preventDefault();
      list.querySelectorAll(".drop-before,.drop-after").forEach((r) => r.classList.remove("drop-before", "drop-after"));
      const r = row.getBoundingClientRect();
      row.classList.add(e.clientY < r.top + r.height / 2 ? "drop-before" : "drop-after");
    });
    list.addEventListener("drop", (e) => {
      const row = e.target.closest(".track");
      if (!row || this.dragIdx == null) return;
      e.preventDefault();
      let to = Number(row.dataset.idx);
      if (row.classList.contains("drop-after")) to += 1;
      if (to > this.dragIdx) to -= 1;
      this.move(this.dragIdx, to);
    });
    list.addEventListener("dragend", () => {
      this.dragIdx = null;
      list.querySelectorAll(".dragging,.drop-before,.drop-after").forEach((r) => r.classList.remove("dragging", "drop-before", "drop-after"));
      list.querySelectorAll(".track[draggable=true]").forEach((r) => (r.draggable = false));
    });
  }

  move(from, to) {
    if (to < 0 || to >= this.items.length || from === to) return;
    const [it] = this.items.splice(from, 1);
    this.items.splice(to, 0, it);
    this.els.sortSelect.value = "";
    this.changed();
  }

  changed() {
    this.render();
    this.saveDraft();
  }

  // ------------------------------------------------------------- preview --
  togglePlayTrack(it) {
    const key = `track:${it.track.id}:${it.track.version}`;
    if (this.playing?.key === key) return this.stop();
    this.startAudio(api.trackAudioUrl(this.pid, it.track), key, it.start, it.start + it.length);
  }

  togglePlayUrl(url, key) {
    if (this.playing?.key === `alt:${key}`) return this.stop();
    this.startAudio(url, `alt:${key}`, 0, null);
  }

  startAudio(url, key, from, stopAt) {
    this.stop(false);
    this.playing = { key, stopAt, loading: true };
    this.audio.src = url;
    const begin = () => {
      this.audio.currentTime = from;
      this.audio.play().catch(() => this.stop());
      if (this.playing) this.playing.loading = false;
      this.paintPlayButtons();
    };
    this.audio.addEventListener("loadedmetadata", begin, { once: true });
    this.audio.load();
    this.paintPlayButtons();
  }

  stop(repaint = true) {
    this.audio.pause();
    this.playing = null;
    if (repaint) this.paintPlayButtons();
  }

  paintPlayButtons() {
    this.els.list.querySelectorAll("[data-playkey]").forEach((b) => {
      const on = this.playing?.key === b.dataset.playkey;
      b.classList.toggle("playing", on && !this.playing.loading);
      b.classList.toggle("loading", on && !!this.playing.loading);
      b.textContent = on ? "■" : "▶";
    });
  }

  // ----------------------------------------------------------- swap panel --
  async toggleSwap(track) {
    if (this.swap?.tid === track.id) { this.swap = null; return this.render(); }
    this.swap = { tid: track.id, loading: true, options: [], error: null };
    this.render();
    this.els.list.querySelector(".swap-panel")?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    try {
      const data = await api.alternatives(this.pid, track.id);
      if (this.swap?.tid !== track.id) return;
      this.swap.options = data.options;
    } catch (e) {
      if (this.swap?.tid === track.id) this.swap.error = e.message;
    }
    if (this.swap?.tid === track.id) {
      this.swap.loading = false;
      this.render();
      this.els.list.querySelector(".swap-panel")?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }

  replaceTrack(it, updated) {
    it.track = { ...it.track, ...updated };
    it.enabled = true;
    it.start = 0;
    it.length = Math.min(it.custom ? it.length : this.clipDefault, it.track.seconds);
    this.stop();
  }

  async chooseAlternative(it, key) {
    this.swap.busy = true;
    this.render();
    try {
      this.replaceTrack(it, await api.chooseSource(this.pid, it.track.id, key));
      this.swap = null;
    } catch (e) {
      this.swap.error = e.message;
      this.swap.busy = false;
    }
    this.changed();
  }

  async uploadFor(it, file) {
    this.swap = { ...(this.swap || { tid: it.track.id, options: [] }), busy: true, error: null };
    this.render();
    try {
      this.replaceTrack(it, await api.upload(this.pid, it.track.id, file));
      it.custom = true; // a full song upload almost always needs a hand-picked start
      it.length = Math.min(this.clipDefault, it.track.seconds);
      this.swap = null;
      this.toast(`Using your file (${formatDuration(it.track.seconds)} long). Set Start to the part you want, e.g. 1:23.`);
    } catch (e) {
      this.swap.error = e.message;
      this.swap.busy = false;
    }
    this.changed();
  }

  toast(msg) {
    this.els.toast.textContent = msg;
    this.els.toast.classList.add("show");
    clearTimeout(this.toastTimer);
    this.toastTimer = setTimeout(() => this.els.toast.classList.remove("show"), 6000);
  }

  // ------------------------------------------------------------ rendering --
  updateSummary() {
    const chosen = this.items.filter((it) => it.enabled && it.track.matched);
    const total = estimateMix(chosen.map((it) => it.length), this.fade);
    const over = total > this.maxSeconds;
    const unmatched = this.items.filter((it) => !it.track.matched).length;
    this.els.summary.classList.toggle("over", over);
    this.els.summary.innerHTML =
      `<b>${chosen.length}</b> clips · <b>${formatDuration(total)}</b> of max ${formatDuration(this.maxSeconds)}` +
      (unmatched ? ` · ${unmatched} not found` : "");
    this.els.renderBtn.disabled = over || !chosen.length;
    this.els.renderBtn.textContent = over ? "Too long: trim or remove clips" : `Render ${formatDuration(total)} mix`;
  }

  rowHtml(it, idx, pos) {
    const t = it.track;
    const chips = [];
    if (!t.matched) chips.push(`<span class="chip bad">no clip found: use ⇄</span>`);
    else chips.push(`<span class="chip src-${esc(t.source)}">${esc(SOURCE_LABEL[t.source] || t.source)}</span>`);
    if (t.genre) chips.push(`<span class="chip">${esc(t.genre)}</span>`);
    if (t.bpm) chips.push(`<span class="chip">${Math.round(t.bpm)} bpm</span>`);
    if (t.release_date) chips.push(`<span class="chip">${esc(t.release_date.slice(0, 4))}</span>`);
    if (t.matched && t.matched_title && t.matched_title.toLowerCase() !== (t.title || "").toLowerCase())
      chips.push(`<span class="chip" title="Clip source title">↳ ${esc(t.matched_title)}</span>`);

    const playKey = `track:${t.id}:${t.version}`;
    const max = t.seconds || 30;
    const cls = ["track", !t.matched ? "unmatched" : "", !it.enabled ? "off" : ""].join(" ");
    return `
      <li class="${cls}" data-idx="${idx}">
        <span class="handle" title="Drag to move">⋮⋮</span>
        <button class="icon-btn" data-act="play" data-playkey="${playKey}" title="Listen to this clip" ${t.matched ? "" : "disabled"}>▶</button>
        <span class="pos">${pos}</span>
        <div class="info">
          <div class="title">${esc(t.title)}</div>
          <div class="artist">${esc(t.artist)}</div>
          <div class="chips">${chips.join("")}</div>
        </div>
        <div class="trim" title="Clip has ${formatDuration(max)} of audio">
          <span>start</span><input data-field="start" value="${fmtStart(it.start)}" inputmode="decimal" ${t.matched ? "" : "disabled"}>
          <span>len</span><input data-field="length" value="${Math.round(it.length * 10) / 10}" inputmode="decimal" ${t.matched ? "" : "disabled"}>
          <button class="mini-btn" data-act="reset" title="Back to the default clip length" style="visibility:${it.custom ? "visible" : "hidden"}">↺</button>
        </div>
        <div class="row-actions">
          <button class="icon-btn" data-act="up" title="Move up">↑</button>
          <button class="icon-btn" data-act="down" title="Move down">↓</button>
          <button class="icon-btn" data-act="swap" title="Pick a different clip / upload your own">⇄</button>
          <button class="icon-btn" data-act="toggle" title="${it.enabled ? "Leave out of the mix" : "Put back in the mix"}" ${t.matched ? "" : "disabled"}>${it.enabled ? "✕" : "+"}</button>
        </div>
        ${this.swap?.tid === t.id ? this.swapHtml(t) : ""}
      </li>`;
  }

  swapHtml(t) {
    const s = this.swap;
    const q = encodeURIComponent(`${t.artist} ${t.title}`);
    let body;
    if (s.loading) body = `<p class="swap-note">Looking for other versions…</p>`;
    else if (!s.options.length) body = `<p class="swap-note">No other versions found on Deezer or iTunes.</p>`;
    else body = `<ul class="swap-options">${s.options.map((o) => `
        <li>
          <button class="icon-btn" data-act="alt-play" data-url="${esc(o.preview_url)}" data-key="${esc(o.key)}" data-playkey="alt:${esc(o.key)}" title="Listen">▶</button>
          <div class="info"><div class="title">${esc(o.title)}</div>
            <div class="artist">${esc(o.artist)}${o.album ? ` · ${esc(o.album)}` : ""} · ${esc(SOURCE_LABEL[o.source] || o.source)}</div></div>
          ${o.current ? `<span class="current">current</span>` : `<button class="mini-btn primary" data-act="alt-use" data-key="${esc(o.key)}" ${s.busy ? "disabled" : ""}>Use this</button>`}
        </li>`).join("")}</ul>`;

    return `
      <div class="swap-panel">
        <h4>Other clips for “${esc(t.title)}”</h4>
        ${s.error ? `<div class="error-box show">${esc(s.error)}</div>` : ""}
        ${body}
        <div class="swap-extra">
          <a href="https://www.youtube.com/results?search_query=${q}" target="_blank" rel="noopener">Find it on YouTube ↗</a>
          <label class="file-label">${s.busy ? "Uploading…" : "Upload your own audio file"}
            <input type="file" accept="audio/*,.mp3,.m4a,.wav,.flac,.ogg,.opus" ${s.busy ? "disabled" : ""}>
          </label>
        </div>
        <p class="swap-note">Previews are fixed 30-second snippets. Want a different part of the song?
          Upload a copy you own, then type where it should start in <b>start</b> (e.g. 1:23). Use YouTube to find the timestamp.</p>
      </div>`;
  }

  render() {
    const f = this.filter;
    const rows = [];
    let pos = 0;
    this.items.forEach((it, idx) => {
      if (it.enabled && it.track.matched) pos += 1;
      const shownPos = it.enabled && it.track.matched ? pos : "–";
      if (f && !`${it.track.title} ${it.track.artist} ${it.track.genre || ""}`.toLowerCase().includes(f)) return;
      rows.push(this.rowHtml(it, idx, shownPos));
    });
    this.els.list.innerHTML = rows.join("") || `<li class="track"><span></span><span></span><span></span><div class="artist">No tracks match “${esc(f)}”.</div></li>`;
    this.paintPlayButtons();
    this.updateSummary();
  }
}
