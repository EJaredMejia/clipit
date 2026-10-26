"use strict";
// Everything the API returns (stream titles, AI text, hashtags) was written by strangers: it only ever reaches the
// page as text, through h() and textContent, never as HTML. Links are only made from http(s) URLs.

const PLATFORMS = { tiktok: "TikTok", youtube: "YouTube" };
const VIEWS = {
  ready: ["Ready to post", (c) => c.verdict === "keep" && posted(c) < Object.keys(PLATFORMS).length],
  review: ["To review", (c) => !c.verdict],
  posted: ["Posted", (c) => posted(c) > 0],
  skip: ["Skipped", (c) => c.verdict === "skip"],
  all: ["All", () => true],
};
const SORTS = {
  new: (a, b) => b.created_at.localeCompare(a.created_at) || a.idx - b.idx,
  score: (a, b) => b.score - a.score || b.created_at.localeCompare(a.created_at),
  streamer: (a, b) => a.streamer.localeCompare(b.streamer) || b.score - a.score,
};
const POLL_MS = 5000;
const FETCH_MS = 15000; // every request has a deadline, so a stalled server shows an error instead of hanging
const POST_MS = 15 * 60000; // posting to YouTube sends the whole video from this PC

const $ = (id) => document.getElementById(id);
let data = { vods: [], clips: [] };
let etag = "";
let view = "ready"; // the Clips tab is where kept clips get posted
let selected = null; // clip id
let shown = { id: null, file: null, key: "" }; // what the detail panel renders now, so polling doesn't redraw it for nothing
const drafts = {}; // post links typed but not saved yet, by "clipId:platform"
let queue = []; // review deck: unreviewed clip ids in random order, queue[0] is the card on top
const history = []; // {id} per decision made in the deck this visit, newest last, for undo
const inflight = new Map(); // id -> verdict writes pending: a poll that raced the save mustn't re-deal or drop them
let deckShown = null; // clip id of the top card now on screen
let muted = false;
let flinging = false;

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, "");
    else if (v !== false && v != null) el.setAttribute(k, v);
  }
  el.append(...kids.flat(Infinity).filter((k) => k != null && k !== false && k !== "")); // strings become text nodes
  return el;
}

const posted = (c) => Object.keys(c.uploads).length;
// clips made before descriptions only have the caption; once the user edits, an empty description is their choice
const description = (c) => (c.edited ? c.youtube : c.youtube || c.caption);
const safeUrl = (u) => (/^https?:\/\/\S+$/i.test(u || "") ? u : null);
const link = (href, text) => (safeUrl(href) ? h("a", { href, target: "_blank", rel: "noopener noreferrer" }, text) : text);
const when = (iso) => (iso ? new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "–");
const score = (s) => String(Number.isInteger(s) ? s : s.toFixed(1));
const two = (n) => String(n).padStart(2, "0");
const day = (iso) => new Date(iso).toLocaleDateString([], { month: "short", day: "numeric" });
const secs = (c) => `${Math.round(c.end_s - c.start_s)} s`;
// a span, not a div: list rows put it inside a button
const meta = (...parts) => h("span", { class: "meta" }, parts.map((p) => (typeof p === "string" ? h("span", {}, p) : p)));
const hms = (s) => `${Math.floor(s / 3600)}:${two(Math.floor(s / 60) % 60)}:${two(Math.floor(s) % 60)}`;

function atTime(url, s) {
  try {
    const u = new URL(url);
    u.searchParams.set("t", `${Math.floor(s / 3600)}h${Math.floor(s / 60) % 60}m${Math.floor(s) % 60}s`);
    return u.href;
  } catch {
    return null;
  }
}

function setStatus(text, bad = false) {
  $("status").textContent = text;
  $("status").classList.toggle("bad", bad);
}

async function load() {
  if (document.hidden) return; // a background tab doesn't need fresh data
  try {
    const r = await fetch("/api/data", { headers: etag ? { "If-None-Match": etag } : {}, signal: AbortSignal.timeout(FETCH_MS) });
    if (r.status !== 304) {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      data = await r.json();
      etag = r.headers.get("ETag") || "";
      render();
    }
    setStatus(""); // only say something when something is wrong
  } catch {
    setStatus("Can't reach the dashboard. Is clipit --dashboard still running?", true);
  }
}

