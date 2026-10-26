// The deck's decide / undo bookkeeping and the post text autosave, against a fake server.
// Nothing here waits a fixed time or a fixed number of ticks: how long a save's follow-up work takes differs between
// machines (CI caught one). Tests await the actions themselves, or vi.waitFor the request they expect.
import { afterEach, expect, test, vi } from "vitest";
import { type Clip, deal, decide, flushTexts, saveText, set, setDraft, startFling, textStatus, undo, useClipit } from "./store";

const drafts = () => useClipit.getState().drafts;

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
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
async function deck(plan: { fail?: boolean; ms?: number }[], steps: () => unknown[]) {
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
  await Promise.all(steps()); // each decide() / undo() resolves once its save and its follow-up are done
  expect(calls.every((c) => c.afterEarlier)).toBe(true); // one verdict write at a time
  expect(useClipit.getState().inflight.size).toBe(0); // none left marked pending
  return { sent: calls.map((c) => c.verdict), history: useClipit.getState().history.map((e) => e.id), queue: useClipit.getState().queue };
}
const fling = (verdict: "keep" | "skip") => startFling() && decide(verdict); // a key press: no animation

test("keep, undo, keep again with the first save failing late: the later keep and its undo entry survive", async () => {
  const got = await deck([{ fail: true, ms: 80 }], () => [fling("keep"), undo(), fling("keep")]);
  expect(got).toEqual({ sent: ["keep", null, "keep"], history: [1], queue: [2] });
});

test("a lone failed keep puts the card back on top, with nothing to undo", async () => {
  expect(await deck([{ fail: true }], () => [fling("keep")])).toEqual({ sent: ["keep"], history: [], queue: [1, 2] });
});

test("when undo's save fails, the keep stays decided and undoable", async () => {
  const got = await deck([{}, { fail: true }], () => [fling("keep"), undo()]);
  expect(got).toEqual({ sent: ["keep", null], history: [1], queue: [2] });
});

test("a quick keep then undo is saved in that order, card back on top", async () => {
  const got = await deck([{ ms: 80 }, {}], () => [fling("keep"), undo()]);
  expect(got).toEqual({ sent: ["keep", null], history: [], queue: [1, 2] });
});

test("a card can't be flung twice while it's flying away", () => {
  set({ data: { vods: [], clips: [clip(1)] }, queue: [1], deckShown: null, flinging: false });
  deal();
  expect(startFling()).toBe(true);
  expect(startFling()).toBe(false);
});

// Text saves: each waits until the test answers it, true for 200 and false for a server error. sent() waits until the
// page has sent the matching request, however many awaits the page needed to get there.
function textServer() {
  const requests: { field: string; text: string; answered: boolean; reply: (ok: boolean) => void }[] = [];
  vi.stubGlobal("fetch", (url: string, opts: RequestInit) => {
    if (url === "/api/data") return Promise.resolve(json({ vods: [], clips: [] }));
    const body = JSON.parse(String(opts.body));
    return new Promise((resolve) => {
      const request = { ...body, answered: false, reply: (ok: boolean) => ((request.answered = true), resolve(ok ? json({ ok: true }) : json({ error: "x" }, 500))) };
      requests.push(request);
    });
  });
  const sent = (text: string) =>
    vi.waitFor(() => {
      const request = requests.find((r) => r.text === text && !r.answered);
      if (!request) throw new Error(`"${text}" wasn't sent`);
      return request;
    });
  return { requests, sent };
}

test("a slow save can't erase newer typing", async () => {
  const { requests, sent } = textServer();
  setDraft("1:text:title", "First");
  const first = saveText(1, "title");
  const slow = await sent("First");
  setDraft("1:text:title", "Latest");
  expect(saveText(1, "title")).toBe(first); // one save at a time per field
  slow.reply(true);
  (await sent("Latest")).reply(true);
  expect(await first).toBe(true);
  expect(requests.map((r) => r.text)).toEqual(["First", "Latest"]);
  expect("1:text:title" in drafts()).toBe(false);
});

test("a saved edit stays in the field even when the refresh after it fails", async () => {
  const { sent } = textServer();
  set({ data: { vods: [], clips: [clip(1)] } });
  vi.stubGlobal("fetch", ((real) => (url: string, opts: RequestInit) => (url === "/api/data" ? Promise.reject(new Error("down")) : real(url, opts)))(fetch));
  setDraft("1:text:title", "New title");
  const saving = saveText(1, "title");
  (await sent("New title")).reply(true);
  expect(await saving).toBe(true);
  expect("1:text:title" in drafts()).toBe(false);
  expect(useClipit.getState().data.clips[0].title).toBe("New title"); // what the field shows once the draft is gone
});

