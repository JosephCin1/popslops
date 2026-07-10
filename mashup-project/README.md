# Seam — playlist-to-mashup

Turns a public Spotify playlist into a single continuous MP3, stitched from
the best available preview clip of each track (Deezer first, iTunes as a
fallback), crossfaded together with half-second fades.

## How it works

```
Spotify playlist URL
      |
      v
[spotify_client.py]  --> track metadata (title, artist, ISRC)
      |
      v
[audio_sources.py]   --> preview clip URL per track (Deezer -> iTunes fallback)
      |
      v
   download clips to a temp folder
      |
      v
[stitcher.py]         --> one ffmpeg pass, chained crossfades, single MP3 out
      |
      v
   final_mix.mp3 (served via /api/download/{job_id})
```

## Backend setup

1. **Install ffmpeg** (required — the stitcher shells out to it):
   - macOS: `brew install ffmpeg`
   - Ubuntu/Debian: `sudo apt install ffmpeg`
   - Windows: install from ffmpeg.org and add it to PATH

2. **Get Spotify API credentials**
   - Go to https://developer.spotify.com/dashboard -> Create app
   - Copy the Client ID and Client Secret
   - This uses the Client Credentials flow, which only works for **public**
     playlists (no user login needed, but no access to private ones either)

3. **Install dependencies and configure**
   ```
   cd backend
   python -m venv venv
   source venv/bin/activate        # Windows: venv\Scripts\activate
   pip install -r requirements.txt
   cp .env.example .env
   # edit .env and paste in your SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET
   ```

4. **Run it**
   ```
   uvicorn main:app --reload --port 8000
   ```
   The API is now live at `http://localhost:8000`.

## Frontend setup

The frontend is a single static HTML file — no build step.

1. Open `frontend/index.html` directly in a browser, **or** serve it locally:
   ```
   cd frontend
   python -m http.server 5500
   ```
2. If your backend isn't running on `http://localhost:8000`, set the API base
   before the page's script runs, e.g. add this in `index.html`'s `<head>`:
   ```html
   <script>window.API_BASE_URL = "https://your-backend-url.com";</script>
   ```

## API reference

| Endpoint | Method | Description |
|---|---|---|
| `/api/jobs` | POST | Body: `{"playlist_url": "..."}`. Starts a job, returns `job_id`. |
| `/api/jobs/{job_id}` | GET | Poll for status/progress. |
| `/api/download/{job_id}` | GET | Downloads the finished MP3 once `status == "done"`. |

## Known limitations / things to harden before real users touch this

- **In-memory job store** — jobs disappear if the server restarts. Fine for
  local use/demos; swap for Redis or a DB table if you deploy this.
- **No auth on the API** — anyone with the URL can submit jobs. Add rate
  limiting or an API key if this goes anywhere public-facing.
- **30-second preview clips only** — this is a hard ceiling set by Deezer/
  iTunes; the "best part" of a song is whatever falls inside their preview,
  not something detected from the full track.
- **Cleanup runs hourly** — `main.py`'s cleanup loop deletes job folders older
  than `FILE_RETENTION_HOURS`. On a serverless/multi-instance deployment
  you'd want this as a proper cron job instead of an in-process thread.
- **Legal note** — this pulls preview clips that Deezer/iTunes serve
  unauthenticated via their own public APIs, which is different from
  stream-ripping full tracks. That said, redistributing a stitched file
  built from other people's copyrighted previews is a different posture
  than personal/local use the moment other people are uploading playlists
  and downloading output from a service you run — worth keeping in mind if
  you take this past a personal project.

## Where the "editing" features from our design discussion would plug in

This backend currently auto-picks the whole preview clip for each track.
The `Mix` data model we discussed earlier (ordered list of
`{song_id, start, end, transition}`) would slot in between the clip-fetching
step and the stitching step: instead of `stitch_clips()` taking a flat list
of full clip files, it would take a list of `(clip_path, start_sec, end_sec)`
tuples, trim each with ffmpeg's `-ss`/`-t` flags before crossfading, and the
reordering feature would just be reordering that list before the stitch call.
