const API_ROOT = "/api/plugins/extensions/astrbot_plugin_html_theater";
const PROFILE_STORAGE_KEY = "html-theater-selected-persona";
const VIEW_STORAGE_KEY = "html-theater-active-view";
const PANEL_THEMES = ["pink-white", "black-white", "blue-white", "gray-white"];

const state = {
  templates: [],
  plays: [],
  profiles: {},
  personas: [],
  currentPlayId: "",
  panelPreferences: { theme: "blue-white", custom_css: "" },
  revision: 0,
  activeProfileId: "",
  selectedTemplates: new Set(),
  selectedPlays: new Set(),
};

let preferencesSaveTimer = null;

const $ = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value ?? "").replace(
  /[&<>"]/g,
  (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[character],
);

function readPreference(key) {
  try { return localStorage.getItem(key) || ""; }
  catch { return ""; }
}

function writePreference(key, value) {
  try { localStorage.setItem(key, value); }
  catch { /* The panel still falls back to the first saved Persona. */ }
}

function removePreference(key) {
  try { localStorage.removeItem(key); }
  catch { /* Ignore unavailable iframe storage. */ }
}

function setMessage(text, isError = false) {
  const node = $("global-message");
  node.textContent = text;
  node.className = `message ${text ? (isError ? "error" : "success") : ""}`;
}

async function api(path, options = {}) {
  const method = String(options.method || "GET").toUpperCase();
  const bridge = window.AstrBotPluginPage;
  const parsed = new URL(path, "http://html-theater.local");
  const endpoint = parsed.pathname.replace(/^\/+/, "");
  const params = Object.fromEntries(parsed.searchParams.entries());
  if (bridge) {
    if (method === "GET") return bridge.apiGet(endpoint, params);
    let body = options.body || {};
    if (typeof body === "string") body = JSON.parse(body);
    return bridge.apiPost(endpoint, body);
  }
  const requestOptions = {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  };
  if (requestOptions.body && typeof requestOptions.body !== "string") {
    requestOptions.body = JSON.stringify(requestOptions.body);
  }
  const response = await fetch(`${API_ROOT}${path}`, requestOptions);
  const data = await response.json();
  if (!response.ok) throw new Error(data.message || `Request failed (${response.status})`);
  return data;
}

function formatTime(timestamp) {
  const value = Number(timestamp || 0);
  return value ? new Date(value * 1000).toLocaleString() : "—";
}

function switchView(viewName) {
  const requested = ["templates", "plays", "profiles", "backup", "theme", "favorites"].includes(viewName) ? viewName : "templates";
  document.querySelectorAll(".nav-item").forEach((button) => {
    const active = button.dataset.view === requested;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  document.querySelectorAll(".view").forEach((view) => {
    const active = view.id === `view-${requested}`;
    view.classList.toggle("active", active);
    view.hidden = !active;
    if (active) $("page-title").textContent = view.dataset.title;
  });
  writePreference(VIEW_STORAGE_KEY, requested);
}

function renderTemplates() {
  $("template-body").innerHTML = state.templates.map((template) => `
    <tr>
      <td><input class="template-check" type="checkbox" data-id="${escapeHtml(template.id)}"${state.selectedTemplates.has(template.id) ? " checked" : ""}></td>
      <td><strong>${escapeHtml(template.title)}</strong></td>
      <td class="prompt-cell"><div class="truncate">${escapeHtml(template.prompt)}</div></td>
      <td><button class="row-button template-edit" type="button" data-id="${escapeHtml(template.id)}">编辑</button></td>
    </tr>
  `).join("") || '<tr><td class="empty" colspan="4">没有匹配的模板</td></tr>';

  document.querySelectorAll(".template-check").forEach((checkbox) => {
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) state.selectedTemplates.add(checkbox.dataset.id);
      else state.selectedTemplates.delete(checkbox.dataset.id);
    });
  });
  document.querySelectorAll(".template-edit").forEach((button) => {
    button.addEventListener("click", () => {
      const template = state.templates.find((item) => item.id === button.dataset.id);
      if (!template) return;
      $("template-id").value = template.id;
      $("template-title").value = template.title;
      $("template-prompt").value = template.prompt;
      $("template-title").focus();
    });
  });
}

function renderPlays() {
  $("play-body").innerHTML = state.plays.map((play) => `
    <tr>
      <td><input class="play-check" type="checkbox" data-id="${escapeHtml(play.id)}"${state.selectedPlays.has(play.id) ? " checked" : ""}></td>
      <td class="text-cell"><strong>${escapeHtml(play.title)}</strong><div class="truncate">${escapeHtml(play.text || "")}</div></td>
      <td><span class="persona-badge">${escapeHtml(play.persona_id || "legacy")}</span></td>
      <td>${escapeHtml(play.chapter ? `chapter ${play.chapter}` : play.template_title || "普通生成")}</td>
      <td>${escapeHtml(formatTime(play.created_at))}</td>
      <td><button class="row-button play-favorite" type="button" data-id="${escapeHtml(play.id)}" data-favorite="${play.favorite ? "true" : "false"}">${play.favorite ? "★ 已收藏" : "☆ 收藏"}</button></td>
      <td><div class="row-actions"><button class="row-button play-preview" type="button" data-id="${escapeHtml(play.id)}">预览</button><button class="row-button play-select" type="button" data-id="${escapeHtml(play.id)}">设为当前</button></div></td>
    </tr>
  `).join("") || '<tr><td class="empty" colspan="7">还没有生成小剧场</td></tr>';

  const options = state.plays.map((play) => `<option value="${escapeHtml(play.id)}"${play.id === state.currentPlayId ? " selected" : ""}>${escapeHtml(play.title)}</option>`).join("");
  $("current-play").innerHTML = options || '<option value="">暂无成品</option>';
  $("current-play").disabled = !state.plays.length;
  $("continuation-source").innerHTML = options || '<option value="">暂无可续写成品</option>';
  $("continuation-source").disabled = !state.plays.length;

  document.querySelectorAll(".play-check").forEach((checkbox) => {
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) state.selectedPlays.add(checkbox.dataset.id);
      else state.selectedPlays.delete(checkbox.dataset.id);
    });
  });
  document.querySelectorAll(".play-favorite").forEach((button) => button.addEventListener("click", () => void favoritePlay(button)));
  document.querySelectorAll(".play-preview").forEach((button) => button.addEventListener("click", () => void previewPlay(button.dataset.id)));
  document.querySelectorAll(".play-select").forEach((button) => button.addEventListener("click", () => void selectPlay(button.dataset.id)));
}

