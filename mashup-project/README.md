# Seam / PopSlop: playlist-to-mashup

Turns a Spotify playlist into one continuous, crossfaded MP3 (up to 12 hours).
Before anything renders you get an editor to reorder, trim, cut and swap clips.

## How it works

```
Spotify playlist URL
      |
      v
[spotify_client.py]  track metadata (title, artist, ISRC)
      |
      v
[projects.py]        match every track to a clip source IN PARALLEL
                     (no audio downloaded yet - fast even for 1,000+ songs)
      |
      v
   EDITOR (frontend)  reorder / sort / trim / remove / swap / upload
                      press ▶ to fetch + hear a single clip on request
      |
      v
[projects.py]        download the chosen clips in parallel
      |
      v
[mixer.py]           parallel decode -> qsin crossfades in numpy ->
                     ONE streaming ffmpeg encode (memory stays flat)
      |
      v
   final_mix.mp3 (served via /api/download/{job_id})
```

### Clip sources (`backend/sources/`)

| Source | Used for | Notes |
|---|---|---|
| `deezer.py` | first choice | ISRC match, then text search. Supplies BPM, genre, release date. |
| `itunes.py` | fallback | Slow (~18 lookups/min), only runs when Deezer misses. |
| `upload.py` | user's own file | Picked per-track in the editor; any section of the song. |

To add or remove a source, edit the `SOURCES` registry in `sources/__init__.py`.
Each source implements `lookup()`, `search()` and `fetch_audio()`.

YouTube isn't a source on purpose: downloading from YouTube breaks its terms of
service, and YouTube blocks yt-dlp from cloud servers like Render anyway. The
editor links out to YouTube so people can *find* the part they want, then
upload their own copy and type the start time.

### Sort options (`frontend/js/sorters.js`)

Original order, genre, genre blocks shuffled, BPM flow (smooth tempo chain),
BPM up/down, release year, artist, title, shuffle, reverse. Each one is a
`{ key, label, sort }` entry, so you can add or remove one by editing that list.

## Backend setup

1. **Install ffmpeg** (the mixer shells out to it): `winget install ffmpeg` /
   `brew install ffmpeg` / `sudo apt install ffmpeg`
2. **Spotify app**: https://developer.spotify.com/dashboard. Add your
   `/api/callback` URL as a redirect URI.
3. **Install and run**
   ```
   cd backend
   python -m venv venv
   venv\Scripts\activate          # macOS/Linux: source venv/bin/activate
   pip install -r requirements.txt
   uvicorn main:app --reload --port 8000
   ```
4. **Offline test** (fakes Spotify and the sources, needs only ffmpeg):
   `python tests/smoke_test.py`

### Environment variables

| Variable | Default | What it does |
|---|---|---|
| `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` | none | Spotify app credentials |
| `SPOTIFY_REDIRECT_URI` | none | e.g. `https://popslop.onrender.com/api/callback` |
| `FRONTEND_URL` | none | where login redirects back to |
| `MAX_MIX_HOURS` | `12` | longest mix a render may produce |
| `MAX_TRACKS` | `3000` | playlist tracks read |
| `RESOLVE_WORKERS` / `DOWNLOAD_WORKERS` | `8` / `8` | parallel lookups / downloads |
| `DECODE_WORKERS` | `2` | parallel ffmpeg decoders while mixing (raise on bigger servers) |
| `MAX_CONCURRENT_RENDERS` | `1` | renders allowed at the same time |
| `MIX_BITRATE` / `MIX_BITRATE_LONG` | `192k` / `128k` | long = mixes over `LONG_MIX_SECONDS` (2 h) |
| `MAX_UPLOAD_MB` | `40` | per-file upload limit |
| `FILE_RETENTION_HOURS` | `24` | projects + mixes are deleted after this |
| `EXPOSE_DOCS` | `0` | set to `1` to get `/docs` back |

## Frontend setup

Static files, no build step: `index.html`, `styles.css`, `js/*.js` (ES modules).
Deploy the **whole `frontend/` folder** to Netlify.

ES modules don't load from `file://`, so serve the folder locally:
```
cd frontend
python -m http.server 5500
```
then open `http://localhost:5500/?api=http://localhost:8000` to point it at a
local backend. The production backend URL lives in `js/config.js`.

## API reference

| Endpoint | Method | Description |
|---|---|---|
| `/api/health` | GET | Wake-up ping |
| `/api/login`, `/api/callback` | GET | Spotify login, gives back a `session_id` |
| `/api/projects` | POST | `{playlist_url, session_id}`: starts matching, returns `project_id` |
| `/api/projects/{pid}` | GET | Poll until `status == "ready"`; includes the track list |
| `/api/projects/{pid}/tracks/{tid}/audio` | GET | One clip, downloaded on first request |
| `/api/projects/{pid}/tracks/{tid}/alternatives` | GET | Other Deezer/iTunes versions |
| `/api/projects/{pid}/tracks/{tid}/source` | PUT | `{key}` from alternatives |
| `/api/projects/{pid}/tracks/{tid}/upload` | POST | multipart `file` |
| `/api/projects/{pid}/render` | POST | `{items:[{track_id,start,length}], fade_duration}` returns `job_id` |
| `/api/jobs/{job_id}` | GET | Render progress |
| `/api/download/{job_id}` | GET | Finished MP3 |
| `/api/jobs` | POST | Old one-shot flow, still supported |

## Known limitations

- **Server size vs 12-hour mixes.** On 2 CPU cores a 12-hour mix renders in
  roughly 7-10 minutes. Render's free instance has 0.1 CPU, which makes that
  well over an hour, and the instance sleeps after 15 idle minutes. For long
  mixes use at least the Starter plan.
- **Output size.** 12 hours at 128 kbps is ~700 MB, so Render's disk must have
  room for the clips plus the mix while a render runs.
- **In-memory state.** Projects and jobs are lost if the server restarts or
  redeploys. Swap in Redis or a DB if that starts to matter.
- **BPM coverage.** Spotify no longer gives new apps audio features, so BPM
  comes from Deezer, which leaves many tracks blank. Those sort to the end.
- **Legal note.** Previews come from Deezer/iTunes' public APIs (no stream
  ripping). Redistributing stitched copyrighted previews is a different
  posture than personal use once other people use the service.