async function save(path, body) {
  try {
    const r = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(FETCH_MS),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
  } catch (e) {
    setStatus(`Couldn't save (${e.message})`, true);
    return false;
  }
  await load();
  return true;
}

// Verdict writes go out one at a time, in order: a quick Keep then Undo must land as Keep then Undo.
let verdicts = Promise.resolve();
function saveVerdict(clip, verdict) {
  const done = verdicts.then(() => save("/api/verdict", { clip, verdict }));
  verdicts = done;
  return done;
}

function render() {
  renderDeck();
  renderStats();
  renderStreamers();
  renderList();
  renderDetail();
  renderVods();
}

function renderStats() {
  $("stats").replaceChildren(
    ...Object.entries(VIEWS).map(([v, [label, test]]) =>
      h(
        "button",
        {
          type: "button",
          class: "stat",
          "aria-pressed": String(v === view),
          onclick: () => {
            view = v;
            renderStats();
            renderList();
          },
        },
        h("span", {}, label),
        h("b", {}, String(data.clips.filter(test).length)),
      ),
    ),
  );
}

function renderStreamers() {
  const pick = $("streamer");
  const was = pick.value;
  const names = [...new Set(data.clips.map((c) => c.streamer).filter(Boolean))].sort();
  pick.replaceChildren(h("option", { value: "" }, "All streamers"), ...names.map((n) => h("option", { value: n }, n)));
  pick.value = names.includes(was) ? was : "";
}

function visible() {
  const q = $("q").value.trim().toLowerCase();
  const who = $("streamer").value;
  return data.clips
    .filter((c) => VIEWS[view][1](c) && (!who || c.streamer === who))
    .filter((c) => !q || `${c.title} ${c.caption} ${c.streamer}`.toLowerCase().includes(q))
    .sort(SORTS[$("sort").value]);
}

function renderList() {
  const rows = visible();
  $("count").textContent = `${rows.length} of ${data.clips.length} clips`;
  $("purge").hidden = view !== "skip" || !rows.length;
  $("list").replaceChildren(...rows.map(row));
  if (!rows.length) {
    const why = data.clips.length ? "No clips match these filters." : "No clips yet. Run clipit on a VOD, or clipit --poll.";
    $("list").append(h("li", { class: "empty" }, why));
  }
}

function row(c) {
  const verdict = { keep: "Keep", skip: "Skip" }[c.verdict];
  return h(
    "li",
    {},
    h(
      "button",
      { type: "button", class: "row", "aria-current": c.id === selected ? "true" : false, onclick: () => select(c.id) },
      h("span", { class: c.score >= 8 ? "score hot" : "score", title: "AI score out of 10" }, score(c.score)),
      h(
        "span",
        { class: "main" },
        h("span", { class: "title" }, c.title || "(untitled)"),
        meta(h("b", {}, c.streamer || "unknown streamer"), secs(c), day(c.created_at)),
      ),
      h(
        "span",
        { class: "badges" },
        view === "all" ? h("span", { class: `badge ${c.verdict || ""}` }, verdict || "New") : "", // other views imply it
        Object.entries(PLATFORMS)
          .filter(([p]) => c.uploads[p])
          .map(([p, name]) =>
            c.uploads[p].status === "uploading"
              ? h("span", { class: "badge processing" }, `Uploading to ${name}`)
              : h("span", { class: "badge posted" }, `✓ ${name}`),
          ),
      ),
    ),
  );
}

function select(id) {
  selected = id;
  renderList();
  renderDetail();
  if (id !== null && matchMedia("(max-width: 860px)").matches) $("detail").scrollIntoView({ block: "start" });
}