function renderFavorites() {
  const favorites = state.plays.filter((play) => Boolean(play.favorite));
  $("favorite-count").textContent = `${favorites.length} 个收藏`;
  $("favorite-grid").innerHTML = favorites.map((play) => `
    <article class="favorite-item">
      <h3>${escapeHtml(play.title)}</h3>
      <p>${escapeHtml(play.text || "暂无正文摘要")}</p>
      <div class="favorite-meta"><span>${escapeHtml(play.persona_id || "legacy")}</span><span>${escapeHtml(formatTime(play.created_at))}</span></div>
      <div class="favorite-actions">
        <button class="row-button favorite-preview" type="button" data-id="${escapeHtml(play.id)}">预览</button>
        <button class="row-button favorite-select" type="button" data-id="${escapeHtml(play.id)}">设为当前</button>
        <button class="row-button favorite-remove" type="button" data-id="${escapeHtml(play.id)}">取消收藏</button>
      </div>
    </article>
  `).join("") || '<div class="empty">还没有收藏的小剧场。</div>';
  document.querySelectorAll(".favorite-preview").forEach((button) => button.addEventListener("click", () => void previewPlay(button.dataset.id)));
  document.querySelectorAll(".favorite-select").forEach((button) => button.addEventListener("click", () => void selectPlay(button.dataset.id)));
  document.querySelectorAll(".favorite-remove").forEach((button) => button.addEventListener("click", () => void setFavoriteById(button.dataset.id, false)));
}

