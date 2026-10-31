"use strict";

// Agents can write these files, so their text reaches the page only as text nodes or through DOMPurify.

const MAX_COLUMNS = 6;
const MAX_HITS = 50;
const PURIFY = {
  USE_PROFILES: { html: true },
  FORBID_TAGS: ["style", "form", "button", "select", "textarea", "video", "audio"],
  FORBID_ATTR: ["style"],
  RETURN_DOM_FRAGMENT: true,
};
const main = document.getElementById("main");
const searchBox = document.getElementById("search");
const treeBox = document.getElementById("tree");
const toastBox = document.getElementById("toast");
const state = {
  tree: { user: [], skills: [] },
  open: new Map(),
  hash: "",
  lastFile: "",
  file: null,
  editor: null,
  mode: "",
  turn: 0,
};

// ---- small helpers

function h(tag, attrs, ...kids) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (key === "on") {
      for (const [type, listener] of Object.entries(value)) node.addEventListener(type, listener);
    } else if (value === true) {
      node.setAttribute(key, "");
    } else if (value !== false && value != null) {
      node.setAttribute(key, value);
    }
  }
  node.append(...kids.flat(Infinity).filter((kid) => kid != null && kid !== false));
  return node;
}

const button = (label, onClick, className = "") =>
  h("button", { type: "button", class: className, on: { click: onClick } }, label);

function debounce(fn, wait) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}

function toast(message) {
  toastBox.textContent = message;
  toastBox.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => toastBox.classList.remove("show"), 4000);
}

const relative = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
const UNITS = [["year", 31536000], ["month", 2592000], ["day", 86400], ["hour", 3600], ["minute", 60]];

function ago(iso) {
  const seconds = (new Date(iso) - Date.now()) / 1000;
  for (const [unit, size] of UNITS) {
    if (Math.abs(seconds) >= size) return relative.format(Math.round(seconds / size), unit);
  }
  return "just now";
}

// ---- server

class ApiError extends Error {
  constructor(status, message, signin = false) {
    super(message);
    this.status = status;
    this.signin = signin;
  }
}

async function api(method, path, params) {
  const init = { method, redirect: "manual" };
  let url = path;
  if (method === "GET") {
    if (params) url += `?${new URLSearchParams(params)}`;
  } else {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(params);
  }
  let response;
  try {
    response = await fetch(url, init);
  } catch {
    throw new ApiError(0, "Can't reach the server.");
  }
  // Cloudflare answers an expired login with a redirect to its sign-in page.
  if (response.type === "opaqueredirect") throw new ApiError(403, "Your sign-in expired.", true);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new ApiError(response.status, data.error || `Request failed (${response.status}).`, Boolean(data.signin));
  }
  return data;
}

function problem(error, actions = []) {
  return h("div", { class: "banner", role: "alert" },
    h("span", null, error.message),
    error.signin && h("a", { class: "button", href: "/", target: "_blank", rel: "noopener" }, "Sign in ↗"),
    error.signin && h("span", null, "Your text stays here; try again after."),
    actions);
}

// Run an action; a failure shows in `note` rather than vanishing.
const attempt = (note, fn) => async () => {
  try {
    await fn();
  } catch (error) {
    note.replaceChildren(problem(error));
  }
};

// ---- routes: #/user/<path> · #/skills/<name>/<path> · #/search/<query> · #/new

const encodePath = (path) => path.split("/").map(encodeURIComponent).join("/");
const route = (store, path) => `#/${store}/${encodePath(path)}`;