function renderDetail() {
  const c = data.clips.find((x) => x.id === selected);
  $("split").classList.toggle("has-detail", Boolean(c));
  if (!c) {
    $("player").replaceChildren();
    $("info").replaceChildren(h("p", { class: "empty" }, "Pick a clip to watch it, rate it and copy its post text."));
    shown = { id: null, file: null, key: "" };
    return;
  }
  const key = JSON.stringify(c);
  if (shown.id === c.id && shown.key === key) return; // unchanged: keep the focus and anything typed
  if (shown.id !== c.id || shown.file !== c.file) {
    // a re-render can give the same clip a new file: the query makes the browser fetch it instead of its cached copy
    const src = `/media/${c.id}?f=${encodeURIComponent(c.file)}`;
    $("player").replaceChildren(h("video", { src, controls: true, preload: "metadata", playsinline: true }));
  }
  shown = { id: c.id, file: c.file, key };
  $("info").replaceChildren(...detail(c).flat());
}

function detail(c) {
  const streamer = /^\w+$/.test(c.streamer) ? link(`https://www.twitch.tv/${c.streamer}`, c.streamer) : "unknown streamer";
  const notes = [["Hook", c.hook], ["Why", c.why], ["Trend", c.trend], ["Sound to add", c.sound]].filter(([, v]) => v);
  return [
    h(
      "div",
      { class: "detail-head" },
      h("h2", {}, c.title || "(untitled)"),
      h("button", { type: "button", class: "ghost", "aria-label": "Close clip", onclick: () => select(null) }, "✕"),
    ),
    meta(h("b", {}, streamer), link(atTime(c.vod_url, c.start_s), `VOD at ${hms(c.start_s)}`), secs(c), `AI score ${score(c.score)}/10`),
    h("div", { class: "file" }, fileTools(c), deleteButton(c), h("span", { class: "path" }, c.file)),
    // one block per platform, in the order you post: copy the text, upload the file, mark it posted
    h("h3", {}, "TikTok"),
    editable(c, "caption", "Caption", c.caption, 2200, "tiktok"),
    upload(c, "tiktok", "TikTok"),
    h("h3", {}, "YouTube Shorts"),
    editable(c, "title", "Title", c.title, 100, "youtube"),
    editable(c, "youtube", "Description", description(c), 5000, "youtube"),
    postNow(c),
    upload(c, "youtube", "YouTube"),
    h(
      "div",
      { class: "group verdict", role: "group", "aria-label": "Worth posting?" },
      h("span", { class: "label" }, "Worth posting?"),
      [["keep", "Keep"], ["skip", "Skip"]].map(([v, label]) =>
        h(
          "button",
          {
            type: "button",
            class: `toggle ${v}`,
            "aria-pressed": String(c.verdict === v),
            onclick: () => saveVerdict(c.id, c.verdict === v ? null : v),
          },
          label,
        ),
      ),
    ),
    notes.length
      ? h("details", {}, h("summary", {}, "Why the AI picked it"), h("dl", {}, notes.map(([k, v]) => [h("dt", {}, k), h("dd", {}, v)])))
      : "",
  ];
}

function upload(c, platform, name) {
  const done = c.uploads[platform];
  const undo = () =>
    h("button", { type: "button", class: "ghost", onclick: () => save("/api/upload", { clip: c.id, platform, url: null }) }, "Undo");
  if (done && done.status === "uploading") {
    // an upload this old belongs to a run that died (youtube.STUCK): the user has to check whether it went up
    const stuck = Date.now() - Date.parse(done.at) > 2 * 3600e3;
    return h(
      "div",
      { class: "upload busy", role: "status" },
      stuck
        ? [h("span", {}, `The upload to ${name} that started ${when(done.at)} never finished. Check YouTube Studio: undo to post it again.`), undo()]
        : h("span", {}, `Uploading to ${name}… started ${when(done.at)}. It keeps going if you leave this page, as long as the dashboard runs.`),
    );
  }
  if (done) {
    return h(
      "div",
      { class: "upload done" },
      h("span", {}, `Posted on ${name}, ${when(done.at)}`, done.url ? [". ", link(done.url, "Open the post")] : ""),
      undo(),
    );
  }
  const key = `${c.id}:${platform}`;
  const input = h("input", {
    type: "url",
    placeholder: "Post link (optional)",
    "aria-label": `${name} post link`,
    value: drafts[key] || "",
    oninput: (e) => {
      drafts[key] = e.target.value;
      e.target.setCustomValidity("");
    },
  });
  const submit = async (e) => {
    e.preventDefault();
    const url = input.value.trim();
    if (url && !safeUrl(url)) {
      input.setCustomValidity("Use a full https:// link");
      input.reportValidity();
      return;
    }
    const order = visible().map((x) => x.id);
    const list = listState();
    if (await save("/api/upload", { clip: c.id, platform, url })) {
      delete drafts[key]; // a failed save keeps the link
      // only if the user is still on this clip and this list: a slow save mustn't replace what they opened since
      if (selected === c.id && listState() === list) advanceFrom(c.id, order);
    }
  };
  return h("form", { class: "upload", onsubmit: submit }, input, h("button", { type: "submit" }, `Mark posted on ${name}`));
}

