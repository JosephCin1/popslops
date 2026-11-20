// Where the backend lives. Change this one line to point at a different server.
// Running the backend locally? Open the page as index.html?api=http://localhost:8000
const fromQuery = new URLSearchParams(window.location.search).get("api");
export const API_BASE = fromQuery || window.API_BASE_URL || "https://popslop.onrender.com";

// Defaults for the editor sliders.
export const DEFAULT_CLIP_SECONDS = 30;
export const DEFAULT_FADE_SECONDS = 0.5;
