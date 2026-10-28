// The dashboard's state (a zustand store) and every call to the server. Components read it through selectors,
// `useClipit((s) => s.tab)`, so each re-renders only when what it reads changes. State is never mutated in place:
// actions replace what they change with set().
// Everything the API returns (stream titles, AI text, hashtags) was written by strangers: components render it as
// React text only, never as HTML (src/safety.test.ts checks), and only make links from http(s) URLs.
import { toast } from "sonner";
import { create } from "zustand";

export const PLATFORMS = { tiktok: "TikTok", youtube: "YouTube" } as const;
export type Platform = keyof typeof PLATFORMS;
export type Verdict = "keep" | "skip";
export type Upload = { url: string | null; at: string; status: "uploading" | "done" };
export type Clip = {
  id: number;
  vod_id: string;
  idx: number;
  file: string;
  start_s: number;
  end_s: number;
  title: string;
  hook: string;
  caption: string;
  youtube: string;
  trend: string;
  sound: string;
  score: number;
  why: string;
  verdict: Verdict | null;
  edited: number;
  created_at: string;
  vod_url: string | null;
  streamer: string;
  uploads: Partial<Record<Platform, Upload>>;
};
export type Vod = {
  id: string;
  url: string;
  streamer: string;
  tier: string;
  status: string;
  attempts: number;
  error: string | null;
  claimed_at: string | null;
  running_at: string | null; // a run started by hand is using its files
  finished_at: string | null;
};
export type Data = { vods: Vod[]; clips: Clip[] };
// A run of clipit --poll, then --upload, started from the page (clipit/runs.py). It goes on without the page or the
// dashboard, so a reopened page picks it up. Lines are clipit's output: untrusted.
export type Step = { name: string; status: "waiting" | "running" | "done" | "failed" | "stopped" | "interrupted" };
export type Run = {
  id: number;
  running: boolean;
  stopping: boolean; // Stop was asked: a YouTube post finishes first
  started: string | null;
  ended: string | null;
  steps: Step[];
  lines: string[];
};

export const posted = (c: Clip) => Object.keys(c.uploads).length;
// clips made before descriptions only have the caption; once the user edits, an empty description is their choice
export const description = (c: Clip) => (c.edited ? c.youtube : c.youtube || c.caption);

export const VIEWS = {
  ready: ["Ready to post", (c: Clip) => c.verdict === "keep" && posted(c) < Object.keys(PLATFORMS).length],
  all: ["All", () => true],
  review: ["To review", (c: Clip) => !c.verdict],
  kept: ["Kept", (c: Clip) => c.verdict === "keep" && !posted(c)], // what's left to post; posted ones are under Posted
  posted: ["Posted", (c: Clip) => posted(c) > 0],
  skip: ["Skipped", (c: Clip) => c.verdict === "skip"],
} as const satisfies Record<string, readonly [string, (c: Clip) => boolean]>;
export type View = keyof typeof VIEWS;
export const LIBRARY_VIEWS: View[] = ["all", "review", "kept", "posted", "skip"]; // "ready" is the Post tab
export const SORTS = {
  new: (a: Clip, b: Clip) => b.created_at.localeCompare(a.created_at) || a.idx - b.idx,
  score: (a: Clip, b: Clip) => b.score - a.score || b.created_at.localeCompare(a.created_at),
  streamer: (a: Clip, b: Clip) => a.streamer.localeCompare(b.streamer) || b.score - a.score,
};
export type Sort = keyof typeof SORTS;
// in the order the work goes: decide, post, look back
export const TABS = { run: "Run", review: "Review", post: "Post", library: "Library", vods: "VODs" } as const;
export type Tab = keyof typeof TABS;

export const POLL_MS = 5000;
export const RUN_MS = 2000; // a run's output, close to live
const FETCH_MS = 15000; // every request has a deadline, so a stalled server shows an error instead of hanging
const POST_MS = 15 * 60000; // posting to YouTube sends the whole video from this PC
export const DELETE_NOTE = "The video is deleted and clipit forgets it. Anything already posted stays up. This can't be undone."; // every clip, any status