function renderThemeOptions() {
  document.querySelectorAll(".theme-option").forEach((button) => {
    const active = button.dataset.theme === state.panelPreferences.theme;
    button.classList.toggle("active", active);
    button.setAttribute("aria-checked", String(active));
  });
}

function applyCustomCss(css) {
  const style = $("custom-panel-css");
  if (style) style.textContent = String(css || "");
}

function queuePreferencesSave() {
  if (preferencesSaveTimer) window.clearTimeout(preferencesSaveTimer);
  preferencesSaveTimer = window.setTimeout(() => { void savePanelPreferences(false); }, 450);
}

function applyTheme(theme, persist = true) {
  const selected = PANEL_THEMES.includes(theme) ? theme : "blue-white";
  state.panelPreferences.theme = selected;
  document.documentElement.dataset.theme = selected;
  renderThemeOptions();
  if (persist) queuePreferencesSave();
}

async function savePanelPreferences(showMessage = true) {
  try {
    const result = await api("/preferences/save", { method: "POST", body: state.panelPreferences });
    state.panelPreferences = { ...state.panelPreferences, ...(result.data || {}) };
    applyTheme(state.panelPreferences.theme, false);
    applyCustomCss(state.panelPreferences.custom_css);
    if (showMessage) setMessage("面板配色和自定义 CSS 已保存。");
  } catch (error) {
    setMessage(error.message, true);
  }
}

function personaDisplayName(personaId) {
  return state.personas.find((item) => item.id === personaId)?.name || personaId;
}

function renderProfiles() {
  const profileIds = Object.keys(state.profiles).sort((left, right) => {
    const rightTime = Number(state.profiles[right]?.updated_at || 0);
    const leftTime = Number(state.profiles[left]?.updated_at || 0);
    return rightTime - leftTime || left.localeCompare(right);
  });
  $("profile-list").innerHTML = profileIds.map((personaId) => {
    const profile = state.profiles[personaId];
    return `<button class="profile-item${personaId === state.activeProfileId ? " active" : ""}" type="button" data-id="${escapeHtml(personaId)}"><strong>${escapeHtml(personaId)}</strong><span>${escapeHtml(personaDisplayName(personaId))}</span><small>更新：${escapeHtml(formatTime(profile.updated_at))}</small></button>`;
  }).join("") || '<div class="empty">尚未保存人设，点击右上角＋新增。</div>';
  document.querySelectorAll(".profile-item").forEach((button) => {
    button.addEventListener("click", () => selectProfile(button.dataset.id));
  });
}

function renderPersonas() {
  const ids = new Set([...state.personas.map((item) => item.id), ...Object.keys(state.profiles)]);
  $("persona-options").innerHTML = [...ids].sort().map((id) => `<option value="${escapeHtml(id)}"></option>`).join("");
  renderProfiles();
}

function clearProfileFields() {
  $("persona-id").value = "";
  $("char-name").value = "";
  $("char-prompt").value = "";
  $("user-name").value = "";
  $("user-prompt").value = "";
}

function newProfile() {
  state.activeProfileId = "";
  clearProfileFields();
  $("persona-id").readOnly = false;
  $("profile-editor-title").textContent = "新增人设配置";
  $("delete-profile").hidden = true;
  renderProfiles();
  $("persona-id").focus();
}

