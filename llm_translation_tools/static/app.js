(() => {
  "use strict";

  // Keep endpoint and payload differences isolated here. The UI below only calls
  // these methods, so backend route changes have a single place to update.
  const ENDPOINTS = Object.freeze({
    project: "/api/project",
    files: "/api/files",
    file: "/api/file",
    settings: "/api/settings",
    models: "/api/models",
    jobs: "/api/jobs",
  });

  class ApiError extends Error {
    constructor(message, status = 0, details = null) {
      super(message);
      this.name = "ApiError";
      this.status = status;
      this.details = details;
    }
  }

  const api = {
    async request(url, { method = "GET", body, signal } = {}) {
      const options = {
        method,
        headers: { Accept: "application/json" },
        signal,
      };

      if (body !== undefined) {
        options.headers["Content-Type"] = "application/json";
        options.body = JSON.stringify(body);
      }

      let response;
      try {
        response = await fetch(url, options);
      } catch (error) {
        setConnectionState(false);
        if (error?.name === "AbortError") throw error;
        throw new ApiError("Could not reach the local translation server. Is it running?", 0, error);
      }

      setConnectionState(true);
      const contentType = response.headers.get("content-type") || "";
      let payload = null;
      if (response.status !== 204) {
        try {
          payload = contentType.includes("json") ? await response.json() : await response.text();
        } catch {
          payload = null;
        }
      }

      if (!response.ok) {
        const message =
          payload?.detail?.message ||
          payload?.detail ||
          payload?.error?.message ||
          payload?.error ||
          payload?.message ||
          (typeof payload === "string" && payload.trim()) ||
          `Request failed (${response.status})`;
        throw new ApiError(String(message), response.status, payload);
      }

      return payload ?? {};
    },

    openProject(path) {
      return this.request(ENDPOINTS.project + "/open", { method: "POST", body: { path } });
    },

    getProject() {
      return this.request(ENDPOINTS.project);
    },

    getFiles() {
      return this.request(ENDPOINTS.files);
    },

    getFile(path, signal) {
      const query = new URLSearchParams({ path });
      return this.request(`${ENDPOINTS.file}?${query}`, { signal });
    },

    saveFile(path, token, updates) {
      return this.request(ENDPOINTS.file, {
        method: "PUT",
        body: { path, token, updates },
      });
    },

    getSettings() {
      return this.request(ENDPOINTS.settings);
    },

    saveSettings(settings) {
      return this.request(ENDPOINTS.settings, { method: "PUT", body: settings });
    },

    getModels() {
      return this.request(ENDPOINTS.models);
    },

    createJob(payload) {
      return this.request(ENDPOINTS.jobs, { method: "POST", body: payload });
    },

    getJob(id) {
      return this.request(`${ENDPOINTS.jobs}/${encodeURIComponent(id)}`);
    },

    getJobResult(id) {
      return this.request(`${ENDPOINTS.jobs}/${encodeURIComponent(id)}/result`);
    },

    async cancelJob(id) {
      try {
        return await this.request(`${ENDPOINTS.jobs}/${encodeURIComponent(id)}/cancel`, { method: "POST", body: {} });
      } catch (error) {
        // Compatibility with the initial API contract. Remove this fallback if
        // DELETE cancellation is never supported by older server builds.
        if (error instanceof ApiError && (error.status === 404 || error.status === 405)) {
          return this.request(`${ENDPOINTS.jobs}/${encodeURIComponent(id)}`, { method: "DELETE" });
        }
        throw error;
      }
    },
  };

  const DEFAULT_SETTINGS = Object.freeze({
    base_url: "http://127.0.0.1:1234/v1",
    model: "",
    system_prompt: "",
    game_context: "",
    target_language: "English",
    temperature: 0.2,
    max_tokens: 4096,
    batch_size: 20,
    context_before: 12,
    context_after: 4,
    allow_remote_lmstudio: false,
  });

  const state = {
    projectPath: "",
    files: [],
    projectStats: {},
    activeFile: null,
    selected: new Set(),
    dirty: new Map(),
    suggestions: new Map(),
    settings: { ...DEFAULT_SETTINGS },
    job: null,
    jobPollTimer: null,
    jobPollFailures: 0,
    saving: false,
    fileFilter: "",
    lineFilter: "",
    loadSequence: 0,
    fileAbortController: null,
    reviewReturnFocus: null,
  };

  const elementIds = [
    "connection-dot", "project-label", "error-banner", "error-title", "error-message", "dismiss-error",
    "open-project-form", "project-path", "open-project-button", "project-summary", "project-path-label",
    "stat-files", "stat-lines", "stat-translated", "project-progress", "completion-label", "file-browser",
    "file-filter", "file-count", "file-list", "no-files", "welcome-state", "focus-path-button", "editor",
    "active-file-name", "active-file-path", "save-state", "save-button", "selection-count", "select-untranslated",
    "select-all", "select-none", "translate-selected", "translate-untranslated", "translate-file", "review-suggestions",
    "line-filter", "visible-line-count", "job-panel", "job-title", "job-message", "job-progressbar", "job-progress",
    "cancel-job", "line-list", "empty-lines", "review-panel", "suggestion-summary", "close-review", "accept-all",
    "reject-all", "suggestion-list", "review-empty", "app-status", "settings-button", "settings-dialog",
    "settings-form", "setting-base-url", "setting-model", "models-list", "models-status", "setting-target-language",
    "setting-batch-size", "setting-context-before", "setting-context-after", "setting-temperature", "setting-max-tokens",
    "setting-allow-remote", "setting-game-context", "setting-system-prompt", "load-models", "settings-status",
    "save-settings", "shortcuts-button", "shortcuts-dialog",
  ];

  const elements = Object.fromEntries(elementIds.map((id) => [id, document.getElementById(id)]));
  const $ = (id) => elements[id];

  function createElement(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = String(text);
    return element;
  }

  function baseName(path) {
    const parts = String(path || "").split(/[\\/]/).filter(Boolean);
    return parts.at(-1) || String(path || "Untitled");
  }

  function formatNumber(value) {
    const numeric = Number(value);
    return Number.isFinite(numeric) ? numeric.toLocaleString() : "—";
  }

  function clamp(value, minimum, maximum) {
    return Math.min(maximum, Math.max(minimum, value));
  }

  function setConnectionState(connected) {
    $("connection-dot").classList.toggle("connected", connected);
  }

  function setStatus(message) {
    $("app-status").textContent = message || "Ready";
  }

  function showError(message, title = "Something went wrong") {
    $("error-title").textContent = title;
    $("error-message").textContent = message instanceof Error ? message.message : String(message);
    $("error-banner").hidden = false;
  }

  function clearError() {
    $("error-banner").hidden = true;
    $("error-message").textContent = "";
  }

  function setButtonBusy(button, busy, busyLabel = "Working…") {
    if (!button) return;
    if (busy) {
      button.dataset.originalLabel = button.textContent;
      button.textContent = busyLabel;
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
    } else {
      if (button.dataset.originalLabel) button.textContent = button.dataset.originalLabel;
      delete button.dataset.originalLabel;
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
  }

  function safeStorageGet(key) {
    try {
      return localStorage.getItem(key) || "";
    } catch {
      return "";
    }
  }

  function safeStorageSet(key, value) {
    try {
      localStorage.setItem(key, value);
    } catch {
      // Storage is a convenience only; the project remains open without it.
    }
  }

  function normalizeFileEntry(raw) {
    if (typeof raw === "string") raw = { path: raw };
    raw = raw || {};
    const path = String(raw.path ?? raw.file ?? raw.name ?? "");
    const lineCount = Number(raw.line_count ?? raw.lines ?? raw.total ?? 0);
    const translatableCount = Number(raw.translatable_count ?? raw.writable_count ?? lineCount);
    const translatedCount = Number(raw.translated_count ?? raw.translated ?? raw.completed ?? 0);
    return {
      path,
      name: String(raw.name || baseName(path)),
      schema: String(raw.schema ?? raw.format ?? ""),
      line_count: Number.isFinite(lineCount) ? lineCount : 0,
      translatable_count: Number.isFinite(translatableCount) ? translatableCount : lineCount,
      translated_count: Number.isFinite(translatedCount) ? translatedCount : 0,
      token: raw.token ?? null,
    };
  }

  function normalizeFiles(payload) {
    const project = payload?.project ?? payload ?? {};
    const rawFiles = Array.isArray(project) ? project : (project.files ?? payload?.files ?? []);
    if (!Array.isArray(rawFiles)) return [];
    return rawFiles
      .map(normalizeFileEntry)
      .filter((file) => file.path)
      .sort((a, b) => a.path.localeCompare(b.path, undefined, { numeric: true, sensitivity: "base" }));
  }

  function normalizeContext(value) {
    if (value === null || value === undefined) return "";
    if (typeof value === "string") return value;
    if (Array.isArray(value)) return value.filter(Boolean).map(normalizeContext).join("\n");
    if (typeof value === "object") {
      return Object.entries(value)
        .filter(([, item]) => item !== null && item !== undefined && item !== "")
        .map(([key, item]) => `${key}: ${typeof item === "object" ? JSON.stringify(item) : item}`)
        .join(" · ");
    }
    return String(value);
  }

  function normalizeLine(raw, fallbackIndex) {
    raw = raw || {};
    const metadata = raw.metadata && typeof raw.metadata === "object" ? raw.metadata : {};
    const id = raw.id ?? raw.line_id ?? raw.index ?? fallbackIndex;
    const hasTranslationField = Object.prototype.hasOwnProperty.call(raw, "translation");
    const rawTranslation = hasTranslationField ? raw.translation : (raw.target ?? null);
    const translation = rawTranslation === null || rawTranslation === undefined ? "" : String(rawTranslation);
    const source = String(raw.source ?? raw.text ?? raw.original ?? "");
    const sourceSegments = raw.source_segments ?? metadata.jp_lines;
    const sourceDisplay = Array.isArray(sourceSegments) && sourceSegments.length
      ? sourceSegments.map(String).join("\n")
      : source;
    const translationActive = typeof raw.translation_active === "boolean"
      ? raw.translation_active
      : rawTranslation !== null && rawTranslation !== undefined;
    return {
      id,
      key: String(id),
      index: raw.index ?? metadata.index ?? fallbackIndex,
      source,
      sourceDisplay,
      translation,
      originalTranslation: translation,
      originalValue: translationActive ? translation : null,
      originalActive: translationActive,
      hasTranslation: translationActive,
      emptyIsApplied: Boolean(raw.empty_is_applied),
      speaker: String(raw.speaker ?? metadata.speaker ?? metadata.character ?? ""),
      kind: String(raw.kind ?? metadata.kind ?? metadata.type ?? ""),
      context: normalizeContext(raw.context ?? raw.note ?? metadata.context ?? metadata.note ?? ""),
      metadata,
      translatable: raw.translatable !== false,
    };
  }

  function normalizeFile(payload, requestedPath) {
    const file = payload?.file ?? payload ?? {};
    const rawLines = Array.isArray(file.lines) ? file.lines : (Array.isArray(file.entries) ? file.entries : []);
    return {
      path: String(file.path ?? requestedPath),
      schema: String(file.schema ?? file.format ?? ""),
      token: file.token ?? null,
      lines: rawLines.map((line, index) => normalizeLine(line, index)),
    };
  }

  function extractProjectPath(payload, fallback = "") {
    const project = payload?.project ?? payload ?? {};
    return String(project.path ?? project.root ?? project.project_path ?? payload?.path ?? fallback);
  }

  function extractProjectStats(payload) {
    const project = payload?.project ?? payload ?? {};
    return project.stats && typeof project.stats === "object" ? project.stats : {};
  }

  function applyProject(payload, fallbackPath = "") {
    state.fileAbortController?.abort();
    state.loadSequence += 1;
    const files = normalizeFiles(payload);
    state.projectPath = extractProjectPath(payload, fallbackPath) || fallbackPath;
    state.files = files;
    state.projectStats = extractProjectStats(payload);
    state.activeFile = null;
    state.selected.clear();
    state.dirty.clear();
    state.suggestions.clear();
    closeReview();
    safeStorageSet("translation-workbench.project-path", state.projectPath);
    $("project-path").value = state.projectPath;
    renderProject();
  }

  async function fetchProjectFiles(openPayload = null) {
    if (openPayload && normalizeFiles(openPayload).length) return openPayload;
    try {
      const filesPayload = await api.getFiles();
      if (openPayload && !extractProjectPath(filesPayload)) {
        return {
          ...filesPayload,
          path: extractProjectPath(openPayload, state.projectPath),
        };
      }
      return filesPayload;
    } catch (error) {
      if (openPayload) return openPayload;
      throw error;
    }
  }

  function renderProject() {
    const hasProject = Boolean(state.projectPath || state.files.length);
    $("project-summary").hidden = !hasProject;
    $("file-browser").hidden = !hasProject;
    $("project-path-label").textContent = state.projectPath || "Current project";
    $("project-path-label").title = state.projectPath;
    $("project-label").textContent = state.projectPath ? baseName(state.projectPath) : "Project open";
    renderProjectStats();
    renderFileList();

    $("welcome-state").hidden = Boolean(state.activeFile);
    $("editor").hidden = !state.activeFile;
    if (hasProject && !state.files.length) {
      const heading = $("welcome-state").querySelector("h1");
      const copy = $("welcome-state").querySelector("p");
      heading.textContent = "No extracted script JSON found";
      copy.textContent = "Choose a game folder containing script/ or point directly to the extracted script folder.";
    }
  }

  function currentFileCounts(fileEntry) {
    if (!state.activeFile || state.activeFile.path !== fileEntry.path) {
      return {
        total: fileEntry.line_count,
        translatable: fileEntry.translatable_count,
        translated: fileEntry.translated_count,
      };
    }
    const translatable = state.activeFile.lines.filter((line) => line.translatable);
    return {
      total: fileEntry.line_count || state.activeFile.lines.length,
      translatable: translatable.length,
      translated: translatable.filter((line) => line.hasTranslation).length,
    };
  }

  function projectTotals() {
    if (state.files.length) {
      return state.files.reduce((totals, file) => {
        const counts = currentFileCounts(file);
        totals.lines += counts.total;
        totals.translatable += counts.translatable;
        totals.translated += counts.translated;
        return totals;
      }, { lines: 0, translatable: 0, translated: 0 });
    }
    return {
      lines: Number(state.projectStats.line_count ?? state.projectStats.lines ?? 0),
      translatable: Number(state.projectStats.translatable_count ?? state.projectStats.translatable ?? state.projectStats.line_count ?? state.projectStats.lines ?? 0),
      translated: Number(state.projectStats.translated_count ?? state.projectStats.translated ?? 0),
    };
  }

  function renderProjectStats() {
    const totals = projectTotals();
    const percentage = totals.translatable ? Math.round((totals.translated / totals.translatable) * 100) : 0;
    $("stat-files").textContent = formatNumber(state.files.length);
    $("stat-lines").textContent = formatNumber(totals.lines);
    $("stat-translated").textContent = formatNumber(totals.translated);
    $("project-progress").style.width = `${clamp(percentage, 0, 100)}%`;
    $("completion-label").textContent = `${percentage}% translated`;
  }

  function renderFileList() {
    const query = state.fileFilter.trim().toLocaleLowerCase();
    const visibleFiles = state.files.filter((file) =>
      !query || `${file.name} ${file.path} ${file.schema}`.toLocaleLowerCase().includes(query)
    );
    const fragment = document.createDocumentFragment();

    visibleFiles.forEach((file) => {
      const button = createElement("button", "file-item");
      button.type = "button";
      button.disabled = state.saving;
      button.dataset.path = file.path;
      button.title = file.path;
      const active = state.activeFile?.path === file.path;
      button.classList.toggle("active", active);
      if (active) button.setAttribute("aria-current", "page");

      const icon = createElement("span", "file-icon", "JS");
      icon.setAttribute("aria-hidden", "true");
      const copy = createElement("span", "file-copy");
      copy.append(createElement("strong", "", file.name));
      copy.append(createElement("span", "", file.schema || file.path));
      const counts = currentFileCounts(file);
      const percent = counts.translatable ? Math.round((counts.translated / counts.translatable) * 100) : 0;
      const progress = createElement("span", "file-percent", `${percent}%`);
      progress.title = `${counts.translated} of ${counts.translatable} translatable lines (${counts.total} total)`;
      button.append(icon, copy, progress);
      fragment.append(button);
    });

    $("file-list").replaceChildren(fragment);
    $("file-count").textContent = query ? `${visibleFiles.length} of ${state.files.length}` : String(state.files.length);
    $("no-files").hidden = visibleFiles.length > 0;
  }

  function pendingSuggestions() {
    return [...state.suggestions.values()].filter((suggestion) => suggestion.status === "pending");
  }

  function confirmCanLeave(action) {
    const dirtyCount = state.dirty.size;
    const suggestionCount = pendingSuggestions().length;
    if (!dirtyCount && !suggestionCount) return true;
    const details = [];
    if (dirtyCount) details.push(`${dirtyCount} unsaved edit${dirtyCount === 1 ? "" : "s"}`);
    if (suggestionCount) details.push(`${suggestionCount} unreviewed suggestion${suggestionCount === 1 ? "" : "s"}`);
    return window.confirm(`You have ${details.join(" and ")}. Discard them and ${action}?`);
  }

  async function handleOpenProject(event) {
    event.preventDefault();
    clearError();
    const path = $("project-path").value.trim();
    if (!path) return;
    if (state.saving) return;
    if (!confirmCanLeave("open another folder")) return;
    if (state.job) {
      showError("Cancel the active translation job before opening another project.", "Translation in progress");
      return;
    }

    setButtonBusy($("open-project-button"), true, "Opening…");
    setStatus("Opening project…");
    try {
      const opened = await api.openProject(path);
      state.projectPath = path;
      const payload = await fetchProjectFiles(opened);
      applyProject(payload, extractProjectPath(opened, path));
      await loadSettings({ showErrors: true });
      setStatus(`Opened ${state.files.length} script file${state.files.length === 1 ? "" : "s"}`);
      if (state.files.length) await loadFile(state.files[0].path, { skipConfirm: true });
    } catch (error) {
      showError(error, "Could not open folder");
      setStatus("Folder could not be opened");
    } finally {
      setButtonBusy($("open-project-button"), false);
    }
  }

  async function loadFile(path, { skipConfirm = false } = {}) {
    if (!path || state.activeFile?.path === path) return;
    if (state.saving) return;
    if (!skipConfirm && !confirmCanLeave("open another script")) return;
    if (state.job) {
      showError("Cancel the active translation job before changing scripts.", "Translation in progress");
      return;
    }

    clearError();
    state.fileAbortController?.abort();
    state.fileAbortController = new AbortController();
    const sequence = ++state.loadSequence;
    $("file-list").setAttribute("aria-busy", "true");
    setStatus(`Loading ${baseName(path)}…`);

    try {
      const payload = await api.getFile(path, state.fileAbortController.signal);
      if (sequence !== state.loadSequence) return;
      state.activeFile = normalizeFile(payload, path);
      state.selected.clear();
      state.dirty.clear();
      state.suggestions.clear();
      state.lineFilter = "";
      $("line-filter").value = "";
      closeReview();
      renderActiveFile();
      setStatus(`Loaded ${state.activeFile.lines.length} lines from ${baseName(path)}`);
    } catch (error) {
      if (error?.name === "AbortError") return;
      showError(error, "Could not load script");
      setStatus("Script could not be loaded");
    } finally {
      if (sequence === state.loadSequence) $("file-list").removeAttribute("aria-busy");
    }
  }

  function renderActiveFile() {
    if (!state.activeFile) {
      renderProject();
      return;
    }
    $("welcome-state").hidden = true;
    $("editor").hidden = false;
    $("active-file-name").textContent = baseName(state.activeFile.path);
    $("active-file-path").textContent = state.activeFile.path;
    $("active-file-path").title = state.activeFile.path;
    renderFileList();
    renderLineList();
    updateDirtyUi();
    updateSelectionUi();
  }

  function lineMatchesFilter(line, query) {
    if (!query) return true;
    return `${line.sourceDisplay}\n${line.translation}\n${line.speaker}\n${line.context}\n${line.kind}`
      .toLocaleLowerCase()
      .includes(query);
  }

  function appendLineSection(container, label, value, className) {
    const section = createElement("div", "line-section");
    section.append(createElement("span", "line-label", label));
    section.append(createElement("p", className, value));
    container.append(section);
  }

  function renderLineList() {
    if (!state.activeFile) return;
    const query = state.lineFilter.trim().toLocaleLowerCase();
    const visibleLines = state.activeFile.lines.filter((line) => lineMatchesFilter(line, query));
    const fragment = document.createDocumentFragment();

    visibleLines.forEach((line, visibleIndex) => {
      const card = createElement("article", "line-card");
      card.dataset.lineKey = line.key;
      card.classList.toggle("is-selected", state.selected.has(line.key));
      card.classList.toggle("is-dirty", state.dirty.has(line.key));
      card.classList.toggle("is-untranslatable", !line.translatable);

      const selectWrap = createElement("label", "line-select-wrap");
      selectWrap.append(createElement("span", "sr-only", `Select line ${line.index}`));
      const checkbox = createElement("input", "line-select");
      checkbox.type = "checkbox";
      checkbox.dataset.lineKey = line.key;
      checkbox.checked = state.selected.has(line.key);
      checkbox.disabled = !line.translatable || state.saving;
      selectWrap.append(checkbox);

      const content = createElement("div", "line-content");
      const meta = createElement("div", "line-meta");
      meta.append(createElement("span", "line-number", `#${line.index}`));
      if (line.speaker) meta.append(createElement("span", "speaker-badge", line.speaker));
      if (line.kind) meta.append(createElement("span", "kind-badge", line.kind));
      if (line.emptyIsApplied && line.hasTranslation && line.translation === "") {
        meta.append(createElement("span", "kind-badge blank-output-badge", "Intentional blank"));
      }
      if (!line.translatable) meta.append(createElement("span", "locked-label", "Not translatable"));
      content.append(meta);

      appendLineSection(content, "Source", line.sourceDisplay || "(empty source)", "source-text");
      if (line.context) appendLineSection(content, "Context", line.context, "context-text");

      const translationSection = createElement("div", "line-section");
      const labelRow = createElement("div", "line-label-row");
      const inputId = `translation-${visibleIndex}`;
      const translationLabel = createElement("label", "line-label", "Translation");
      translationLabel.htmlFor = inputId;
      const lineActions = createElement("span", "line-actions");
      const clearButton = createElement("button", "line-action", "Clear");
      clearButton.type = "button";
      clearButton.dataset.lineAction = "clear";
      clearButton.setAttribute("aria-label", `Clear translation for line ${line.index}`);
      clearButton.title = "Mark this line untranslated (save null)";
      clearButton.disabled = !line.translatable || (!line.hasTranslation && line.translation === "");
      lineActions.append(clearButton);
      if (line.emptyIsApplied) {
        const blankButton = createElement("button", "line-action", "Set blank");
        blankButton.type = "button";
        blankButton.dataset.lineAction = "blank";
        blankButton.setAttribute("aria-label", `Set intentional blank output for line ${line.index}`);
        blankButton.title = "Save an intentional empty output for this engine";
        blankButton.disabled = !line.translatable || (line.hasTranslation && line.translation === "");
        lineActions.append(blankButton);
      }
      labelRow.append(translationLabel, lineActions);
      translationSection.append(labelRow);
      const textarea = createElement("textarea", "translation-input");
      textarea.id = inputId;
      textarea.dataset.lineKey = line.key;
      textarea.value = line.translation;
      textarea.placeholder = line.translatable ? "Enter translation…" : "This line is not translatable";
      textarea.disabled = !line.translatable || state.saving;
      textarea.rows = 2;
      textarea.setAttribute("aria-label", `Translation for line ${line.index}`);
      translationSection.append(textarea);
      content.append(translationSection);
      card.append(selectWrap, content);
      fragment.append(card);

      // Defer height measurement until the fragment is attached.
      textarea.dataset.autosize = String(visibleIndex);
    });

    $("line-list").replaceChildren(fragment);
    $("visible-line-count").textContent = query
      ? `${visibleLines.length} of ${state.activeFile.lines.length} lines`
      : `${state.activeFile.lines.length} lines`;
    $("empty-lines").hidden = visibleLines.length > 0;
    [...$("line-list").querySelectorAll("textarea")].forEach(autoSizeTextarea);
  }

  function autoSizeTextarea(textarea) {
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(Math.max(textarea.scrollHeight, 72), 280)}px`;
  }

  function findLine(key) {
    return state.activeFile?.lines.find((line) => line.key === String(key)) ?? null;
  }

  function updateLineTranslation(key, translation, textarea = null, forceActive = null) {
    const line = findLine(key);
    if (!line || !line.translatable) return;
    line.translation = translation;
    line.hasTranslation = typeof forceActive === "boolean" ? forceActive : translation !== "";
    if (translation === line.originalTranslation && line.hasTranslation === line.originalActive) {
      state.dirty.delete(line.key);
      line.hasTranslation = line.originalActive;
    } else {
      state.dirty.set(line.key, { id: line.id, translation: line.hasTranslation ? translation : null });
    }

    const card = $("line-list").querySelector(`[data-line-key="${CSS.escape(line.key)}"].line-card`);
    card?.classList.toggle("is-dirty", state.dirty.has(line.key));
    card?.querySelector('[data-line-action="clear"]')?.toggleAttribute("disabled", !line.hasTranslation && line.translation === "");
    card?.querySelector('[data-line-action="blank"]')?.toggleAttribute("disabled", line.hasTranslation && line.translation === "");
    let blankBadge = card?.querySelector(".blank-output-badge");
    if (line.emptyIsApplied && line.hasTranslation && line.translation === "" && card && !blankBadge) {
      blankBadge = createElement("span", "kind-badge blank-output-badge", "Intentional blank");
      card.querySelector(".line-meta")?.append(blankBadge);
    } else if ((!line.hasTranslation || line.translation !== "") && blankBadge) {
      blankBadge.remove();
    }
    if (textarea) autoSizeTextarea(textarea);
    updateDirtyUi();
    renderProjectStats();
    renderFileList();
  }

  function updateDirtyUi() {
    const count = state.dirty.size;
    $("save-button").disabled = count === 0 || Boolean(state.job) || state.saving;
    $("save-state").textContent = count ? `${count} unsaved change${count === 1 ? "" : "s"}` : "Saved";
    $("save-state").classList.toggle("unsaved", count > 0);
  }

  function updateSelectionUi() {
    const count = state.selected.size;
    const jobActive = Boolean(state.job) || state.saving;
    $("selection-count").textContent = String(count);
    $("translate-selected").disabled = count === 0 || jobActive;
    $("translate-untranslated").disabled = jobActive || !state.activeFile?.lines.some((line) => line.translatable && !line.hasTranslation);
    $("translate-file").disabled = jobActive || !state.activeFile?.lines.some((line) => line.translatable);
    $("select-untranslated").disabled = jobActive;
    $("select-all").disabled = jobActive;
    $("select-none").disabled = count === 0 || jobActive;
  }

  function setSelection(predicate) {
    if (!state.activeFile) return;
    state.selected.clear();
    state.activeFile.lines.forEach((line) => {
      if (line.translatable && predicate(line)) state.selected.add(line.key);
    });
    $("line-list").querySelectorAll(".line-select").forEach((checkbox) => {
      checkbox.checked = state.selected.has(checkbox.dataset.lineKey);
      checkbox.closest(".line-card")?.classList.toggle("is-selected", checkbox.checked);
    });
    updateSelectionUi();
  }

  async function saveActiveFile({ quiet = false } = {}) {
    if (!state.activeFile || !state.dirty.size) return true;
    if (state.saving) return false;
    clearError();
    const file = state.activeFile;
    const path = file.path;
    const token = file.token;
    const queued = [...state.dirty.entries()].map(([key, update]) => ({ key, ...update }));
    const updates = queued.map(({ id, translation }) => ({ id, translation }));
    setSavingState(true);
    setButtonBusy($("save-button"), true, "Saving…");
    $("save-state").textContent = "Saving…";
    setStatus(`Saving ${updates.length} edit${updates.length === 1 ? "" : "s"}…`);

    try {
      const payload = await api.saveFile(path, token, updates);
      if (state.activeFile !== file) throw new ApiError("The active file changed while saving; reload it before continuing.");
      const filePayload = payload?.file ?? payload ?? {};
      if (filePayload.token !== undefined) file.token = filePayload.token;
      queued.forEach((saved) => {
        const line = file.lines.find((candidate) => candidate.key === saved.key);
        if (!line) return;
        line.originalValue = saved.translation;
        line.originalTranslation = saved.translation ?? "";
        line.originalActive = saved.translation !== null && (line.emptyIsApplied || saved.translation !== "");
        const current = state.dirty.get(saved.key);
        if (current && current.id === saved.id && current.translation === saved.translation) {
          state.dirty.delete(saved.key);
          line.hasTranslation = line.originalActive;
        }
      });
      const entry = state.files.find((candidate) => candidate.path === path);
      if (entry) {
        entry.token = file.token;
        entry.translatable_count = file.lines.filter((line) => line.translatable).length;
        entry.translated_count = file.lines.filter((line) => line.translatable && line.hasTranslation).length;
      }
      $("line-list").querySelectorAll(".line-card").forEach((card) => {
        card.classList.toggle("is-dirty", state.dirty.has(card.dataset.lineKey));
      });
      updateDirtyUi();
      renderProjectStats();
      renderFileList();
      setStatus(state.dirty.size ? "Saved; newer edits are still unsaved" : `Saved ${baseName(path)}`);
      return true;
    } catch (error) {
      if (!quiet) showError(error, error.status === 409 ? "File changed on disk" : "Could not save changes");
      $("save-state").textContent = `${state.dirty.size} unsaved change${state.dirty.size === 1 ? "" : "s"}`;
      setStatus("Changes were not saved");
      return false;
    } finally {
      setButtonBusy($("save-button"), false);
      setSavingState(false);
      updateDirtyUi();
    }
  }

  function setSavingState(saving) {
    state.saving = saving;
    $("project-path").disabled = saving;
    $("open-project-button").disabled = saving;
    $("file-list").querySelectorAll(".file-item").forEach((button) => { button.disabled = saving; });
    $("line-list").querySelectorAll(".line-select").forEach((input) => {
      const line = findLine(input.dataset.lineKey);
      input.disabled = saving || !line?.translatable;
    });
    $("line-list").querySelectorAll(".translation-input").forEach((textarea) => {
      const line = findLine(textarea.dataset.lineKey);
      textarea.disabled = saving || !line?.translatable;
    });
    $("line-list").querySelectorAll("[data-line-action]").forEach((button) => {
      const line = findLine(button.closest(".line-card")?.dataset.lineKey);
      const noOp = button.dataset.lineAction === "blank"
        ? line?.hasTranslation && line?.translation === ""
        : !line?.hasTranslation && line?.translation === "";
      button.disabled = saving || !line?.translatable || Boolean(noOp);
    });
    $("suggestion-list").querySelectorAll("button").forEach((button) => { button.disabled = saving; });
    $("accept-all").disabled = saving || pendingSuggestions().length === 0;
    $("reject-all").disabled = saving || pendingSuggestions().length === 0;
    updateSelectionUi();
  }

  function collectSettings({ validate = true } = {}) {
    if (validate && !$("settings-form").reportValidity()) return null;
    const numeric = (id, fallback) => {
      const value = Number($(id).value);
      return Number.isFinite(value) ? value : fallback;
    };
    return {
      base_url: $("setting-base-url").value.trim(),
      model: $("setting-model").value.trim(),
      system_prompt: $("setting-system-prompt").value,
      game_context: $("setting-game-context").value,
      target_language: $("setting-target-language").value.trim(),
      temperature: numeric("setting-temperature", DEFAULT_SETTINGS.temperature),
      max_tokens: numeric("setting-max-tokens", DEFAULT_SETTINGS.max_tokens),
      batch_size: numeric("setting-batch-size", DEFAULT_SETTINGS.batch_size),
      context_before: numeric("setting-context-before", DEFAULT_SETTINGS.context_before),
      context_after: numeric("setting-context-after", DEFAULT_SETTINGS.context_after),
      allow_remote_lmstudio: $("setting-allow-remote").checked,
    };
  }

  function normalizeSettings(payload) {
    const settings = payload?.settings ?? payload ?? {};
    return { ...DEFAULT_SETTINGS, ...(settings && typeof settings === "object" ? settings : {}) };
  }

  function applySettingsForm(settings = state.settings) {
    $("setting-base-url").value = settings.base_url ?? DEFAULT_SETTINGS.base_url;
    $("setting-model").value = settings.model ?? "";
    $("setting-system-prompt").value = settings.system_prompt ?? "";
    $("setting-game-context").value = settings.game_context ?? "";
    $("setting-target-language").value = settings.target_language ?? DEFAULT_SETTINGS.target_language;
    $("setting-temperature").value = settings.temperature ?? DEFAULT_SETTINGS.temperature;
    $("setting-max-tokens").value = settings.max_tokens ?? DEFAULT_SETTINGS.max_tokens;
    $("setting-batch-size").value = settings.batch_size ?? DEFAULT_SETTINGS.batch_size;
    $("setting-context-before").value = settings.context_before ?? DEFAULT_SETTINGS.context_before;
    $("setting-context-after").value = settings.context_after ?? DEFAULT_SETTINGS.context_after;
    $("setting-allow-remote").checked = Boolean(settings.allow_remote_lmstudio);
    $("settings-status").textContent = "";
  }

  async function loadSettings({ showErrors = false } = {}) {
    try {
      state.settings = normalizeSettings(await api.getSettings());
      applySettingsForm();
    } catch (error) {
      state.settings = { ...DEFAULT_SETTINGS };
      applySettingsForm();
      if (showErrors) showError(error, "Could not load settings");
    }
  }

  async function saveSettings(event) {
    event?.preventDefault();
    clearError();
    const settings = collectSettings();
    if (!settings) return false;
    setButtonBusy($("save-settings"), true, "Saving…");
    $("settings-status").textContent = "Saving…";
    try {
      const payload = await api.saveSettings(settings);
      const returnedSettings = payload?.settings ?? payload;
      state.settings = returnedSettings && typeof returnedSettings === "object" && "base_url" in returnedSettings
        ? normalizeSettings(payload)
        : settings;
      applySettingsForm(state.settings);
      $("settings-status").textContent = "Settings saved";
      setStatus("Translation settings saved");
      if (event) $("settings-dialog").close();
      return true;
    } catch (error) {
      showError(error, "Could not save settings");
      $("settings-status").textContent = "Settings not saved";
      return false;
    } finally {
      setButtonBusy($("save-settings"), false);
    }
  }

  function normalizeModels(payload) {
    const raw = payload?.models ?? payload?.data ?? payload ?? [];
    if (!Array.isArray(raw)) return [];
    return raw
      .map((model) => typeof model === "string" ? model : (model?.id ?? model?.name ?? model?.model))
      .filter(Boolean)
      .map(String);
  }

  async function loadModels() {
    clearError();
    const baseUrl = $("setting-base-url").value.trim();
    if (!baseUrl) {
      $("setting-base-url").reportValidity();
      return;
    }
    setButtonBusy($("load-models"), true, "Loading…");
    $("models-status").textContent = "Connecting to LM Studio…";
    try {
      // GET /api/models uses persisted connection settings, so persist only the
      // connection fields before discovery without closing the dialog.
      const savedConnection = await api.saveSettings({
        base_url: baseUrl,
        allow_remote_lmstudio: $("setting-allow-remote").checked,
      });
      const returnedConnection = savedConnection?.settings ?? savedConnection ?? {};
      state.settings = {
        ...state.settings,
        base_url: returnedConnection.base_url ?? baseUrl,
        allow_remote_lmstudio: returnedConnection.allow_remote_lmstudio ?? $("setting-allow-remote").checked,
      };
      const models = normalizeModels(await api.getModels());
      $("models-list").replaceChildren(...models.map((model) => {
        const option = document.createElement("option");
        option.value = model;
        return option;
      }));
      if (models.length === 1 && !$("setting-model").value.trim()) $("setting-model").value = models[0];
      $("models-status").textContent = models.length
        ? `${models.length} model${models.length === 1 ? "" : "s"} available`
        : "LM Studio returned no loaded models";
    } catch (error) {
      $("models-status").textContent = error.message;
      showError(error, "Could not load LM Studio models");
    } finally {
      setButtonBusy($("load-models"), false);
    }
  }

  function jobPayload(lineIds) {
    const settings = state.settings;
    return {
      files: [state.activeFile.path],
      line_ids: lineIds,
      base_url: settings.base_url,
      model: settings.model,
      system_prompt: settings.system_prompt,
      game_context: settings.game_context,
      target_language: settings.target_language,
      temperature: settings.temperature,
      max_tokens: settings.max_tokens,
      batch_size: settings.batch_size,
      context_before: settings.context_before,
      context_after: settings.context_after,
      allow_remote_lmstudio: settings.allow_remote_lmstudio,
    };
  }

  function normalizeJob(payload) {
    const job = payload?.job ?? payload ?? {};
    const status = String(job.status ?? job.state ?? "queued").toLocaleLowerCase();
    let completed = Number(job.completed ?? job.processed ?? job.completed_lines ?? 0);
    let total = Number(job.total ?? job.total_lines ?? job.line_count ?? 0);
    let percentage = null;
    if (typeof job.progress === "number") {
      percentage = job.progress <= 1 ? job.progress * 100 : job.progress;
    } else if (job.progress && typeof job.progress === "object") {
      completed = Number(job.progress.completed ?? job.progress.processed ?? completed);
      total = Number(job.progress.total ?? total);
      if (typeof job.progress.percent === "number") percentage = job.progress.percent;
    }
    if (percentage === null && total > 0) percentage = (completed / total) * 100;
    return {
      id: job.id ?? job.job_id ?? payload?.job_id ?? null,
      status,
      completed: Number.isFinite(completed) ? completed : 0,
      total: Number.isFinite(total) ? total : 0,
      percentage: percentage === null || !Number.isFinite(percentage) ? null : clamp(percentage, 0, 100),
      message: String(job.message ?? job.detail ?? job.current_file ?? ""),
      error: job.error ?? job.failure ?? null,
      raw: payload,
    };
  }

  function extractSuggestions(payload) {
    const job = payload?.job ?? payload ?? {};
    const result = payload?.result ?? job?.result;
    let candidates =
      payload?.suggestions ??
      job?.suggestions ??
      result?.suggestions ??
      (Array.isArray(result) ? result : null) ??
      payload?.results ??
      job?.results ??
      [];
    if (candidates && !Array.isArray(candidates)) candidates = candidates.suggestions ?? candidates.results ?? [];
    if (!Array.isArray(candidates)) return [];
    return candidates
      .map((suggestion) => ({
        lineId: suggestion?.line_id ?? suggestion?.id ?? suggestion?.line?.id,
        translation: suggestion?.suggestion ?? suggestion?.translation ?? suggestion?.proposed_translation ?? suggestion?.target ?? suggestion?.text,
      }))
      .filter((suggestion) => suggestion.lineId !== undefined && suggestion.translation !== undefined)
      .map((suggestion) => ({ ...suggestion, key: String(suggestion.lineId), translation: String(suggestion.translation) }));
  }

  function isFinishedStatus(status) {
    return ["completed", "complete", "done", "succeeded", "success", "failed", "error", "cancelled", "canceled"].includes(status);
  }

  function isSuccessStatus(status) {
    return ["completed", "complete", "done", "succeeded", "success"].includes(status);
  }

  function isCancelledStatus(status) {
    return ["cancelled", "canceled"].includes(status);
  }

  function renderJob() {
    const job = state.job;
    $("job-panel").hidden = !job;
    if (!job) return;
    const hasProgress = job.percentage !== null;
    $("job-progressbar").classList.toggle("indeterminate", !hasProgress);
    const percentage = hasProgress ? Math.round(job.percentage) : 0;
    $("job-progress").style.width = `${percentage}%`;
    if (hasProgress) $("job-progressbar").setAttribute("aria-valuenow", String(percentage));
    else $("job-progressbar").removeAttribute("aria-valuenow");
    $("job-message").textContent = job.message || (job.total
      ? `${job.completed} of ${job.total} lines`
      : job.status === "queued" ? "Waiting for LM Studio…" : "Building context and translating…");
    $("cancel-job").disabled = isFinishedStatus(job.status) || job.status === "cancelling";
    updateSelectionUi();
    updateDirtyUi();
  }

  async function startTranslation(scope) {
    if (!state.activeFile || state.job || state.saving) return;
    clearError();
    let lines;
    if (scope === "selected") {
      lines = state.activeFile.lines.filter((line) => state.selected.has(line.key));
    } else if (scope === "untranslated") {
      lines = state.activeFile.lines.filter((line) => line.translatable && !line.hasTranslation);
    } else {
      lines = state.activeFile.lines.filter((line) => line.translatable);
    }

    if (!lines.length) {
      showError(scope === "selected" ? "Select at least one translatable line first." : "There are no matching lines to translate.", "Nothing to translate");
      return;
    }
    if (pendingSuggestions().length && !window.confirm("Discard the current unreviewed suggestions and start a new translation?")) return;
    if (state.dirty.size && !(await saveActiveFile())) return;
    if (!state.settings.model) {
      showError("Choose an LM Studio model in Settings before starting a translation.", "Model required");
      openSettings();
      return;
    }

    state.suggestions.clear();
    closeReview();
    setStatus(`Starting translation for ${lines.length} line${lines.length === 1 ? "" : "s"}…`);
    [$("translate-selected"), $("translate-untranslated"), $("translate-file")].forEach((button) => button.disabled = true);
    try {
      const response = await api.createJob(jobPayload(lines.map((line) => String(line.id))));
      state.job = normalizeJob(response);
      if (!state.job.id) throw new ApiError("The server started a job but did not return a job ID.");
      state.jobPollFailures = 0;
      renderJob();
      setStatus(`Translation job started for ${lines.length} lines`);
      if (isFinishedStatus(state.job.status)) {
        await finishJob(state.job);
      } else {
        scheduleJobPoll(400);
      }
    } catch (error) {
      state.job = null;
      renderJob();
      showError(error, "Could not start translation");
      setStatus("Translation did not start");
    } finally {
      updateSelectionUi();
    }
  }

  function scheduleJobPoll(delay = 800) {
    clearTimeout(state.jobPollTimer);
    state.jobPollTimer = window.setTimeout(pollJob, delay);
  }

  async function pollJob() {
    if (!state.job?.id) return;
    const expectedId = state.job.id;
    try {
      const payload = await api.getJob(expectedId);
      if (state.job?.id !== expectedId) return;
      state.job = normalizeJob(payload);
      if (!state.job.id) state.job.id = expectedId;
      state.jobPollFailures = 0;
      renderJob();
      if (isFinishedStatus(state.job.status)) {
        await finishJob(state.job);
      } else {
        scheduleJobPoll();
      }
    } catch (error) {
      if (state.job?.id !== expectedId) return;
      state.jobPollFailures += 1;
      if (state.jobPollFailures < 3) {
        $("job-message").textContent = "Connection interrupted; retrying…";
        scheduleJobPoll(1200);
      } else {
        state.job = null;
        renderJob();
        showError(error, "Lost contact with translation job");
        setStatus("Translation status unavailable");
      }
    }
  }

  async function finishJob(job) {
    clearTimeout(state.jobPollTimer);
    if (isCancelledStatus(job.status)) {
      state.job = null;
      renderJob();
      setStatus("Translation cancelled");
      return;
    }
    if (!isSuccessStatus(job.status)) {
      state.job = null;
      renderJob();
      showError(job.error || job.message || "The local model could not complete this translation.", "Translation failed");
      setStatus("Translation failed");
      return;
    }

    let suggestions = extractSuggestions(job.raw);
    if (!suggestions.length) {
      try {
        suggestions = extractSuggestions(await api.getJobResult(job.id));
      } catch (error) {
        // Some server versions include suggestions only in the final job object.
        if (error.status !== 404) {
          state.job = null;
          renderJob();
          showError(error, "Could not load translation results");
          return;
        }
      }
    }

    state.job = null;
    renderJob();
    state.suggestions.clear();
    suggestions.forEach((suggestion) => {
      if (findLine(suggestion.key)) state.suggestions.set(suggestion.key, { ...suggestion, status: "pending" });
    });
    renderSuggestions();
    if (pendingSuggestions().length) {
      openReview();
      setStatus(`${pendingSuggestions().length} proposed translation${pendingSuggestions().length === 1 ? "" : "s"} ready to review`);
    } else {
      showError("The job completed without returning suggestions for this file.", "No suggestions returned");
      setStatus("Translation completed with no suggestions");
    }
  }

  async function cancelActiveJob() {
    if (!state.job?.id || state.job.status === "cancelling") return;
    clearError();
    const id = state.job.id;
    state.job.status = "cancelling";
    state.job.message = "Waiting for the current model request to stop…";
    renderJob();
    setStatus("Cancelling translation…");
    try {
      const response = await api.cancelJob(id);
      if (!state.job || state.job.id !== id) return;
      const cancelled = normalizeJob(response);
      if (cancelled.id) state.job = cancelled;
      if (isFinishedStatus(state.job.status)) await finishJob(state.job);
      else scheduleJobPoll(300);
    } catch (error) {
      if (!state.job || state.job.id !== id) return;
      state.job.status = "running";
      renderJob();
      scheduleJobPoll();
      showError(error, "Could not cancel translation");
    }
  }

  function renderSuggestions() {
    const suggestions = pendingSuggestions();
    const fragment = document.createDocumentFragment();
    suggestions.forEach((suggestion) => {
      const line = findLine(suggestion.key);
      if (!line) return;
      const card = createElement("article", "suggestion-card");
      card.dataset.lineKey = suggestion.key;
      const meta = createElement("div", "suggestion-meta");
      meta.append(createElement("span", "line-number", `#${line.index}`));
      if (line.speaker) meta.append(createElement("span", "speaker-badge", line.speaker));
      card.append(meta);
      card.append(createElement("p", "suggestion-source", line.sourceDisplay));

      const diff = createElement("div", "diff-block");
      const current = createElement("div", "diff-row diff-current");
      current.append(createElement("strong", "", "Current"));
      const currentText = line.translation || (line.hasTranslation ? "(intentional blank)" : "(untranslated)");
      current.append(createElement("p", "", currentText));
      const proposed = createElement("div", "diff-row diff-proposed");
      proposed.append(createElement("strong", "", "Proposed"));
      proposed.append(createElement("p", "", suggestion.translation));
      diff.append(current, proposed);
      card.append(diff);

      const actions = createElement("div", "suggestion-actions");
      const reject = createElement("button", "button button-quiet", "Reject");
      reject.type = "button";
      reject.dataset.action = "reject";
      reject.setAttribute("aria-label", `Reject proposed translation for line ${line.index}`);
      const accept = createElement("button", "button button-primary", "Accept");
      accept.type = "button";
      accept.dataset.action = "accept";
      accept.setAttribute("aria-label", `Accept proposed translation for line ${line.index}`);
      actions.append(reject, accept);
      card.append(actions);
      fragment.append(card);
    });

    $("suggestion-list").replaceChildren(fragment);
    $("suggestion-summary").textContent = `${suggestions.length} suggestion${suggestions.length === 1 ? "" : "s"} to review`;
    $("review-empty").hidden = suggestions.length > 0;
    $("accept-all").disabled = suggestions.length === 0 || state.saving;
    $("reject-all").disabled = suggestions.length === 0 || state.saving;
    $("review-suggestions").hidden = suggestions.length === 0 || !$("review-panel").hidden;
  }

  function decideSuggestion(key, decision) {
    if (state.saving) return;
    const suggestion = state.suggestions.get(String(key));
    if (!suggestion || suggestion.status !== "pending") return;
    if (decision === "accept") {
      const line = findLine(suggestion.key);
      if (line) {
        const textarea = $("line-list").querySelector(`textarea[data-line-key="${CSS.escape(line.key)}"]`);
        if (textarea) textarea.value = suggestion.translation;
        updateLineTranslation(line.key, suggestion.translation, textarea);
      }
      suggestion.status = "accepted";
    } else {
      suggestion.status = "rejected";
    }
    renderSuggestions();
  }

  function decideAllSuggestions(decision) {
    if (state.saving) return;
    pendingSuggestions().forEach((suggestion) => {
      if (decision === "accept") {
        const line = findLine(suggestion.key);
        if (line) {
          line.translation = suggestion.translation;
          line.hasTranslation = true;
          if (line.translation === line.originalTranslation && line.originalActive) {
            state.dirty.delete(line.key);
            line.hasTranslation = line.originalActive;
          } else {
            state.dirty.set(line.key, { id: line.id, translation: line.translation });
          }
        }
        suggestion.status = "accepted";
      } else {
        suggestion.status = "rejected";
      }
    });
    if (decision === "accept") renderLineList();
    renderSuggestions();
    updateDirtyUi();
    renderProjectStats();
    renderFileList();
    if (decision === "accept") setStatus("All proposed translations accepted as unsaved edits");
    else setStatus("All proposed translations rejected");
  }

  function setReviewBackgroundInert(inert) {
    const background = [document.querySelector(".topbar"), document.querySelector(".sidebar"), document.querySelector(".editor-shell"), document.querySelector(".statusbar")];
    background.forEach((element) => { if (element) element.inert = inert; });
  }

  function openReview() {
    if (!pendingSuggestions().length) return;
    state.reviewReturnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    $("review-panel").hidden = false;
    document.querySelector(".app-layout").classList.add("review-open");
    $("review-suggestions").hidden = true;
    const overlay = window.matchMedia("(max-width: 1180px)").matches;
    $("review-panel").setAttribute("role", overlay ? "dialog" : "complementary");
    if (overlay) {
      $("review-panel").setAttribute("aria-modal", "true");
      setReviewBackgroundInert(true);
    } else {
      $("review-panel").removeAttribute("aria-modal");
    }
    requestAnimationFrame(() => $("accept-all").focus());
  }

  function closeReview({ restoreFocus = true } = {}) {
    const wasOpen = !$("review-panel").hidden;
    $("review-panel").hidden = true;
    document.querySelector(".app-layout").classList.remove("review-open");
    $("review-suggestions").hidden = pendingSuggestions().length === 0;
    $("review-panel").removeAttribute("aria-modal");
    $("review-panel").setAttribute("role", "complementary");
    setReviewBackgroundInert(false);
    if (wasOpen && restoreFocus && state.reviewReturnFocus?.isConnected) state.reviewReturnFocus.focus();
    state.reviewReturnFocus = null;
  }

  function openSettings() {
    applySettingsForm(state.settings);
    $("settings-dialog").showModal();
    requestAnimationFrame(() => $("setting-base-url").focus());
  }

  function closeDialog(button) {
    const dialog = button.closest("dialog");
    if (!dialog) return;
    if (dialog === $("settings-dialog")) applySettingsForm(state.settings);
    dialog.close();
  }

  async function restoreProject() {
    const storedPath = safeStorageGet("translation-workbench.project-path");
    if (storedPath) $("project-path").value = storedPath;
    try {
      let payload;
      try {
        const descriptor = await api.getProject();
        const serverPath = extractProjectPath(descriptor, "");
        if (!serverPath) return;
        const filesPayload = normalizeFiles(descriptor).length ? descriptor : await api.getFiles();
        payload = { ...filesPayload, project: descriptor.project ?? descriptor };
      } catch (error) {
        if (![404, 405].includes(error.status)) throw error;
        payload = await api.getFiles();
      }
      const files = normalizeFiles(payload);
      const projectPath = extractProjectPath(payload, storedPath);
      if (files.length || projectPath) {
        applyProject(payload, projectPath);
        if (files.length) await loadFile(files[0].path, { skipConfirm: true });
      }
    } catch (error) {
      // A missing open project is a normal initial state. Connectivity errors are
      // shown, while 4xx responses simply leave the welcome screen visible.
      if (!error.status || error.status >= 500) showError(error, "Local server unavailable");
    }
  }

  function bindEvents() {
    $("open-project-form").addEventListener("submit", handleOpenProject);
    $("focus-path-button").addEventListener("click", () => $("project-path").focus());
    $("dismiss-error").addEventListener("click", clearError);
    $("save-button").addEventListener("click", () => saveActiveFile());
    $("settings-button").addEventListener("click", openSettings);
    $("shortcuts-button").addEventListener("click", () => $("shortcuts-dialog").showModal());
    $("settings-form").addEventListener("submit", saveSettings);
    $("load-models").addEventListener("click", loadModels);

    document.querySelectorAll(".dialog-close").forEach((button) => {
      button.addEventListener("click", () => closeDialog(button));
    });

    $("file-filter").addEventListener("input", (event) => {
      state.fileFilter = event.target.value;
      renderFileList();
    });
    $("file-list").addEventListener("click", (event) => {
      const item = event.target.closest(".file-item");
      if (item) loadFile(item.dataset.path);
    });
    $("line-filter").addEventListener("input", (event) => {
      state.lineFilter = event.target.value;
      renderLineList();
    });

    $("line-list").addEventListener("change", (event) => {
      if (!event.target.matches(".line-select")) return;
      const key = event.target.dataset.lineKey;
      if (event.target.checked) state.selected.add(key);
      else state.selected.delete(key);
      event.target.closest(".line-card")?.classList.toggle("is-selected", event.target.checked);
      updateSelectionUi();
    });
    $("line-list").addEventListener("input", (event) => {
      if (!event.target.matches(".translation-input")) return;
      updateLineTranslation(event.target.dataset.lineKey, event.target.value, event.target);
    });
    $("line-list").addEventListener("click", (event) => {
      const action = event.target.closest("[data-line-action]");
      const card = event.target.closest(".line-card");
      if (!action || !card || state.saving) return;
      const line = findLine(card.dataset.lineKey);
      const textarea = card.querySelector(".translation-input");
      if (!line || !line.translatable || !textarea) return;
      const intentionalBlank = action.dataset.lineAction === "blank" && line.emptyIsApplied;
      textarea.value = "";
      updateLineTranslation(line.key, "", textarea, intentionalBlank);
      setStatus(intentionalBlank
        ? `Line ${line.index} will be saved as intentional blank output`
        : `Line ${line.index} marked untranslated`);
      textarea.focus();
    });

    $("select-untranslated").addEventListener("click", () => setSelection((line) => !line.hasTranslation));
    $("select-all").addEventListener("click", () => setSelection(() => true));
    $("select-none").addEventListener("click", () => setSelection(() => false));
    $("translate-selected").addEventListener("click", () => startTranslation("selected"));
    $("translate-untranslated").addEventListener("click", () => startTranslation("untranslated"));
    $("translate-file").addEventListener("click", () => startTranslation("file"));
    $("cancel-job").addEventListener("click", cancelActiveJob);

    $("suggestion-list").addEventListener("click", (event) => {
      const action = event.target.closest("[data-action]");
      const card = event.target.closest(".suggestion-card");
      if (action && card) decideSuggestion(card.dataset.lineKey, action.dataset.action);
    });
    $("accept-all").addEventListener("click", () => decideAllSuggestions("accept"));
    $("reject-all").addEventListener("click", () => decideAllSuggestions("reject"));
    $("close-review").addEventListener("click", closeReview);
    $("review-suggestions").addEventListener("click", openReview);

    document.addEventListener("keydown", (event) => {
      const modifier = event.ctrlKey || event.metaKey;
      if (modifier && event.key.toLocaleLowerCase() === "s") {
        event.preventDefault();
        if (state.dirty.size && !state.job) saveActiveFile();
      } else if (modifier && event.key === "Enter") {
        event.preventDefault();
        if (state.selected.size && !state.job) startTranslation("selected");
      } else if (modifier && event.key.toLocaleLowerCase() === "k") {
        event.preventDefault();
        $("file-filter").focus();
        $("file-filter").select();
      } else if (event.altKey && event.key.toLocaleLowerCase() === "a") {
        event.preventDefault();
        if (state.activeFile && !state.job) setSelection(() => true);
      } else if (event.key === "Escape" && !$("review-panel").hidden) {
        closeReview();
      }
    });

    window.addEventListener("beforeunload", (event) => {
      if (!state.dirty.size && !state.job && !state.saving) return;
      event.preventDefault();
      event.returnValue = "";
    });
  }

  async function initialize() {
    bindEvents();
    applySettingsForm();
    setStatus("Connecting to local server…");
    await Promise.all([loadSettings(), restoreProject()]);
    if (!state.activeFile) setStatus("Ready");
  }

  initialize();
})();
