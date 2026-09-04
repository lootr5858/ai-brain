"use strict";

const opsCsrf = document.querySelector('meta[name="csrf-token"]').content;
const operationResult = document.querySelector('#operation-result');

async function postOperation(path, body) {
  const response = await fetch(path, {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-CSRF-Token': opsCsrf},
    body: JSON.stringify(body),
  });
  const payload = await response.json();
  if (!response.ok || !payload.ok) throw new Error(payload.message || 'Operation failed.');
  return payload.result;
}

for (const button of document.querySelectorAll('button[data-service]')) {
  button.addEventListener('click', async () => {
    button.disabled = true;
    operationResult.textContent = `${button.dataset.action} requested…`;
    try {
      await postOperation('/api/services/action', {service: button.dataset.service, action: button.dataset.action});
      location.reload();
    } catch (error) {
      operationResult.textContent = error.message;
      button.disabled = false;
    }
  });
}

for (const button of document.querySelectorAll('button[data-backup-action]')) {
  button.addEventListener('click', async () => {
    const action = button.dataset.backupAction;
    const confirmed = action !== 'replace' || window.confirm(
      'Replace current user data and brain.config.toml with the verified staged restore?'
    );
    if (!confirmed) return;
    button.disabled = true;
    operationResult.textContent = `${action} requested…`;
    try {
      await postOperation('/api/backup/action', {action, confirm: action === 'replace'});
      location.reload();
    } catch (error) {
      operationResult.textContent = error.message;
      button.disabled = false;
    }
  });
}

const addClient = document.querySelector('#brain-mcp-add');
if (addClient) addClient.addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = addClient.querySelector('button');
  const fields = new FormData(addClient);
  button.disabled = true;
  operationResult.textContent = 'Adding client…';
  try {
    const created = await postOperation('/api/brain-mcp/action', {
      action: 'add-client',
      name: fields.get('name'),
      profile: fields.get('profile'),
      redirect_uri: fields.get('redirect_uri'),
    });
    const credentials = document.querySelector('#brain-mcp-credentials');
    credentials.textContent = `Brain-mcp URL: ${created.resource}\nClient ID: ${created.client_id}\nClient secret: ${created.client_secret}\n\nSave this now. It cannot be displayed again.`;
    credentials.hidden = false;
    addClient.reset();
    operationResult.textContent = `Added ${created.name}.`;
  } catch (error) {
    operationResult.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

for (const button of document.querySelectorAll('button[data-mcp-revoke]')) {
  button.addEventListener('click', async () => {
    const name = button.dataset.mcpRevoke;
    if (!window.confirm(`Revoke ${name} and all of its active credentials?`)) return;
    button.disabled = true;
    operationResult.textContent = `Revoking ${name}…`;
    try {
      await postOperation('/api/brain-mcp/action', {action: 'revoke-client', name, confirm: true});
      location.reload();
    } catch (error) {
      operationResult.textContent = error.message;
      button.disabled = false;
    }
  });
}
const csrf = document.querySelector('meta[name="csrf-token"]').content;
const tree = document.getElementById("content-tree");
const searchForm = document.getElementById("content-search");
const searchStatus = document.getElementById("search-status");
const searchResults = document.getElementById("search-results");
const pageTitle = document.getElementById("page-title");
const rendered = document.getElementById("page-rendered");
const source = document.getElementById("page-source");
const backlinks = document.getElementById("backlinks");
const catalogStatus = document.getElementById("catalog-status");
const catalogRebuild = document.getElementById("catalog-rebuild");
const semanticStatus = document.getElementById("semantic-status");
const showRendered = document.getElementById("show-rendered");
const showSource = document.getElementById("show-source");
const pageEdit = document.getElementById("page-edit");
const pageNew = document.getElementById("page-new");
const editorStatus = document.getElementById("editor-status");
const editorForm = document.getElementById("page-editor-form");
const editorPath = document.getElementById("editor-path");
const pageEditor = document.getElementById("page-editor");
const editorContentLabel = document.getElementById("editor-content-label");
const editorCancel = document.getElementById("editor-cancel");
const pageActions = document.getElementById("page-actions");
const destinationPath = document.getElementById("destination-path");
const pageMove = document.getElementById("page-move");
const pageArchive = document.getElementById("page-archive");
const pageDelete = document.getElementById("page-delete");
const pageUndo = document.getElementById("page-undo");

let currentPage = null;
let editorBase = null;
let creating = false;

async function api(path, payload = {}) {
  const response = await fetch(path, {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf},
    body: JSON.stringify(payload),
  });
  const body = await response.json();
  if (!body.ok) {
    const error = new Error(`${body.message} [${response.headers.get("X-Correlation-ID")}]`);
    error.code = body.error;
    error.result = body.result;
    throw error;
  }
  return body.result;
}

