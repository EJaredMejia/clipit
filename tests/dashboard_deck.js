// Runs clipit/dashboard.js under Node to test the review deck's decide / undo bookkeeping.
// tests/test_dashboard.py builds one file: PRELUDE (this file, up to the SCENARIOS marker), then dashboard.js,
// then SCENARIOS. The page script runs against a stand-in DOM where every element accepts anything, and against a
// fetch the scenarios script: which verdict saves fail and how long each takes.

// A stand-in for any DOM object: every property read gives another stand-in, calls return one, writes are kept.
const anything = () => {
  const store = {};
  const proxy = new Proxy(function () {}, {
    get: (_, key) => {
      if (key in store) return store[key];
      if (key === Symbol.iterator) return function* () {}; // querySelectorAll(): nothing
      if (key === Symbol.toPrimitive) return () => "";
      if (key === "then") return undefined; // not a promise
      return anything();
    },
    set: (_, key, value) => ((store[key] = value), true),
    apply: () => anything(),
  });
  return proxy;
};
globalThis.document = anything();
document.hidden = false; // so save() reloads /api/data, as in the browser
globalThis.window = globalThis;
globalThis.location = { hash: "" };
globalThis.matchMedia = () => ({ matches: false });
globalThis.setInterval = () => 0;
globalThis.getSelection = anything;
globalThis.innerWidth = 1280;

// A fake server: /api/data serves the clips with the verdicts saved so far. Verdict saves follow net.plan[i] =
// { fail, ms } for the i-th one, and each records whether every earlier save had finished when it was sent.
const net = { plan: [], calls: [], clips: [] };
globalThis.fetch = (url, opts) => {
  if (String(url) === "/api/data") return Promise.resolve(new Response(JSON.stringify({ vods: [], clips: net.clips })));
  const i = net.calls.length;
  const { fail = false, ms = 10 } = net.plan[i] || {};
  const body = JSON.parse(opts.body);
  const call = { verdict: body.verdict, afterEarlier: net.calls.every((c) => c.done), done: false };
  net.calls.push(call);
  return new Promise((resolve, reject) =>
    setTimeout(() => {
      call.done = true;
      if (fail) return reject(new Error("down"));
      net.clips = net.clips.map((c) => (c.id === body.clip ? { ...c, verdict: body.verdict } : c));
      resolve(new Response("{}"));
    }, ms),
  );
};
// SCENARIOS
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const clip = (id) => ({ id, verdict: null, file: `v1/clips/${id}.mp4`, title: `clip ${id}`, streamer: "s", start_s: 0,
  end_s: 20, score: 7, why: "", hook: "", uploads: {}, created_at: "2026-10-24", idx: id }); // prettier-ignore

async function run(plan, steps) {
  net.clips = [clip(1), clip(2)];
  data = { vods: [], clips: net.clips };
  etag = "";
  queue = [1, 2]; // dealt in a known order
  history.length = 0;
  inflight.clear();
  deckShown = null;
  flinging = false;
  verdicts = Promise.resolve();
  net.plan = plan;
  net.calls = [];
  renderDeck();
  steps();
  await sleep(50);
  await verdicts;
  await sleep(50); // decide() / undo() finish after their save settles
  return {
    sent: net.calls.map((c) => c.verdict),
    ordered: net.calls.every((c) => c.afterEarlier),
    history: history.map((e) => e.id),
    queue: [...queue],
    pending: inflight.size,
  };
}

(async () => {
  const out = {
    // Keep 1, Undo, Keep 1 again: the first save fails slowly, after the reviewer already moved on
    keepUndoKeep: await run([{ fail: true, ms: 80 }], () => {
      fling("keep", false);
      undo();
      fling("keep", false);
    }),
    // a lone Keep whose save fails: the card comes back on top, nothing to undo
    keepFails: await run([{ fail: true }], () => fling("keep", false)),
    // Keep saved, then its Undo fails: the decision stays and can be undone again
    undoFails: await run([{}, { fail: true }], () => {
      fling("keep", false);
      undo();
    }),
    // Keep then a quick Undo: both saved, in that order, card back on top
    keepUndo: await run([{ ms: 80 }, {}], () => {
      fling("keep", false);
      undo();
    }),
  };
  console.log(JSON.stringify(out));
})().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