const hashTab = globalThis.location?.hash.slice(1);
const initial = {
  data: { vods: [], clips: [] } as Data,
  loaded: false,
  etag: "",
  status: { text: "Loading…", bad: false },
  tab: (hashTab && hashTab in TABS ? hashTab : "review") as Tab, // a reload comes back to this tab
  current: null as number | null, // the Post tab's clip; null (or one that left the queue) means the first in the queue
  view: "all" as View, // the Library's filter
  q: "",
  streamer: "",
  sort: "new" as Sort,
  sheet: null as number | null, // clip open in the side sheet, over any tab
  released: [] as number[], // clips whose videos every player let go of, so they can be deleted (Windows locks open files)
  drafts: {} as Record<string, string>, // text typed but not saved yet: "id:platform" post links, "id:text:field" post text
  textStatus: {} as Record<string, string>, // "id:text:field" -> what its autosave says, so a re-render never loses an error
  queue: [] as number[], // review deck: unreviewed clip ids in random order, queue[0] is the card on top
  history: [] as { id: number; verdict: Verdict }[], // one per decision made in the deck this visit, newest last, for undo
  inflight: new Map<number, number>(), // id -> verdict writes pending: a poll that raced the save mustn't re-deal or drop them
  deckShown: null as number | null, // clip id of the top card now on screen
  flinging: false,
  muted: false,
  run: null as Run | null, // the dashboard's latest run, null until the first answer
};
export type State = typeof initial;

export const useClipit = create<State>()(() => initial);
const get = useClipit.getState;
export const set = useClipit.setState;

const without = <T>(o: Record<string, T>, key: string) => {
  const rest = { ...o };
  delete rest[key];
  return rest;
};
export const setDraft = (key: string, text: string) => set((s) => ({ drafts: { ...s.drafts, [key]: text } }));
const dropDraft = (key: string) => set((s) => ({ drafts: without(s.drafts, key) }));

export function setStatus(text: string, bad = false): void {
  const { status } = get();
  if (status.text !== text || status.bad !== bad) set({ status: { text, bad } }); // polling: don't re-render for nothing
}

export const clipOf = (clips: Clip[], id: number | null | undefined) => clips.find((c) => c.id === id);

// a background tab doesn't need fresh data
export const load = (): Promise<void> => (globalThis.document?.hidden ? Promise.resolve() : fetchData());

async function fetchData(): Promise<void> {
  try {
    const { etag } = get();
    const r = await fetch("/api/data", { headers: etag ? { "If-None-Match": etag } : {}, signal: AbortSignal.timeout(FETCH_MS) });
    if (r.status !== 304) {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = (await r.json()) as Data;
      const streamer = streamers(data.clips).includes(get().streamer) ? get().streamer : "";
      set({ data, etag: r.headers.get("ETag") || "", loaded: true, streamer });
      deal();
    }
    setStatus(""); // only say something when something is wrong
  } catch {
    setStatus("Can't reach the dashboard. Is clipit --dashboard still running?", true);
  }
}

async function post(path: string, body: unknown, ms = FETCH_MS): Promise<Record<string, unknown>> {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(ms),
  });
  const reply = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(reply.error || `HTTP ${r.status}`);
  return reply;
}

export async function save(path: string, body: unknown): Promise<Record<string, unknown> | false> {
  let reply;
  try {
    reply = await post(path, body);
  } catch (e) {
    setStatus(`Couldn't save (${(e as Error).message})`, true);
    return false;
  }
  await load();
  return reply;
}

