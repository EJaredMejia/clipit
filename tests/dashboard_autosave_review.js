// Regression scenarios run after dashboard.js with the dashboard_deck.js prelude.
(async () => {
  const assert = require("node:assert/strict");
  await load(); // let the startup load settle before replacing its stand-in DOM
  class Element {
    constructor(tag) {
      this.tagName = tag.toUpperCase();
      this.children = [];
      this.listeners = {};
      this.attrs = {};
      this.value = "";
      this.classList = { toggle() {} };
    }
    setAttribute(key, value) { this.attrs[key] = value; }
    addEventListener(name, callback) { this.listeners[name] = callback; }
    append(...children) {
      this.children.push(...children);
      for (const child of children) if (child instanceof Element) child.parent = this;
    }
    replaceChildren(...children) {
      for (const child of this.children) if (child instanceof Element) child.parent = null;
      this.children = [];
      this.append(...children);
    }
    contains(node) { return node === this || Boolean(node?.parent && this.contains(node.parent)); }
  }
  const elements = new Map();
  document = {
    activeElement: null,
    createElement: (tag) => new Element(tag),
    getElementById: (id) => {
      if (!elements.has(id)) elements.set(id, new Element("div"));
      return elements.get(id);
    },
  };
  const descendants = (el) => [el, ...el.children.filter((x) => x instanceof Element).flatMap(descendants)];
  const fieldBox = (el) => descendants(el).find((x) => x.tagName === "TEXTAREA");
  const status = (el) => descendants(el).find((x) => x.attrs.role === "status");
  const clip = { id: 1, file: "v1/clips/1.mp4", title: "Original", youtube: "Description", caption: "Caption",
    streamer: "s", start_s: 0, end_s: 20, score: 7, uploads: {}, created_at: "2026-10-24", idx: 1 };

  // Failed and invalid drafts retain their actual status when the fields are rebuilt.
  save = async () => false;
  drafts["1:text:youtube"] = "Unsaved description";
  await saveText(1, "youtube");
  let editor = editable(clip, "youtube", "Description", clip.youtube, 5000, "youtube");
  assert.match(status(editor).textContent ?? status(editor).children.join(""), /Couldn't save/);
  assert.equal(textState("1:text:youtube").running, null);
  assert.equal(fieldBox(editor).value, "Unsaved description");
  drafts["1:text:title"] = "   ";
  await saveText(1, "title");
  editor = editable(clip, "title", "Title", clip.title, 100, "youtube");
  assert.equal(status(editor).children.join(""), "Title is required");
  delete drafts["1:text:title"];
  delete drafts["1:text:youtube"];
  textWrites.clear();

  // A poll updates cached data while focused; blur redraws it even with no remaining draft or future 200 response.
  save = async () => true;
  data = { clips: [clip], vods: [] };
  selected = 1;
  renderDetail();
  const box = fieldBox($("info"));
  document.activeElement = box;
  const oldKey = shown.key;
  data.clips = [{ ...clip, title: "Updated", file: "v1/clips/new.mp4", uploads: { youtube: { status: "done" } } }];
  renderDetail();
  assert.equal(shown.key, oldKey);
  document.activeElement = null;
  await box.listeners.blur();
  assert.equal(shown.key, JSON.stringify(data.clips[0]));
  assert.equal(shown.file, "v1/clips/new.mp4");
  assert.match($("player").children[0].attrs.src, /new.mp4/);
  assert.equal($("info").children[0].children[0].children[0], "Updated");

  // A new title save starts after the first title finishes, while the original description is still pending.
  const tick = async () => { for (let i = 0; i < 15; i++) await Promise.resolve(); };
  const pendingSaves = [];
  save = (path, body) => new Promise((resolve) => pendingSaves.push({ ...body, resolve }));
  drafts["1:text:title"] = "First title";
  drafts["1:text:youtube"] = "New description";
  let confirmations = 0;
  let alerts = 0;
  globalThis.confirm = () => { confirmations++; return false; }; // never publish from this test
  globalThis.alert = () => { alerts++; };
  const button = postNow(clip).children[0];
  const posting = button.listeners.click();
  await tick();
  assert.equal(pendingSaves.length, 2);
  pendingSaves.find((s) => s.field === "title").resolve(true);
  await tick();
  drafts["1:text:title"] = "Latest title";
  const latest = saveText(1, "title");
  await tick();
  pendingSaves.find((s) => s.field === "youtube").resolve(true);
  await tick();
  assert.equal(confirmations, 0);
  pendingSaves.find((s) => s.text === "Latest title").resolve(true);
  await latest;
  await posting;
  assert.equal(confirmations, 1);
  assert.equal(alerts, 0);

  // Save errors still prevent confirmation and publishing.
  save = async () => false;
  drafts["1:text:title"] = "Failed title";
  await postNow(clip).children[0].listeners.click();
  assert.equal(confirmations, 1);
  assert.equal(alerts, 1);
})().catch((e) => { console.error(e); process.exitCode = 1; });
