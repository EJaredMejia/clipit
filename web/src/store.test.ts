// The deck's decide / undo bookkeeping and the post text autosave, against a fake server.
import { afterEach, expect, test, vi } from "vitest";
import { type Clip, deal, decide, flushTexts, saveText, set, setDraft, startFling, textStatus, undo, useClipit, verdictsSettled } from "./store";

const drafts = () => useClipit.getState().drafts;

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const tick = async () => {
  for (let i = 0; i < 20; i++) await Promise.resolve();
};
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const clip = (id: number): Clip => ({
  id, vod_id: "v1", idx: id, file: `v1/clips/${id}.mp4`, start_s: 0, end_s: 20, title: `clip ${id}`, hook: "", caption: "",
  youtube: "", trend: "", sound: "", score: 7, why: "", verdict: null, edited: 0, created_at: "2026-10-24", vod_url: null,
  streamer: "s", uploads: {},
}); // prettier-ignore

afterEach(() => {
  vi.unstubAllGlobals();
  set({ drafts: {} });
});

// A fake server: /api/data serves the clips with the verdicts saved so far. Verdict saves follow plan[i] =
// { fail, ms } for the i-th one, and each records whether every earlier save had finished when it was sent.
async function deck(plan: { fail?: boolean; ms?: number }[], steps: () => void) {
  let clips = [clip(1), clip(2)];
  const calls: { verdict: string | null; afterEarlier: boolean; done: boolean }[] = [];
  vi.stubGlobal("fetch", async (url: string, opts: RequestInit) => {
    if (url === "/api/data") return json({ vods: [], clips });
    const { fail = false, ms = 10 } = plan[calls.length] || {};
    const body = JSON.parse(String(opts.body));
    const call = { verdict: body.verdict, afterEarlier: calls.every((c) => c.done), done: false };
    calls.push(call);
    await sleep(ms);
    call.done = true;
    if (fail) throw new Error("down");
    clips = clips.map((c) => (c.id === body.clip ? { ...c, verdict: body.verdict } : c));
    return json({});
  });
  set({ data: { vods: [], clips }, etag: "", queue: [1, 2], history: [], inflight: new Map(), deckShown: null, flinging: false });
  deal(); // dealt in a known order: both are already in the queue
  steps();
  await sleep(50);
  await verdictsSettled();
  await sleep(50); // decide() / undo() finish after their save settles
  expect(calls.every((c) => c.afterEarlier)).toBe(true); // one verdict write at a time
  expect(useClipit.getState().inflight.size).toBe(0); // none left marked pending
  return { sent: calls.map((c) => c.verdict), history: useClipit.getState().history.map((e) => e.id), queue: useClipit.getState().queue };
}
const fling = (verdict: "keep" | "skip") => startFling() && decide(verdict); // a key press: no animation

test("keep, undo, keep again with the first save failing late: the later keep and its undo entry survive", async () => {
  const got = await deck([{ fail: true, ms: 80 }], () => {
    fling("keep");
    undo();
    fling("keep");
  });
  expect(got).toEqual({ sent: ["keep", null, "keep"], history: [1], queue: [2] });
});

test("a lone failed keep puts the card back on top, with nothing to undo", async () => {
  expect(await deck([{ fail: true }], () => fling("keep"))).toEqual({ sent: ["keep"], history: [], queue: [1, 2] });
});

test("when undo's save fails, the keep stays decided and undoable", async () => {
  const got = await deck([{}, { fail: true }], () => {
    fling("keep");
    undo();
  });
  expect(got).toEqual({ sent: ["keep", null], history: [1], queue: [2] });
});

test("a quick keep then undo is saved in that order, card back on top", async () => {
  const got = await deck([{ ms: 80 }, {}], () => {
    fling("keep");
    undo();
  });
  expect(got).toEqual({ sent: ["keep", null], history: [], queue: [1, 2] });
});

test("a card can't be flung twice while it's flying away", () => {
  set({ data: { vods: [], clips: [clip(1)] }, queue: [1], deckShown: null, flinging: false });
  deal();
  expect(startFling()).toBe(true);
  expect(startFling()).toBe(false);
});

// Text saves: each resolves only when the test says so, true for 200 and false for a server error.
function textServer() {
  const pending: { field: string; text: string; reply: (ok: boolean) => void }[] = [];
  vi.stubGlobal("fetch", (url: string, opts: RequestInit) => {
    if (url === "/api/data") return Promise.resolve(json({ vods: [], clips: [] }));
    const body = JSON.parse(String(opts.body));
    return new Promise((resolve) => pending.push({ ...body, reply: (ok) => resolve(ok ? json({ ok: true }) : json({ error: "x" }, 500)) }));
  });
  return pending;
}

test("a slow save can't erase newer typing", async () => {
  const pending = textServer();
  setDraft("1:text:title", "First");
  const first = saveText(1, "title");
  await tick();
  setDraft("1:text:title", "Latest");
  expect(saveText(1, "title")).toBe(first); // one save at a time per field
  pending[0].reply(true);
  await tick();
  pending[1].reply(true);
  expect(await first).toBe(true);
  expect(pending.map((p) => p.text)).toEqual(["First", "Latest"]);
  expect("1:text:title" in drafts()).toBe(false);
});

test("a failed save keeps the draft and says so, and the next save sends it", async () => {
  const pending = textServer();
  setDraft("1:text:youtube", "Keep on failure");
  const failed = saveText(1, "youtube");
  await tick();
  pending[0].reply(false);
  expect(await failed).toBe(false);
  expect(drafts()["1:text:youtube"]).toBe("Keep on failure");
  expect(textStatus("1:text:youtube")).toMatch(/Couldn't save/);
  const retry = saveText(1, "youtube");
  await tick();
  pending[1].reply(true);
  expect(await retry).toBe(true);
  expect("1:text:youtube" in drafts()).toBe(false);
  expect(textStatus("1:text:youtube")).toBe("Saved automatically");
});

test("an empty title is never sent, an empty description is", async () => {
  const pending = textServer();
  setDraft("1:text:title", "   ");
  expect(await saveText(1, "title")).toBe(false);
  expect(drafts()["1:text:title"]).toBe("   ");
  expect(textStatus("1:text:title")).toBe("Title is required");
  expect(pending).toEqual([]);
  setDraft("1:text:youtube", "");
  const empty = saveText(1, "youtube");
  await tick();
  pending[0].reply(true);
  expect(await empty).toBe(true);
});

test("before posting, every field's latest draft is saved, even one typed while the other was still saving", async () => {
  const pending = textServer();
  setDraft("1:text:title", "First title");
  setDraft("1:text:youtube", "New description");
  let flushed: boolean | null = null;
  const flushing = flushTexts(1, ["title", "youtube"]).then((ok) => (flushed = ok));
  await tick();
  expect(pending.length).toBe(2);
  pending.find((p) => p.field === "title")!.reply(true);
  await tick();
  setDraft("1:text:title", "Latest title");
  const latest = saveText(1, "title");
  await tick();
  pending.find((p) => p.field === "youtube")!.reply(true);
  await tick();
  expect(flushed).toBe(null); // still waiting for the latest title
  pending.find((p) => p.text === "Latest title")!.reply(true);
  await latest;
  await flushing;
  expect(flushed).toBe(true);

  setDraft("1:text:title", "Failed title");
  const failing = flushTexts(1, ["title", "youtube"]);
  await tick();
  pending.at(-1)!.reply(false);
  expect(await failing).toBe(false); // the page says so and never asks to post
});