function clear(element) {
  element.replaceChildren();
}

function setRendered(html) {
  rendered.innerHTML = html;
}

function setView(view) {
  const sourceVisible = view === "source";
  const editing = !editorForm.classList.contains("is-hidden");
  source.classList.toggle("is-hidden", !sourceVisible || editing);
  rendered.classList.toggle("is-hidden", sourceVisible);
  if (editing) {
    pageEditor.classList.toggle("is-hidden", !sourceVisible);
    editorContentLabel.classList.toggle("is-hidden", !sourceVisible);
  }
  showSource.setAttribute("aria-pressed", String(sourceVisible));
  showRendered.setAttribute("aria-pressed", String(!sourceVisible));
}

function pageButton(item, compact = false) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = compact ? item.title : `${item.title} — ${item.path}`;
  button.title = item.path;
  button.dataset.contentRepository = item.repository;
  button.dataset.contentPath = item.path;
  button.addEventListener("click", () => openPage(item));
  return button;
}

function markSelected(page) {
  document.querySelectorAll("[data-content-path]").forEach((button) => {
    const selected = button.dataset.contentRepository === page.repository
      && button.dataset.contentPath === page.path;
    if (selected) {
      button.setAttribute("aria-current", "page");
      for (let directory = button.closest("details"); directory;
        directory = directory.parentElement.closest("details")) directory.open = true;
    } else button.removeAttribute("aria-current");
  });
}

function listMessage(list, message) {
  clear(list);
  const item = document.createElement("li");
  item.textContent = message;
  list.append(item);
}

function renderBacklinks(items) {
  if (!items.length) return listMessage(backlinks, "No backlinks.");
  clear(backlinks);
  for (const page of items) {
    const item = document.createElement("li");
    item.append(pageButton(page));
    backlinks.append(item);
  }
}

function editingState(page) {
  editorForm.classList.add("is-hidden");
  pageNew.classList.remove("is-hidden");
  const editable = page.repository === "user" && page.kind === "raw" && !page.dirty;
  pageEdit.classList.toggle("is-hidden", !editable || page.deleted);
  pageActions.classList.toggle("is-hidden", !editable);
  pageMove.classList.toggle("is-hidden", !editable || page.deleted);
  pageArchive.classList.toggle("is-hidden", !editable || page.deleted || page.format !== "markdown");
  pageDelete.classList.toggle("is-hidden", !editable || page.deleted);
  pageUndo.classList.toggle("is-hidden", !editable);
  destinationPath.value = "";
  editorStatus.textContent = page.dirty
    ? "Read-only: this target has uncommitted changes. Clean it outside the dashboard first."
    : editable ? "Ready to edit." : "Read-only content.";
}

async function openPage(item) {
  pageTitle.textContent = "Loading page…";
  listMessage(backlinks, "Loading backlinks…");
  const target = {repository: item.repository, path: item.path};
  try {
    const page = await api("/api/content/page", target);
    currentPage = page;
    pageTitle.textContent = page.title;
    setRendered(page.rendered_html);
    source.textContent = page.content;
    editingState(page);
    setView(page.format === "markdown" ? "rendered" : "source");
    markSelected(page);
  } catch (error) {
    currentPage = null;
    pageTitle.textContent = "Page unavailable";
    rendered.textContent = error.message;
    source.textContent = "";
    listMessage(backlinks, "Backlinks unavailable.");
    return;
  }
  try {
    renderBacklinks((await api("/api/content/backlinks", target)).results);
  } catch (error) {
    listMessage(backlinks, `Backlinks unavailable. ${error.message}`);
  }
}