function selectProfile(personaId) {
  const profile = state.profiles[personaId];
  if (!profile) return newProfile();
  state.activeProfileId = personaId;
  writePreference(PROFILE_STORAGE_KEY, personaId);
  $("persona-id").value = personaId;
  $("persona-id").readOnly = true;
  $("char-name").value = profile.char_name || "";
  $("char-prompt").value = profile.char_prompt || "";
  $("user-name").value = profile.user_name || "";
  $("user-prompt").value = profile.user_prompt || "";
  $("profile-editor-title").textContent = `编辑 ${personaId}`;
  $("delete-profile").hidden = false;
  renderProfiles();
}

function restoreProfileSelection(preferredId = "") {
  const candidate = preferredId || state.activeProfileId || readPreference(PROFILE_STORAGE_KEY);
  if (candidate && state.profiles[candidate]) return selectProfile(candidate);
  const first = Object.keys(state.profiles)[0];
  if (first) return selectProfile(first);
  return newProfile();
}

async function loadState(preferredProfileId = "") {
  try {
    const query = new URLSearchParams({ q: $("global-search").value.trim() });
    const [stateResponse, personaResponse] = await Promise.all([api(`/state?${query}`), api("/personas")]);
    const payload = stateResponse.data || stateResponse;
    state.templates = payload.templates || [];
    state.plays = payload.plays || [];
    state.profiles = payload.profiles || {};
    state.currentPlayId = payload.current_play_id || "";
    state.panelPreferences = { theme: "blue-white", custom_css: "", ...(payload.panel_preferences || {}) };
    state.revision = Number(payload.revision || 0);
    state.personas = personaResponse.data || [];
    state.selectedTemplates.clear();
    state.selectedPlays.clear();
    $("template-select-all").checked = false;
    $("play-select-all").checked = false;
    renderTemplates();
    renderPlays();
    renderFavorites();
    $("custom-panel-css-input").value = state.panelPreferences.custom_css || "";
    applyTheme(state.panelPreferences.theme, false);
    applyCustomCss(state.panelPreferences.custom_css);
    renderPersonas();
    restoreProfileSelection(preferredProfileId);
    setMessage(`已读取 ${state.templates.length} 个模板、${state.plays.length} 个成品、${Object.keys(state.profiles).length} 组人设。`);
  } catch (error) {
    setMessage(error.message, true);
  }
}

function clearTemplateEditor() {
  $("template-id").value = "";
  $("template-title").value = "";
  $("template-prompt").value = "";
}

async function saveTemplate() {
  const title = $("template-title").value.trim();
  const prompt = $("template-prompt").value.trim();
  if (!title || !prompt) return setMessage("标题和小剧场提示词都必须填写。", true);
  try {
    const result = await api("/templates/save", { method: "POST", body: { id: $("template-id").value, title, prompt } });
    clearTemplateEditor();
    await loadState();
    setMessage(`模板《${result.data.title}》已保存。`);
  } catch (error) { setMessage(error.message, true); }
}

async function deleteTemplates() {
  if (!state.selectedTemplates.size) return setMessage("请先选择要删除的模板。", true);
  if (!window.confirm(`确定删除选中的 ${state.selectedTemplates.size} 个模板吗？`)) return;
  try {
    const result = await api("/templates/delete", { method: "POST", body: { ids: [...state.selectedTemplates] } });
    await loadState();
    setMessage(`已删除 ${result.deleted} 个模板。`);
  } catch (error) { setMessage(error.message, true); }
}

async function deletePlays() {
  if (!state.selectedPlays.size) return setMessage("请先选择要删除的成品。", true);
  if (!window.confirm(`确定删除选中的 ${state.selectedPlays.size} 个成品及 HTML 文件吗？`)) return;
  try {
    const result = await api("/plays/delete", { method: "POST", body: { ids: [...state.selectedPlays] } });
    await loadState();
    setMessage(`已删除 ${result.deleted} 个成品。`);
  } catch (error) { setMessage(error.message, true); }
}

async function favoritePlay(button) {
  await setFavoriteById(button.dataset.id, button.dataset.favorite !== "true");
}