// What a run made: clips found while it ran, and its posts to YouTube. Server times are all UTC ISO, so they sort as text.
export function runResult(run: Run, clips: Clip[]): { clips: number; posted: number } {
  const since = (at: string | undefined) => !!at && !!run.started && at >= run.started && (!run.ended || at <= run.ended);
  return {
    clips: clips.filter((c) => since(c.created_at)).length,
    posted: clips.filter((c) => c.uploads.youtube?.status === "done" && since(c.uploads.youtube.at)).length,
  };
}

const plural = (n: number, what: string) => `${n} ${what}${n === 1 ? "" : "s"}`;

// Polled even in a background tab, so a run that ends while you're elsewhere still tells you when you come back.
let runAsks = 0;
let runShown = 0; // the newest ask whose answer is shown
export async function loadRun(): Promise<void> {
  const ask = ++runAsks;
  let run: Run;
  try {
    const r = await fetch("/api/run", { signal: AbortSignal.timeout(FETCH_MS) });
    if (!r.ok) return; // load() says when the server is gone
    run = (await r.json()) as Run;
  } catch {
    return;
  }
  if (ask < runShown) return; // a newer answer is shown: this older one could put a finished run back to running
  runShown = ask;
  const before = get().run;
  if (JSON.stringify(before) === JSON.stringify(run)) return; // polling: don't re-render for nothing
  set({ run });
  if (!before?.running || before.id !== run.id || run.running) return; // only the end of a run this page watched
  await fetchData(); // the clips it made, even in a background tab
  const made = runResult(run, get().data.clips);
  const failed = run.steps.filter((s) => s.status === "failed" || s.status === "interrupted").map((s) => s.name);
  const stopped = run.steps.some((s) => s.status === "stopped");
  const text = `${plural(made.clips, "new clip")}, ${made.posted} posted to YouTube`;
  const open = { label: "See the run", onClick: () => showTab("run") };
  const opts = { description: text, duration: Infinity, closeButton: true, action: open };
  if (failed.length) toast.error(`Run finished, but this failed: ${failed.join(", ")}`, opts);
  else if (stopped) toast(`Run stopped`, opts);
  else toast.success("Run finished", { ...opts, action: made.clips ? { label: "Review", onClick: () => showTab("review") } : open });
}

export async function startRun(): Promise<void> {
  try {
    await post("/api/run", { action: "start" });
  } catch (e) {
    toast.error(`Couldn't start a run: ${(e as Error).message}`);
  }
  await loadRun();
}

export async function stopRun(): Promise<void> {
  try {
    await post("/api/run", { action: "stop" });
  } catch (e) {
    toast.error(`Couldn't stop the run: ${(e as Error).message}`);
  }
  await loadRun();
}

export const reveal = (clip: number) => post("/api/reveal", { clip });
// Posts the clip to the channel clipit --youtube-login signed in to, through the server (the code --upload runs).
export const postYoutube = (clip: number) => post("/api/youtube", { clip }, POST_MS);

// Verdict writes go out one at a time, in order: a quick Keep then Undo must land as Keep then Undo.
let verdicts: Promise<unknown> = Promise.resolve();
export function saveVerdict(clip: number, verdict: Verdict | null): Promise<Record<string, unknown> | false> {
  const done = verdicts.then(() => save("/api/verdict", { clip, verdict }));
  verdicts = done;
  return done;
}
export const verdictsSettled = () => verdicts;

export function showTab(tab: Tab): void {
  globalThis.history.replaceState(null, "", `#${tab}`);
  set({ tab });
}

export const openClip = (id: number | null) => set({ sheet: id });

// Library

export const streamers = (clips: Clip[]) => [...new Set(clips.map((c) => c.streamer).filter(Boolean))].sort();

// The clips the Library shows. A selector: use it with useShallow, it builds a new array on every call.
export function visible(s: State = get()): Clip[] {
  const q = s.q.trim().toLowerCase();
  return s.data.clips
    .filter((c) => VIEWS[s.view][1](c) && (!s.streamer || c.streamer === s.streamer))
    .filter((c) => !q || `${c.title} ${c.caption} ${c.streamer}`.toLowerCase().includes(q))
    .sort(SORTS[s.sort]);
}