function parseRoute() {
  const raw = location.hash.replace(/^#\/?/, "");
  const slash = raw.indexOf("/");
  const head = slash < 0 ? raw : raw.slice(0, slash);
  try {
    return { head, rest: slash < 0 ? "" : decodeURIComponent(raw.slice(slash + 1)) };
  } catch {
    return { head: "", rest: "" };
  }
}

const edited = () => Boolean(state.editor) && state.editor.area.value !== state.editor.original;
const mayLeave = () => !edited() || confirm("Drop your unsaved changes?");

async function navigate() {
  if (location.hash !== state.hash && !mayLeave()) {
    history.replaceState(null, "", state.hash);
    return;
  }
  state.editor = null;
  state.hash = location.hash;
  document.body.classList.remove("nav-open");
  const { head, rest } = parseRoute();
  if (head === "search" && rest) await showSearch(rest);
  else if (head === "new") showNew();
  else if ((head === "user" || head === "skills") && rest) await showFile(head, rest);
  else landing();
  markCurrent();
}

function landing() {
  const files = state.tree.user.map((file) => file.path);
  const first = files.includes("README.md") ? "README.md" : files[0];
  if (first) location.replace(route("user", first));
  else setMain(h("p", { class: "empty" }, "No knowledge files yet."));
}

function setMain(...kids) {
  main.classList.remove("editing");
  main.replaceChildren(...kids.flat(Infinity).filter(Boolean));
  main.scrollTop = 0;
}

const menuButton = () => h("button", {
  type: "button", class: "ghost menu", "aria-label": "Show files",
  on: { click: () => document.body.classList.add("nav-open") },
}, "☰");

function bar(path, chips, actions) {
  const parts = path.split("/");
  return h("header", { class: "bar" },
    menuButton(),
    h("h1", { class: "crumbs", title: path },
      parts.slice(0, -1).map((part) => h("span", { class: "dim" }, `${part} / `)), parts.at(-1)),
    chips,
    h("div", { class: "actions" }, actions));
}

// ---- rendering a file's text

function renderText(path, text, where) {
  if (!text.trim()) return h("p", { class: "empty" }, "This file is empty.");
  if (/\.(md|markdown)$/i.test(path)) return renderMarkdown(text, where);
  if (/\.ya?ml$/i.test(path)) return renderYaml(text);
  return h("pre", { class: "source" }, text);
}

// Failsafe keeps every scalar as written: 12.50 stays 12.50 and dates stay dates.
const loadYaml = (text) => jsyaml.load(text, { schema: jsyaml.FAILSAFE_SCHEMA });

function renderMarkdown(text, where) {
  const box = h("div", { class: "prose" });
  let body = text;
  const front = /^---\r?\n([\s\S]*?)\r?\n---[ \t]*(\r?\n|$)/.exec(text);
  if (front) {
    try {
      const meta = loadYaml(front[1]);
      if (isMap(meta)) {
        box.append(h("div", { class: "front" }, keyValues(meta)));
        body = text.slice(front[0].length);
      }
    } catch {
      // Not frontmatter after all; it renders as ordinary text.
    }
  }
  const clean = DOMPurify.sanitize(marked.parse(body), PURIFY);
  tidyLinks(clean, where);
  box.append(clean);
  return box;
}

function tidyLinks(fragment, where) {
  for (const link of fragment.querySelectorAll("a[href]")) {
    const href = link.getAttribute("href");
    if (/^[a-z][a-z0-9+.-]*:/i.test(href) || href.startsWith("//")) {
      link.target = "_blank";
      link.rel = "noopener noreferrer";
    } else {
      const local = href.startsWith("#") ? null : storeLink(href, where);
      if (local) link.setAttribute("href", local);
      else link.removeAttribute("href");
    }
  }
  // Nothing here serves images, and a remote one would report back that the file was opened.
  for (const image of fragment.querySelectorAll("img")) {
    const label = image.getAttribute("alt") || image.getAttribute("src") || "";
    image.replaceWith(h("span", { class: "dim" }, `[image: ${label}]`));
  }
}

function storeLink(href, where) {
  const folder = where.path.split("/").slice(0, -1).map((part) => `${part}/`).join("");
  try {
    const url = new URL(href, `https://store.invalid/${encodePath(folder)}`);
    return route(where.store, decodeURIComponent(url.pathname.slice(1)));
  } catch {
    return null;
  }
}

const isMap = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const isPlain = (value) => value === null || typeof value !== "object";
const isBlank = (value) => value == null || value === "" || value === "null" || value === "~";
const label = (key) => key.replace(/_/g, " ");

function renderYaml(text) {
  let data;
  try {
    data = loadYaml(text);
  } catch (error) {
    return h("div", null,
      h("div", { class: "banner" }, `This YAML doesn't parse: ${error.message}`),
      h("pre", { class: "source" }, text));
  }
  if (Array.isArray(data) && data.length && data.every(isMap)) return records(data);
  return isMap(data) || Array.isArray(data) ? value(data) : h("pre", { class: "source" }, text);
}

function value(item) {
  if (Array.isArray(item)) {
    if (item.every(isPlain)) return h("span", { class: "chips" }, item.map((one) => h("span", { class: "chip" }, String(one))));
    return h("div", { class: "stack" }, item.map(value));
  }
  if (isMap(item)) return keyValues(item);
  return isBlank(item) ? h("span", { class: "dim" }, "—") : h("span", { class: "text" }, String(item));
}

const keyValues = (map) => h("dl", { class: "kv" },
  Object.entries(map).map(([key, item]) => [h("dt", null, label(key)), h("dd", null, value(item))]));

// Columns come from the data: the first keys holding a plain value in at least half the rows.
// After the first, identifier-like keys (all different, no spaces) stay in the expanded record only.
function columnsFor(rows) {
  const values = new Map();
  for (const row of rows) {
    for (const [key, item] of Object.entries(row)) {
      if (!values.has(key)) values.set(key, []);
      if (isPlain(item) && !isBlank(item)) values.get(key).push(String(item));
    }
  }
  const identifier = (found) => new Set(found).size === found.length && !found.some((one) => /\s/.test(one));
  const keys = [...values]
    .filter(([, found], index) => found.length * 2 >= rows.length && (index === 0 || !identifier(found)))
    .map(([key]) => key);
  return (keys.length ? keys : [...values.keys()]).slice(0, MAX_COLUMNS);
}

function records(rows) {
  const columns = columnsFor(rows);
  const body = h("tbody");
  const count = h("span", { class: "dim" });
  const entries = rows.map((row) => {
    const entry = { search: JSON.stringify(row).toLowerCase(), detail: null };
    const toggle = h("button", { type: "button", class: "twist", "aria-expanded": "false" },
      h("span", { class: "arrow", "aria-hidden": "true" }, "▸"), cell(row[columns[0]]));
    entry.row = h("tr", {
      class: "row",
      on: {
        click: () => {
          const open = toggle.getAttribute("aria-expanded") !== "true";
          toggle.setAttribute("aria-expanded", String(open));
          entry.detail ||= h("tr", { class: "detail" }, h("td", { colspan: columns.length }, keyValues(row)));
          if (open) entry.row.after(entry.detail);
          else entry.detail.remove();
        },
      },
    }, h("td", null, toggle), columns.slice(1).map((key) => h("td", { title: String(row[key] ?? "") }, cell(row[key]))));
    return entry;
  });
  const apply = () => {
    const needle = filter.value.trim().toLowerCase();
    const shown = entries.filter((entry) => !needle || entry.search.includes(needle));
    const expanded = (entry) => (entry.row.querySelector(".twist").getAttribute("aria-expanded") === "true" ? [entry.row, entry.detail] : [entry.row]);
    body.replaceChildren(...shown.flatMap(expanded));
    count.textContent = needle ? `${shown.length} of ${rows.length}` : `${rows.length} items`;
  };
  const filter = h("input", { class: "filter", type: "search", placeholder: "Filter", "aria-label": "Filter rows", on: { input: apply } });
  apply();
  return h("div", { class: "records" },
    h("div", { class: "tools" }, filter, count),
    h("div", { class: "table-wrap" }, h("table", null,
      h("thead", null, h("tr", null, columns.map((key) => h("th", { scope: "col" }, label(key))))),
      body)));
}

const cell = (item) => (isBlank(item) ? h("span", { class: "dim" }, "—") : isPlain(item) ? String(item) : h("span", { class: "dim" }, "…"));

// ---- reading

async function showFile(store, path) {
  const turn = ++state.turn;
  try {
    const file = await api("GET", "/api/file", { store, path });
    if (turn !== state.turn) return;
    state.file = { store, path, ...file };
    state.lastFile = route(store, path);
    showReader();
  } catch (error) {
    if (turn !== state.turn) return;
    state.file = null;
    state.mode = "";
    setMain(bar(path), h("div", { class: "page" }, problem(error)));
  }
}

function dirtyChip(dirty) {
  return dirty && h("span", { class: "chip warn", title: "Changed outside the dashboard. The next save records it as its own commit." }, "uncommitted");
}

function showReader() {
  const { store, path, text, dirty, writable } = state.file;
  state.mode = "read";
  document.title = `${path} · Brain`;
  const body = renderText(path, text, { store, path });
  setMain(
    bar(path, writable ? dirtyChip(dirty) : h("span", { class: "chip" }, "view only"),
      writable && [button("History", showHistory), button("Edit", startEdit, "primary")]),
    h("article", { class: `page${body.classList.contains("records") ? " wide" : ""}` }, body),
  );
}

// ---- editing: one view for a change and for a new file

function startEdit() {
  const { path, text, hash } = state.file;
  const area = h("textarea", { class: "code", spellcheck: "false", wrap: /\.ya?ml$/i.test(path) ? "off" : "soft", "aria-label": `Text of ${path}` });
  area.value = text;
  openEditor({ path: () => path, base: hash, original: text, area }, h("h1", { class: "crumbs", title: path }, h("span", { class: "dim" }, "Editing "), path));
}

function showNew() {
  state.mode = "new";
  document.title = "New file · Brain";
  const folders = [...new Set(state.tree.user.map((file) => file.path.split("/").slice(0, -1).join("/")))];
  if (!folders.includes("")) folders.push("");
  const folder = h("select", { "aria-label": "Folder" }, folders.sort().map((one) => h("option", { value: one }, one ? `${one} /` : "top level")));
  const name = h("input", { class: "name", type: "text", placeholder: "name.md", "aria-label": "File name", autocomplete: "off", spellcheck: "false" });
  const path = () => {
    const given = name.value.trim();
    return given && (folder.value ? `${folder.value}/${given}` : given);
  };
  const area = h("textarea", { class: "code", spellcheck: "false", "aria-label": "Text of the new file" });
  const editor = { path, original: "", area, create: true };
  name.addEventListener("input", () => editor.draw());
  folder.addEventListener("change", () => editor.draw());
  openEditor(editor, h("div", { class: "new-path" }, folder, name));
  name.focus();
}

function openEditor(editor, heading) {
  const { area } = editor;
  state.editor = editor;
  state.mode = "edit";
  editor.note = h("div", { class: "note" });
  const preview = h("div", { class: "preview" });
  const save = button("Save", () => saveEditor(), "primary");
  editor.draw = () => {
    const path = editor.path() || "untitled.md";
    preview.replaceChildren(renderText(path, area.value, { store: "user", path }));
    save.disabled = !editor.path() || (!editor.create && area.value === editor.original);
  };
  area.addEventListener("input", debounce(editor.draw, 150));
  const grid = h("div", { class: "edit", "data-pane": "write" }, area, preview);
  const pane = (name, text) => button(text, () => { grid.dataset.pane = name; }, "ghost");
  setMain(
    h("header", { class: "bar" }, menuButton(), heading, h("div", { class: "actions" }, button("Cancel", cancelEdit), save)),
    editor.note,
    h("div", { class: "panes" }, pane("write", "Write"), pane("preview", "Preview")),
    grid,
  );
  main.classList.add("editing");
  editor.draw();
  area.focus();
}

function cancelEdit() {
  if (!mayLeave()) return;
  const create = state.editor.create;
  state.editor = null;
  if (create) location.hash = state.lastFile || "#/";
  else showReader();
}

async function saveEditor(anyway = false) {
  const editor = state.editor;
  if (!editor || editor.saving) return;
  const path = editor.path();
  if (!path || (!editor.create && editor.area.value === editor.original)) return;
  editor.saving = true;
  editor.note.replaceChildren();
  try {
    const text = editor.area.value;
    const result = editor.create
      ? await api("POST", "/api/file", { path, text })
      : await api("PUT", "/api/file", { path, text, base: anyway ? (await api("GET", "/api/file", { path })).hash : editor.base });
    state.editor = null;
    toast(`Saved as commit ${result.commit}`);
    await refreshTree();
    if (editor.create) location.hash = route("user", path);
    else await showFile("user", path);
    markCurrent();
  } catch (error) {
    editor.note.replaceChildren(problem(error, error.status === 409 ? conflictActions(editor) : []));
  } finally {
    editor.saving = false;
  }
}

function conflictActions(editor) {
  const load = attempt(editor.note, async () => {
    const latest = await api("GET", "/api/file", { path: editor.path() });
    editor.base = latest.hash;
    editor.original = latest.text;
    editor.area.value = latest.text;
    editor.note.replaceChildren();
    editor.draw();
  });
  return [button("Load latest (drops your text)", load), button("Save anyway", () => saveEditor(true))];
}

// ---- history

async function showHistory() {
  const { path, dirty } = state.file;
  const turn = ++state.turn;
  const note = h("div", { class: "note" });
  let rows = [];
  try {
    rows = await api("GET", "/api/history", { path });
  } catch (error) {
    note.replaceChildren(problem(error));
  }
  if (turn !== state.turn) return;
  state.mode = "history";
  const restore = (sha) => attempt(note, async () => {
    const result = await api("POST", "/api/restore", { path, sha });
    toast(`Restored as commit ${result.commit}`);
    await refreshTree();
    await showFile("user", path);
  });
  const undoTo = dirty ? rows[0] : rows[1];
  setMain(
    bar(path, dirtyChip(dirty), [
      undoTo && button("Undo last change", restore(undoTo.sha)),
      button("Back to file", showReader),
    ]),
    note,
    h("section", { class: "page" },
      h("h2", { class: "section-title" }, "History"),
      h("ol", { class: "events" },
        dirty && event(path, { title: "Uncommitted changes" }),
        rows.map((row, index) => event(path, {
          ...row,
          title: row.subject,
          current: !dirty && index === 0,
          restore: dirty || index > 0 ? restore(row.sha) : null,
        }))),
      !dirty && !rows.length && h("p", { class: "empty" }, "No commits touch this file yet.")),
  );
}

function event(path, { title, date, via, sha, current, restore }) {
  const changes = h("pre", { class: "diff", hidden: true });
  const toggle = button("Changes", attempt(changes, async () => {
    changes.hidden = !changes.hidden;
    if (changes.hidden || changes.childElementCount) return;
    const { diff } = await api("GET", "/api/diff", sha ? { path, sha } : { path });
    drawDiff(changes, diff, sha);
  }), "small");
  return h("li", { class: "event" },
    h("div", { class: "event-head" },
      h("span", { class: "event-title" }, title),
      current && h("span", { class: "chip" }, "current"),
      via && h("span", { class: "chip" }, via),
      date && h("time", { datetime: date, title: new Date(date).toLocaleString() }, ago(date)),
      h("span", { class: "actions" }, toggle, restore && button("Restore this version", restore, "small"))),
    changes);
}

function drawDiff(box, diff, sha) {
  const lines = diff.split("\n");
  const start = lines.findIndex((line) => line.startsWith("@@"));
  const shown = start < 0 ? lines : lines.slice(start);
  if (!diff) shown[0] = sha ? "No change to show." : "New file, not committed yet.";
  box.replaceChildren(...shown.map((line) => {
    const kind = line.startsWith("@@") ? "hunk" : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : "";
    return h("span", { class: kind }, line || " ");
  }));
}

// ---- search

async function showSearch(query) {
  const turn = ++state.turn;
  state.mode = "search";
  document.title = "Search · Brain";
  if (document.activeElement !== searchBox) searchBox.value = query;
  const heading = () => h("header", { class: "bar" }, menuButton(), h("h1", { class: "crumbs" }, h("span", { class: "dim" }, "Search "), query));
  let hits;
  try {
    hits = await api("GET", "/api/search", { q: query });
  } catch (error) {
    if (turn === state.turn) setMain(heading(), h("div", { class: "page" }, problem(error)));
    return;
  }
  if (turn !== state.turn) return;
  const files = new Map();
  for (const hit of hits) {
    const key = `${hit.store}/${hit.path}`;
    if (!files.has(key)) files.set(key, []);
    files.get(key).push(hit);
  }
  setMain(heading(), h("section", { class: "page" },
    !hits.length && h("p", { class: "empty" }, "No matches."),
    hits.length >= MAX_HITS && h("p", { class: "dim" }, `Showing the first ${MAX_HITS} per store. Narrow the search for more.`),
    [...files.values()].map((group) => h("div", { class: "hits" },
      h("a", { class: "hit-file", href: route(group[0].store, group[0].path) },
        group[0].store === "skills" && h("span", { class: "chip" }, "skill"), group[0].path),
      h("ul", null, group.map((hit) => h("li", null,
        h("span", { class: "line-no" }, hit.line), h("span", null, highlight(hit.text, query)))))))));
}

function highlight(text, query) {
  const lower = text.toLowerCase();
  const needle = query.toLowerCase();
  const parts = [];
  let at = 0;
  for (let found = lower.indexOf(needle); found >= 0; found = lower.indexOf(needle, at)) {
    parts.push(text.slice(at, found), h("mark", null, text.slice(found, found + needle.length)));
    at = found + needle.length;
  }
  parts.push(text.slice(at));
  return parts;
}

// ---- file tree

async function refreshTree() {
  state.tree = await api("GET", "/api/tree");
  drawTree();
}

function folderOf(files) {
  const root = { folders: new Map(), files: [] };
  for (const file of files) {
    const parts = file.path.split("/");
    let node = root;
    for (const part of parts.slice(0, -1)) {
      if (!node.folders.has(part)) node.folders.set(part, { folders: new Map(), files: [] });
      node = node.folders.get(part);
    }
    node.files.push({ ...file, name: parts.at(-1) });
  }
  return root;
}

function disclosure(key, opened, summary, kids, title) {
  return h("details", {
    open: state.open.get(key) ?? opened,
    title,
    on: { toggle: (event) => state.open.set(key, event.target.open) },
  }, h("summary", null, summary), h("div", { class: "branch" }, kids));
}

function drawFolder(node, prefix) {
  return [
    node.files.map((file) => h("a", { href: route("user", file.path) }, file.name,
      file.dirty && h("span", { class: "dot", title: "Uncommitted changes" }))),
    [...node.folders].map(([name, child]) => disclosure(`user:${prefix}${name}`, true, name, drawFolder(child, `${prefix}${name}/`))),
  ];
}

function drawTree() {
  treeBox.replaceChildren(
    h("h2", null, "Knowledge"),
    ...drawFolder(folderOf(state.tree.user), "").flat(Infinity),
    h("h2", null, "Skills", h("small", null, "view only")),
    ...state.tree.skills.map((skill) => disclosure(`skill:${skill.name}`, false, skill.name,
      skill.files.map((file) => h("a", { href: route("skills", `${skill.name}/${file}`) }, file)), skill.description)),
  );
  markCurrent();
}

function markCurrent() {
  for (const link of treeBox.querySelectorAll("a")) {
    if (link.getAttribute("href") === location.hash) {
      link.setAttribute("aria-current", "page");
      for (let folder = link.closest("details"); folder; folder = folder.parentElement.closest("details")) folder.open = true;
    } else {
      link.removeAttribute("aria-current");
    }
  }
}

// ---- page events

const runSearch = debounce(() => {
  const query = searchBox.value.trim();
  const searching = parseRoute().head === "search";
  if (query.length >= 2) {
    const target = `#/search/${encodeURIComponent(query)}`;
    if (searching) location.replace(target);
    else location.hash = target;
  } else if (!query && searching) {
    location.hash = state.lastFile || "#/";
  }
}, 200);

// Coming back to the tab picks up what agents changed; an open editor relies on the save's conflict check.
async function refreshOnReturn() {
  if (document.hidden) return;
  await refreshTree().catch(() => {});
  if (state.mode !== "read" || !state.file) return;
  const { store, path, hash, dirty } = state.file;
  const turn = state.turn;
  const fresh = await api("GET", "/api/file", { store, path }).catch(() => null);
  if (fresh && turn === state.turn && (fresh.hash !== hash || fresh.dirty !== dirty)) {
    state.file = { store, path, ...fresh };
    showReader();
  }
}

searchBox.addEventListener("input", runSearch);
document.getElementById("new").addEventListener("click", () => { location.hash = "#/new"; });
document.getElementById("scrim").addEventListener("click", () => document.body.classList.remove("nav-open"));
document.addEventListener("visibilitychange", refreshOnReturn);
window.addEventListener("hashchange", navigate);
window.addEventListener("beforeunload", (event) => { if (edited()) event.preventDefault(); });
window.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s" && state.editor) {
    event.preventDefault();
    saveEditor();
  }
});

refreshTree().then(navigate, (error) => setMain(bar("Brain"), h("div", { class: "page" }, problem(error))));