const listState = () => [view, $("q").value, $("streamer").value, $("sort").value].join("\n");

// After posting, a clip that left the list (e.g. Ready to post) hands over to the next one, so posting is a run.
function advanceFrom(id, order) {
  const inView = (x) => {
    const c = data.clips.find((y) => y.id === x);
    return c && VIEWS[view][1](c);
  };
  if (inView(id)) return;
  const i = order.indexOf(id);
  select([...order.slice(i + 1), ...order.slice(0, i).reverse()].find(inView) ?? null);
}

// Post text the user can edit and copy. Saved edits are theirs: a re-render of the clip never overwrites them.
function editable(c, field, label, text, max, platform) {
  const key = `${c.id}:text:${field}`; // post-link drafts use "id:platform"
  const box = h("textarea", { rows: field === "title" ? 2 : 5, maxlength: String(max), "aria-label": label });
  box.value = drafts[key] ?? text;
  const count = h("span", { class: "count" });
  const saveButton = h("button", { type: "button", class: "ghost" }, "Save");
  const copyButton = h("button", { type: "button", class: "ghost" }, "Copy");
  const sync = () => {
    count.textContent = `${box.value.length}/${max}`;
    saveButton.disabled = box.value === text || (field === "title" && !box.value.trim());
  };
  sync();
  box.addEventListener("input", () => {
    drafts[key] = box.value; // survives the redraws polling makes until it's saved
    sync();
  });
  saveButton.addEventListener("click", async () => {
    saveButton.disabled = true;
    if (await save("/api/text", { clip: c.id, field, text: box.value })) delete drafts[key];
    else sync();
  });
  copyButton.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(box.value);
      copyButton.textContent = "Copied";
    } catch {
      box.select(); // clipboard blocked (some embedded browsers): select it for Ctrl+C
      copyButton.textContent = "Press Ctrl+C";
    }
    setTimeout(() => (copyButton.textContent = "Copy"), 2000);
  });
  const live = c.uploads[platform] && c.uploads[platform].status === "done";
  return h(
    "div",
    { class: "copy" },
    h("div", { class: "copy-head" }, h("span", { class: "label" }, label), count, saveButton, copyButton),
    box,
    live ? h("p", { class: "hint" }, `Already posted: saving here doesn't change the ${PLATFORMS[platform]} post.`) : "",
  );
}

// Posts the clip to the channel clipit --youtube-login signed in to, through the server (the code --upload runs).
function postNow(c) {
  if (c.uploads.youtube) return "";
  const button = h("button", { type: "button", class: "primary" }, "Post to YouTube now");
  button.addEventListener("click", async () => {
    const saved = { title: c.title, youtube: description(c) };
    if (Object.entries(saved).some(([f, text]) => (drafts[`${c.id}:text:${f}`] ?? text) !== text)) {
      alert("Save your title and description changes first: the post uses the saved text.");
      return;
    }
    if (!confirm("Post this clip publicly on your YouTube channel now?")) return;
    button.disabled = true;
    button.textContent = "Posting…";
    setTimeout(load, 1000); // show the uploading state now, not at the next poll
    try {
      const r = await fetch("/api/youtube", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ clip: c.id }),
        signal: AbortSignal.timeout(POST_MS),
      });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || `HTTP ${r.status}`);
    } catch (e) {
      alert(`Couldn't post to YouTube: ${e.message}`); // stays up until read: a status line would be cleared by the next poll
      button.disabled = false;
      button.textContent = "Post to YouTube now";
    }
    await load();
  });
  return h("div", { class: "group" }, button);
}

