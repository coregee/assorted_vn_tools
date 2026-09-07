// Exercise the production editor in a minimal DOM without starting a model.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class Element {
  constructor(tag = "div") {
    this.tagName = tag;
    this.children = [];
    this.dataset = {};
    this.style = {};
    this.className = "";
    this.value = "";
    this.selectionStart = 0;
    this.selectionEnd = 0;
    this.selectionDirection = "none";
    this.classList = { toggle() {}, add() {}, remove() {} };
    this.listeners = {};
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(name, value) { this[name] = value; }
  removeAttribute(name) { delete this[name]; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  focus() { document.activeElement = this; }
  setSelectionRange(start, end, direction) {
    this.selectionStart = Math.min(start, this.value.length);
    this.selectionEnd = Math.min(end, this.value.length);
    this.selectionDirection = direction;
  }
  matches(selector) {
    return selector.startsWith(".")
      ? this.className.split(" ").includes(selector.slice(1))
      : this.tagName === selector;
  }
  contains(element) { return this === element || this.children.some(child => child.contains(element)); }
  querySelectorAll(selector) {
    return this.children.flatMap(child => [
      ...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector),
    ]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] ?? null; }
}

const elements = new Map();
const document = {
  title: "Translation workbench",
  activeElement: null,
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  },
  createElement: tag => new Element(tag),
  createDocumentFragment: () => new Element(),
  querySelectorAll: () => [],
  addEventListener() {},
};
const context = vm.createContext({
  document, window: { setTimeout, addEventListener() {} }, clearTimeout,
  CSS: { escape: value => value }, URLSearchParams,
});
let script = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
script = script.replace("  initialize();", `
  globalThis.editor = { state, api, normalizeFile, renderActiveFile, renderJob,
    refreshTranslatedFiles, finishJob, updateLineTranslation, saveActiveFile, bindEvents };
`);
vm.runInContext(script, context);
const editor = context.editor;
const { state, api } = editor;
const $ = id => document.getElementById(id);
const snapshot = (token = "initial") => ({
  path: "script/scene.json", token,
  lines: [
    { id: "a", source: "Source A", translation: "Old A", translatable: true },
    { id: "b", source: "Source B", translation: null, empty_is_applied: true },
    { id: "c", source: "Protected", translation: null, translatable: false },
  ],
});

async function run() {
  editor.bindEvents();
  state.activeFile = editor.normalizeFile(snapshot());
  state.job = { id: "job", status: "running", completed: 0, total: 2, percentage: 0 };
  editor.renderActiveFile();
  editor.renderJob();
  let inputs = $("line-list").querySelectorAll(".translation-input");
  assert.equal(inputs[0].disabled, false);
  assert.equal(inputs[1].disabled, false);
  assert.equal(inputs[2].disabled, true);
  assert.equal($("select-all").disabled, false);
  assert.equal($("translate-file").disabled, true);

  // Progress replaces edits made while the request was in flight with saved output.
  let resolveFile;
  let requestedFile;
  const requested = new Promise(resolve => { requestedFile = resolve; });
  api.getFiles = async () => ({ files: [] });
  api.getFile = () => { requestedFile(); return new Promise(resolve => { resolveFile = resolve; }); };
  document.activeElement = inputs[0];
  inputs[0].value = "Manual draft";
  inputs[0].setSelectionRange(3, 5, "forward");
  const refresh = editor.refreshTranslatedFiles();
  await requested;
  editor.updateLineTranslation("a", "Manual draft");
  editor.updateLineTranslation("b", "", null, true);
  state.selected.add("b");
  const updated = snapshot("progress");
  updated.lines[0].translation = "Model A";
  updated.lines[1].translation = "Model B";
  resolveFile(updated);
  await refresh;
  assert.equal(state.activeFile.lines[0].translation, "Model A");
  assert.equal(state.activeFile.lines[0].originalTranslation, "Model A");
  assert.equal(state.activeFile.token, "progress");
  assert.equal(state.activeFile.lines[1].translation, "Model B");
  assert.equal(state.activeFile.lines[1].hasTranslation, true);
  assert.equal(state.dirty.size, 0);
  assert.equal(state.selected.has("b"), true);
  inputs = $("line-list").querySelectorAll(".translation-input");
  assert.equal(document.activeElement, inputs[0]);
  assert.equal(inputs[0].value, "Model A");
  assert.equal(inputs[0].selectionStart, 3);
  assert.equal(inputs[0].selectionEnd, 5);
  assert.equal(inputs[0].disabled, false);
  assert.equal($("save-button").disabled, true);
  api.getFile = async () => updated;
  let runningSave;
  api.saveFile = async (path, token, updates, allowManagedChanges) => {
    runningSave = { allowManagedChanges, translation: updates[0].translation };
    return { token: "manual-save" };
  };
  editor.updateLineTranslation("a", "Saved during job");
  assert.equal($("save-button").disabled, false);
  assert.equal(await editor.saveActiveFile(), true);
  assert.deepEqual(runningSave, { allowManagedChanges: true, translation: "Saved during job" });
  // The next model refresh still wins over a manual save.
  assert.equal(state.activeFile.lines[0].translation, "Model A");
  for (const status of ["completed", "cancelled", "failed"]) {
    state.job = { id: "job", status, completed: 2, total: 2, percentage: 100 };
    editor.updateLineTranslation("a", "Draft during job");
    assert.equal($("save-button").disabled, false);
    await editor.finishJob(state.job);
    assert.equal(state.job, null);
    assert.equal(state.activeFile.lines[0].translation, "Model A");
    assert.equal(state.dirty.size, 0);
    assert.equal(state.selected.has("b"), true);
    assert.equal($("save-button").disabled, true);
  }

  // Saving after completion sends both kinds of empty value with the latest token.
  editor.updateLineTranslation("a", "", null, false);
  editor.updateLineTranslation("b", "", null, true);
  assert.equal($("save-button").disabled, false);
  let saved;
  api.saveFile = async (path, token, updates) => {
    saved = JSON.parse(JSON.stringify({ path, token, updates }));
    return { token: "saved" };
  };
  assert.equal(await editor.saveActiveFile(), true);
  assert.deepEqual(saved, {
    path: "script/scene.json", token: "progress",
    updates: [{ id: "a", translation: null }, { id: "b", translation: "" }],
  });
  assert.equal(state.dirty.size, 0);

  // A late poll must not replace another file opened while it was in flight.
  api.getFile = () => new Promise(resolve => { resolveFile = resolve; });
  const staleRefresh = editor.refreshTranslatedFiles();
  await Promise.resolve();
  const other = editor.normalizeFile({ path: "script/other.json", token: "other", lines: [] });
  state.activeFile = other;
  state.loadSequence += 1;
  resolveFile(updated);
  await staleRefresh;
  assert.equal(state.activeFile, other);
  console.log("Editor behavior checks passed");
}
run().catch(error => { console.error(error); process.exitCode = 1; });