// The VODs tab's search: id, streamer, status or error, ignoring case and outer spaces.
export function searchVods(vods: Vod[], q: string): Vod[] {
  const needle = q.trim().toLowerCase();
  return needle ? vods.filter((v) => `${v.id} ${v.streamer} ${v.status} ${v.error ?? ""}`.toLowerCase().includes(needle)) : vods;
}

// Post: kept clips not yet on every platform, newest first. Selectors, like visible().
export const postQueue = (s: State = get()) => s.data.clips.filter(VIEWS.ready[1]).sort(SORTS.new);
export const currentPost = (s: State = get()) => {
  const queue = postQueue(s);
  return clipOf(queue, s.current) ?? queue[0];
};

export async function markPosted(c: Clip, platform: Platform, url: string): Promise<void> {
  const order = postQueue().map((x) => x.id);
  const { current } = get();
  const posting = get().tab === "post" && currentPost()?.id === c.id;
  if (await save("/api/upload", { clip: c.id, platform, url })) {
    dropDraft(`${c.id}:${platform}`); // a failed save keeps the link
    // A clip that left the queue hands over to the one after it, so posting is a run. Only if the user is still on
    // it: a slow save mustn't replace a clip they picked since.
    const left = new Set(postQueue().map((x) => x.id));
    if (posting && get().current === current && !left.has(c.id)) {
      const i = order.indexOf(c.id);
      set({ current: [...order.slice(i + 1), ...order.slice(0, i).reverse()].find((x) => left.has(x)) ?? null });
    }
  }
}

// Every player lets go of these clips' videos (Windows can't delete a file that's being read); [] puts them back.
export const release = (ids: number[]) => set({ released: ids });

// Removes the clip from the catalog and deletes its video. The caller has already released it.
export async function deleteClip(id: number): Promise<void> {
  if (await save("/api/delete", { clip: id })) set((s) => ({ sheet: s.sheet === id ? null : s.sheet }));
  release([]); // so a failed delete puts the players back
}

// Deletes every skipped clip (the caller has released them). Returns how many videos couldn't be deleted: open elsewhere.
export async function deleteSkipped(): Promise<number> {
  const reply = await save("/api/delete-skipped", { skipped: true });
  release([]);
  return reply && typeof reply.failed === "number" ? reply.failed : 0;
}

// Deletes the VOD and every file of it (chat, transcripts, audio...) but the clip videos: its clips stay.
export const deleteVod = (id: string) => save("/api/delete-vod", { vod: id });

// Post text autosave: one ordered save per clip field.
const running = new Map<string, Promise<boolean>>();
export const textStatus = (key: string) => get().textStatus[key] ?? "Saved automatically";

// Keep the latest draft until its exact value has reached the database. New input during a save is sent next.
export function saveText(clip: number, field: string): Promise<boolean> {
  const key = `${clip}:text:${field}`;
  const say = (text: string) => set((s) => ({ textStatus: { ...s.textStatus, [key]: text } }));
  const already = running.get(key);
  if (already) return already;
  const write = Promise.resolve()
    .then(async () => {
      while (key in get().drafts) {
        const text = get().drafts[key];
        if (field === "title" && !text.trim()) {
          say("Title is required");
          return false;
        }
        say("Saving…");
        try {
          await post("/api/text", { clip, field, text });
        } catch (e) {
          setStatus(`Couldn't save (${(e as Error).message})`, true);
          say("Couldn't save — edit or leave the field to retry");
          return false;
        }
        // The server has it. Show the saved field now, *then* refresh: if the refresh fails (or a hidden tab skips
        // it) the field still shows what was saved, not old text that could get saved over the edit; when it works,
        // the server's data replaces this, so overlapping saves of other fields can't be undone by it. Like
        // db.set_text, a never-edited clip's empty description takes its caption (the one before this save); once
        // edited, an empty description is the user's choice and stays empty.
        const saved = (x: Clip) => ({ ...x, youtube: !x.edited && !x.youtube ? x.caption : x.youtube, [field]: text, edited: 1 });
        set((s) => ({ data: { ...s.data, clips: s.data.clips.map((x) => (x.id === clip ? saved(x) : x)) } }));
        await load();
        if (get().drafts[key] === text) dropDraft(key);
      }
      say("Saved automatically");
      return true;
    })
    .finally(() => running.delete(key));
  running.set(key, write);
  return write;
}