// Asks the server to open Explorer on the PC running clipit with the file selected, ready to drag into an upload page.
function fileTools(c) {
  const show = h("button", { type: "button", class: "ghost" }, "Show in folder");
  show.addEventListener("click", async () => {
    try {
      const r = await fetch("/api/reveal", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ clip: c.id }),
        signal: AbortSignal.timeout(FETCH_MS),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      show.textContent = "Opened";
    } catch (e) {
      setStatus(`Couldn't open the folder (${e.message})`, true);
    }
    setTimeout(() => (show.textContent = "Show in folder"), 2000);
  });
  return show;
}

// Removes the clip from the catalog and deletes its video. Not while it's being uploaded: the server refuses that too.
function deleteButton(c) {
  if (Object.values(c.uploads).some((u) => u.status === "uploading")) return "";
  const gone = posted(c) ? " Its posts stay up, clipit just forgets them." : "";
  return h(
    "button",
    {
      type: "button",
      class: "ghost",
      onclick: async () => {
        if (confirm(`Delete this clip's video and remove it from clipit? This can't be undone.${gone}`)) {
          if (await save("/api/delete", { clip: c.id })) select(null);
        }
      },
    },
    "Delete",
  );
}

$("purge").addEventListener("click", async () => {
  const n = data.clips.filter(VIEWS.skip[1]).length;
  if (confirm(`Delete all ${n} skipped clips and their videos? This can't be undone.`)) await save("/api/delete-skipped", {});
});