async function setFavoriteById(id, favorite) {
  try {
    await api("/plays/favorite", { method: "POST", body: { id, favorite } });
    await loadState();
  } catch (error) { setMessage(error.message, true); }
}

async function selectPlay(id) {
  if (!id) return;
  try {
    await api("/plays/select", { method: "POST", body: { id } });
    state.currentPlayId = id;
    renderPlays();
    setMessage("当前 Web 页面显示项已更新。");
  } catch (error) { setMessage(error.message, true); }
}

async function previewPlay(id) {
  try {
    const response = await api(`/plays/content/${encodeURIComponent(id)}`);
    const play = state.plays.find((item) => item.id === id);
    $("preview-title").textContent = play ? play.title : "小剧场预览";
    $("preview-frame").srcdoc = response.html || "";
    $("preview-dialog").showModal();
  } catch (error) { setMessage(error.message, true); }
}

async function continuePlay() {
  const sourceId = $("continuation-source").value;
  const prompt = $("continuation-prompt").value.trim();
  if (!sourceId || !prompt) return setMessage("请选择成品并填写续写提示词。", true);
  const button = $("continue-play");
  button.disabled = true;
  button.textContent = "正在生成续写…";
  try {
    const result = await api("/plays/continue", { method: "POST", body: { source_id: sourceId, prompt } });
    $("continuation-prompt").value = "";
    await loadState();
    setMessage(`续写《${result.data.title}》已生成；未向 QQ 会话注入。`);
  } catch (error) { setMessage(error.message, true); }
  finally { button.disabled = false; button.textContent = "生成下一章"; }
}

async function saveProfile() {
  const personaId = $("persona-id").value.trim();
  if (!personaId) return setMessage("Persona ID 不能为空。", true);
  try {
    await api("/profiles/save", { method: "POST", body: {
      persona_id: personaId,
      char_name: $("char-name").value.trim(),
      char_prompt: $("char-prompt").value.trim(),
      user_name: $("user-name").value.trim(),
      user_prompt: $("user-prompt").value.trim(),
    } });
    state.activeProfileId = personaId;
    writePreference(PROFILE_STORAGE_KEY, personaId);
    await loadState(personaId);
    setMessage(`Persona ${personaId} 的人设配置已保存。`);
  } catch (error) { setMessage(error.message, true); }
}

async function deleteProfile() {
  const personaId = state.activeProfileId;
  if (!personaId || !window.confirm(`确定删除 Persona ${personaId} 的页面人设配置吗？`)) return;
  try {
    await api("/profiles/delete", { method: "POST", body: { persona_id: personaId } });
    state.activeProfileId = "";
    removePreference(PROFILE_STORAGE_KEY);
    await loadState();
    setMessage(`Persona ${personaId} 的页面人设配置已删除。`);
  } catch (error) { setMessage(error.message, true); }
}

async function exportBackup() {
  const button = $("export-backup");
  button.disabled = true;
  try {
    const bridge = window.AstrBotPluginPage;
    const filename = `html_theater_backup_${new Date().toISOString().replace(/[:.]/g, "-")}.zip`;
    if (bridge?.download) await bridge.download("backup/export", {}, filename);
    else {
      const response = await fetch(`${API_ROOT}/backup/export`, { credentials: "same-origin" });
      if (!response.ok) throw new Error(`导出失败 (${response.status})`);
      const url = URL.createObjectURL(await response.blob());
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filename;
      anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 0);
    }
    setMessage("完整备份已导出，服务器备份目录也保存了一份。");
  } catch (error) { setMessage(error.message, true); }
  finally { button.disabled = false; }
}

function fileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener("load", () => resolve(String(reader.result).split(",", 2)[1] || ""));
    reader.addEventListener("error", () => reject(reader.error || new Error("读取备份失败")));
    reader.readAsDataURL(file);
  });
}

