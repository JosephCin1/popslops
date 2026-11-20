// Thin wrappers around the backend. Every network call in the app goes through here.
import { API_BASE } from "./config.js";

async function request(path, options = {}) {
  const resp = await fetch(`${API_BASE}${path}`, options);
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    const err = new Error(body.detail || `Server error (${resp.status})`);
    err.status = resp.status;
    throw err;
  }
  return resp.json();
}

const json = (method, body) => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export const api = {
  loginUrl: () => `${API_BASE}/api/login`,
  health: () => request("/api/health"),

  createProject: (playlistUrl, sessionId) =>
    request("/api/projects", json("POST", { playlist_url: playlistUrl, session_id: sessionId })),
  getProject: (pid) => request(`/api/projects/${pid}`),

  // URL (not a fetch) so the <audio> element can stream it. `v` busts the
  // browser cache after the track's clip source changes.
  trackAudioUrl: (pid, track) => `${API_BASE}/api/projects/${pid}/tracks/${track.id}/audio?v=${track.version}`,
  alternatives: (pid, tid) => request(`/api/projects/${pid}/tracks/${tid}/alternatives`),
  chooseSource: (pid, tid, key) => request(`/api/projects/${pid}/tracks/${tid}/source`, json("PUT", { key })),
  upload: (pid, tid, file) => {
    const form = new FormData();
    form.append("file", file);
    return request(`/api/projects/${pid}/tracks/${tid}/upload`, { method: "POST", body: form });
  },

  render: (pid, items, fade) =>
    request(`/api/projects/${pid}/render`, json("POST", { items, fade_duration: fade })),
  getJob: (jobId) => request(`/api/jobs/${jobId}`),
  downloadUrl: (path) => `${API_BASE}${path}`,
};
