// Ways to reorder the editor list. Each entry gets one option in the "Arrange"
// dropdown. To add one, append { key, label, sort } - `sort` receives a copy of
// the items array (each item has .track with title/artist/bpm/genre/release_date/
// original_index) and returns it in the new order. Delete an entry to remove it.

const byKey = (getKey, dir = 1) => (a, b) => {
  const ka = getKey(a), kb = getKey(b);
  const missingA = ka == null || ka === "", missingB = kb == null || kb === "";
  if (missingA !== missingB) return missingA ? 1 : -1; // unknowns always sink to the bottom
  if (missingA) return a.track.original_index - b.track.original_index;
  if (ka < kb) return -dir;
  if (ka > kb) return dir;
  return a.track.original_index - b.track.original_index; // stable tiebreak
};

const year = (it) => (it.track.release_date ? parseInt(it.track.release_date.slice(0, 4), 10) : null);
const genre = (it) => (it.track.genre ? it.track.genre.toLowerCase() : null);
const bpm = (it) => it.track.bpm || null;

function shuffle(items) {
  for (let i = items.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [items[i], items[j]] = [items[j], items[i]];
  }
  return items;
}

function groupBy(items, getKey) {
  const groups = new Map();
  for (const it of items) {
    const k = getKey(it) ?? "￿"; // unknown group
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(it);
  }
  return groups;
}

// Greedy "smooth tempo" chain: start at the slowest song, always step to the
// unused song with the closest BPM. Gives gentle tempo changes between clips.
function bpmFlow(items) {
  const known = items.filter(bpm);
  const unknown = items.filter((it) => !bpm(it));
  if (!known.length) return items;
  known.sort(byKey(bpm));
  const out = [known.shift()];
  while (known.length) {
    const last = bpm(out[out.length - 1]);
    let best = 0;
    for (let i = 1; i < known.length; i++) {
      if (Math.abs(bpm(known[i]) - last) < Math.abs(bpm(known[best]) - last)) best = i;
    }
    out.push(known.splice(best, 1)[0]);
  }
  return out.concat(unknown);
}

export const SORTERS = [
  { key: "original", label: "Original playlist order", sort: (items) => items.sort(byKey((it) => it.track.original_index)) },
  { key: "genre", label: "Genre (grouped A→Z)", sort: (items) => items.sort(byKey(genre)) },
  { key: "genre-shuffle", label: "Genre blocks, shuffled", random: true,
    sort: (items) => shuffle([...groupBy(shuffle(items), genre).values()]).flat() },
  { key: "bpm-flow", label: "BPM flow (smooth tempo)", sort: bpmFlow },
  { key: "bpm-asc", label: "BPM: slow → fast", sort: (items) => items.sort(byKey(bpm)) },
  { key: "bpm-desc", label: "BPM: fast → slow", sort: (items) => items.sort(byKey(bpm, -1)) },
  { key: "year-asc", label: "Release year: old → new", sort: (items) => items.sort(byKey(year)) },
  { key: "year-desc", label: "Release year: new → old", sort: (items) => items.sort(byKey(year, -1)) },
  { key: "artist", label: "Artist A→Z", sort: (items) => items.sort(byKey((it) => it.track.artist?.toLowerCase())) },
  { key: "title", label: "Title A→Z", sort: (items) => items.sort(byKey((it) => it.track.title?.toLowerCase())) },
  { key: "shuffle", label: "Shuffle", random: true, sort: shuffle },
  { key: "reverse", label: "Reverse current order", sort: (items) => items.reverse() },
];