function shuffle(a) {
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

function renderDeck() {
  const todo = new Set(data.clips.filter((c) => !c.verdict).map((c) => c.id));
  queue = queue.filter((id) => todo.has(id) || inflight.has(id)); // drop clips rated elsewhere (Clips tab, another window)
  const dealt = new Set(queue);
  queue.push(...shuffle([...todo].filter((id) => !dealt.has(id) && !inflight.has(id)))); // new clips join at random spots in the back
  $("left").textContent = `${queue.length} left to review`;
  $("undo").disabled = !history.length;
  $("skip").disabled = $("keep").disabled = !queue.length;

  const top = data.clips.find((c) => c.id === queue[0]);
  const next = data.clips.find((c) => c.id === queue[1]);
  const back = next ? h("div", { class: "card back", "aria-hidden": "true" }) : null;
  if (top && top.id === deckShown) {
    $("deck").querySelector(".back")?.remove();
    if (back) $("deck").prepend(back);
    return; // same card on top: don't restart its video on every poll
  }
  deckShown = top ? top.id : null;
  flinging = false;
  $("review").classList.toggle("empty", !top);
  $("tools").replaceChildren(
    ...(top ? [fileTools(top), h("button", { type: "button", class: "ghost", onclick: () => openClips("all", top.id) }, "Open details")] : []),
  );
  $("story").replaceChildren(
    ...(top
      ? [
          h("h2", {}, top.title || "(untitled)"),
          meta(h("b", {}, top.streamer || "unknown streamer"), secs(top), `AI score ${score(top.score)}/10`),
          top.why ? h("p", { class: "why" }, top.why) : "",
        ]
      : []),
  );
  if (!top) {
    const kept = data.clips.filter(VIEWS.ready[1]).length;
    $("deck").replaceChildren(
      h(
        "div",
        { class: "done-card" },
        h("h2", {}, data.clips.length ? "Nothing left to review" : "No clips yet"),
        h(
          "p",
          {},
          data.clips.length
            ? kept
              ? `${kept} kept clip${kept === 1 ? " is" : "s are"} ready to post.`
              : "New clips show up here as clipit makes them."
            : "Run clipit on a VOD, or clipit --poll.",
        ),
        kept ? h("button", { type: "button", class: "primary", onclick: () => openClips("ready") }, `Post ${kept === 1 ? "it" : `the ${kept} clips`}`) : "",
      ),
    );
    return;
  }
  $("deck").replaceChildren(...[back, card(top)].filter(Boolean));
}

function card(c) {
  const video = h("video", { src: `/media/${c.id}?f=${encodeURIComponent(c.file)}`, loop: true, playsinline: true, preload: "auto" });
  const keep = h("div", { class: "stamp keep" }, "KEEP");
  const skip = h("div", { class: "stamp skip" }, "SKIP");
  const sound = h("button", { type: "button", class: "sound" });
  const bar = h("i");
  const el = h(
    "div",
    { class: "card top", tabindex: "0", "aria-label": `${c.title || "Untitled clip"}. Right arrow keeps it, left arrow skips it.` },
    video,
    keep,
    skip,
    sound,
    h("div", { class: "progress" }, bar),
  );
  video.addEventListener("timeupdate", () => (bar.style.transform = `scaleX(${video.currentTime / video.duration || 0})`));
  const showSound = () => (sound.textContent = video.muted ? "Sound off" : "Sound on");
  sound.addEventListener("click", () => {
    muted = video.muted = !video.muted;
    showSound();
  });
  video.addEventListener("play", () => el.classList.remove("paused"));
  video.addEventListener("pause", () => el.classList.add("paused"));
  video.muted = muted;
  showSound();
  if (!$("review-panel").hidden) {
    video.play().catch(() => {
      // the browser blocks autoplay with sound until the page has been clicked: start muted instead
      muted = video.muted = true;
      showSound();
      video.play().catch(() => el.classList.add("paused"));
    });
  } else el.classList.add("paused");

  let x0 = null;
  let t0 = 0;
  let dx = 0;
  const move = (x) => {
    dx = x;
    el.style.transform = `translateX(${x}px) rotate(${x / 18}deg)`;
    keep.style.opacity = String(Math.min(1, Math.max(0, x / 100)));
    skip.style.opacity = String(Math.min(1, Math.max(0, -x / 100)));
  };
  el.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || e.target.closest("button") || flinging) return;
    x0 = e.clientX;
    t0 = e.timeStamp;
    dx = 0;
    el.setPointerCapture(e.pointerId);
    el.classList.add("dragging");
    el.style.transition = "none";
  });
  el.addEventListener("pointermove", (e) => x0 !== null && move(e.clientX - x0));
  const release = (e) => {
    if (x0 === null) return;
    x0 = null;
    el.classList.remove("dragging");
    el.style.transition = "";
    const flick = Math.abs(dx) > 40 && Math.abs(dx) / (e.timeStamp - t0) > 0.5; // a quick flick counts, however short
    if (flick || Math.abs(dx) > Math.min(120, el.offsetWidth / 3)) return fling(dx > 0 ? "keep" : "skip");
    if (Math.abs(dx) < 6) toggle(video); // a tap, not a drag
    move(0);
  };
  el.addEventListener("pointerup", release);
  el.addEventListener("pointercancel", () => {
    // the browser took the gesture over (a scroll): put the card back, never rate it
    if (x0 === null) return;
    x0 = null;
    el.classList.remove("dragging");
    el.style.transition = "";
    move(0);
  });
  el.move = move;
  return el;
}

function toggle(video) {
  if (video.paused) video.play().catch(() => {});
  else video.pause();
}

// animate: false for keys, which are pressed over and over and should feel instant
function fling(verdict, animate = true) {
  const el = $("deck").querySelector(".card.top");
  if (!el || flinging) return;
  flinging = true;
  if (!animate || matchMedia("(prefers-reduced-motion: reduce)").matches) return decide(verdict);
  const sign = verdict === "keep" ? 1 : -1;
  el.move(sign * 100); // shows the stamp
  el.style.transition = "transform 220ms var(--ease-out), opacity 220ms var(--ease-out)";
  el.style.transform = `translateX(${sign * (innerWidth / 2 + el.offsetWidth)}px) rotate(${sign * 20}deg)`;
  el.style.opacity = "0";
  setTimeout(() => decide(verdict), 180);
}

// A clip can have a Keep and its Undo pending at once: count its writes, so the first to finish doesn't unmark it.
function pending(id, n) {
  const left = (inflight.get(id) || 0) + n;
  if (left > 0) inflight.set(id, left);
  else inflight.delete(id);
}