test("after a save the page shows the description the server stored, not one it made up", async () => {
  // db.set_text fills an empty description from the caption as it was *before* the save: both empty, it stays empty
  const server = { ...clip(1), caption: "", youtube: "" };
  set({ data: { vods: [], clips: [server] }, etag: "" });
  vi.stubGlobal("fetch", async (url: string, opts: RequestInit) => {
    if (url === "/api/data") return json({ vods: [], clips: [server] });
    const { field, text } = JSON.parse(String(opts.body));
    Object.assign(server, { youtube: server.youtube || server.caption, [field]: text, edited: 1 });
    return json({ ok: true });
  });
  setDraft("1:text:caption", "New caption");
  expect(await saveText(1, "caption")).toBe(true);
  const shown = useClipit.getState().data.clips[0];
  expect([shown.caption, shown.youtube]).toEqual(["New caption", ""]);
});

test("saves of two fields that overlap both show, whichever finishes first", async () => {
  const server = { ...clip(1), title: "Old title", youtube: "Old description", caption: "Caption" };
  const waiting: Record<string, () => void> = {};
  set({ data: { vods: [], clips: [{ ...server }] }, etag: "" });
  vi.stubGlobal("fetch", async (url: string, opts: RequestInit) => {
    if (url === "/api/data") return json({ vods: [], clips: [{ ...server }] });
    const { field, text } = JSON.parse(String(opts.body));
    await new Promise<void>((done) => (waiting[text] = done)); // the test decides when the server answers
    Object.assign(server, { youtube: server.youtube || server.caption, [field]: text, edited: 1 });
    return json({ ok: true });
  });
  const answer = async (text: string) => (await vi.waitFor(() => waiting[text] ?? Promise.reject(new Error(`${text} not sent`))))();
  setDraft("1:text:title", "New title");
  const title = saveText(1, "title");
  setDraft("1:text:youtube", "New description"); // typed while the title is still saving
  const youtube = saveText(1, "youtube");
  await answer("New description");
  expect(await youtube).toBe(true);
  await answer("New title"); // the title's save finishes last
  expect(await title).toBe(true);
  const shown = useClipit.getState().data.clips[0];
  expect([shown.title, shown.youtube]).toEqual(["New title", "New description"]);
});

test("a failed save keeps the draft and says so, and the next save sends it", async () => {
  const { sent } = textServer();
  setDraft("1:text:youtube", "Keep on failure");
  const failed = saveText(1, "youtube");
  (await sent("Keep on failure")).reply(false);
  expect(await failed).toBe(false);
  expect(drafts()["1:text:youtube"]).toBe("Keep on failure");
  expect(textStatus("1:text:youtube")).toMatch(/Couldn't save/);
  const retry = saveText(1, "youtube");
  (await sent("Keep on failure")).reply(true);
  expect(await retry).toBe(true);
  expect("1:text:youtube" in drafts()).toBe(false);
  expect(textStatus("1:text:youtube")).toBe("Saved automatically");
});

test("an empty title is never sent, an empty description is", async () => {
  const { requests, sent } = textServer();
  setDraft("1:text:title", "   ");
  expect(await saveText(1, "title")).toBe(false);
  expect(drafts()["1:text:title"]).toBe("   ");
  expect(textStatus("1:text:title")).toBe("Title is required");
  expect(requests).toEqual([]);
  setDraft("1:text:youtube", "");
  const empty = saveText(1, "youtube");
  (await sent("")).reply(true);
  expect(await empty).toBe(true);
});

test("before posting, every field's latest draft is saved, even one typed while the other was still saving", async () => {
  const { sent } = textServer();
  setDraft("1:text:title", "First title");
  setDraft("1:text:youtube", "New description");
  let flushed: boolean | null = null;
  const flushing = flushTexts(1, ["title", "youtube"]).then((ok) => (flushed = ok));
  const description = await sent("New description");
  (await sent("First title")).reply(true);
  // the title's save has finished entirely (not just its request) while the description is still saving
  await vi.waitFor(() => expect(textStatus("1:text:title")).toBe("Saved automatically"));
  await vi.waitFor(() => expect("1:text:title" in drafts()).toBe(false));
  setDraft("1:text:title", "Latest title");
  const latest = saveText(1, "title");
  description.reply(true);
  const title = await sent("Latest title");
  expect(flushed).toBe(null); // still waiting for the latest title
  title.reply(true);
  await latest;
  await flushing;
  expect(flushed).toBe(true);

  setDraft("1:text:title", "Failed title");
  const failing = flushTexts(1, ["title", "youtube"]);
  (await sent("Failed title")).reply(false);
  expect(await failing).toBe(false); // the page says so and never asks to post
});