async function importBackup() {
  const file = $("backup-file").files[0];
  const mode = $("import-mode").value;
  if (!file) return setMessage("请先选择 ZIP 备份文件。", true);
  if (mode === "replace" && $("replace-confirm").value.trim() !== "完整恢复") return setMessage("完整恢复需要输入确认文本：完整恢复", true);
  const button = $("import-backup");
  button.disabled = true;
  button.textContent = "正在校验并导入…";
  try {
    const response = await api("/backup/import", { method: "POST", body: {
      content_base64: await fileAsBase64(file), mode, confirm: $("replace-confirm").value.trim(),
    } });
    $("backup-file").value = "";
    $("replace-confirm").value = "";
    await loadState();
    setMessage(`导入完成：${response.data.templates} 个模板，${response.data.plays} 个成品。`);
  } catch (error) { setMessage(error.message, true); }
  finally { button.disabled = false; button.textContent = "导入备份"; }
}

function toggleAll(kind) {
  const checkbox = kind === "template" ? $("template-select-all") : $("play-select-all");
  const items = kind === "template" ? state.templates : state.plays;
  const selected = kind === "template" ? state.selectedTemplates : state.selectedPlays;
  selected.clear();
  if (checkbox.checked) items.forEach((item) => selected.add(item.id));
  if (kind === "template") renderTemplates(); else renderPlays();
}

function bindEvents() {
  document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
  $("search-button").addEventListener("click", () => void loadState());
  $("refresh-button").addEventListener("click", () => { $("global-search").value = ""; void loadState(); });
  $("global-search").addEventListener("keydown", (event) => { if (event.key === "Enter") void loadState(); });
  $("save-template").addEventListener("click", () => void saveTemplate());
  $("clear-template").addEventListener("click", clearTemplateEditor);
  $("delete-templates").addEventListener("click", () => void deleteTemplates());
  $("delete-plays").addEventListener("click", () => void deletePlays());
  $("template-select-all").addEventListener("change", () => toggleAll("template"));
  $("play-select-all").addEventListener("change", () => toggleAll("play"));
  $("current-play").addEventListener("change", (event) => void selectPlay(event.target.value));
  $("continue-play").addEventListener("click", () => void continuePlay());
  $("add-profile").addEventListener("click", newProfile);
  $("save-profile").addEventListener("click", () => void saveProfile());
  $("delete-profile").addEventListener("click", () => void deleteProfile());
  $("reset-profile").addEventListener("click", () => restoreProfileSelection());
  $("persona-id").addEventListener("change", (event) => { if (state.profiles[event.target.value.trim()]) selectProfile(event.target.value.trim()); });
  $("export-backup").addEventListener("click", () => void exportBackup());
  $("import-backup").addEventListener("click", () => void importBackup());
  $("import-mode").addEventListener("change", (event) => { $("confirm-wrap").hidden = event.target.value !== "replace"; });
  $("close-preview").addEventListener("click", () => { $("preview-frame").srcdoc = ""; $("preview-dialog").close(); });
  document.querySelectorAll(".theme-option").forEach((button) => button.addEventListener("click", () => applyTheme(button.dataset.theme)));
  $("custom-panel-css-input").addEventListener("input", (event) => {
    state.panelPreferences.custom_css = event.target.value;
    applyCustomCss(state.panelPreferences.custom_css);
    queuePreferencesSave();
  });
  $("clear-custom-css").addEventListener("click", () => {
    $("custom-panel-css-input").value = "";
    state.panelPreferences.custom_css = "";
    applyCustomCss("");
    void savePanelPreferences();
  });
  $("save-panel-preferences").addEventListener("click", () => void savePanelPreferences());
}

async function init() {
  bindEvents();
  switchView(readPreference(VIEW_STORAGE_KEY) || "templates");
  await window.AstrBotPluginPage?.ready?.();
  await loadState();
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => void init(), { once: true });
else void init();