async function decide(verdict) {
  const id = queue.shift();
  const entry = { id }; // its own object: a failed save removes exactly this decision, never a later one for the same clip
  history.push(entry);
  pending(id, 1);
  renderDeck();
  const ok = await saveVerdict(id, verdict);
  pending(id, -1);
  const i = history.indexOf(entry);
  if (!ok && i >= 0) {
    // still the reviewer's latest word on this clip (not undone since): take it back and put the card on top
    history.splice(i, 1);
    if (!queue.includes(id)) queue.unshift(id); // the status bar says why
    renderDeck();
  }
}

async function undo() {
  if (flinging || !history.length) return; // mid-fling, decide() is about to take queue[0]
  const entry = history.pop();
  const { id } = entry;
  pending(id, 1);
  queue.unshift(id);
  renderDeck();
  const ok = await saveVerdict(id, null);
  pending(id, -1);
  if (!ok) {
    const i = queue.indexOf(id);
    if (i >= 0) queue.splice(i, 1);
    history.push(entry);
    renderDeck();
  }
}

function openClips(v, id = null) {
  view = v;
  selected = id;
  showTab($("clips-tab"));
  renderStats();
  select(id ?? firstOnWide());
}

// On a wide screen the Clips tab opens on its first clip, so there's never an empty detail panel to stare at.
const firstOnWide = () => (matchMedia("(max-width: 860px)").matches ? null : (visible()[0]?.id ?? null));

function renderVods() {
  const clips = {};
  for (const c of data.clips) clips[c.vod_id] = (clips[c.vod_id] || 0) + 1;
  const failed = data.vods.filter((v) => v.status === "failed").length;
  $("vods-tab").textContent = failed ? `VODs (${failed} failed)` : "VODs";
  $("vods").replaceChildren(
    ...data.vods.map((v) =>
      h(
        "tr",
        {},
        h("td", {}, link(v.url, v.id)),
        h("td", {}, v.streamer || "–"),
        h(
          "td",
          {},
          h("span", { class: `badge ${v.status}` }, v.status),
          v.attempts > 1 ? ` try ${v.attempts}` : "",
          v.error ? h("div", { class: "error" }, v.error) : "",
        ),
        h("td", { class: "num" }, String(clips[v.id] || 0)),
        h("td", {}, v.tier),
        h("td", {}, when(v.finished_at || v.claimed_at)),
      ),
    ),
  );
  if (!data.vods.length) $("vods").append(h("tr", {}, h("td", { colspan: "6", class: "empty" }, "No VODs yet.")));
}

function showTab(tab) {
  for (const t of document.querySelectorAll("[role=tab]")) {
    t.setAttribute("aria-selected", String(t === tab));
    $(t.getAttribute("aria-controls")).hidden = t !== tab;
  }
  for (const v of document.querySelectorAll("[role=tabpanel][hidden] video")) v.pause();
  window.history.replaceState(null, "", `#${tab.id.replace(/-tab$/, "")}`); // a reload comes back to this tab
  if (tab.id === "clips-tab" && selected === null) select(firstOnWide());
}

for (const tab of document.querySelectorAll("[role=tab]")) tab.addEventListener("click", () => showTab(tab));
$("skip").addEventListener("click", () => fling("skip"));
$("keep").addEventListener("click", () => fling("keep"));
$("undo").addEventListener("click", undo);
document.addEventListener("keydown", (e) => {
  if ($("review-panel").hidden || e.ctrlKey || e.metaKey || e.altKey || e.target.closest("input, select, textarea")) return;
  const video = $("deck").querySelector(".card.top video");
  if (e.key === "ArrowRight") fling("keep", false);
  else if (e.key === "ArrowLeft") fling("skip", false);
  else if (e.key === "z" || e.key === "Z" || e.key === "Backspace") undo();
  else if (e.key === " " && video && !e.target.closest("button")) toggle(video);
  else return;
  e.preventDefault();
});
$("q").addEventListener("input", renderList);
$("streamer").addEventListener("change", renderList);
$("sort").addEventListener("change", renderList);
document.addEventListener("visibilitychange", load);
setInterval(load, POLL_MS);
const start = document.getElementById(`${location.hash.slice(1)}-tab`);
if (start?.getAttribute("role") === "tab") showTab(start);
load().then(() => !$("clips-panel").hidden && selected === null && select(firstOnWide()));