function renderBranch(pages, depth = 0) {
  const list = document.createElement("ul");
  const directories = new Map();
  for (const page of pages) {
    const parts = page.path.split("/");
    if (parts.length === depth + 1) {
      const item = document.createElement("li");
      item.append(pageButton(page, true));
      list.append(item);
      continue;
    }
    const name = parts[depth];
    if (!directories.has(name)) directories.set(name, []);
    directories.get(name).push(page);
  }
  for (const [name, children] of directories) {
    const item = document.createElement("li");
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = name;
    details.append(summary, renderBranch(children, depth + 1));
    item.append(details);
    list.append(item);
  }
  return list;
}

async function loadTree() {
  const result = await api("/api/content/tree");
  clear(tree);
  for (const [repository, pages] of Object.entries(result.repositories)) {
    const group = document.createElement("section");
    group.className = "tree-group";
    const heading = document.createElement("h3");
    heading.textContent = repository;
    const ordered = pages
      .map((page) => ({...page, repository}))
      .sort((left, right) => left.path.localeCompare(right.path));
    group.append(heading, renderBranch(ordered));
    tree.append(group);
  }
  if (!tree.children.length) tree.textContent = "No content found.";
}

async function loadStatus() {
  const status = await api("/api/content/catalog/status");
  catalogStatus.textContent = status.available
    ? `${status.indexed} indexed; ${status.skipped} skipped; ${status.errors} errors.`
    : "Catalog unavailable; direct browsing remains available.";
  semanticStatus.textContent = status.semantic_available
    ? "Semantic search available."
    : "Semantic search unavailable; lexical search remains available.";
}

function startEditor(create) {
  editorBase = currentPage;
  creating = create;
  editorPath.readOnly = !create;
  editorPath.value = create ? "" : currentPage.path;
  pageEditor.value = create ? "" : currentPage.content;
  pageTitle.textContent = create ? "New user file" : `Edit ${currentPage.title}`;
  editorForm.classList.remove("is-hidden");
  pageNew.classList.add("is-hidden");
  pageActions.classList.add("is-hidden");
  pageEdit.classList.add("is-hidden");
  source.classList.add("is-hidden");
  rendered.classList.add("is-hidden");
  pageEditor.classList.remove("is-hidden");
  editorContentLabel.classList.remove("is-hidden");
  showSource.setAttribute("aria-pressed", "true");
  showRendered.setAttribute("aria-pressed", "false");
  editorStatus.textContent = create ? "Enter a repository-relative user path." : "Unsaved draft.";
  (create ? editorPath : pageEditor).focus();
}

function finishMutation(message, result) {
  editorStatus.textContent = `${message} Commit ${result.commit.slice(0, 12)}.`;
}

searchForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(searchForm);
  const payload = {query: form.get("query"), mode: form.get("mode")};
  searchStatus.textContent = "Searching…";
  clear(searchResults);
  try {
    const result = await api("/api/content/search", payload);
    searchStatus.textContent = result.results.length
      ? `${result.results.length} result${result.results.length === 1 ? "" : "s"}.`
      : "No results.";
    if (payload.mode === "semantic") {
      semanticStatus.textContent = result.semantic_available
        ? "Semantic search available."
        : "Semantic search unavailable; showing lexical results.";
    }
    for (const page of result.results) {
      const item = document.createElement("li");
      item.append(pageButton(page));
      const snippet = document.createElement("p");
      snippet.className = "result-snippet";
      snippet.textContent = page.snippet;
      item.append(snippet);
      searchResults.append(item);
    }
  } catch (error) {
    searchStatus.textContent = error.message;
  }
});

