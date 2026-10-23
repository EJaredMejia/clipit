"use strict";
// Everything the API returns (stream titles, AI text, hashtags) was written by strangers: it only ever reaches the
// page as text, through h() and textContent, never as HTML. Links are only made from http(s) URLs.

const PLATFORMS = { tiktok: "TikTok", youtube: "YouTube" };
const VIEWS = {
  all: ["All clips", () => true],
  review: ["To review", (c) => !c.verdict],
  ready: ["Kept, not posted", (c) => c.verdict === "keep" && posted(c) < Object.keys(PLATFORMS).length],
  posted: ["Posted", (c) => posted(c) > 0],
  skip: ["Skipped", (c) => c.verdict === "skip"],
};
const SORTS = {
  new: (a, b) => b.created_at.localeCompare(a.created_at) || a.idx - b.idx,
  score: (a, b) => b.score - a.score || b.created_at.localeCompare(a.created_at),
  streamer: (a, b) => a.streamer.localeCompare(b.streamer) || b.score - a.score,
};
const POLL_MS = 5000;

const $ = (id) => document.getElementById(id);
let data = { vods: [], clips: [] };
let etag = "";
let view = "all";
let selected = null; // clip id
let shown = { id: null, key: "" }; // what the detail panel renders now, so polling doesn't redraw it for nothing
const drafts = {}; // post links typed but not saved yet, by "clipId:platform"

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
const safeUrl = (u) => (/^https?:\/\/\S+$/i.test(u || "") ? u : null);
const link = (href, text) => (safeUrl(href) ? h("a", { href, target: "_blank", rel: "noopener noreferrer" }, text) : text);
const when = (iso) => (iso ? new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "–");
const score = (s) => String(Number.isInteger(s) ? s : s.toFixed(1));
const two = (n) => String(n).padStart(2, "0");
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
    const r = await fetch("/api/data", { headers: etag ? { "If-None-Match": etag } : {} });
    if (r.status !== 304) {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      data = await r.json();
      etag = r.headers.get("ETag") || "";
      render();
    }
    setStatus("Live");
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
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
  } catch (e) {
    setStatus(`Couldn't save (${e.message})`, true);
    return;
  }
  await load();
}

function render() {
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
        h("b", {}, String(data.clips.filter(test).length)),
        h("span", {}, label),
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

function renderList() {
  const q = $("q").value.trim().toLowerCase();
  const who = $("streamer").value;
  const rows = data.clips
    .filter((c) => VIEWS[view][1](c) && (!who || c.streamer === who))
    .filter((c) => !q || `${c.title} ${c.caption} ${c.streamer}`.toLowerCase().includes(q))
    .sort(SORTS[$("sort").value]);
  $("count").textContent = `${rows.length} of ${data.clips.length} clips`;
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
      h("span", { class: "score", title: "AI score out of 10" }, score(c.score)),
      h(
        "span",
        { class: "main" },
        h("span", { class: "title" }, c.title || "(untitled)"),
        h("span", { class: "meta" }, `${c.streamer || "unknown streamer"} · ${Math.round(c.end_s - c.start_s)} s · ${when(c.created_at)}`),
      ),
      h(
        "span",
        { class: "badges" },
        h("span", { class: `badge ${c.verdict || ""}` }, verdict || "New"),
        Object.entries(PLATFORMS)
          .filter(([p]) => c.uploads[p])
          .map(([, name]) => h("span", { class: "badge posted" }, `✓ ${name}`)),
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
    shown = { id: null, key: "" };
    return;
  }
  const key = JSON.stringify(c);
  if (shown.id === c.id && shown.key === key) return; // unchanged: keep the focus and anything typed
  if (shown.id !== c.id) {
    $("player").replaceChildren(h("video", { src: `/media/${c.id}`, controls: true, preload: "metadata", playsinline: true }));
  }
  shown = { id: c.id, key };
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
    h(
      "p",
      { class: "meta" },
      streamer,
      " · ",
      link(atTime(c.vod_url, c.start_s), `VOD at ${hms(c.start_s)}`),
      ` · ${Math.round(c.end_s - c.start_s)} s · AI score ${score(c.score)}/10`,
    ),
    h(
      "div",
      { class: "group", role: "group", "aria-label": "Worth posting?" },
      h("span", { class: "label" }, "Worth it?"),
      [["keep", "Keep"], ["skip", "Skip"]].map(([v, label]) =>
        h(
          "button",
          {
            type: "button",
            class: `toggle ${v}`,
            "aria-pressed": String(c.verdict === v),
            onclick: () => save("/api/verdict", { clip: c.id, verdict: c.verdict === v ? null : v }),
          },
          label,
        ),
      ),
    ),
    h("h3", {}, "Posted"),
    Object.entries(PLATFORMS).map(([p, name]) => upload(c, p, name)),
    h("h3", {}, "Post text"),
    copy("YouTube title", c.title),
    copy("YouTube description", c.youtube || c.caption), // clips made before descriptions only have the caption
    copy("TikTok caption", c.caption),
    notes.length
      ? h("details", {}, h("summary", {}, "Why the AI picked it"), h("dl", {}, notes.map(([k, v]) => [h("dt", {}, k), h("dd", {}, v)])))
      : "",
  ];
}

function upload(c, platform, name) {
  const done = c.uploads[platform];
  if (done) {
    return h(
      "div",
      { class: "upload" },
      h("span", { class: "label" }, name),
      h("span", {}, `✓ Posted ${when(done.at)}`, done.url ? [" · ", link(done.url, "open post")] : ""),
      h("button", { type: "button", class: "ghost", onclick: () => save("/api/upload", { clip: c.id, platform, url: null }) }, "Undo"),
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
  const submit = (e) => {
    e.preventDefault();
    const url = input.value.trim();
    if (url && !safeUrl(url)) {
      input.setCustomValidity("Use a full https:// link");
      input.reportValidity();
      return;
    }
    delete drafts[key];
    save("/api/upload", { clip: c.id, platform, url });
  };
  return h("form", { class: "upload", onsubmit: submit }, h("span", { class: "label" }, name), input, h("button", { type: "submit" }, "Mark posted"));
}

function copy(label, text) {
  const button = h("button", { type: "button", class: "ghost" }, "Copy");
  const pre = h("pre", {}, text || "–");
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(text);
      button.textContent = "Copied";
    } catch {
      getSelection().selectAllChildren(pre); // clipboard blocked (some embedded browsers): select it for Ctrl+C
      button.textContent = "Press Ctrl+C";
    }
    setTimeout(() => (button.textContent = "Copy"), 2000);
  });
  return h("div", { class: "copy" }, h("div", { class: "copy-head" }, h("span", { class: "label" }, label), button), pre);
}

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

for (const tab of document.querySelectorAll("[role=tab]")) {
  tab.addEventListener("click", () => {
    for (const t of document.querySelectorAll("[role=tab]")) {
      t.setAttribute("aria-selected", String(t === tab));
      $(t.getAttribute("aria-controls")).hidden = t !== tab;
    }
  });
}
$("q").addEventListener("input", renderList);
$("streamer").addEventListener("change", renderList);
$("sort").addEventListener("change", renderList);
document.addEventListener("visibilitychange", load);
setInterval(load, POLL_MS);
load();