// Saves every field's latest draft, false when one can't be saved. Used before posting, so the post has the latest text.
export async function flushTexts(clip: number, fields: string[]): Promise<boolean> {
  const busy = (field: string) => `${clip}:text:${field}` in get().drafts || running.has(`${clip}:text:${field}`);
  do {
    const results = await Promise.all(fields.map((field) => saveText(clip, field)));
    if (results.some((ok) => !ok)) return false;
    // A field can be edited again after its save finishes while the other field is still saving.
  } while (fields.some(busy));
  return true;
}

// Review deck

function shuffle<T>(a: T[]): T[] {
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

// Brings the deck in line with the data: drops clips rated elsewhere (Clips tab, another window), deals new ones.
export function deal(): void {
  const { data, inflight, deckShown } = get();
  const todo = new Set(data.clips.filter((c) => !c.verdict).map((c) => c.id));
  const queue = get().queue.filter((id) => todo.has(id) || inflight.has(id));
  const dealt = new Set(queue);
  queue.push(...shuffle([...todo].filter((id) => !dealt.has(id) && !inflight.has(id)))); // new clips join at random spots in the back
  const top = clipOf(data.clips, queue[0]);
  // a new card on top: the last one has finished flying away
  set(!top || top.id !== deckShown ? { queue, deckShown: top ? top.id : null, flinging: false } : { queue });
}

// The top card may start flying away: false while one already is, or when there's no card.
export function startFling(): boolean {
  const { flinging, data, queue } = get();
  if (flinging || !clipOf(data.clips, queue[0])) return false;
  set({ flinging: true });
  return true;
}

// A clip can have a Keep and its Undo pending at once: count its writes, so the first to finish doesn't unmark it.
function pending(id: number, n: number): void {
  const inflight = new Map(get().inflight);
  const left = (inflight.get(id) || 0) + n;
  if (left > 0) inflight.set(id, left);
  else inflight.delete(id);
  set({ inflight });
}

export async function decide(verdict: Verdict): Promise<void> {
  const [id, ...rest] = get().queue;
  if (id === undefined) return;
  const entry = { id, verdict }; // its own object: a failed save removes exactly this decision, never a later one for the same clip
  set((s) => ({ queue: rest, history: [...s.history, entry] }));
  pending(id, 1);
  deal();
  const ok = await saveVerdict(id, verdict);
  pending(id, -1);
  if (!ok && get().history.includes(entry)) {
    // still the reviewer's latest word on this clip (not undone since): take it back and put the card on top
    set((s) => ({ history: s.history.filter((e) => e !== entry), queue: s.queue.includes(id) ? s.queue : [id, ...s.queue] })); // the status bar says why
    deal();
  }
}

export async function undo(): Promise<void> {
  const { flinging, history } = get();
  if (flinging || !history.length) return; // mid-fling, decide() is about to take queue[0]
  const entry = history[history.length - 1];
  const { id } = entry;
  pending(id, 1);
  set((s) => ({ history: s.history.slice(0, -1), queue: [id, ...s.queue] }));
  deal();
  const ok = await saveVerdict(id, null);
  pending(id, -1);
  if (!ok) {
    set((s) => ({ queue: s.queue.filter((x) => x !== id), history: [...s.history, entry] }));
    deal();
  }
}