showRendered.addEventListener("click", async () => {
  if (editorForm.classList.contains("is-hidden")) return setView("rendered");
  if (!editorPath.value.endsWith(".md")) {
    editorStatus.textContent = "Preview is available for Markdown files only.";
    return;
  }
  editorStatus.textContent = "Rendering draft…";
  try {
    setRendered((await api("/api/editing/preview", {content: pageEditor.value})).rendered_html);
    setView("rendered");
    editorStatus.textContent = "Previewing unsaved draft.";
  } catch (error) {
    editorStatus.textContent = error.message;
  }
});
showSource.addEventListener("click", () => setView("source"));
pageNew.addEventListener("click", () => startEditor(true));
pageEdit.addEventListener("click", () => startEditor(false));
editorCancel.addEventListener("click", () => {
  if (editorBase) {
    currentPage = editorBase;
    pageTitle.textContent = editorBase.title;
    setRendered(editorBase.rendered_html);
    source.textContent = editorBase.content;
    editingState(editorBase);
    setView(editorBase.format === "markdown" ? "rendered" : "source");
  } else {
    editorForm.classList.add("is-hidden");
    pageNew.classList.remove("is-hidden");
    pageTitle.textContent = "Select a page";
    editorStatus.textContent = "";
  }
});

editorForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const path = editorPath.value;
  const content = pageEditor.value;
  const route = creating ? "create" : "edit";
  const payload = creating
    ? {path, content}
    : {path, content, expected_digest: currentPage.digest};
  editorStatus.textContent = "Saving and committing…";
  try {
    const result = await api(`/api/editing/${route}`, payload);
    await loadTree();
    await openPage({repository: "user", path: result.path});
    finishMutation(creating ? "Created." : "Saved.", result);
  } catch (error) {
    editorStatus.textContent = error.message;
  }
});

pageMove.addEventListener("click", async () => {
  const destination = destinationPath.value;
  const parent = (path) => path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
  const action = parent(destination) === parent(currentPage.path) ? "rename" : "move";
  editorStatus.textContent = "Moving and committing…";
  try {
    const result = await api(`/api/editing/${action}`, {
      path: currentPage.path,
      destination,
      expected_digest: currentPage.digest,
    });
    await loadTree();
    await openPage({repository: "user", path: result.path});
    finishMutation(action === "rename" ? "Renamed." : "Moved.", result);
  } catch (error) {
    editorStatus.textContent = error.message;
  }
});

pageArchive.addEventListener("click", async () => {
  editorStatus.textContent = "Archiving and committing…";
  try {
    const result = await api("/api/editing/archive", {
      path: currentPage.path,
      expected_digest: currentPage.digest,
    });
    await openPage({repository: "user", path: result.path});
    finishMutation("Archived.", result);
  } catch (error) {
    editorStatus.textContent = error.message;
  }
});

pageDelete.addEventListener("click", async () => {
  if (!window.confirm(`Delete ${currentPage.path}?`)) return;
  editorStatus.textContent = "Deleting and committing…";
  try {
    const result = await api("/api/editing/delete", {
      path: currentPage.path,
      expected_digest: currentPage.digest,
    });
    currentPage = {...currentPage, digest: "", content: "", rendered_html: "", deleted: true};
    pageTitle.textContent = `Deleted ${result.path}`;
    rendered.textContent = "";
    source.textContent = "";
    listMessage(backlinks, "File deleted.");
    editingState(currentPage);
    await loadTree();
    finishMutation("Deleted.", result);
  } catch (error) {
    editorStatus.textContent = error.message;
  }
});

pageUndo.addEventListener("click", async () => {
  editorStatus.textContent = "Restoring the previous Git version…";
  try {
    const result = await api("/api/editing/undo", {
      path: currentPage.path,
      expected_digest: currentPage.digest,
    });
    await loadTree();
    await openPage({repository: "user", path: result.path});
    finishMutation("Restored.", result);
  } catch (error) {
    editorStatus.textContent = error.message;
  }
});

catalogRebuild.addEventListener("click", async () => {
  catalogRebuild.disabled = true;
  catalogStatus.textContent = "Rebuilding catalog…";
  try {
    await api("/api/content/catalog/rebuild");
    await loadStatus();
    await loadTree();
  } catch (error) {
    catalogStatus.textContent = error.message;
  } finally {
    catalogRebuild.disabled = false;
  }
});

loadTree().catch((error) => { tree.textContent = error.message; });
loadStatus().catch((error) => {
  catalogStatus.textContent = error.message;
  semanticStatus.textContent = "Semantic status unavailable.";
});
