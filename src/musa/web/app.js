// Musa · galería de imágenes generadas (módulo ES)
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

// Respuesta no JSON (p. ej. HTML de un proxy o un 502): se devuelve un error sintético con el estado HTTP.
async function parseBody(r) {
  try { return await r.json(); } catch (e) { return { code: "http_error", status: r.status }; }
}
// Solo el fetch se convierte en «network»; cualquier otra excepción es un error de programación y no se disfraza.
async function doFetch(path, opts) {
  try { return await fetch(path, opts); } catch (e) { throw { code: "network" }; }
}
const api = {
  async get(path) { const r = await doFetch(path, { credentials: "same-origin" }); const data = await parseBody(r); if (!r.ok) throw data; return data; },
  async send(method, path, body) {
    const opts = { method, credentials: "same-origin", headers: {} };
    if (body instanceof FormData) opts.body = body;
    else if (body !== undefined) { opts.headers["content-type"] = "application/json"; opts.body = JSON.stringify(body); }
    const r = await doFetch(path, opts); const data = r.status === 204 ? null : await parseBody(r);
    if (!r.ok) throw data; return data;
  },
};

const state = { route: null, baseHash: "#inicio", style: null, quota: null,
  gallery: { filter: "all", query: "", style: null, model: null, since: "", until: "", sort: "newest", collection: null, items: [], total: 0, loading: false, seq: 0, error: null },
  styles: [], stylesLoaded: false, collections: [], counts: {},
  itemsByKey: new Map(), recentItems: [], detailList: null, seenModels: new Set(),
  models: [], modelDefault: null, prompts: null, promptsError: "", promptQuery: "",
  jobs: [], eta_s: 30, dismissedJobs: new Set(), collTab: "imgs", templates: {}, testBatch: null,
  cmp: { ratio: "1:1", n: 1, model: "auto", refs: [], dest: "", template: null, tplVars: {}, edit: null, anim: null } };

// Versión de la portada por estilo: la URL no cambia al sustituir la imagen y el navegador la cachea.
const coverVer = {};
const coverSrc = s => s.cover_url ? s.cover_url + (coverVer[s.id] ? "?v=" + coverVer[s.id] : "") : null;
let styleFamily = "all";
const gemBusy = new Set();   // style ids with a gem toggle in flight
let gemDlgStyle = null;      // style id whose gem dialog is open
let detailItem = null;
let lastHash = null;
let pendingFocusKey = null;  // tarjeta que recupera el foco al cerrar el detalle

/* ─── utilidades ─── */
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (k === "checked") el.checked = true;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat(9)) {
    if (c == null || c === false) continue;
    el.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return el;
}
const SVG_NS = "http://www.w3.org/2000/svg";
const svgUse = id => {
  const s = document.createElementNS(SVG_NS, "svg");
  s.setAttribute("class", "i");
  s.setAttribute("aria-hidden", "true");
  const u = document.createElementNS(SVG_NS, "use");
  u.setAttribute("href", "#" + id);
  s.append(u);
  return s;
};
// Nombres de modelos conocidos cuando la lista viva de la cuenta no los trae (p. ej. el modelo por defecto «gemini-flash»).
const MODEL_ALIAS = { "gemini-flash": "Flash", "gemini-flash-lite": "Flash Lite", "gemini-pro": "Pro", "gemini-carbon": "Carbon", "gemini-4-hydrogen": "Hydrogen" };
const knownModelName = id => {
  const e = state.models.find(x => x.id === id);
  return (e && e.name) || MODEL_ALIAS[id] || null;
};
// Nombre del modelo: lista de la cuenta; el «requested» por defecto usa el nombre de su «resolved»; luego alias; al final el id.
const modelName = m => {
  if (!m) return "—";
  const e = state.models.find(x => x.id === m);
  if (e && e.name) return e.name;
  const d = state.modelDefault;
  if (d && m === d.requested) { const r = d.resolved && state.models.find(x => x.id === d.resolved); if (r && r.name) return r.name; }
  return MODEL_ALIAS[m] || String(m);
};
// Familias de estilo: una sola lista alimenta los chips, el diálogo «Nuevo estilo» y las etiquetas.
const FAMILIES = [["ilustracion", "Ilustración"], ["pintura", "Pintura"], ["grabado", "Grabado"], ["diseno", "Diseño"], ["otros", "Otros"]];
const FAM_LABEL = Object.fromEntries(FAMILIES);
const FAM_CHIPS = [["all", "Todos"], ...FAMILIES, ["user", "Mis estilos"]];
const ACCOUNT_ES = { AVAILABLE: "Servicio operativo", UNAUTHENTICATED: "Sesión no iniciada" };
const short = (t, n = 48) => { t = String(t || ""); return t.length > n ? t.slice(0, n - 1).trimEnd() + "…" : t; };
const fmtDate = ts => { try { return new Date(ts).toLocaleString("es-ES", { dateStyle: "medium", timeStyle: "short" }); } catch (e) { return String(ts || ""); } };
function rememberItems(items) { for (const it of items) { state.itemsByKey.set(it.key, it); if (it.model) state.seenModels.add(it.model); } }
// Mensajes de error en español, con la acción a seguir, por código de la API.
const ERR_ES = {
  auth_expired: "La sesión ha caducado. Ejecuta «musa auth» (Gemini) o «musa auth --chatgpt» (ChatGPT) en la terminal y vuelve a intentarlo.",
  auth_failed: "No se pudo iniciar sesión o abrir el navegador. Gemini necesita Firefox y ChatGPT, Google Chrome; ejecuta «musa auth» de nuevo.",
  quota_guard: "Musa detuvo la generación antes de llegar al límite de Gemini. Espera a que se renueve la cuota.",
  usage_limit: "Has alcanzado el límite de uso del proveedor. Espera a que se renueve la cuota.",
  temporarily_blocked: "El proveedor ha bloqueado la petición temporalmente (o ChatGPT está ocupado en otro proceso). Espera unos minutos antes de reintentar.",
  queue_full: "La cola está llena (20 pendientes). Espera a que termine alguna o cancela las que no necesites.",
  job_running: "Esa generación ya está en curso y no se puede cancelar.",
  unknown_job: "Esa generación ya no está en la cola.",
  not_supported: "Esta acción solo está disponible en macOS.",
  gem_missing: "El Gem de este estilo ya no existe en Gemini; la imagen se generó sin él.",
  gem_ignored: "ChatGPT no usa Gems; el estilo se aplicó solo como texto.",
  no_image: "El modelo no devolvió ninguna imagen. Reformula el prompt e inténtalo de nuevo.",
  timeout: "El proveedor tardó demasiado en responder. Inténtalo de nuevo; si se repite, simplifica el prompt o usa menos referencias.",
  generated_not_saved: "La imagen se generó pero no se pudo guardar en el disco. Reintenta la descarga o genera de nuevo.",
  content_blocked: "Gemini ha bloqueado el prompt por sus políticas. Cambia el texto o las referencias.",
  invalid_ref: "La imagen de referencia no es válida. Usa un archivo de imagen o una imagen de la galería.",
  ref_not_allowed: "Esa imagen de referencia está fuera de las carpetas permitidas.",
  ssrf_blocked: "Esa dirección apunta a una red privada. Usa una URL pública.",
  unknown_style: "Ese estilo ya no existe. Elige otro en el selector.",
  unknown_model: "Tu cuenta no tiene ese modelo. Elige otro en el selector.",
  unknown_collection: "Esa colección ya no existe.",
  unknown_image: "Esa imagen ya no está disponible.",
  unknown_upload: "La imagen subida ha caducado. Súbela de nuevo.",
  invalid_collection: "El nombre de la colección está vacío o ya existe. Elige otro nombre.",
  invalid_style: "No se pudo guardar el estilo. Revisa el nombre y que la imagen sea PNG, JPEG, WebP o GIF de menos de 20 MB.",
  invalid_aspect: "Esa proporción no es válida. Elige otra en el selector.",
  invalid_image: "No se puede leer ese archivo de imagen. Prueba con otro.",
  invalid_json: "La petición no es válida. Recarga la página e inténtalo de nuevo.",
  template_missing_var: "Rellena todos los campos de la plantilla.",
  unknown_conversation: "Esa imagen ya no se puede editar: su conversación no está disponible.",
  invalid_query: "La petición no es válida. Revisa los datos e inténtalo de nuevo.",
  invalid_prompt: "No se pudo guardar el prompt. Usa un nombre único (máx. 80 caracteres) y un texto no vacío (máx. 4000); se admiten hasta 200 prompts.",
  unknown_prompt: "Ese prompt ya no existe. Se actualizará la lista.",
  invalid_animation: "Los ajustes de animación no son válidos.",
  unknown_animation: "Esta imagen ya no tiene animación.",
  invalid_composition: "Los datos de la composición no son válidos.",
  unknown_composition: "La composición ya no existe.",
  interrupted: "Musa se reinició antes de terminar.",
  cancelled: "Cancelada.",
  network: "No se pudo conectar con Musa. Comprueba que la app sigue abierta.",
  upstream_error: "Gemini devolvió un error inesperado. Inténtalo de nuevo en unos minutos.",
  internal_error: "Error interno de Musa. Inténtalo de nuevo; si se repite, revisa los registros del servidor.",
  unauthorized: "Tu sesión de Musa ha caducado. Vuelve a abrir la app desde la terminal.",
  forbidden: "Musa no permitió esa acción. Recarga la página e inténtalo de nuevo.",
};
const errMsg = e => {
  if (e && e.code === "http_error") return `El servidor respondió con un error (HTTP ${e.status}). Vuelve a intentarlo en unos segundos.`;
  return (e && ERR_ES[e.code]) || "No se pudo completar la acción. Inténtalo de nuevo.";
};

// El toast vive dentro del diálogo abierto (un diálogo modal deja inertes el resto de la página).
const toastHost = () => $("#confirm-dialog[open]") || $$("dialog[open]").pop() || document.body;
function placeToasts() {
  const host = toastHost();
  for (const el of $$(".toast")) if (el.parentElement !== host) host.append(el);
}
function toast(msg, kind = "ok") {
  const err = kind === "err";
  const t = $(err ? "#toast-err" : "#toast"), other = $(err ? "#toast" : "#toast-err");
  placeToasts();
  other.classList.remove("show");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(t._t); t._t = setTimeout(() => t.classList.remove("show"), err ? 6000 : 2800);
}
document.addEventListener("close", placeToasts, true);

// Confirmación propia (en lugar de confirm() nativo).
let cfResolve = null;
function confirmDlg({ title, text, ok, danger = true, icon }) {
  return new Promise(resolve => {
    if (cfResolve) cfResolve(false);
    cfResolve = resolve;
    $("#cf-title").textContent = title;
    $("#cf-text").textContent = text;
    $("#cf-ok").replaceChildren(svgUse(icon || (danger ? "i-trash" : "i-check")), ok);
    $("#cf-ok").classList.toggle("btn-danger", danger);
    $("#confirm-dialog").showModal();
    $("#cf-cancel").focus();
  });
}
function settleConfirm(v) { const r = cfResolve; cfResolve = null; if (r) r(v); }

/* ─── enrutado ─── */
function parseRoute() {
  const [path, qs = ""] = location.hash.replace(/^#/, "").split("?");
  const parts = path.split("/").filter(Boolean);
  const params = new URLSearchParams(qs);
  if (parts[0] === "imagen" && parts[1] && parts[2] !== undefined) return { name: "imagen", gen: parts[1], i: parts[2] };
  if (parts[0] === "colecciones" && parts[1]) return { name: "colecciones", id: parts[1], params };
  if (parts[0] === "colecciones") return { name: "colecciones", id: null };
  if (parts[0] === "galeria") return { name: "galeria", params };
  if (parts[0] === "composiciones" && parts[1]) return { name: "composiciones", id: parts[1] };
  if (parts[0] === "composiciones") return { name: "composiciones", id: null };
  if (parts[0] === "inicio" || parts[0] === "estilos" || parts[0] === "limites") return { name: parts[0] };
  return { name: "inicio" };
}

async function render() {
  closeImgMenu(false);
  closeFilterPop(false);
  lastHash = location.hash;
  const route = parseRoute();
  const prevRoute = state.route;
  state.route = route;
  if (route.name !== "composiciones") compPauseStage();
  // los filtros viven en el hash: solo la galería y el detalle de una colección los leen; el filtro de colección solo existe en esta última
  const inColl = route.name === "colecciones" && !!route.id;
  if (route.name === "galeria" || inColl) readFilters(route.params, inColl);
  if (route.name !== "imagen") { state.gallery.collection = inColl ? route.id : null; state.baseHash = location.hash || "#inicio"; selScope(state.baseHash); }
  $$(".screen").forEach(s => s.classList.toggle("active", s.id === "screen-" + (route.name === "imagen" ? "galeria" : route.name)));
  $$(".nav-item[data-nav]").forEach(b => b.setAttribute("aria-current", b.dataset.nav === route.name ? "page" : "false"));
  const curNav = $('.nav-item[aria-current="page"]');
  if (curNav && matchMedia("(max-width: 860px)").matches) curNav.scrollIntoView({ inline: "center", block: "nearest" });
  if (route.name !== "imagen") { try { localStorage.setItem("musa-screen", route.name); } catch (e) {} }
  try { const s = await api.get("/api/state"); state.counts = s.counts || {}; } catch (e) {}
  try { state.collections = await api.get("/api/collections"); } catch (e) {}
  updateCounts();
  renderDest();
  loadQuota();
  try { await ensureStyles(); } catch (e) {}
  if (!state.style) state.style = defaultStyleId();
  updateComposer();
  try {
    const scr = $("#screen-" + (route.name === "imagen" ? "galeria" : route.name));
    if (scr && scr.dataset.title) setTitle(scr.dataset.title);
    if (route.name === "imagen") {
      state.gallery.collection = null;
      if (!state.gallery.items.length) await loadGallery();
      renderGallery();
      await openDetail(route.gen, route.i);
      return;
    }
    closeDetail();
    if (route.name === "inicio") {
      await renderInicio();
    } else if (route.name === "galeria") {
      try { await loadGallery(); } catch (e) { toast(errMsg(e), "err"); }
      renderGallery();
    } else if (route.name === "limites") {
      await renderLimites();
    } else if (route.name === "estilos") {
      renderStyles();
    } else if (route.name === "colecciones") {
      if (route.id) await renderCollection(route.id);
      else renderCollections();
    } else if (route.name === "composiciones") {
      await renderComposiciones(route.id);
    }
  } catch (e) { toast(errMsg(e), "err"); }
  if (!(prevRoute && prevRoute.name === "imagen")) $("#main").scrollTo(0, 0);
  if (pendingFocusKey) { focusTile(pendingFocusKey); pendingFocusKey = null; }
}

function setTitle(t) { $("#page-title").textContent = t; }
function updateCounts() {
  $("#count-gal").textContent = state.counts.images ?? "";
  $("#count-col").textContent = state.counts.collections ?? "";
}

function setHash(hash) { if (location.hash !== hash) location.hash = hash; }

/* ─── galería ─── */
async function loadGallery({ append = false, limit = 60 } = {}) {
  const g = state.gallery;
  if (append && g.loading) return;
  const seq = ++g.seq;   // una respuesta vieja nunca pisa a una más reciente
  g.loading = true;
  g.error = null;
  try {
    const r = await api.get("/api/generations?" + filterQuery(g, { limit: append ? 60 : limit, offset: append ? g.items.length : 0 }));
    if (seq !== g.seq) return;
    g.items = append ? [...g.items, ...r.items] : r.items;
    rememberItems(r.items);
    g.total = r.total;
  } catch (e) { if (seq === g.seq) { g.error = errMsg(e); if (!append) { g.items = []; g.total = 0; } throw e; } }
  finally { if (seq === g.seq) g.loading = false; }
}

// Tarjeta: un botón real cubre la imagen y «Favorita» es su hermano (nunca un botón dentro de otro).
function tileEl(item, natural = true) {
  const t = h("div", { class: "tile" + (natural ? " natural" : ""), dataset: { key: item.key } });
  const img = h("img", { src: item.thumb_url, alt: "", loading: "lazy", decoding: "async" });
  if (item.width && item.height) { img.setAttribute("width", item.width); img.setAttribute("height", item.height); }
  t.append(h("button", { type: "button", class: "tile-open", "aria-label": "Abrir imagen: " + imgName(item), dataset: { open: item.key } },
    img,
    h("span", { class: "mtag", title: "Generada con " + modelName(item.model) }, svgUse("i-gemini"),
      h("span", { class: "mtag-full" }, modelName(item.model)), h("span", { class: "mtag-short" }, modelName(item.model).replace(/^Gemini /, ""))),
    item.animation ? h("span", { class: "atag", title: "Animación" }, svgUse("i-film"), h("span", { class: "sr" }, "Animación")) : null,
    h("span", { class: "lbl" }, h("span", { class: "glass" }, item.style_name || short(item.prompt, 40))),
    h("span", { class: "selmark", "aria-hidden": "true" }, svgUse("i-check"))));
  const fav = favLabel(item.favorite);
  t.append(h("button", { type: "button", class: "save" + (item.favorite ? " on" : ""),
    "aria-label": fav, title: fav, dataset: { fav: item.key } }, svgUse("i-bookmark")));
  t.append(h("button", { type: "button", class: "kebab", "aria-haspopup": "menu", "aria-expanded": "false",
    "aria-label": "Más acciones: " + imgName(item), title: "Más acciones", dataset: { menu: item.key } }, svgUse("i-more")));
  if (item.animation) wireTileAnim(t, item);
  syncTileSel(t, item);
  return t;
}
const favLabel = on => on ? "Quitar de favoritas" : "Añadir a favoritas";
// Nombre accesible único: dos generaciones con el mismo prompt se distinguen por el id corto de la generación (+ posición si hay variaciones).
const imgName = item => `${short(item.prompt, 60)} · ${String(item.gen_id || "").slice(-4)}` + (item.index != null ? ` · imagen ${Number(item.index) + 1}` : "");

/* ─── modo seleccionar y acciones por lote ───
   La selección vive en `sel.keys` (claves de imagen): sobrevive a la paginación y a los repintados; se poda solo contra listas completas. */
const MAX_ZIP = 100, MAX_BATCH = 200;
const sel = { on: false, keys: new Set(), anchor: null, busy: null, scope: null, failed: new Set(), failures: [] };
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
const imgWord = n => plural(n, "imagen", "imágenes");
const selLabel = (key, item) => { const it = item || state.itemsByKey.get(key); return it ? imgName(it) : "Imagen " + key; };
const paintedKeys = () => $$(".screen.active .tile[data-key]:not(.pending)").filter(t => t.offsetParent !== null).map(t => t.dataset.key);

// Casilla de una miniatura: en el modo, el botón que cubre la imagen pasa a ser un checkbox (clic, Espacio o Enter alternan).
function syncTileSel(t, item) {
  const b = $(".tile-open", t), key = t.dataset.key;
  if (!b || !key) return;
  if (sel.on && t._ap) stopTilePlayer(t);
  const checked = sel.on && sel.keys.has(key);
  t.classList.toggle("selectable", sel.on);
  t.classList.toggle("selected", checked);
  if (sel.on) {
    b.setAttribute("role", "checkbox");
    b.setAttribute("aria-checked", String(checked));
    b.setAttribute("aria-label", "Seleccionar imagen: " + selLabel(key, item));
  } else {
    b.removeAttribute("role");
    b.removeAttribute("aria-checked");
    b.setAttribute("aria-label", "Abrir imagen: " + selLabel(key, item));
  }
}
function setAction(btn, off) { if (off) btn.setAttribute("aria-disabled", "true"); else btn.removeAttribute("aria-disabled"); }
function syncSelbar() {
  const bar = $("#selbar"), n = sel.keys.size, busy = !!sel.busy;
  const text = plural(n, "seleccionada", "seleccionadas");
  const count = $("#sel-count");
  if (count.textContent !== text) count.textContent = text;   // nodo vivo fijo: solo cambia su texto
  bar.setAttribute("aria-busy", String(busy));
  const route = state.route;
  $("#sel-uncoll").hidden = !(route && route.name === "colecciones" && route.id);
  const over = n > MAX_ZIP, overBatch = n > MAX_BATCH;
  for (const b of $$("[data-sel-act]", bar)) {
    const act = b.dataset.selAct;
    setAction(b, busy || n === 0 || (act === "zip" ? over : overBatch));
    b.classList.toggle("busy", sel.busy === act);
  }
  setAction($("[data-sel-all]", bar), busy || !paintedKeys().length);
  setAction($("[data-sel-clear]", bar), busy || n === 0);
  setAction($("[data-sel-exit]", bar), busy);
  const note = $("#sel-note");
  note.hidden = !over;
  note.textContent = overBatch ? `Las acciones admiten hasta ${MAX_BATCH} imágenes por vez y hay ${n} seleccionadas. Quita ${n - MAX_BATCH} para poder aplicarlas.`
    : over ? `El zip admite hasta ${MAX_ZIP} imágenes y hay ${n} seleccionadas. Quita ${n - MAX_ZIP} para poder descargarlo.` : "";
  $("#sel-fails").hidden = !sel.failures.length;
}
function paintSel() { $$(".tile[data-key]").forEach(t => syncTileSel(t)); syncSelbar(); }
function setSelectMode(on) {
  if (sel.on === on) return;
  const bar = $("#selbar"), hadFocus = bar.contains(document.activeElement);
  sel.on = on;
  if (on) { closePops(); closeImgMenu(false); }
  else { sel.keys.clear(); sel.anchor = null; sel.failed.clear(); sel.failures = []; closeSelPop(false); }
  document.body.classList.toggle("selecting", on);
  $("#dock").classList.toggle("selecting", on);
  bar.hidden = !on;
  $$("[data-sel-toggle]").forEach(b => b.setAttribute("aria-pressed", String(on)));
  paintSel();
  if (!on && hadFocus) { const tb = $$("[data-sel-toggle]").find(b => b.offsetParent !== null); if (tb) tb.focus(); }
}
function clearSelection() {
  sel.keys.clear(); sel.anchor = null; sel.failed.clear(); sel.failures = [];
  paintSel();
}
// Al cambiar de pantalla (o de filtros, que cambian el hash base) la selección y el modo no se arrastran.
function selScope(scope) {
  if (sel.scope !== null && sel.scope !== scope) setSelectMode(false);
  sel.scope = scope;
}
function toggleSel(key, { range = false } = {}) {
  if (sel.busy) return;
  sel.failed.clear(); sel.failures = [];
  const keys = paintedKeys(), a = keys.indexOf(sel.anchor), b = keys.indexOf(key);
  if (range && a >= 0 && b >= 0) { for (let i = Math.min(a, b); i <= Math.max(a, b); i++) sel.keys.add(keys[i]); }
  else if (sel.keys.has(key)) sel.keys.delete(key);
  else sel.keys.add(key);
  sel.anchor = key;
  paintSel();
}
function selectAllVisible() {
  if (sel.busy) return;
  sel.failed.clear(); sel.failures = [];
  for (const k of paintedKeys()) sel.keys.add(k);
  paintSel();
}
// Tras repintar una lista: con la lista completa se descartan las claves que ya no existen (borradas o fuera de filtro).
function pruneSel(present, complete) {
  if (!sel.on) return;
  if (complete) for (const k of [...sel.keys]) if (!present.has(k) && !sel.failed.has(k)) sel.keys.delete(k);
  if (sel.anchor && complete && !present.has(sel.anchor)) sel.anchor = null;
  syncSelbar();
}
// Repintar una rejilla no debe soltar el foco de la tarjeta que lo tenía.
function keepTileFocus(paint) {
  const a = document.activeElement, key = a && a.matches && a.matches(".tile-open") ? a.dataset.open : null;
  paint();
  if (key) focusTile(key);
}
function forgetImages(keys) {
  for (const k of keys) state.itemsByKey.delete(k);
  state.recentItems = state.recentItems.filter(it => !keys.has(it.key));
  state.gallery.items = state.gallery.items.filter(it => !keys.has(it.key));
  if (state.detailList) state.detailList = state.detailList.filter(it => !keys.has(it.key));
  if (detailItem && keys.has(detailItem.key)) closeDetail();
}

const BATCH_ERR = { invalid_ref: "La imagen ya no existe (se borró o falta su archivo)." };
const batchErrMsg = e => (e && BATCH_ERR[e.code]) || errMsg(e);

async function withBusy(act, fn) {
  if (sel.busy) return;
  sel.busy = act;
  syncSelbar();
  try { await fn(); } finally { sel.busy = null; syncSelbar(); }
}

// Refresca contadores y la vista actual tras una acción (el evento SSE que sigue repinta lo mismo).
async function refreshAfterBatch() {
  try { const s = await api.get("/api/state"); state.counts = s.counts || {}; updateCounts(); } catch (e) {}
  try { state.collections = await api.get("/api/collections"); renderDest(); } catch (e) {}
  const r = state.route || parseRoute();
  try {
    if (r.name === "inicio") await renderInicio();
    else if (r.name === "colecciones" && r.id) await renderCollection(r.id);
    else if (r.name === "galeria" || r.name === "imagen") {
      await loadGallery({ limit: Math.min(200, Math.max(60, state.gallery.items.length)) });
      renderGallery();
    }
  } catch (e) { /* la siguiente pasada del SSE lo reintenta */ }
}

// Una acción por lote sobre toda la selección; las que fallan se quedan seleccionadas para reintentar.
async function applyBatch(action, collection) {
  const keys = [...sel.keys];
  sel.failures = [];
  let results;
  try {
    const body = { action, keys };
    if (collection) body.collection = collection;
    const r = await api.send("POST", "/api/images/batch", body);
    results = Array.isArray(r && r.results) ? r.results : [];
  } catch (e) { toast(errMsg(e), "err"); return; }
  const bad = results.filter(r => !r.ok), okKeys = new Set(results.filter(r => r.ok).map(r => r.key));
  if (action === "delete") forgetImages(okKeys);
  sel.keys = new Set(bad.filter(r => !(r.error && r.error.code === "invalid_ref")).map(r => r.key));
  sel.failed = new Set(sel.keys);
  sel.failures = bad;
  sel.anchor = null;
  if (!bad.length) toast(`Listo: ${imgWord(okKeys.size)}`);
  else toast(`${okKeys.size} de ${results.length}; ${plural(bad.length, "falló", "fallaron")}`, "err");
  await refreshAfterBatch();
  paintSel();
}
function openFailures() {
  const bad = sel.failures;
  if (!bad.length) return;
  $("#bd-title").textContent = bad.length === 1 ? "1 imagen falló" : `${bad.length} imágenes fallaron`;
  $("#bd-text").textContent = bad.some(r => sel.keys.has(r.key))
    ? "Las que siguen existiendo quedan seleccionadas para que puedas reintentar la acción."
    : "Esas imágenes ya no existen y se quitaron de la selección.";
  $("#bd-list").replaceChildren(...bad.map(r => h("li", {}, h("span", {}, selLabel(r.key)), h("small", {}, batchErrMsg(r.error)))));
  $("#batch-dialog").showModal();
}

/* popover «Añadir a colección…» */
function closeSelPop(restore) {
  const pop = $("#sel-coll-pop");
  if (pop.hidden) return;
  pop.hidden = true;
  pop.replaceChildren();
  $("#sel-coll-btn").setAttribute("aria-expanded", "false");
  if (restore) $("#sel-coll-btn").focus();
}
function openSelPop() {
  const pop = $("#sel-coll-pop"), n = sel.keys.size, here = state.gallery.collection;
  const input = h("input", { class: "input", maxlength: "80", placeholder: "Nueva colección…", "aria-label": "Nombre de la nueva colección" });
  const colls = state.collections.filter(c => c.id !== here);
  pop.replaceChildren(
    h("div", { class: "pop-head" }, h("span", {}, `Añadir ${imgWord(n)} a…`), h("span", {}, String(colls.length))),
    ...(colls.length ? colls.map(c => h("button", { type: "button", class: "opt", dataset: { selColl: c.id } },
      svgUse("i-folder"), h("span", { class: "minw-0" }, c.name), c.count != null ? h("span", { class: "n" }, String(c.count)) : null))
      : [h("p", { class: "pop-note" }, "Aún no hay colecciones. Crea la primera aquí abajo.")]),
    h("form", { class: "menu-form", onsubmit: e => {
      e.preventDefault();
      const name = input.value.trim();
      if (!name) { input.focus(); return; }
      addSelToCollection({ name });
    } }, input, h("button", { type: "submit", class: "btn btn-primary" }, svgUse("i-plus"), "Crear y añadir")));
  pop.hidden = false;
  $("#sel-coll-btn").setAttribute("aria-expanded", "true");
  (pop.querySelector(".opt") || input).focus();
}
async function addSelToCollection({ id = null, name = null }) {
  closeSelPop(true);
  await withBusy("collection", async () => {
    await applyBatch("add_to_collection", id || name);
  });
}

/* descarga .zip: POST con las claves, Blob y <a download> con el nombre de Content-Disposition */
async function downloadZip() {
  const keys = [...sel.keys];
  if (!keys.length || keys.length > MAX_ZIP) return;
  await withBusy("zip", async () => {
    let blob, name = "musa-imagenes.zip";
    try {
      const r = await doFetch("/api/images/zip", { method: "POST", credentials: "same-origin",
        headers: { "content-type": "application/json" }, body: JSON.stringify({ keys }) });
      if (!r.ok) {
        const e = await parseBody(r), miss = e && e.details && Array.isArray(e.details.missing) ? e.details.missing : [], missing = miss.length;
        if (r.status === 404 && missing) {
          for (const k of miss) sel.keys.delete(k);
          paintSel();
          toast(`No se pudo crear el zip: ${missing === 1 ? "1 imagen ya no existe" : missing + " imágenes ya no existen"}. Las quité de la selección; vuelve a descargar.`, "err");
        }
        else if (e && e.code === "invalid_query" && /zip limit/.test(e.message || "")) toast("La selección supera el límite de 500 MB del zip. Descarga menos imágenes a la vez.", "err");
        else toast(errMsg(e), "err");
        return;
      }
      const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(r.headers.get("content-disposition") || "");
      if (m) name = m[1];
      try { blob = await r.blob(); } catch (e) { throw { code: "network" }; }
    } catch (e) { toast(errMsg(e), "err"); return; }
    const url = URL.createObjectURL(blob);
    const a = h("a", { href: url, download: name });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    toast(`Descarga iniciada: ${imgWord(keys.length)} en ${name}`);
  });
}

async function runSelAction(act) {
  if (sel.busy || !sel.keys.size) return;
  const n = sel.keys.size;
  if (act === "collection") { if ($("#sel-coll-pop").hidden) openSelPop(); else closeSelPop(true); return; }
  closeSelPop(false);
  if (act === "zip") { await downloadZip(); return; }
  if (act === "delete") {
    if (!await confirmDlg({ title: `¿Borrar ${imgWord(n)}?`, text: `Se eliminan del disco ${n === 1 ? "la imagen seleccionada" : "las " + n + " imágenes seleccionadas"} y no se puede deshacer.`, ok: `Borrar ${imgWord(n)}` })) return;
    if (sel.busy || !sel.on) return;
    if (sel.keys.size !== n) { toast("La selección cambió; revisa y vuelve a borrar.", "err"); return; }
  }
  const coll = act === "remove_from_collection" ? state.gallery.collection : null;
  await withBusy(act, () => applyBatch(act, coll));
}
function wireSelection() {
  const bar = $("#selbar");
  bar.addEventListener("click", e => {
    const act = e.target.closest("[data-sel-act]");
    if (act) { if (act.getAttribute("aria-disabled") !== "true") runSelAction(act.dataset.selAct); return; }
    const coll = e.target.closest("[data-sel-coll]");
    if (coll) { addSelToCollection({ id: coll.dataset.selColl }); return; }
    const tool = e.target.closest("[data-sel-all], [data-sel-clear], [data-sel-exit], [data-sel-fails]");
    if (!tool || tool.getAttribute("aria-disabled") === "true") return;
    if (tool.matches("[data-sel-all]")) selectAllVisible();
    else if (tool.matches("[data-sel-clear]")) clearSelection();
    else if (tool.matches("[data-sel-exit]")) setSelectMode(false);
    else openFailures();
  });
  $("#sel-coll-pop").addEventListener("keydown", e => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    const items = $$(".opt, input, button[type=submit]", $("#sel-coll-pop")), i = items.indexOf(document.activeElement);
    if (!items.length) return;
    e.preventDefault();
    items[(i + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length].focus();
  });
  document.addEventListener("click", e => {
    if (!$("#sel-coll-pop").hidden && !e.target.closest("#sel-coll-pop, #sel-coll-btn")) closeSelPop(false);
  });
  // Mays+clic no debe seleccionar texto entre miniaturas
  document.addEventListener("mousedown", e => { if (sel.on && e.shiftKey && e.target.closest(".tile .tile-open")) e.preventDefault(); });
  document.addEventListener("keydown", e => {
    if (e.key !== "Escape" || !sel.on || e.defaultPrevented) return;
    if (!$("#sel-coll-pop").hidden) { e.preventDefault(); closeSelPop(true); return; }
    if (sel.busy || imgMenu.el || $$("dialog[open]").length || !$("#filter-pop").hidden || e.target.closest("input, textarea, select")) return;
    e.preventDefault();
    const tb = $$("[data-sel-toggle]").find(b => b.offsetParent !== null);
    setSelectMode(false);
    if (tb) tb.focus();
  });
  $("#bd-close").addEventListener("click", () => $("#batch-dialog").close());
  $("#batch-dialog").addEventListener("click", e => { if (e.target === $("#batch-dialog")) $("#batch-dialog").close(); });
  $("#batch-dialog").addEventListener("close", () => { const f = $("#sel-fails"); if (sel.on && !f.hidden) f.focus(); });
}

/* ─── menú ⋮ por imagen ───
   Un único menú flotante en <body>: sobrevive a los repintados (SSE) de las tarjetas y se reancla al ⋮ de la misma imagen. */
const imgMenu = { el: null, key: null, anchor: null, view: "main", mo: null, off: [] };
const kebabFor = key => $$(".kebab").find(b => b.dataset.menu === key && b.offsetParent !== null) || null;
function menuItem(icon, label, act, extra = {}) {
  return h("button", { type: "button", role: "menuitem", class: "menu-item" + (extra.danger ? " danger" : ""), tabindex: "-1",
    "aria-haspopup": extra.sub ? "menu" : null, dataset: { menuAct: act } },
    svgUse(icon), h("span", { class: "mi-txt" }, label), extra.sub ? svgUse("i-next") : null);
}
function menuFocusables() { return imgMenu.el ? $$('[role^="menuitem"], .menu-form input, .menu-form button', imgMenu.el) : []; }
function renderImgMenu(focusSel) {
  const m = imgMenu;
  if (!m.el) return;
  if (m.key.startsWith("style:")) { renderStyleMenu(focusSel); return; }
  const item = state.itemsByKey.get(m.key);
  if (!item) { closeImgMenu(false); return; }
  const menu = h("div", { role: "menu", "aria-label": "Acciones de la imagen" });
  let extra = null;
  if (m.view === "coll") {
    const input = h("input", { class: "input", maxlength: "80", placeholder: "Nueva colección…", "aria-label": "Nombre de la nueva colección" });
    const form = h("form", { class: "menu-form" }, input,
      h("button", { type: "submit", class: "btn btn-sm btn-primary" }, svgUse("i-plus"), "Crear"));
    form.addEventListener("submit", async e => {
      e.preventDefault();
      const name = input.value.trim();
      if (!name) { input.focus(); return; }
      try { await addToNewCollection(item, name); } catch (err) { toast(errMsg(err), "err"); return; }
      if (imgMenu.el) { renderImgMenu(); placeImgMenu(); const f = menuFocusables(); if (f.length) f[Math.max(0, state.collections.length)].focus(); }
    });
    menu.append(
      menuItem("i-prev", "Añadir a colección", "back"),
      ...(state.collections.length ? [h("div", { class: "menu-sep", role: "separator" })] : []),
      ...state.collections.map(c => h("button", { type: "button", role: "menuitemcheckbox", class: "menu-item", tabindex: "-1",
        "aria-checked": String(item.collections.includes(c.id)), dataset: { menuColl: c.id } },
        h("span", { class: "cb", "aria-hidden": "true" }), h("span", { class: "mi-txt" }, c.name))));
    extra = h("div", { class: "menu-extra" },
      state.collections.length ? null : h("p", { class: "menu-note" }, "Aún no hay colecciones. Crea la primera aquí."),
      form);
  } else {
    menu.append(...[
      menuItem("i-expand", "Abrir", "open"),
      item.conversation_id ? menuItem("i-edit", "Editar imagen", "edit") : null,
      h("button", { type: "button", role: "menuitemcheckbox", class: "menu-item", tabindex: "-1", "aria-checked": String(!!item.favorite), dataset: { menuAct: "fav" } },
        svgUse("i-bookmark"), h("span", { class: "mi-txt" }, "Favorita")),
      menuItem("i-folder-plus", "Añadir a colección", "coll", { sub: true }),
      menuItem("i-copy", "Copiar ID", "copy"),
      h("div", { class: "menu-sep", role: "separator" }),
      menuItem("i-trash", "Borrar", "delete", { danger: true })].filter(Boolean));
  }
  m.el.replaceChildren(...[menu, extra].filter(Boolean));
  if (focusSel) { const f = $(focusSel, m.el); if (f) f.focus(); }
}
// Menú ⋮ de una tarjeta de estilo: mismo contenedor flotante, anclaje y teclado que el de las imágenes.
function renderStyleMenu(focusSel) {
  const m = imgMenu, s = state.styles.find(x => "style:" + x.id === m.key);
  if (!s) { closeImgMenu(false); return; }
  const own = s.source === "user";
  const menu = h("div", { role: "menu", "aria-label": "Acciones del estilo" },
    ...[menuItem("i-check", "Usar", "style-use"),
      menuItem(own ? "i-edit" : "i-eye", own ? "Editar" : "Ver", "style-edit"),
      own ? h("div", { class: "menu-sep", role: "separator" }) : null,
      own ? menuItem("i-trash", "Borrar", "style-del", { danger: true }) : null].filter(Boolean));
  m.el.replaceChildren(menu);
  if (focusSel) { const f = $(focusSel, m.el); if (f) f.focus(); }
}
function placeImgMenu() {
  const m = imgMenu;
  if (!m.el) return;
  let a = m.anchor;
  if (!a || !a.isConnected || a.offsetParent === null) {
    a = kebabFor(m.key);
    if (!a) { closeImgMenu(false); return; }
    m.anchor = a; a.setAttribute("aria-expanded", "true");
  }
  const r = a.getBoundingClientRect(), el = m.el, vw = document.documentElement.clientWidth, vh = window.innerHeight;
  el.style.maxHeight = (vh - 16) + "px";
  const mw = el.offsetWidth, mh = el.offsetHeight;
  let left = r.left;
  if (left + mw > vw - 8) left = r.right - mw;           // no cabe a la derecha: se abre hacia la izquierda
  left = Math.max(8, Math.min(left, vw - 8 - mw));
  let top = r.bottom + 4;
  if (top + mh > vh - 8) top = Math.max(8, r.top - 4 - mh); // no cabe abajo: se abre hacia arriba
  el.style.left = left + "px"; el.style.top = top + "px";
}
function openImgMenu(btn) {
  const key = btn.dataset.menu;
  if (imgMenu.el && imgMenu.key === key) { closeImgMenu(true); return; }
  closeImgMenu(false);
  closePops();
  const m = imgMenu;
  m.el = h("div", { class: "img-menu" });
  m.key = key; m.anchor = btn; m.view = "main";
  btn.setAttribute("aria-expanded", "true");
  document.body.append(m.el);
  renderImgMenu();
  placeImgMenu();
  const first = menuFocusables()[0]; if (first) first.focus();
  const el = m.el;
  let raf = 0;
  const again = () => { cancelAnimationFrame(raf); raf = requestAnimationFrame(placeImgMenu); };
  const onDoc = e => { if (!e.composedPath().includes(el) && !e.target.closest(".kebab")) closeImgMenu(false); };
  const onKey = e => { if (e.key === "Escape") { e.preventDefault(); closeImgMenu(true); } };
  document.addEventListener("click", onDoc);
  document.addEventListener("keydown", onKey);
  document.addEventListener("scroll", again, { capture: true, passive: true });
  window.addEventListener("resize", again);
  m.mo = new MutationObserver(again);
  m.mo.observe($("#main"), { childList: true, subtree: true });
  m.off = [() => document.removeEventListener("click", onDoc), () => document.removeEventListener("keydown", onKey),
    () => document.removeEventListener("scroll", again, { capture: true }), () => window.removeEventListener("resize", again),
    () => cancelAnimationFrame(raf)];
  el.addEventListener("keydown", onImgMenuKey);
  el.addEventListener("click", onImgMenuClick);
}
function closeImgMenu(restoreFocus) {
  const m = imgMenu;
  if (!m.el) return;
  const key = m.key, a = m.anchor && m.anchor.isConnected ? m.anchor : kebabFor(key);
  m.off.forEach(f => f()); m.off = [];
  if (m.mo) { m.mo.disconnect(); m.mo = null; }
  m.el.remove(); m.el = null; m.key = null; m.anchor = null; m.view = "main";
  $$('.kebab[aria-expanded="true"]').forEach(b => b.setAttribute("aria-expanded", "false"));
  if (restoreFocus && a) a.focus();
}
function onImgMenuKey(e) {
  const m = imgMenu, items = menuFocusables(), i = items.indexOf(document.activeElement), inField = e.target.matches("input");
  if (!items.length) return;
  if (e.key === "ArrowDown") { e.preventDefault(); items[(i + 1) % items.length].focus(); }
  else if (e.key === "ArrowUp") { e.preventDefault(); items[(i - 1 + items.length) % items.length].focus(); }
  else if (e.key === "Home" && !inField) { e.preventDefault(); items[0].focus(); }
  else if (e.key === "End" && !inField) { e.preventDefault(); items[items.length - 1].focus(); }
  else if (e.key === "ArrowRight" && !inField && e.target.dataset.menuAct === "coll") { e.preventDefault(); e.target.click(); }
  else if (e.key === "ArrowLeft" && !inField && m.view === "coll") { e.preventDefault(); m.view = "main"; renderImgMenu('[data-menu-act="coll"]'); placeImgMenu(); }
  else if (e.key === "Tab") {
    // dentro del formulario «Nueva colección» el Tab recorre campo → botón sin cerrar ni perder el texto
    const inForm = e.target.closest(".menu-form"), last = items[items.length - 1], first = $(".menu-form input", imgMenu.el);
    if (inForm && (e.shiftKey ? e.target !== first : e.target !== last)) return;
    closeImgMenu(true);
  }
}
async function onImgMenuClick(e) {
  const m = imgMenu, b = e.target.closest("[data-menu-act], [data-menu-coll]");
  if (!b) return;
  if (m.key.startsWith("style:")) {
    const sid = m.key.slice(6);
    closeImgMenu(true);
    if (b.dataset.menuAct === "style-use") selectStyle(sid);
    else if (b.dataset.menuAct === "style-edit") openStyleDialog({ styleId: sid });
    else if (b.dataset.menuAct === "style-del") deleteStyle(sid);
    return;
  }
  const item = state.itemsByKey.get(m.key);
  if (!item) { closeImgMenu(false); return; }
  if (b.dataset.menuColl) {
    const id = b.dataset.menuColl, el = m.el;
    await toggleCollection(item, id);
    if (m.el === el) renderImgMenu(`[data-menu-coll="${CSS.escape(id)}"]`);
    return;
  }
  switch (b.dataset.menuAct) {
    case "open": closeImgMenu(false); openDetail(item.gen_id, item.index); break;
    case "edit": closeImgMenu(false); startEdit(item); break;
    case "fav": closeImgMenu(true); toggleFavorite(item); break;
    case "coll": m.view = "coll"; renderImgMenu(); placeImgMenu(); { const f = menuFocusables()[1] || menuFocusables()[0]; if (f) f.focus(); } break;
    case "back": m.view = "main"; renderImgMenu('[data-menu-act="coll"]'); placeImgMenu(); break;
    case "copy": closeImgMenu(true); copyItemId(item); break;
    case "delete": closeImgMenu(true); deleteItem(item); break;
  }
}
function focusTile(key) {
  const el = $$(".tile-open").find(b => b.dataset.open === key && b.offsetParent !== null);
  if (el) el.focus();
}

/* ─── filtros y orden: el estado vive en el hash (#galeria?model=…&style=…&since=…&until=…&sort=…&q=…) ─── */
const SORTS = [["newest", "Más recientes"], ["oldest", "Más antiguas"], ["favorites", "Favoritas primero"]];
const GAL_TABS = ["all", "in_collection", "no_collection", "favorites"];
const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;
const localDay = (d, add = 0) => { const [y, m, dd] = d.split("-").map(Number); return new Date(y, m - 1, dd + add); };
const CHIP_CLEAR = { model: { model: null }, style: { style: null }, date: { since: "", until: "" }, query: { query: "" } };

function filterQuery(g, { limit = 60, offset = 0 } = {}) {
  const p = new URLSearchParams();
  if (g.query) p.set("query", g.query);
  if (g.collection) p.set("collection", g.collection);
  if (g.filter && g.filter !== "all") p.set("filter", g.filter);
  if (g.style) p.set("style", g.style);
  if (g.model) p.set("model", g.model);
  if (g.sort && g.sort !== "newest") p.set("sort", g.sort);
  // el hash guarda el día local legible; la API recibe instantes locales (el servidor trata «YYYY-MM-DD» como UTC)
  if (g.since) p.set("since", localDay(g.since).toISOString());
  if (g.until) p.set("until", new Date(localDay(g.until, 1) - 1).toISOString().replace(/\.\d{3}Z$/, ".999999Z"));
  p.set("limit", String(limit));
  p.set("offset", String(offset));
  return p.toString();
}
function galleryHash() {
  const g = state.gallery, p = new URLSearchParams();
  if (g.filter !== "all") p.set("filter", g.filter);
  if (g.model) p.set("model", g.model);
  if (g.style) p.set("style", g.style);
  if (g.since) p.set("since", g.since);
  if (g.until) p.set("until", g.until);
  if (g.sort !== "newest") p.set("sort", g.sort);
  if (g.query) p.set("q", g.query);
  const qs = p.toString();
  return (g.collection ? "#colecciones/" + g.collection : "#galeria") + (qs ? "?" + qs : "");
}
// Día «YYYY-MM-DD» válido y representable (años 1000–9998: el servidor y toISOString rechazan o reinterpretan el resto).
const validDay = v => ISO_DAY.test(v || "") && +v.slice(0, 4) >= 1000 && +v.slice(0, 4) <= 9998 && ymd(localDay(v)) === v;
function readFilters(params, inColl = false) {
  const g = state.gallery, p = params || new URLSearchParams();
  const tabs = inColl ? ["all", "favorites"] : GAL_TABS;
  g.filter = tabs.includes(p.get("filter")) ? p.get("filter") : "all";
  g.model = p.get("model") || null;
  g.style = p.get("style") || null;
  g.since = validDay(p.get("since")) ? p.get("since") : "";
  g.until = validDay(p.get("until")) ? p.get("until") : "";
  g.sort = SORTS.some(([v]) => v === p.get("sort")) ? p.get("sort") : "newest";
  g.query = (p.get("q") || "").trim();
  $("#global-search").value = g.query;
}
const isFiltered = g => !!(g.filter !== "all" || g.query || g.style || g.model || g.since || g.until);
const dayText = d => { const [y, m, dd] = d.split("-").map(Number); return new Date(y, m - 1, dd).toLocaleDateString("es-ES", { day: "numeric", month: "short", year: "numeric" }); };
function dateRangeText(since, until) {
  if (since && until) return since === until ? dayText(since) : `${dayText(since)} – ${dayText(until)}`;
  return since ? "desde " + dayText(since) : "hasta " + dayText(until);
}
function activeChips(g) {
  const chips = [];
  if (g.model) chips.push({ key: "model", label: "Modelo: " + (modelChoices().find(m => m.id === g.model) || { name: modelName(g.model) }).name });
  if (g.style) { const s = state.styles.find(x => x.id === g.style); chips.push({ key: "style", label: "Estilo: " + (s ? s.name : g.style) }); }
  if (g.since || g.until) chips.push({ key: "date", label: "Fecha: " + dateRangeText(g.since, g.until) });
  if (g.query) chips.push({ key: "query", label: "Búsqueda: " + short(g.query, 28) });
  return chips;
}
function modelChoices() {
  const out = [], add = (id, name) => { if (id && !out.some(x => x.id === id)) out.push({ id, name: name || modelName(id) }); };
  for (const m of state.models) if (m.id !== "auto") add(m.id, m.name);
  for (const id of state.seenModels) add(id);
  add(state.gallery.model);
  // dos ids con el mismo nombre visible (p. ej. el modelo por defecto y su equivalente real) se distinguen por el id
  return out.map(m => out.filter(x => x.name === m.name).length > 1 ? { ...m, name: `${m.name} (${m.id})` } : m);
}

// Cambia filtros: actualiza el hash sin disparar render (push = entrada de historial) y recarga solo los resultados.
function applyFilters(patch = {}, { push = true } = {}) {
  const g = state.gallery;
  Object.assign(g, patch);
  if (push && "query" in patch) $("#global-search").value = g.query;
  const hash = galleryHash();
  if (hash !== location.hash) {
    lastHash = hash;
    state.baseHash = hash;
    sel.scope = hash;
    if (sel.keys.size) clearSelection();
    history[push ? "pushState" : "replaceState"](null, "", hash);
  }
  $$(".fbar").forEach(fillToolbar);
  return reloadGallery();
}
const focusFbtn = () => { const b = $$(".fbtn").find(x => x.offsetParent !== null); if (b) b.focus(); };

// Barra de la galería y del detalle de colección: pestañas, «Filtros», orden, píldoras activas y contador.
function fillToolbar(el) {
  const g = state.gallery, inColl = !!g.collection;
  const tabs = inColl ? [["all", "Todas", null], ["favorites", "Favoritas", null]]
    : [["all", "Todas", state.counts.images], ["in_collection", "En colección", null],
      ["no_collection", "Sin colección", null], ["favorites", "Favoritas", state.counts.favorites]];
  const nf = (g.model ? 1 : 0) + (g.style ? 1 : 0) + (g.since || g.until ? 1 : 0);
  const chips = activeChips(g);
  const act = document.activeElement, fk = act && el.contains(act) ? act.dataset.fk : null;
  const fres = $(".fres", el) || h("span", { class: "fres", role: "status", "aria-live": "polite" });   // nodo vivo fijo: solo cambia su texto
  const resText = g.total === 1 ? "1 resultado" : `${g.total} resultados`;
  if (fres.textContent !== resText) fres.textContent = resText;
  el.replaceChildren(
    h("div", { class: "fbar-main" },
      h("div", { class: "pills", role: "group", "aria-label": "Filtro" }, ...tabs.map(([v, l, n]) =>
        h("button", { class: "pill", "aria-pressed": String(g.filter === v), dataset: { gf: v, fk: "tab-" + v } }, l, n != null ? [" ", h("span", { class: "n" }, String(n))] : null))),
      h("div", { class: "fbar-tools" },
        h("button", { type: "button", class: "btn sel-toggle", "aria-pressed": String(sel.on), dataset: { selToggle: "1", fk: "seltoggle" } },
          svgUse("i-check-square"), "Seleccionar"),
        h("button", { type: "button", class: "btn fbtn" + (nf ? " on" : ""), "aria-haspopup": "dialog", "aria-controls": "filter-pop",
          "aria-expanded": String(!$("#filter-pop").hidden), dataset: { filters: "1", fk: "filters" } },
          svgUse("i-sliders"), "Filtros", nf ? h("span", { class: "fnum" }, String(nf)) : null),
        h("select", { class: "input fsort", "aria-label": "Ordenar", dataset: { sort: "1", fk: "sort" } },
          ...SORTS.map(([v, l]) => h("option", { value: v, selected: g.sort === v }, l))))),
    h("div", { class: "fbar-chips" },
      ...chips.map(c => h("span", { class: "chip-active" }, c.label,
        h("button", { type: "button", dataset: { chip: c.key }, "aria-label": "Quitar filtro: " + c.label }, svgUse("i-close")))),
      chips.length ? h("button", { type: "button", class: "link fclear", dataset: { clearChips: "1", fk: "clear" } }, "Limpiar filtros") : null,
      fres));
  if (fk) { const t = $(`[data-fk="${fk}"]`, el); if (t) t.focus(); }
}

/* popover de filtros: modelo, estilo y fecha; «Aplicar» confirma, «Limpiar» quita estos tres */
const ymd = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const daysAgo = n => { const d = new Date(); d.setDate(d.getDate() - n); return ymd(d); };
const DATE_SHORTCUTS = [["Hoy", 0], ["Últimos 7 días", 6], ["Últimos 30 días", 29]];
function closeFilterPop(restore) {
  const pop = $("#filter-pop");
  if (pop.hidden) return;
  pop.hidden = true;
  pop.replaceChildren();
  $$("[data-filters]").forEach(b => b.setAttribute("aria-expanded", "false"));
  if (restore) focusFbtn();
}
function toggleFilterPop(btn) {
  const pop = $("#filter-pop");
  if (!pop.hidden) { closeFilterPop(true); return; }
  const g = state.gallery;
  const styles = state.styles.some(s => s.id === g.style) || !g.style ? state.styles : [...state.styles, { id: g.style, name: g.style }];
  const mSel = h("select", { class: "input", id: "fp-model" }, h("option", { value: "" }, "Todos los modelos"),
    ...modelChoices().map(m => h("option", { value: m.id, selected: g.model === m.id }, m.name)));
  const sSel = h("select", { class: "input", id: "fp-style" }, h("option", { value: "" }, "Todos los estilos"),
    ...styles.map(s => h("option", { value: s.id, selected: g.style === s.id }, s.name)));
  const from = h("input", { type: "date", class: "input", id: "fp-since", value: g.since });
  const to = h("input", { type: "date", class: "input", id: "fp-until", value: g.until });
  const err = h("p", { class: "fp-err", id: "fp-err", role: "alert", hidden: true });
  const shorts = DATE_SHORTCUTS.map(([label, n]) => h("button", { type: "button", class: "pill", "aria-pressed": "false",
    onclick: () => { from.value = daysAgo(n); to.value = ymd(new Date()); sync(); } }, label));
  const sync = () => {
    err.hidden = true;
    from.removeAttribute("aria-invalid"); to.removeAttribute("aria-invalid");
    DATE_SHORTCUTS.forEach(([, n], i) => shorts[i].setAttribute("aria-pressed", String(from.value === daysAgo(n) && to.value === ymd(new Date()))));
  };
  from.addEventListener("input", sync); to.addEventListener("input", sync);
  const apply = async e => {
    e.preventDefault();
    const bad = [from, to].filter(x => x.value && !validDay(x.value));
    if (bad.length || (from.value && to.value && from.value > to.value)) {
      err.textContent = bad.length ? "Usa fechas válidas entre los años 1000 y 9998." : "«Desde» no puede ser posterior a «Hasta».";
      err.hidden = false;
      from.setAttribute("aria-invalid", "true"); to.setAttribute("aria-invalid", "true");
      (bad[0] || from).focus();
      return;
    }
    const patch = { model: mSel.value || null, style: sSel.value || null, since: from.value, until: to.value };
    closeFilterPop(true);
    await applyFilters(patch);
  };
  const clear = async () => { closeFilterPop(true); await applyFilters(CHIP_CLEAR_ALL); };
  pop.replaceChildren(h("form", { class: "fp", onsubmit: apply, novalidate: true },
    h("div", { class: "fp-head" }, h("h2", {}, "Filtros"),
      h("button", { type: "button", class: "btn icon-btn", "aria-label": "Cerrar filtros", onclick: () => closeFilterPop(true) }, svgUse("i-close"))),
    h("div", {}, h("label", { class: "label", for: "fp-model" }, "Modelo"), mSel),
    h("div", {}, h("label", { class: "label", for: "fp-style" }, "Estilo"), sSel),
    h("fieldset", { class: "fp-date" }, h("legend", { class: "label" }, "Fecha"),
      h("div", { class: "fp-short" }, ...shorts),
      h("div", { class: "fp-range" },
        h("div", {}, h("label", { class: "label", for: "fp-since" }, "Desde"), from),
        h("div", {}, h("label", { class: "label", for: "fp-until" }, "Hasta"), to)),
      err),
    h("div", { class: "fp-foot" },
      h("button", { type: "button", class: "btn", onclick: clear }, "Limpiar"),
      h("button", { type: "submit", class: "btn btn-primary" }, "Aplicar"))));
  sync();
  pop.hidden = false;
  btn.setAttribute("aria-expanded", "true");
  if (matchMedia("(max-width: 860px)").matches) { pop.style.left = ""; pop.style.top = ""; }
  else {
    const r = btn.getBoundingClientRect();
    pop.style.left = Math.max(12, Math.min(r.left, innerWidth - pop.offsetWidth - 12)) + "px";
    pop.style.top = (r.bottom + 6) + "px";
  }
  mSel.focus();
}
const CHIP_CLEAR_ALL = { model: null, style: null, since: "", until: "" };

function renderGallery() {
  const g = state.gallery;
  fillToolbar($("#gal-toolbar"));
  const filtered = isFiltered(g);
  const pend = filtered || g.sort !== "newest" ? [] : pendingBatches().map(b => pendingTile(b, true));
  keepTileFocus(() => $("#masonry").replaceChildren(...pend, ...state.gallery.items.map(it => tileEl(it))));
  pruneSel(new Set(state.gallery.items.map(it => it.key)), !g.error && g.items.length >= g.total);
  const empty = $("#gal-empty");
  if (!g.items.length && !pend.length) {
    empty.hidden = false;
    const text = g.error ? g.error
      : g.query && !(g.style || g.model || g.since || g.until || g.filter !== "all") ? `Nada coincide con «${g.query}».`
      : state.counts.images === 0 && !filtered ? "Aún no hay imágenes. Genera la primera con el compositor de abajo."
      : "No hay imágenes con estos filtros.";
    empty.replaceChildren(...[h("p", {}, text),
      filtered && !g.error ? h("button", { class: "btn btn-sm", dataset: { clearFilters: "1" } }, "Quitar filtros") : null].filter(Boolean));
  } else empty.hidden = true;
  watchScroll();
}


let galObserver = null;
function watchScroll() {
  if (galObserver) galObserver.disconnect();
  galObserver = new IntersectionObserver(entries => {
    if (!entries.some(e => e.isIntersecting)) return;
    const g = state.gallery;
    if (g.loading || g.items.length >= g.total) return;
    const before = g.items.length;
    loadGallery({ append: true }).then(() => {
      $("#masonry").append(...state.gallery.items.slice(before).map(it => tileEl(it)));
      syncSelbar();
    }).catch(e => toast(errMsg(e), "err"));
  }, { root: $("#main"), rootMargin: "600px" });
  const s = $("#gal-sentinel"); if (s) galObserver.observe(s);
}

async function reloadGallery() {
  const g = state.gallery, r = state.route;
  if (r && r.name === "colecciones" && r.id) return refreshCollectionGrid();
  g.items = [];
  const p = loadGallery().catch(e => toast(errMsg(e), "err"));
  const mine = g.seq;   // loadGallery incrementa `seq` de forma síncrona
  await p;
  if (mine !== g.seq) return;
  renderGallery();
}
function clearGalleryFilters() {
  Object.assign(state.gallery, { filter: "all", query: "", style: null, model: null, since: "", until: "" });
  $("#global-search").value = "";
}

// Detalle de colección: misma consulta filtrada, hasta 200 imágenes (sin paginar). false = una consulta más reciente la sustituyó.
async function fetchCollectionItems() {
  const g = state.gallery, seq = ++g.seq;
  g.error = null;
  try {
    const r = await api.get("/api/generations?" + filterQuery(g, { limit: 200 }));
    if (seq !== g.seq) return false;
    g.items = r.items; g.total = r.total;
    rememberItems(r.items);
  } catch (e) {
    if (seq !== g.seq) return false;
    g.items = []; g.total = 0; g.error = errMsg(e);
    toast(g.error, "err");
  }
  return true;
}
async function refreshCollectionGrid() {
  if (await fetchCollectionItems()) paintCollectionGrid();
}
function paintCollectionGrid() {
  const g = state.gallery, bar = $("#coll-toolbar"), host = $("#coll-results");
  if (!bar || !host) return;
  fillToolbar(bar);
  if (g.items.length) {
    keepTileFocus(() => host.replaceChildren(h("div", { class: "grid-results" }, ...g.items.map(it => tileEl(it, false)))));
    pruneSel(new Set(g.items.map(it => it.key)), !g.error && g.items.length >= g.total);
    return;
  }
  pruneSel(new Set(), !g.error);
  const filtered = isFiltered(g);
  host.replaceChildren(h("div", { class: "empty" }, ...[
    h("p", {}, g.error || (filtered ? "No hay imágenes con estos filtros." : "Esta colección aún no tiene imágenes. Guarda imágenes desde la galería.")),
    filtered && !g.error ? h("button", { class: "btn btn-sm", dataset: { clearFilters: "1" } }, "Quitar filtros") : null].filter(Boolean)));
}

/* ─── inicio ─── */
async function ensureStyles() {
  if (!state.stylesLoaded) { state.styles = await api.get("/api/styles"); state.stylesLoaded = true; }
}
function defaultStyleId() {
  let saved = null;
  try { saved = localStorage.getItem("musa-style"); } catch (e) {}
  if (saved === "") return null;               // el usuario quitó el estilo a propósito
  if (saved && state.styles.some(s => s.id === saved)) return saved;
  return null;                                   // sin estilo por defecto
}
function selectStyle(id) {
  if (id === "") {
    state.style = null;
    try { localStorage.setItem("musa-style", ""); } catch (e) {}
    renderStyles(); renderToolRow(); updateComposer();
    toast("Sin estilo: solo el prompt.");
    return;
  }
  const s = state.styles.find(x => x.id === id);
  if (!s) return;
  state.style = id;
  try { localStorage.setItem("musa-style", id); } catch (e) {}
  renderStyles();
  renderToolRow();
  updateComposer();
  toast("Estilo seleccionado: " + s.name + ".");
}
function styleThumbEl(s) {
  const thumb = h("span", { class: "thumb" });
  if (s.cover_url) thumb.append(h("img", { src: coverSrc(s), alt: "", loading: "lazy", decoding: "async" }));
  else thumb.append(h("span", { class: "ph" }, svgUse("i-spark")));
  thumb.append(h("span", { class: "sel", "aria-hidden": "true" }, svgUse("i-check")));
  return thumb;
}
function renderToolRow() {
  const row = $("#tool-row");
  if (!row) return;
  row.replaceChildren(...state.styles.slice(0, 4).map(s =>
    h("button", { class: "tool", dataset: { start: s.id }, "aria-pressed": String(state.style === s.id),
      "aria-label": "Seleccionar estilo " + s.name },
      styleThumbEl(s),
      h("span", {}, h("h3", {}, s.name), s.description ? h("p", {}, s.description) : null))));
}
async function renderInicio() {
  await ensureStyles();
  if (!state.style) state.style = defaultStyleId();
  renderToolRow();
  const link = $("#see-all-styles");
  if (link) link.textContent = state.styles.length > 4 ? `Ver los ${state.styles.length} estilos` : "Ver estilos";
  try {
    const r = await api.get("/api/generations?limit=4");
    state.recentItems = r.items;
    rememberItems(r.items);
    state.recentError = null;
  } catch (e) { state.recentItems = []; state.recentError = errMsg(e); }
  renderRecentStrip();
  renderQuotaSide();
}
function renderRecentStrip() {
  const pend = pendingBatches().slice(0, 4).map(b => pendingTile(b, false));
  const fill = Math.max(0, 4 - pend.length);
  keepTileFocus(() => $("#strip-recent").replaceChildren(...pend, ...state.recentItems.slice(0, fill).map(it => tileEl(it, false))));
  pruneSel(new Set(state.recentItems.slice(0, fill).map(it => it.key)), !state.recentError);
  const none = !pend.length && !state.recentItems.length;
  const empty = $("#recent-empty");
  empty.hidden = !none;
  empty.textContent = state.recentError || "Aún no hay imágenes. Genera la primera con el compositor de abajo.";
}

/* ─── cuota (/api/quota) ─── */
const fmtHM = ts => { try { return new Date(ts).toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit" }); } catch (e) { return ""; } };
function quotaOk() {
  const q = state.quota;
  return !!(q && q.available !== false && q.window_5h_pct != null);
}
function quotaFree() { return quotaOk() ? Math.max(0, Math.round(100 - state.quota.window_5h_pct)) : null; }
async function loadQuota() {
  try { state.quota = await api.get("/api/quota"); }
  catch (e) { state.quota = null; }
  renderQuota();
}
function renderQuota() {
  const pill = $("#quota-pill-pct");
  const est = quotaOk() ? state.quota.est_images_left_5h : null;
  if (pill) {
    pill.textContent = quotaOk() ? quotaFree() + "%" : "—";
    pill.parentElement.title = quotaOk()
      ? (est != null ? `≈ ${est} imágenes libres en esta ventana de 5 h` : "Cuota de la ventana de 5 h")
      : "Sin datos de cuota";
  }
  if (!quotaOk()) {
    $("#plan-tier").textContent = "Sin datos de cuota";
    $("#plan-used").textContent = "";
    $("#plan-used-lbl").textContent = "";
    $("#plan-bar").style.width = "0";
    $("#plan-bar").classList.remove("warn");
    $("#plan-reset").textContent = "";
  } else {
    const q = state.quota, free = quotaFree();
    $("#plan-tier").replaceChildren("Plan ", h("span", { class: "upper" }, String(q.tier || "PRO")));
    $("#plan-used").textContent = free + "%";
    $("#plan-used-lbl").textContent = " libre en 5 h";
    $("#plan-bar").style.width = free + "%";
    $("#plan-bar").classList.toggle("warn", free <= 20);
    $("#plan-reset").textContent = q.resets_at_5h ? "Se renueva a las " + fmtHM(q.resets_at_5h) + "." : "";
  }
  renderQuotaSide();
  syncStyleTest();
}
function renderQuotaSide() {
  const el = $("#side-quota"), lbl = $("#side-quota-lbl");
  if (!el || !lbl) return;
  const free = quotaFree();
  const est = quotaOk() ? state.quota.est_images_left_5h : null;
  el.textContent = free != null ? free + "%" : "—";
  lbl.textContent = free != null
    ? (est != null ? `libre en 5 h · ≈ ${est} imágenes` : "libre en 5 h")
    : "Sin datos de cuota";
}

/* ─── compositor y cola ─── */
const RATIOS = [
  { r: "1:1",  n: "Cuadrado",       g: "Cuadrado" },
  { r: "4:5",  n: "Retrato",        g: "Vertical" }, { r: "3:4", n: "Retrato clásico", g: "Vertical" },
  { r: "2:3",  n: "Póster",         g: "Vertical" }, { r: "9:16", n: "Historia", g: "Vertical" },
  { r: "5:4",  n: "Impreso",        g: "Horizontal" }, { r: "4:3", n: "Fotografía", g: "Horizontal" },
  { r: "3:2",  n: "35 mm",          g: "Horizontal" }, { r: "16:9", n: "Pantalla ancha", g: "Horizontal" },
  { r: "21:9", n: "Panorámica",     g: "Horizontal" }
];
const MAX_REFS = 3;
const modelLabel = id => !id || id === "auto" ? "Auto" : modelName(id);
const styleName = id => { const s = state.styles.find(x => x.id === id); return s ? s.name : (id ? "Estilo eliminado" : "Sin estilo"); };
const ratioVal = r => { const [a, b] = String(r).split(":").map(Number); return a / b; };
const ricoStyle = (r, max = 14) => { const v = ratioVal(r); const w = v >= 1 ? max : Math.round(max * v), hh = v >= 1 ? Math.round(max / v) : max; return `width:${Math.max(w, 5)}px;height:${Math.max(hh, 5)}px`; };
const autosize = el => {
  el.style.height = "auto";
  const lh = parseFloat(getComputedStyle(el).lineHeight) || 24;
  el.style.height = Math.min(el.scrollHeight, lh * 6) + "px";
};
const setReady = ready => { const b = $("#send"); b.classList.toggle("ready", ready); b.setAttribute("aria-disabled", String(!ready)); };

const FALLBACK_MODELS = [{ id: "auto", name: "Auto", description: "" }, { id: "gemini-flash", name: "Flash", description: "" }, { id: "gemini-pro", name: "Pro", description: "" }];
async function loadModels() {
  try {
    const r = await api.get("/api/models");
    const list = Array.isArray(r && r.models) ? r.models.filter(m => m && m.id) : [];
    state.models = list.length ? list : FALLBACK_MODELS;
    state.modelDefault = (r && r.default) || null;
  } catch (e) { state.models = FALLBACK_MODELS; state.modelDefault = null; }
  if (!state.models.some(m => m.id === state.cmp.model)) state.cmp.model = "auto";
  updateComposer();
}
function updateComposer() {
  const c = state.cmp, ratio = c.anim ? ANIM_GRIDS[c.anim.grid].ratio : c.ratio, ri = RATIOS.find(x => x.r === ratio);
  $("#cost").textContent = c.n + " img";
  $("#var-n").textContent = String(c.n);
  $("#var-minus").disabled = $("#var-plus").disabled = !!c.edit;
  const mName = modelLabel(c.model);
  $("#model-txt").textContent = mName.replace(/^Gemini /, "");
  $("#model-btn").setAttribute("aria-label", "Modelo: " + mName);
  $("#ratio-txt").textContent = ratio;
  $("#ratio-btn").disabled = !!c.anim;
  $("#ratio-btn").setAttribute("aria-label", "Proporción: " + ratio + (ri ? " " + ri.g : "") + (c.anim ? " (la fija la rejilla de la animación)" : ""));
  $("#ratio-ico").style.cssText = ricoStyle(ratio);
  const selStyle = state.styles.find(x => x.id === state.style);
  const sName = styleName(state.style) + (selStyle && selStyle.gem ? " · Gem" : "");
  $("#style-chip-txt").textContent = sName;
  $("#style-btn").setAttribute("aria-label", "Estilo: " + sName);
  $("#tpl-btn").hidden = !Object.keys(state.templates || {}).length;
  $("#tpl-btn").disabled = !!c.edit || !!c.anim;
  $("#tpl-txt").textContent = c.template ? tplLabel(c.template) : "Ninguna";
  $("#tpl-btn").classList.toggle("on", !!c.template);
  $("#tpl-btn").setAttribute("aria-label", "Plantilla: " + (c.template ? tplLabel(c.template) : "ninguna"));
  const ab = $("#anim-btn"), ag = c.anim && ANIM_GRIDS[c.anim.grid];
  ab.disabled = !!c.edit;
  ab.classList.toggle("on", !!c.anim);
  $("#anim-txt").textContent = c.anim ? "Anim. " + ag.label : "Imagen";
  ab.setAttribute("aria-label", "Tipo: " + (c.anim ? `animación ${ag.label}, ${animBgName(c.anim)}` : "imagen"));
  $("#edit-banner").hidden = !c.edit;
  if (c.edit) { $("#edit-thumb").src = c.edit.thumb; $("#edit-prompt").textContent = short(c.edit.prompt, 90); $("#edit-prompt").title = c.edit.prompt; }
  $("#prompt").placeholder = c.edit ? "Describe qué quieres cambiar en esta imagen…" : c.anim ? "Describe el personaje y la acción (p. ej. «gato astronauta caminando hacia la derecha»)" : "Describe la imagen que quieres generar…";
  const sendLbl = c.edit ? "Enviar edición" : "Generar";
  $("#send").setAttribute("aria-label", sendLbl); $("#send").title = sendLbl;
  if (!c.template) $("#prompt-label").textContent = c.edit ? "Describe qué cambiar en la imagen" : c.anim ? "Describe el personaje y la acción" : "Describe la imagen";
}

/* referencias del compositor */
function renderRefs() {
  const c = state.cmp;
  const kids = c.refs.map((r, i) =>
    h("div", { class: "ref", title: r.name },
      h("img", { src: r.url, alt: "Referencia: " + r.name }),
      h("button", { type: "button", class: "rx", dataset: { unref: String(i) }, "aria-label": "Quitar referencia " + r.name }, svgUse("i-close"))));
  $("#refs").replaceChildren(...kids);
  $("#ref-count").textContent = `${c.refs.length}/${MAX_REFS}`;
  $("#attach").classList.toggle("has", c.refs.length > 0);
  $("#attach").setAttribute("aria-label", `Añadir imagen de referencia, ${c.refs.length}/${MAX_REFS}`);
}
function refKey(r) { return r.kind === "upload" ? "upload:" + r.id : r.ref; }
function addRef(ref) {
  const c = state.cmp;
  if (c.refs.length >= MAX_REFS) { toast(`Máximo ${MAX_REFS} referencias por generación.`, "err"); return false; }
  if (c.refs.some(r => refKey(r) === refKey(ref))) return false;
  c.refs.push(ref); renderRefs(); return true;
}
async function uploadFiles(files) {
  for (const f of files) {
    if (!f.type.startsWith("image/")) continue;
    try {
      const fd = new FormData(); fd.append("file", f);
      const r = await api.send("POST", "/api/uploads", fd);
      addRef({ kind: "upload", id: r.id, url: r.thumb_url, name: f.name });
    } catch (e) { toast(errMsg(e), "err"); }
  }
}
function renderAttachPop() {
  const c = state.cmp;
  const gal = [...state.recentItems, ...state.gallery.items].filter((it, i, arr) => arr.findIndex(x => x.key === it.key) === i).slice(0, 8);
  const upIc = svgUse("i-upload"); upIc.classList.add("i-sm");
  $("#attach-pop").replaceChildren(
    h("div", { class: "pop-head" }, h("span", {}, "Imagen de referencia"), h("span", {}, `${c.refs.length}/${MAX_REFS}`)),
    h("button", { class: "opt", dataset: { upload: "1" } },
      h("span", { class: "mk" }, upIc), "Subir desde tu equipo", h("span", { class: "prov" }, "o arrastra aquí")),
    h("div", { class: "opt-sep" }),
    h("div", { class: "pop-sub" }, "De tu galería"),
    h("div", { class: "ref-grid pop-refs" }, ...gal.map(it =>
      h("button", { class: "ref-pick", dataset: { pickRef: it.key },
        "aria-pressed": String(c.refs.some(r => r.ref === "gen:" + it.key)),
        "aria-label": "Usar " + it.prompt + " como referencia" },
        h("img", { src: it.thumb_url, alt: "" })))));
}

/* popovers del compositor */
function renderStylePop() {
  $("#style-pop").replaceChildren(
    h("div", { class: "pop-head" }, h("span", {}, "Estilo"), h("span", {}, String(state.styles.length))),
    h("button", { class: "sopt", role: "option", "aria-selected": String(!state.style), dataset: { pickStyle: "" } },
      h("span", { class: "mk" }, svgUse("i-close")),
      h("span", { class: "minw-0" }, h("b", {}, "Sin estilo"), h("small", {}, "Solo el prompt"))),
    ...state.styles.map(s => {
      const b = h("button", { class: "sopt", role: "option", "aria-selected": String(s.id === state.style), dataset: { pickStyle: s.id } });
      if (s.cover_url) b.append(h("img", { src: coverSrc(s), alt: "", width: 38, height: 38 }));
      b.append(h("span", { class: "minw-0" }, h("b", {}, s.name), h("small", {}, s.description || FAM_LABEL[s.family] || "")));
      return b;
    }),
    h("button", { class: "pop-foot", dataset: { nav: "estilos" } }, "Ver todos los estilos"));
}
function renderRatioPop() {
  const groups = ["Cuadrado", "Vertical", "Horizontal"];
  const kids = [h("div", { class: "pop-head" }, h("span", {}, "Proporción"), h("span", {}, RATIOS.length + " formatos"))];
  for (const g of groups) {
    kids.push(h("div", { class: "pop-sub" }, g));
    kids.push(h("div", { class: "ratio-grid" }, ...RATIOS.filter(x => x.g === g).map(x => {
      const rico = h("span", { class: "rico" }); rico.style.cssText = ricoStyle(x.r, 18);
      return h("button", { class: "ropt", role: "option", "aria-selected": String(x.r === state.cmp.ratio), dataset: { pickRatio: x.r } },
        h("span", { class: "rico-box" }, rico),
        h("span", {}, h("b", {}, x.r), h("small", {}, x.n)));
    })));
  }
  kids.push(h("p", { class: "pop-note" }, "Gemini trata la proporción como una pista de texto."));
  $("#ratio-pop").replaceChildren(...kids);
}
function renderModelPop() {
  const dflt = state.modelDefault && state.modelDefault.resolved;
  const real = dflt && dflt !== "auto" ? state.models.find(x => x.id === dflt) : null;
  $("#model-pop").replaceChildren(
    h("div", { class: "pop-head" }, h("span", {}, "Modelo de imagen"), h("span", {}, "Gemini")),
    ...state.models.map(m => {
      const auto = m.id === "auto";
      const mk = auto ? svgUse("i-spark") : h("i");
      if (auto) mk.classList.add("i-sm");
      const usa = auto && dflt && dflt !== "auto" ? knownModelName(dflt) : null;
      const title = auto ? (usa ? `Auto · usa ${usa}` : "Auto") : m.name;
      const desc = auto ? (real && real.description) || "Gemini elige el modelo" : m.description;
      return h("button", { class: "opt", role: "option", "aria-selected": String(m.id === state.cmp.model), dataset: { model: m.id } },
        h("span", { class: "mk" }, mk),
        h("span", { class: "minw-0" }, h("b", {}, title), desc ? h("small", {}, desc) : null),
        h("span", { class: "prov" }, auto && !usa ? "Recomendado" : ""));
    }));
}

/* prompts guardados */
const usesLabel = n => n === 1 ? "1 uso" : n > 1 ? `${n} usos` : "";
let promptsLocalAt = 0;   // última recarga provocada por una acción propia (el evento SSE que sigue no recarga otra vez)
async function loadPrompts(local = false) {
  try {
    const r = await api.get("/api/prompts");
    state.prompts = Array.isArray(r) ? r : [];
  } catch (e) { if (!state.prompts) state.promptsError = errMsg(e); }
  if (local) promptsLocalAt = Date.now();
  renderPromptList();
}
function promptRow(p) {
  return h("div", { class: "prow", role: "listitem" },
    h("button", { type: "button", class: "opt", title: p.text, dataset: { pnav: "", fk: "i" + p.id }, onclick: () => insertPrompt(p) },
      h("span", { class: "minw-0" }, h("b", {}, p.name), h("small", {}, String(p.text).replace(/\s+/g, " "))),
      h("span", { class: "uses tnum" }, usesLabel(p.uses))),
    h("button", { type: "button", class: "ra", dataset: { fk: "e" + p.id }, "aria-label": `Editar «${p.name}»`, title: "Editar", onclick: () => openPromptDialog({ prompt: p }) }, svgUse("i-edit")),
    h("button", { type: "button", class: "ra", dataset: { fk: "d" + p.id }, "aria-label": `Borrar «${p.name}»`, title: "Borrar", onclick: () => deletePrompt(p) }, svgUse("i-trash")));
}
function renderPromptList() {
  const list = $("#pp-list");
  if (!list) return;
  const act = document.activeElement, fk = act && list.contains(act) ? act.dataset.fk : null;
  const all = state.prompts, q = state.promptQuery.trim().toLowerCase();
  let kids;
  if (!all) {
    kids = [h("p", { class: "q-empty", role: state.promptsError ? "alert" : "status" }, state.promptsError || "Cargando prompts…")];
  } else {
    const rows = q ? all.filter(p => (p.name + " " + p.text).toLowerCase().includes(q)) : all;
    kids = rows.length ? rows.map(promptRow)
      : [h("p", { class: "q-empty" }, all.length ? "Ningún prompt coincide con la búsqueda." : "Aún no has guardado prompts. Escribe uno y pulsa «Guardar el prompt actual…».")];
  }
  list.replaceChildren(...kids);
  $("#pp-count").textContent = all ? String(all.length) : "";
  if (fk) { const again = list.querySelector(`[data-fk="${CSS.escape(fk)}"]`); if (again) again.focus(); }
}
function syncPromptSave() {
  const b = $("#pp-save");
  if (!b) return;
  const off = !!state.cmp.template || !$("#prompt").value.trim();
  b.disabled = off;
  b.title = off ? "Escribe un prompt en el compositor para guardarlo" : "";
  if (off) b.removeAttribute("data-pnav"); else b.setAttribute("data-pnav", "");
}
function renderPromptPop() {
  state.promptQuery = "";
  state.promptsError = "";
  $("#prompt-pop").replaceChildren(
    h("div", { class: "pop-head" }, h("span", {}, "Prompts guardados"), h("span", { id: "pp-count" })),
    h("div", { class: "pp-search" }, svgUse("i-search"),
      h("input", { class: "input", id: "pp-q", type: "search", autocomplete: "off", placeholder: "Buscar prompts…", "aria-label": "Buscar prompts guardados", "data-autofocus": "",
        oninput: e => { state.promptQuery = e.target.value; renderPromptList(); } })),
    h("div", { class: "pp-list", id: "pp-list", role: "list" }),
    h("button", { type: "button", class: "pop-foot pp-foot", id: "pp-save", onclick: () => { const t = $("#prompt").value.trim(); if (t) { closePops(); openPromptDialog({ text: t }); } } },
      "Guardar el prompt actual…"));
  syncPromptSave();
  renderPromptList();
  loadPrompts();
}
function insertPrompt(p) {
  const c = state.cmp, pr = $("#prompt");
  if (c.template) setTemplate(null);
  const cur = pr.value.trimEnd();
  pr.value = cur ? cur + "\n" + p.text : p.text;
  histIdx = -1; autosize(pr); refreshReady();
  closePops();
  pr.focus(); pr.setSelectionRange(pr.value.length, pr.value.length);
  toast(cur ? `«${p.name}» añadido al final del prompt.` : `Prompt «${p.name}» insertado.`);
  api.send("POST", `/api/prompts/${encodeURIComponent(p.id)}/use`).catch(() => {});
}
async function deletePrompt(p) {
  closePops();
  const ok = await confirmDlg({ title: "¿Borrar este prompt?", text: `«${p.name}» se quitará de tus prompts guardados.`, ok: "Borrar" });
  $("#prompts-btn").focus();
  if (!ok) return;
  promptsLocalAt = Date.now();
  try { await api.send("DELETE", `/api/prompts/${encodeURIComponent(p.id)}`); toast("Prompt borrado."); }
  catch (e) { toast(errMsg(e), "err"); }
  await loadPrompts(true);
}
// Diálogo de guardar/editar: `prompt` edita uno existente; sin él crea uno nuevo con `text`.
let pdState = null;
function openPromptDialog({ prompt = null, text = "" } = {}) {
  const a = document.activeElement;
  pdState = { id: prompt ? prompt.id : null, opener: a && a.closest("#prompt-pop") ? $("#prompts-btn") : a };
  closePops();
  $("#pd-title").textContent = prompt ? "Editar prompt" : "Guardar prompt";
  $("#pd-ok").replaceChildren(svgUse("i-check"), prompt ? "Guardar cambios" : "Guardar");
  $("#pd-name").value = prompt ? prompt.name : "";
  $("#pd-text").value = prompt ? prompt.text : text;
  $("#prompt-dialog").showModal();
  $("#pd-name").focus();
}
async function submitPromptDialog(e) {
  e.preventDefault();
  const name = $("#pd-name").value.trim(), text = $("#pd-text").value.trim();
  if (!name || !text || !pdState) return;
  const btn = $("#pd-ok"), id = pdState.id;
  btn.disabled = true; promptsLocalAt = Date.now();
  try {
    if (id) await api.send("PUT", `/api/prompts/${encodeURIComponent(id)}`, { name, text });
    else await api.send("POST", "/api/prompts", { name, text });
    $("#prompt-dialog").close();
    toast(id ? "Prompt actualizado." : `Prompt guardado: ${name}.`);
    await loadPrompts(true);
  } catch (err) {
    toast(errMsg(err), "err");
    if (err && err.code === "unknown_prompt") { $("#prompt-dialog").close(); loadPrompts(true); }
  } finally { btn.disabled = false; }
}
/* tipo de generación: imagen o animación (sprite sheet) */
const ANIM_GRIDS = { "4x2": { label: "4×2", frames: 8, ratio: "16:9" }, "3x3": { label: "3×3", frames: 9, ratio: "1:1" }, "2x2": { label: "2×2", frames: 4, ratio: "1:1" } };
const ANIM_BGS = [
  { id: "sprite:green", mode: "sprite", key_color: "green", name: "Mascota · verde", desc: "Fondo verde que pasa a transparente" },
  { id: "sprite:magenta", mode: "sprite", key_color: "magenta", name: "Mascota · magenta", desc: "Para personajes verdes" },
  { id: "scene", mode: "scene", key_color: null, name: "Escena · sin transparencia", desc: "La imagen queda opaca" },
];
const animDefault = () => ({ grid: "4x2", mode: "sprite", key_color: "green" });
const animBgId = a => a.mode === "scene" ? "scene" : "sprite:" + (a.key_color || "green");
const animBgName = a => (ANIM_BGS.find(b => b.id === animBgId(a)) || ANIM_BGS[0]).name;
function renderAnimPop() {
  const a = state.cmp.anim, cur = a && animBgId(a);
  const opt = (key, val, title, desc, on) => h("button", { type: "button", class: "opt", role: "option", "aria-selected": String(on), dataset: { [key]: val } },
    h("span", { class: "minw-0" }, h("b", {}, title), desc ? h("small", {}, desc) : null), h("span", { class: "prov" }));
  $("#anim-pop").replaceChildren(
    h("div", { class: "pop-head" }, h("span", {}, "Tipo de generación"), h("span", {}, "No gasta más cuota")),
    h("div", { role: "group", "aria-label": "Tipo" },
      opt("pickAnimType", "img", "Imagen", "Una imagen normal", !a),
      opt("pickAnimType", "anim", "Animación", "Una hoja de frames que Musa reproduce", !!a)),
    h("div", { class: "pop-sub" }, "Rejilla"),
    h("div", { class: "anim-grids", role: "group", "aria-label": "Rejilla" }, ...Object.entries(ANIM_GRIDS).map(([id, g]) => {
      const rico = h("span", { class: "rico" }); rico.style.cssText = ricoStyle(g.ratio, 18);
      return h("button", { type: "button", class: "ropt", role: "option", "aria-selected": String(!!a && a.grid === id), dataset: { pickAnimGrid: id } },
        h("span", { class: "rico-box" }, rico),
        h("span", {}, h("b", {}, `${g.label} · ${g.frames} frames · ${g.ratio}`)));
    })),
    h("div", { class: "pop-sub" }, "Fondo"),
    h("div", { role: "group", "aria-label": "Fondo" }, ...ANIM_BGS.map(b => opt("pickAnimBg", b.id, b.name, b.desc, cur === b.id))),
    h("p", { class: "pop-note" }, "Usa verde si tu personaje es rosa o morado; magenta si es verde."));
}
function setAnim(a) {
  const c = state.cmp;
  if (a && c.template) setTemplate(null);
  c.anim = a;
  updateComposer();
}
const POPS = {
  attach: ["#attach", "#attach-pop", renderAttachPop],
  anim:   ["#anim-btn", "#anim-pop", renderAnimPop],
  style:  ["#style-btn", "#style-pop", renderStylePop],
  ratio:  ["#ratio-btn", "#ratio-pop", renderRatioPop],
  tpl:    ["#tpl-btn", "#tpl-pop", renderTplPop],
  model:  ["#model-btn", "#model-pop", renderModelPop],
  prompts: ["#prompts-btn", "#prompt-pop", renderPromptPop],
  queue:  ["#queue-btn", "#queue-pop", () => {}],
};
function closePops(except) {
  for (const [k, [b, p]] of Object.entries(POPS)) {
    if (k !== except) { $(p).hidden = true; $(b).setAttribute("aria-expanded", "false"); }
  }
}
function wirePops() {
  for (const [k, [b, p, render]] of Object.entries(POPS)) {
    $(b).addEventListener("click", e => {
      e.stopPropagation();
      const pop = $(p), btn = $(b), wasOpen = !pop.hidden;
      closeImgMenu(false);
      closePops(k);
      if (!wasOpen) {
        render(); pop.hidden = false;
        const host = $(".dock-inner").getBoundingClientRect(), r = btn.getBoundingClientRect();
        pop.style.left = Math.max(0, Math.min(r.left - host.left - 6, host.width - pop.offsetWidth)) + "px";
        const sel = pop.querySelector('[aria-selected="true"]') || pop.querySelector("[data-autofocus]"); if (sel) sel.focus();
      } else pop.hidden = true;
      btn.setAttribute("aria-expanded", String(!wasOpen));
    });
    // ↑/↓ recorren las opciones y Enter activa la enfocada
    $(p).addEventListener("keydown", e => {
      const opts = [...$(p).querySelectorAll('[role="option"], [data-pnav]')];
      if (!opts.length) return;
      const cur = opts.indexOf(document.activeElement);
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        const n = e.key === "ArrowDown" ? (cur + 1) % opts.length : (cur - 1 + opts.length) % opts.length;
        opts[n].focus();
      } else if (e.key === "Enter" && cur > -1) {
        e.preventDefault(); opts[cur].click();
      }
    });
  }
  document.addEventListener("click", e => { if (!e.composedPath().some(n => n.classList && n.classList.contains("pop"))) closePops(); });
  document.addEventListener("keydown", e => {
    if (e.key !== "Escape" || imgMenu.el || sel.on || e.defaultPrevented) return;
    const open = Object.values(POPS).find(([, p]) => !$(p).hidden);
    closePops();
    if (open) { $(open[0]).focus(); return; }
    // Esc sale del modo edición (si no hay ningún diálogo abierto)
    if (state.cmp.edit && !$$("dialog[open]").length && $("#loader").hidden) { cancelEdit(); toast("Edición cancelada."); }
  });
}

/* ─── cola de trabajos ─── */
function visibleBatches() {
  const map = new Map();
  for (const j of state.jobs) {
    if (j.status === "cancelled" || state.dismissedJobs.has(j.batch + ":" + j.id)) continue;
    if (j.status === "done") continue;
    if (!map.has(j.batch)) map.set(j.batch, []);
    map.get(j.batch).push(j);
  }
  return [...map.entries()].map(([batch, jobs]) => ({ batch, jobs, n: jobs.length }));
}
const pendingBatches = visibleBatches;
function batchPct(b) {
  const run = b.jobs.find(j => j.status === "running");
  if (!run || !run.started) return 0;
  return Math.min(95, Math.round(((Date.now() / 1000) - run.started) / Math.max(1, state.eta_s) * 100));
}
function batchLabel(b) {
  const q = visibleBatches().filter(x => x.jobs.some(j => j.status === "queued"));
  if (b.jobs.some(j => j.status === "error")) return "Error";
  if (b.jobs.some(j => j.status === "running")) return `Generando · ≈${batchPct(b)}%`;
  if (b.jobs.some(j => j.status === "queued")) return `En cola · #${q.findIndex(x => x.batch === b.batch) + 1}`;
  return "Pendiente";
}
function batchRow(b) {
  const j0 = b.jobs[0];
  const err = b.jobs.find(j => j.status === "error");
  const p = batchPct(b);
  const meta = [modelLabel(j0.model), styleName(j0.style), `${b.n} img`, j0.ratio,
    (j0.refs.length + j0.uploads.length) ? `${j0.refs.length + j0.uploads.length} ref` : null].filter(Boolean).join(" · ");
  const cancelable = b.jobs.some(j => j.status === "queued");
  const cls = err ? "job err" : "job " + (b.jobs.some(j => j.status === "running") ? "running" : "queued");
  const errText = err ? errMsg(err.error) : "";
  return h("div", { class: cls, dataset: { batch: b.batch } },
    h("span", { class: "jt" }, svgUse("i-spark")),
    h("div", { class: "minw-0" },
      h("p", { class: "jp" }, short(j0.prompt, 60)),
      h("p", { class: "jm", title: errText || null }, err ? errText : meta),
      b.jobs.some(j => j.status === "running") && !err
        ? h("div", { class: "jbar" }, h("i", { dataset: { pBatch: b.batch }, style: `width:${p}%` }))
        : h("p", { class: "jm", dataset: { tBatch: b.batch } }, batchLabel(b))),
    err
      ? h("button", { class: "jx", dataset: { dismissJob: b.batch + ":" + err.id }, "aria-label": "Descartar", title: "Descartar" }, svgUse("i-close"))
      : cancelable
        ? h("button", { class: "jx", dataset: { cancelBatch: b.batch }, "aria-label": "Cancelar", title: "Cancelar" }, svgUse("i-close"))
        : null);
}
const batchShort = (b, p) => b.jobs.some(j => j.status === "error") ? "Error"
  : b.jobs.some(j => j.status === "running") ? "≈" + p + "%" : "En cola";
function pendingTile(b, natural) {
  const j0 = b.jobs[0];
  const p = batchPct(b);
  const running = b.jobs.some(j => j.status === "running");
  return h("div", { class: "tile pending " + (running ? "running" : "queued"), dataset: { batch: b.batch }, role: "group",
    "aria-label": "Generación pendiente: " + short(j0.prompt, 40) },
    h("span", { class: "mtag" }, svgUse("i-gemini"), h("span", { class: "mtag-full" }, modelLabel(j0.model)), h("span", { class: "mtag-short" }, modelLabel(j0.model).replace(/^Gemini /, ""))),
    h("div", { class: "pend-c" },
      h("b", { dataset: { pcBatch: b.batch } }, batchShort(b, p)),
      h("small", {}, `${b.n} ${b.n === 1 ? "imagen" : "imágenes"} · ${j0.ratio}`)),
    h("i", { class: "pend-bar", dataset: { pBatch: b.batch }, style: `width:${p}%` }));
}
function renderPending() {
  const batches = visibleBatches();
  const list = batches.length ? batches.map(batchRow) : [h("p", { class: "q-empty" }, "Nada pendiente. Lo que envíes desde el compositor aparecerá aquí.")];
  $("#queue-pop").replaceChildren(h("div", { class: "pop-head" }, h("span", {}, "Pendientes de generación"), h("span", {}, String(batches.length))), ...list);
  $("#side-queue").replaceChildren(...list.map(x => x.cloneNode(true)));
  const jobs = state.jobs || [];
  const g = jobs.filter(j => j.status === "running").length;
  const q = jobs.filter(j => j.status === "queued").length;
  const qtxt = (g + q) ? `${g} generando${q ? " · " + q + " en cola" : ""}` : "Nada pendiente";
  $("#queue-txt").textContent = qtxt;
  if ($("#queue-live").textContent !== qtxt) $("#queue-live").textContent = qtxt;
  $("#queue-btn").classList.toggle("live", g + q > 0);
  if (state.route && state.route.name === "inicio") renderRecentStrip();
  if (state.route && (state.route.name === "galeria" || state.route.name === "imagen")) renderGallery();
}
function tickProgress() {
  for (const b of visibleBatches()) {
    const p = batchPct(b);
    $$(`[data-p-batch="${b.batch}"]`).forEach(el => { el.style.width = p + "%"; });
    $$(`[data-pc-batch="${b.batch}"]`).forEach(el => { el.textContent = batchShort(b, p); });
    $$(`[data-t-batch="${b.batch}"]`).forEach(el => { el.textContent = batchLabel(b); });
  }
}
let seenJobs = new Map();
let notifiedBatches = new Set();
function applyJobs(payload) {
  state.jobs = payload.jobs || [];
  state.eta_s = payload.eta_s || 30;
  for (const j of state.jobs) {
    const prev = seenJobs.get(j.id);
    if (j.status === "error" && prev !== "error") {
      toast(errMsg(j.error), "err");
    }
    seenJobs.set(j.id, j.status);
  }
  // «Listo» una vez por lote, cuando todos sus trabajos terminaron
  const byBatch = new Map();
  for (const j of state.jobs) {
    if (!byBatch.has(j.batch)) byBatch.set(j.batch, []);
    byBatch.get(j.batch).push(j);
  }
  for (const [batch, jobs] of byBatch) {
    if (notifiedBatches.has(batch) || !jobs.length) continue;
    if (!jobs.every(j => j.status === "done" || j.status === "error" || j.status === "cancelled")) continue;
    notifiedBatches.add(batch);
    const done = jobs.filter(j => j.status === "done");
    if (done.length) {
      const imgs = done.reduce((n, j) => n + (j.images ? j.images.length : 0), 0);
      const n = imgs || done.length;
      toast(`Listo: ${n} ${n === 1 ? "imagen" : "imágenes"} de «${short(jobs[0].prompt, 28)}».`);
    }
    const w = jobs.find(j => j.warning);
    if (w) {
      if (w.warning.code === "gem_missing") {
        toast(`El Gem de ${styleName(w.style)} ya no existía; se generó sin él.`, "err");
      } else {
        toast(errMsg(w.warning), "err");
      }
    }
    if (state.testBatch === batch) {
      state.testBatch = null;
      const key = done.length && done[0].images && done[0].images[0];
      if (key) openTestResult(key);
    }
  }
  renderPending();
  compJobsTick();
}
function openTestResult(key) {
  const [gen, i] = key.split("#");
  api.get("/api/generations?limit=8").then(r => {
    rememberItems(r.items);
    const target = `#imagen/${gen}/${i}`;
    if (!$$("dialog[open]").length) setHash(target);
    else { state.pendingOpen = target; toast("Prueba lista. Se abrirá al cerrar el diálogo."); }
  }).catch(() => {});
}
document.addEventListener("close", () => {
  const t = state.pendingOpen;
  if (t && !$$("dialog[open]").length) { state.pendingOpen = null; setTimeout(() => setHash(t), 0); }
}, true);
async function loadJobs() {
  try { applyJobs(await api.get("/api/jobs")); } catch (e) { /* se reintenta por SSE */ }
}
async function sendJob() {
  const sendBtn = $("#send");
  if (sendBtn.classList.contains("sending")) return;
  const c = state.cmp, t = c.template && state.templates[c.template];
  const body = { style: state.style || undefined, model: c.model, ratio: c.ratio, n: c.n,
    refs: c.refs.map(r => r.kind === "upload" ? { upload: r.id } : { gen: r.ref }),
    collection: c.dest || undefined };
  let prompt = "";
  if (t) {
    const miss = t.vars.find(v => !(c.tplVars[v] || "").trim());
    if (miss) { toast("Rellena todos los campos de la plantilla.", "err"); $("#tv-" + miss).focus(); return; }
    body.template = c.template;
    body.template_vars = Object.fromEntries(t.vars.map(v => [v, c.tplVars[v].trim()]));
  } else {
    prompt = $("#prompt").value.trim();
    if (!prompt) { toast(c.edit ? "Describe qué quieres cambiar." : "Escribe qué imagen quieres generar.", "err"); $("#prompt").focus(); return; }
    body.prompt = prompt;
  }
  if (c.anim && !c.edit && !t) {
    body.animation = { grid: c.anim.grid, mode: c.anim.mode };
    if (c.anim.mode === "sprite") body.animation.key_color = c.anim.key_color || "green";
    delete body.ratio;
  }
  if (c.edit) { body.edit_of = c.edit.ref; body.n = 1; }
  sendBtn.classList.add("sending"); sendBtn.setAttribute("aria-busy", "true");
  try {
    const r = await api.send("POST", "/api/jobs", body);
    toast(c.edit ? "Edición enviada · 1 en cola." : c.model === "auto" ? `Enviado · ${r.jobs.length} en cola.` : `Enviado a ${modelLabel(c.model)} · ${r.jobs.length} en cola.`);
    if (prompt) pushHist(prompt);
    $("#prompt").value = ""; autosize($("#prompt"));
    if (t) { c.tplVars = {}; renderTemplateFields(); }
    setReady(false);
    c.refs = []; renderRefs();
    if (c.edit) cancelEdit();
    await loadJobs();
  } catch (e) { toast(e && e.code === "invalid_ref" && c.edit ? ERR_ES.unknown_conversation : errMsg(e), "err"); }
  finally { sendBtn.classList.remove("sending"); sendBtn.removeAttribute("aria-busy"); }
}
async function cancelBatch(batch) {
  const jobs = (state.jobs || []).filter(j => j.batch === batch && j.status === "queued");
  for (const j of jobs) {
    try { await api.send("DELETE", `/api/jobs/${j.id}`); } catch (e) { toast(errMsg(e), "err"); }
  }
  if (jobs.length) toast("Generación cancelada.");
  await loadJobs();
}
function renderDest() {
  if (state.cmp.dest && !state.collections.some(c => c.id === state.cmp.dest)) state.cmp.dest = "";
  const sel = $("#dest-coll");
  sel.replaceChildren(h("option", { value: "", selected: !state.cmp.dest }, "Sin colección"),
    ...state.collections.map(c => h("option", { value: c.id, selected: state.cmp.dest === c.id }, c.name)));
}

function connectComposer() {
  wirePops();
  loadHist();
  $("#var-minus").addEventListener("click", () => { state.cmp.n = Math.max(1, state.cmp.n - 1); updateComposer(); });
  $("#var-plus").addEventListener("click", () => { state.cmp.n = Math.min(8, state.cmp.n + 1); updateComposer(); });
  const pr = $("#prompt");
  autosize(pr);
  pr.addEventListener("input", e => { histIdx = -1; autosize(e.target); refreshReady(); });
  pr.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendJob(); return; }
    if ((e.key === "ArrowUp" || e.key === "ArrowDown") && !e.shiftKey && !e.altKey && !e.metaKey && !e.ctrlKey) {
      const atStart = pr.selectionStart === 0 && pr.selectionEnd === 0, atEnd = pr.selectionStart === pr.value.length;
      if (e.key === "ArrowUp" && pr.value === "" && histIdx < 0 && hist.length) { e.preventDefault(); histStep(1); }
      else if (histIdx >= 0 && pr.value === hist[histIdx] && (e.key === "ArrowUp" ? atStart : atEnd)) { e.preventDefault(); histStep(e.key === "ArrowUp" ? 1 : -1); }
    }
  });
  $("#send").addEventListener("click", sendJob);
  $("#dest-coll").addEventListener("change", e => { state.cmp.dest = e.target.value; });
  $("#ref-file").addEventListener("change", e => { uploadFiles([...e.target.files]); e.target.value = ""; closePops(); });
  $("#refs").addEventListener("click", e => {
    const x = e.target.closest("[data-unref]"); if (!x) return;
    state.cmp.refs.splice(+x.dataset.unref, 1); renderRefs(); $("#attach").focus();
  });
  $("#edit-cancel").addEventListener("click", () => { cancelEdit(); $("#prompt").focus(); });
  document.addEventListener("click", e => {
    const up = e.target.closest("[data-upload]");
    if (up) { $("#ref-file").click(); return; }
    const pr = e.target.closest("[data-pick-ref]");
    if (pr) {
      const it = state.itemsByKey.get(pr.dataset.pickRef);
      if (it) {
        const at = state.cmp.refs.findIndex(r => r.ref === "gen:" + it.key);
        if (at > -1) { state.cmp.refs.splice(at, 1); renderRefs(); }
        else addRef({ kind: "gen", ref: "gen:" + it.key, url: it.thumb_url, name: it.prompt });
      }
      renderAttachPop(); return;
    }
    const ps = e.target.closest("[data-pick-style]");
    if (ps) { selectStyle(ps.dataset.pickStyle); updateComposer(); closePops(); $("#style-btn").focus(); return; }
    const pr2 = e.target.closest("[data-pick-ratio]");
    if (pr2) { state.cmp.ratio = pr2.dataset.pickRatio; updateComposer(); closePops(); $("#ratio-btn").focus(); return; }
    const pick = (attr, el) => { renderAnimPop(); const n = $(`#anim-pop [${attr}="${el.getAttribute(attr)}"]`); if (n) n.focus(); };
    const pat = e.target.closest("[data-pick-anim-type]");
    if (pat) { setAnim(pat.dataset.pickAnimType === "anim" ? (state.cmp.anim || animDefault()) : null); pick("data-pick-anim-type", pat); return; }
    const pag = e.target.closest("[data-pick-anim-grid]");
    if (pag) { setAnim({ ...(state.cmp.anim || animDefault()), grid: pag.dataset.pickAnimGrid }); pick("data-pick-anim-grid", pag); return; }
    const pab = e.target.closest("[data-pick-anim-bg]");
    if (pab) {
      const b = ANIM_BGS.find(x => x.id === pab.dataset.pickAnimBg);
      if (b) { setAnim({ ...(state.cmp.anim || animDefault()), mode: b.mode, key_color: b.key_color }); pick("data-pick-anim-bg", pab); }
      return;
    }
    const mo = e.target.closest("[data-model]");
    if (mo) { state.cmp.model = mo.dataset.model; updateComposer(); closePops(); $("#model-btn").focus(); return; }
    const tp = e.target.closest("[data-tpl]");
    if (tp) { closePops(); setTemplate(tp.dataset.tpl || null); if (!tp.dataset.tpl) $("#tpl-btn").focus(); return; }
    const cb = e.target.closest("[data-cancel-batch]");
    if (cb) { e.stopPropagation(); cancelBatch(cb.dataset.cancelBatch); return; }
    const dj = e.target.closest("[data-dismiss-job]");
    if (dj) { state.dismissedJobs.add(dj.dataset.dismissJob); renderPending(); return; }
  });
  const cmp = $(".cmp");
  const hasFiles = e => [...(e.dataTransfer?.types || [])].includes("Files");
  cmp.addEventListener("dragover", e => { if (!hasFiles(e)) return; e.preventDefault(); cmp.classList.add("drop"); });
  cmp.addEventListener("dragleave", e => { if (!cmp.contains(e.relatedTarget)) cmp.classList.remove("drop"); });
  cmp.addEventListener("drop", e => { if (!hasFiles(e)) return; e.preventDefault(); cmp.classList.remove("drop"); uploadFiles([...e.dataTransfer.files]); });
  cmp.addEventListener("paste", e => { const f = [...(e.clipboardData?.files || [])].filter(x => x.type.startsWith("image/")); if (f.length) { e.preventDefault(); uploadFiles(f); } });
  setInterval(tickProgress, 250);
}

/* historial de prompts: flecha arriba en el textarea vacío */
const HIST_KEY = "musa-prompt-history", HIST_MAX = 20;
let hist = [], histIdx = -1;
function loadHist() {
  try { const v = JSON.parse(localStorage.getItem(HIST_KEY)); hist = Array.isArray(v) ? v.filter(x => typeof x === "string").slice(0, HIST_MAX) : []; }
  catch (e) { hist = []; }
}
function pushHist(p) {
  hist = [p, ...hist.filter(x => x !== p)].slice(0, HIST_MAX);
  try { localStorage.setItem(HIST_KEY, JSON.stringify(hist)); } catch (e) {}
  histIdx = -1;
}
function histStep(dir) {
  const pr = $("#prompt");
  const next = histIdx + dir;
  if (next >= hist.length) return;
  histIdx = next;
  pr.value = next < 0 ? "" : hist[next];
  autosize(pr); refreshReady();
  const at = dir > 0 ? 0 : pr.value.length;   // ↑ deja el cursor al inicio y ↓ al final, para seguir recorriendo
  pr.setSelectionRange(at, at);
}

/* plantillas */
async function loadTemplates() {
  try { state.templates = await api.get("/api/templates"); } catch (e) { state.templates = {}; }
  updateComposer();
}
const tplLabel = n => { const t = String(n).replace(/_/g, " "); return t.charAt(0).toUpperCase() + t.slice(1); };
function setTemplate(name) {
  const c = state.cmp;
  c.template = name && state.templates[name] ? name : null;
  c.tplVars = {};
  if (c.template && c.edit) cancelEdit();
  if (c.template && c.anim) c.anim = null;
  renderTemplateFields();
  updateComposer();
  if (c.template) { const f = $("#tpl-fields input"); if (f) f.focus(); } else refreshReady();
}
function renderTemplateFields() {
  const c = state.cmp, box = $("#tpl-fields"), t = c.template && state.templates[c.template];
  $("#prompt").hidden = !!t;
  box.hidden = !t;
  box.replaceChildren(...(t ? t.vars.map(v => h("div", {},
    h("label", { for: "tv-" + v }, tplLabel(v)),
    h("input", { class: "input", id: "tv-" + v, autocomplete: "off", value: c.tplVars[v] || "",
      oninput: e => { c.tplVars[v] = e.target.value; refreshReady(); },
      onkeydown: e => { if (e.key === "Enter") { e.preventDefault(); sendJob(); } } }))) : []));
  $("#prompt-label").textContent = t ? "Plantilla: " + tplLabel(c.template) : "Describe la imagen";
}
function refreshReady() {
  const c = state.cmp, t = c.template && state.templates[c.template];
  const ready = t ? t.vars.every(v => (c.tplVars[v] || "").trim()) : !!$("#prompt").value.trim();
  setReady(ready);
}
function renderTplPop() {
  const names = Object.keys(state.templates);
  $("#tpl-pop").replaceChildren(
    h("div", { class: "pop-head" }, h("span", {}, "Plantilla"), h("span", {}, String(names.length))),
    h("button", { class: "opt", role: "option", "aria-selected": String(!state.cmp.template), dataset: { tpl: "" } },
      h("span", { class: "minw-0" }, h("b", {}, "Sin plantilla"), h("small", {}, "Escribes el prompt completo"))),
    ...names.map(n => h("button", { class: "opt", role: "option", "aria-selected": String(state.cmp.template === n), dataset: { tpl: n } },
      h("span", { class: "minw-0" }, h("b", {}, tplLabel(n)), state.templates[n].description ? h("small", {}, state.templates[n].description) : null))));
}

/* editar una imagen existente (misma conversación, gasta 1 imagen) */
function startEdit(item) {
  setSelectMode(false);
  const c = state.cmp;
  if (c.template) setTemplate(null);
  if (!c.edit) c.prevAnim = c.anim;
  c.anim = null;
  if (!c.edit) c.prevN = c.n;
  c.edit = { ref: item.ref, key: item.key, prompt: item.prompt, thumb: item.thumb_url };
  c.n = 1;
  updateComposer();
  $("#detail-dialog").close();
  $("#prompt").focus();
}
function cancelEdit() {
  if (!state.cmp.edit) return;
  const c = state.cmp;
  c.edit = null;
  if (c.prevN) { c.n = c.prevN; c.prevN = null; }
  c.anim = c.prevAnim || null; c.prevAnim = null;
  updateComposer();
}

/* ─── animaciones (sprite sheets) ─── */
const reducedMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
const noHover = () => matchMedia("(hover: none)").matches;
const clampFps = v => Math.min(30, Math.max(2, Math.round(Number(v)) || 10));

// Reproductor: un div con la hoja normalizada (una fila de N frames) de fondo; avanza con requestAnimationFrame.
function createSpritePlayer(host, anim, { autoplay = true, onframe = null } = {}) {
  const N = Math.max(1, Number(anim.frames) || 1);
  const node = h("div", { class: "sprite" });
  node.style.setProperty("--ar", String(anim.frame_w / anim.frame_h || 1));
  node.style.backgroundImage = `url("${String(anim.sheet_url).replace(/"/g, "%22")}")`;
  node.style.backgroundSize = `${N * 100}% 100%`;
  host.append(node);
  new Image().src = anim.sheet_url;   // calienta la caché antes del primer frame
  let fps = clampFps(anim.fps), loop = anim.loop === "pingpong" ? "pingpong" : "loop";
  let idx = 0, seq = 0, acc = 0, last = null, raf = 0, playing = false;
  const period = () => loop === "pingpong" && N > 1 ? 2 * N - 2 : N;
  const frameAt = s => s < N ? s : 2 * N - 2 - s;
  const paint = () => { node.style.backgroundPosition = `${N > 1 ? idx / (N - 1) * 100 : 0}% 0`; if (onframe) onframe(idx, N); };
  const tick = ts => {
    raf = 0;
    if (!playing) return;
    if (!node.isConnected) { p.destroy(); return; }   // la tarjeta o el panel se repintó: no dejar el bucle huérfano
    if (last == null) last = ts;
    acc += Math.min(ts - last, 250) * fps / 1000; last = ts;
    const adv = Math.floor(acc);
    if (adv > 0) {
      acc -= adv; seq = (seq + adv) % period();
      const n = frameAt(seq);
      if (n !== idx) { idx = n; paint(); }
    }
    raf = requestAnimationFrame(tick);
  };
  const p = {
    get playing() { return playing; },
    get frame() { return idx; },
    frames: N,
    play() { if (playing) return; playing = true; last = null; raf = requestAnimationFrame(tick); },
    pause() { playing = false; if (raf) cancelAnimationFrame(raf); raf = 0; },
    step(d) { idx = ((idx + d) % N + N) % N; seq = idx; acc = 0; paint(); },
    seek(i) { idx = ((i % N) + N) % N; seq = idx; acc = 0; paint(); },
    setFps(v) { fps = clampFps(v); },
    setLoop(v) { loop = v === "pingpong" ? "pingpong" : "loop"; seq = idx; acc = 0; },
    destroy() { p.pause(); node.remove(); },
  };
  paint();
  if (autoplay) p.play();
  return p;
}

// Tarjeta de galería: el fotograma 0 normalizado (en pausa) cubre la miniatura cruda de Gemini mientras la tarjeta está en pantalla;
// al pasar el ratón o enfocar se reproduce, y en táctil con el botón ▶. Al salir vuelve al fotograma 0.
function mountTileFrame(t) {
  if (t._ap) return t._ap;
  const item = t._anItem;
  if (!item || !item.animation || !item.animation.sheet_url) return null;
  const host = h("div", { class: "aplayer", "aria-hidden": "true" });
  $(".tile-open", t).after(host);
  t._ap = { host, manual: false, on: false, player: createSpritePlayer(host, item.animation, { autoplay: false }) };
  return t._ap;
}
function unmountTileFrame(t) {
  const ap = t._ap;
  if (!ap) return;
  t._ap = null; ap.player.destroy(); ap.host.remove();
  syncAplay(t);
}
function startTilePlayer(t, manual = false) {
  const ap = mountTileFrame(t);
  if (!ap) return;
  if (manual) ap.manual = true;
  if (!ap.on) { ap.on = true; ap.player.play(); }
  syncAplay(t);
}
function stopTilePlayer(t) {
  const ap = t._ap;
  if (!ap) return;
  ap.on = false; ap.manual = false;
  ap.player.pause(); ap.player.seek(0);
  syncAplay(t);
}
function syncAplay(t) {
  const b = $(".aplay", t);
  if (!b) return;
  const on = !!(t._ap && t._ap.on);
  b.setAttribute("aria-pressed", String(on));
  b.title = on ? "Pausar animación" : "Reproducir animación";
  b.querySelector("use").setAttribute("href", on ? "#i-pause" : "#i-play");
}
// Un solo observador: monta el fotograma 0 al acercarse la tarjeta y lo destruye al salir de pantalla o al retirarse del DOM.
let tileIO = null, tileWired = 0;
const tileSeen = new Set();
function dropTile(t) { unmountTileFrame(t); tileSeen.delete(t); if (tileIO) tileIO.unobserve(t); }
function tileObserver() {
  if (!tileIO) tileIO = new IntersectionObserver(entries => {
    for (const e of entries) {
      if (!e.target.isConnected) dropTile(e.target);
      else if (e.isIntersecting) mountTileFrame(e.target);
      else unmountTileFrame(e.target);
    }
  }, { rootMargin: "200px" });
  // las tarjetas retiradas mientras estaban fuera de pantalla no generan entrada: se purgan de vez en cuando
  if (++tileWired % 40 === 0) for (const t of [...tileSeen]) if (!t.isConnected) dropTile(t);
  return tileIO;
}
function wireTileAnim(t, item) {
  t.classList.add("has-anim");
  t._anItem = item;
  const hoverOk = () => !sel.on && !reducedMotion() && !noHover();
  t.addEventListener("pointerenter", e => { if (e.pointerType !== "touch" && hoverOk()) startTilePlayer(t); });
  t.addEventListener("pointerleave", () => { if (t._ap && t._ap.on && !t._ap.manual) stopTilePlayer(t); });
  t.addEventListener("focusin", () => { if (hoverOk()) startTilePlayer(t); });
  t.addEventListener("focusout", e => { if (t._ap && t._ap.on && !t._ap.manual && !t.contains(e.relatedTarget)) stopTilePlayer(t); });
  const b = h("button", { type: "button", class: "aplay", "aria-pressed": "false", "aria-label": "Reproducir animación: " + imgName(item), title: "Reproducir animación" }, svgUse("i-play"));
  b.addEventListener("click", e => { e.stopPropagation(); if (t._ap && t._ap.on) stopTilePlayer(t); else startTilePlayer(t, true); });
  t.append(b);
  if (typeof IntersectionObserver === "function") { tileObserver().observe(t); tileSeen.add(t); } else mountTileFrame(t);
}

// Panel de detalle: reproductor con controles y vista de la hoja original con las celdas encima.
let dAnim = null;
let animBg = "";   // «light» | «dark» | «checker»; vacío = según el modo de la primera animación abierta
function flushAnimSave() {
  const d = dAnim;
  if (!d || !d.saveT) return;
  clearTimeout(d.saveT); d.saveT = 0;
  const sent = { fps: d.fps, loop: d.loop };
  const an = d.item.animation;
  const saved = d.saved || { fps: clampFps(an && an.fps), loop: an && an.loop === "pingpong" ? "pingpong" : "loop" };
  api.send("PUT", `/api/animations/${d.gen}/${d.i}`, sent).then(() => {
    d.saved = sent;
    if (d.item.animation) Object.assign(d.item.animation, sent);   // el elemento compartido solo cambia cuando el servidor lo aceptó
  }).catch(e => {
    toast(errMsg(e), "err");
    d.fps = saved.fps; d.loop = saved.loop; d.saved = saved;
    if (dAnim === d && d.revertUI) d.revertUI();
  });
}
function killDetailAnim() {
  if (!dAnim) return;
  flushAnimSave();
  dAnim.player.destroy();
  dAnim = null;
}
function buildDetailArt(item) {
  const art = $("#dlg-art"), an = item.animation;
  if (dAnim && an && dAnim.key === item.key && dAnim.rev === an.rev && dAnim.status === an.status) { dAnim.item = item; return; }
  killDetailAnim();
  art.classList.toggle("has-anim", !!an);
  if (!an) { art.replaceChildren(h("img", { src: item.image_url, alt: item.prompt })); return; }
  if (!animBg) animBg = an.mode === "scene" ? "light" : "checker";
  const d = dAnim = { key: item.key, gen: item.gen_id, i: item.index, rev: an.rev, status: an.status, item, fps: clampFps(an.fps),
    loop: an.loop === "pingpong" ? "pingpong" : "loop", view: "anim", ficha: null, saveT: 0, player: null, wasPlaying: false };
  const BG = { light: "abg-light", dark: "abg-dark", checker: "checker" };
  const group = (label, ...kids) => h("div", { class: "pills", role: "group", "aria-label": label }, ...kids);
  const pill = (label, on, onclick, extra = {}) => h("button", { type: "button", class: "pill", "aria-pressed": String(on), onclick, ...extra }, label);

  const paneAnim = h("div", { class: "apane " + BG[animBg] });
  const paneSheet = h("div", { class: "spane", hidden: true });
  const frameLbl = h("span", { class: "aframe tnum" }, `Frame 1/${an.frames}`);
  const playBtn = h("button", { type: "button", class: "btn btn-sm icon-btn", "aria-label": "Reproducir", title: "Reproducir" }, svgUse("i-play"));
  const syncPlay = () => {
    const on = d.player.playing;
    playBtn.setAttribute("aria-label", on ? "Pausar" : "Reproducir"); playBtn.title = on ? "Pausar" : "Reproducir";
    playBtn.querySelector("use").setAttribute("href", on ? "#i-pause" : "#i-play");
  };
  d.player = createSpritePlayer(paneAnim, { ...an, fps: d.fps, loop: d.loop }, {
    autoplay: !reducedMotion(), onframe: (i, n) => { frameLbl.textContent = `Frame ${i + 1}/${n}`; } });
  const step = dir => { d.player.pause(); d.player.step(dir); syncPlay(); };
  playBtn.addEventListener("click", () => { if (d.player.playing) d.player.pause(); else d.player.play(); syncPlay(); });
  const prevBtn = h("button", { type: "button", class: "btn btn-sm icon-btn", "aria-label": "Frame anterior", title: "Frame anterior", onclick: () => step(-1) }, svgUse("i-prev"));
  const nextBtn = h("button", { type: "button", class: "btn btn-sm icon-btn", "aria-label": "Frame siguiente", title: "Frame siguiente", onclick: () => step(1) }, svgUse("i-next"));
  syncPlay();

  const fpsIn = h("input", { type: "range", id: "dlg-afps", class: "arange", "aria-label": "FPS", min: "2", max: "30", step: "1", value: String(d.fps) });
  const fpsOut = h("output", { class: "tnum", for: "dlg-afps" }, `${d.fps} fps`);
  const loopBtns = [];
  const schedule = () => { clearTimeout(d.saveT); d.saveT = setTimeout(flushAnimSave, 400); };
  // si el PUT falla, la interfaz vuelve a lo último guardado
  d.revertUI = () => {
    fpsIn.value = String(d.fps); fpsOut.textContent = `${d.fps} fps`; d.player.setFps(d.fps); d.player.setLoop(d.loop);
    loopBtns.forEach(b => b.setAttribute("aria-pressed", String(b.dataset.v === d.loop)));
  };
  fpsIn.addEventListener("input", () => { d.fps = clampFps(fpsIn.value); d.player.setFps(d.fps); fpsOut.textContent = `${d.fps} fps`; schedule(); });
  loopBtns.push(...[["loop", "Bucle"], ["pingpong", "Ida y vuelta"]].map(([v, l]) => pill(l, d.loop === v, () => {
    d.loop = v; d.player.setLoop(v); loopBtns.forEach(b => b.setAttribute("aria-pressed", String(b.dataset.v === v))); schedule();
  }, { dataset: { v } })));
  const bgBtns = [["light", "Claro"], ["dark", "Oscuro"], ["checker", "Cuadros"]].map(([v, l]) => pill(l, animBg === v, () => {
    animBg = v; paneAnim.className = "apane " + BG[v]; bgBtns.forEach(b => b.setAttribute("aria-pressed", String(b.dataset.v === v)));
  }, { dataset: { v } }));
  const row = (label, ...kids) => h("div", { class: "actl-row" }, h("span", { class: "label" }, label), h("div", { class: "actl-c" }, ...kids));
  const controls = h("div", { class: "actl" },
    h("div", { class: "actl-play" }, playBtn, prevBtn, frameLbl, nextBtn),
    row("FPS", fpsIn, fpsOut),
    row("Repetición", group("Repetición", ...loopBtns)),
    row("Fondo", group("Fondo", ...bgBtns)));

  const setView = async v => {
    d.view = v;
    tabs.querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.v === v)));
    paneAnim.hidden = v !== "anim"; paneSheet.hidden = v !== "sheet"; controls.hidden = v !== "anim";
    if (v === "sheet") {
      d.wasPlaying = d.player.playing; d.player.pause(); syncPlay();
      if (!paneSheet.firstChild) await buildSheetPane();
    } else if (d.wasPlaying) { d.player.play(); syncPlay(); }
  };
  async function buildSheetPane() {
    try { d.ficha = d.ficha || await api.get(`/api/animations/${d.gen}/${d.i}`); }
    catch (e) { toast(errMsg(e), "err"); if (dAnim === d) setView("anim"); return; }
    if (dAnim !== d) return;
    const f = d.ficha;
    const img = h("img", { src: item.image_url, alt: "Hoja original con las celdas detectadas" });
    const cells = h("div", { class: "sheetcells", "aria-hidden": "true" });
    const draw = (w, hh) => cells.replaceChildren(...(f.cells || []).map((c, k) => {
      const box = h("span", { class: "cell" }, h("i", {}, String(k + 1)));
      box.style.cssText = `left:${c[0] / w * 100}%;top:${c[1] / hh * 100}%;width:${(c[2] - c[0]) / w * 100}%;height:${(c[3] - c[1]) / hh * 100}%`;
      return box;
    }));
    if (item.width && item.height) draw(item.width, item.height);
    else img.addEventListener("load", () => draw(img.naturalWidth, img.naturalHeight));
    paneSheet.replaceChildren(h("div", { class: "sheetwrap" }, img, cells),
      h("p", { class: "muted t-xs" }, `Rejilla ${f.cols}×${f.rows} · ${f.frames} frames`));
  }
  const tabs = group("Vista", pill("Animación", true, () => setView("anim"), { dataset: { v: "anim" } }),
    pill("Hoja original", false, () => setView("sheet"), { dataset: { v: "sheet" } }));
  tabs.classList.add("atabs");
  const notice = an.status === "review"
    ? h("div", { class: "anotice", role: "status" }, h("p", {}, "No encontré la rejilla esperada; revisa las celdas."),
        h("button", { type: "button", class: "btn btn-sm", onclick: () => openAnimDialog(d.item) }, "Ajustar"))
    : null;
  art.replaceChildren(...[tabs, notice, h("div", { class: "aview" }, paneAnim, paneSheet), controls].filter(Boolean));
}

function applyAnimToItem(item, f) {
  const an = f ? { frames: f.frames, fps: f.fps, loop: f.loop, mode: f.mode, status: f.status, rev: f.rev,
    frame_w: f.frame_w, frame_h: f.frame_h, sheet_url: f.sheet_url } : null;
  for (const it of new Set([item, state.itemsByKey.get(item.key), detailItem && detailItem.key === item.key ? detailItem : null])) if (it) it.animation = an;
  if (detailItem && detailItem.key === item.key) buildDetail(detailItem);
}

// Diálogo «Convertir / Ajustar animación»
let adState = null;
async function openAnimDialog(item) {
  const adjusting = !!item.animation, opener = document.activeElement;
  let f = null;
  if (adjusting) {
    try { f = await api.get(`/api/animations/${item.gen_id}/${item.index}`); }
    catch (e) { toast(errMsg(e), "err"); return; }
  }
  adState = { item, adjusting, opener };
  closePops();
  const wide = !!(item.width && item.height) && item.width / item.height >= 1.3;
  const v = f || { cols: wide ? 4 : 3, rows: wide ? 2 : 3, mode: "sprite", key_color: "green", tolerance: 25, fps: 10, loop: "loop" };
  $("#ad-title").textContent = adjusting ? "Ajustar animación" : "Convertir en animación";
  $("#ad-cols").value = v.cols; $("#ad-rows").value = v.rows;
  $("#ad-mode").value = v.mode === "scene" ? "scene" : "sprite";
  $("#ad-key").value = v.key_color === "magenta" ? "magenta" : "green";
  $("#ad-tol").value = v.tolerance; $("#ad-fps").value = v.fps;
  $("#ad-loop").value = v.loop === "pingpong" ? "pingpong" : "loop";
  $("#ad-redetect").checked = false;
  $("#ad-redetect-row").hidden = !adjusting;
  $("#ad-remove").hidden = !adjusting;
  syncAnimDialog();
  $("#anim-dialog").showModal();
  $("#ad-cols").focus();
}
function readAnimForm() {
  const n = id => Number($("#" + id).value);
  const mode = $("#ad-mode").value;
  const f = { cols: n("ad-cols"), rows: n("ad-rows"), mode, tolerance: n("ad-tol"), fps: n("ad-fps"), loop: $("#ad-loop").value };
  if (mode === "sprite") f.key_color = $("#ad-key").value;
  const inR = (v, a, b) => Number.isInteger(v) && v >= a && v <= b;
  const frames = f.cols * f.rows;
  f.valid = inR(f.cols, 1, 8) && inR(f.rows, 1, 8) && frames >= 2 && frames <= 36 && inR(f.tolerance, 5, 80) && inR(f.fps, 2, 30);
  f.frames = frames;
  return f;
}
function syncAnimDialog() {
  const f = readAnimForm();
  $("#ad-key").disabled = f.mode === "scene";
  const gridOk = Number.isInteger(f.cols) && Number.isInteger(f.rows) && f.cols >= 1 && f.cols <= 8 && f.rows >= 1 && f.rows <= 8 && f.frames >= 2 && f.frames <= 36;
  $("#ad-count").textContent = gridOk ? `${plural(f.frames, "frame", "frames")} en total.` : "Usa de 1 a 8 columnas y filas, con entre 2 y 36 frames en total.";
  $("#ad-count").classList.toggle("err", !gridOk);
  $("#ad-ok").disabled = !f.valid;
}
async function submitAnimDialog(e) {
  e.preventDefault();
  if (!adState) return;
  const f = readAnimForm();
  if (!f.valid) return;
  const { item, adjusting } = adState, btn = $("#ad-ok");
  const { valid, frames, ...body } = f;
  if (adjusting) body.redetect = $("#ad-redetect").checked;
  btn.disabled = true;
  try {
    const r = await api.send("PUT", `/api/animations/${item.gen_id}/${item.index}`, body);
    applyAnimToItem(item, r);
    $("#anim-dialog").close();
    toast(adjusting ? "Animación actualizada." : "Animación creada.");
  } catch (err) {
    toast(errMsg(err), "err");
    if (err && err.code === "unknown_image") $("#anim-dialog").close();
  } finally { btn.disabled = false; if ($("#anim-dialog").open) syncAnimDialog(); }
}
async function removeAnimation() {
  if (!adState) return;
  const item = adState.item;
  if (!await confirmDlg({ title: "¿Quitar la animación?", text: "La imagen se queda como está; solo se elimina la animación y sus ajustes.", ok: "Quitar animación" })) return;
  try {
    await api.send("DELETE", `/api/animations/${item.gen_id}/${item.index}`);
    applyAnimToItem(item, null);
    $("#anim-dialog").close();
    toast("Animación quitada.");
  } catch (err) {
    toast(errMsg(err), "err");
    if (err && err.code === "unknown_animation") { applyAnimToItem(item, null); $("#anim-dialog").close(); }
  }
}

/* ─── detalle de imagen ─── */
async function findItem(gen, i) {
  const key = `${gen}#${i}`;
  const cached = state.itemsByKey.get(key);
  if (cached) return cached;
  let offset = state.gallery.items.length;
  while (offset < state.gallery.total) {
    const r = await api.get(`/api/generations?limit=60&offset=${offset}`);
    if (!r.items.length) break;
    state.gallery.items.push(...r.items);
    rememberItems(r.items);
    const item = r.items.find(x => x.key === key);
    if (item) return item;
    offset += r.items.length;
  }
  return null;
}

async function openDetail(gen, i) {
  if (detailItem && detailItem.key === `${gen}#${i}` && $("#detail-dialog").open) return;
  const item = await findItem(gen, i);
  if (!item) {
    toast("La imagen ya no está disponible.", "err");
    setHash(state.baseHash || "#galeria");
    return;
  }
  detailItem = item;
  state.detailList = [state.gallery.items, state.recentItems].find(l => l.includes(item)) || [item];
  buildDetail(item);
  const dlg = $("#detail-dialog");
  if (!dlg.open) dlg.showModal();
  const target = `#imagen/${gen}/${i}`;
  if (location.hash !== target) setHash(target);
}

function closeDetail() { const d = $("#detail-dialog"); if (d.open) d.close(); }

function buildDetail(item) {
  buildDetailArt(item);
  $("#dlg-title").textContent = short(item.prompt, 60);
  const dims = item.width && item.height ? `${item.width}×${item.height}` : null;
  $("#dlg-meta").textContent = [modelName(item.model), item.style ? (item.style_name || "Estilo eliminado") : null, fmtDate(item.ts), dims].filter(Boolean).join(" · ");
  $("#dlg-prompt").textContent = item.prompt;
  $("#dlg-credit").textContent = "ID: " + item.ref;
  syncFavBtn(item);
  renderDetailColls(item);
  syncDlgRef();
  $("#dlg-edit").hidden = !item.conversation_id;
  $("#dlg-anim-txt").textContent = item.animation ? "Ajustar animación" : "Convertir en animación";
  $("#dlg-anim-export").hidden = !item.animation;
}

function syncFavBtn(item) {
  $("#dlg-fav-txt").textContent = favLabel(item.favorite);
}
function isGenRef(key) { return state.cmp.refs.some(r => r.ref === "gen:" + key); }
function syncDlgRef() {
  if (!detailItem) return;
  const on = isGenRef(detailItem.key), full = !on && state.cmp.refs.length >= MAX_REFS;
  $("#dlg-ref-txt").textContent = on ? "Quitar de referencias" : full ? `Máximo ${MAX_REFS} referencias` : "Usar como referencia";
  $("#dlg-ref").querySelector("use").setAttribute("href", on ? "#i-close" : "#i-plus");
  $("#dlg-ref").disabled = full;
}
$("#dlg-reuse").addEventListener("click", () => {
  if (!detailItem) return;
  const pr = $("#prompt"), had = pr.value.trim() && pr.value.trim() !== detailItem.prompt;
  pr.value = detailItem.prompt; autosize(pr); setReady(true);
  let note = "Prompt listo en el compositor.";
  if ($("#dlg-reuse-all").checked) {
    if (detailItem.style && state.styles.some(s => s.id === detailItem.style)) { state.style = detailItem.style; renderStyles(); renderToolRow(); }
    if (detailItem.model && state.models.some(m => m.id === detailItem.model)) state.cmp.model = detailItem.model;
    if (detailItem.ratio) state.cmp.ratio = detailItem.ratio;
    updateComposer();
    note = `Prompt y ajustes listos · ${styleName(state.style)} · ${state.cmp.ratio} · ${modelLabel(state.cmp.model)}.`;
  }
  $("#detail-dialog").close();
  pr.focus(); pr.setSelectionRange(pr.value.length, pr.value.length);
  toast(had ? note.replace(/\.$/, "") + " (reemplazó el texto anterior)." : note);
});
$("#dlg-ref").addEventListener("click", () => {
  if (!detailItem) return;
  if (isGenRef(detailItem.key)) {
    state.cmp.refs = state.cmp.refs.filter(r => r.ref !== "gen:" + detailItem.key);
    renderRefs(); syncDlgRef(); toast("Quitada de las referencias.");
    return;
  }
  if (addRef({ kind: "gen", ref: "gen:" + detailItem.key, url: detailItem.thumb_url, name: detailItem.prompt })) {
    $("#detail-dialog").close();
    const pr = $("#prompt"); pr.focus(); pr.setSelectionRange(pr.value.length, pr.value.length);
    toast(`Referencia añadida · ${state.cmp.refs.length} de ${MAX_REFS}.`);
  }
});
$("#dlg-done").addEventListener("click", () => $("#detail-dialog").close());

function renderDetailColls(item) {
  const list = $("#dlg-colls");
  if (!state.collections.length) {
    list.replaceChildren(h("p", { class: "none" }, "Aún no hay colecciones. Crea una abajo."));
    return;
  }
  list.replaceChildren(...state.collections.map(c =>
    h("button", { "aria-pressed": String(item.collections.includes(c.id)), dataset: { toggleColl: c.id } },
      h("span", { class: "cb", "aria-hidden": "true" }), c.name)));
}

async function toggleFavorite(item) {
  try {
    const r = item.favorite
      ? await api.send("DELETE", `/api/favorites/${item.gen_id}/${item.index}`)
      : await api.send("PUT", `/api/favorites/${item.gen_id}/${item.index}`);
    item.favorite = r.favorite;
    syncFavBtn(item);
    $$(`.tile [data-fav="${CSS.escape(item.key)}"]`).forEach(t => {
      t.classList.toggle("on", r.favorite);
      t.setAttribute("aria-label", favLabel(r.favorite)); t.setAttribute("title", favLabel(r.favorite));
    });
    if (state.gallery.filter === "favorites" && !r.favorite) await reloadGallery();
  } catch (e) { toast(errMsg(e), "err"); }
}

async function toggleCollection(item, collId) {
  const has = item.collections.includes(collId);
  try {
    const r = has
      ? await api.send("DELETE", `/api/collections/${collId}/items/${item.gen_id}/${item.index}`)
      : await api.send("POST", `/api/collections/${collId}/items`, { key: item.key });
    item.collections = r.collections;
    if (detailItem && detailItem.key === item.key) renderDetailColls(item);
    const c = state.collections.find(c => c.id === collId);
    toast(has ? `Quitada de ${c ? c.name : "la colección"}.` : `Guardada en ${c ? c.name : "la colección"}.`);
  } catch (e) { toast(errMsg(e), "err"); }
}

async function copyItemId(item) {
  try { await navigator.clipboard.writeText(item.ref); toast("ID copiado."); }
  catch (e) { toast("No se pudo copiar el ID.", "err"); }
}

async function deleteItem(item) {
  if (!await confirmDlg({ title: "¿Borrar esta imagen?", text: "Se elimina del disco y no se puede deshacer.", ok: "Borrar imagen" })) return;
  try {
    await api.send("DELETE", `/api/images/${item.gen_id}/${item.index}`);
    toast("Imagen borrada.");
    if (detailItem && detailItem.key === item.key) $("#detail-dialog").close();
    state.itemsByKey.delete(item.key);
    state.recentItems = state.recentItems.filter(x => x.key !== item.key);
    await reloadGallery();
    const route = state.route || parseRoute();
    if (route.name === "inicio") await renderInicio();
    else if (route.name === "colecciones" && route.id) await renderCollection(route.id);
  } catch (e) { toast(errMsg(e), "err"); }
}

// Crea una colección y guarda en ella la imagen (compartido por el detalle y el menú ⋮); los errores los gestiona quien llama.
async function addToNewCollection(item, name) {
  const c = await api.send("POST", "/api/collections", { name });
  await api.send("POST", `/api/collections/${c.id}/items`, { key: item.key });
  item.collections = [...item.collections, c.id];
  state.collections = await api.get("/api/collections");
  if (detailItem && detailItem.key === item.key) renderDetailColls(item);
  toast("Guardada en " + c.name + ".");
  return c;
}

function stepDetail(dir) {
  if (!detailItem) return;
  const items = state.detailList || state.gallery.items;
  const idx = items.findIndex(x => x.key === detailItem.key);
  const next = items[idx + dir];
  if (next) openDetail(next.gen_id, next.index);
}

/* ─── estilos ─── */
function renderStyles() {
  $("#style-filter").replaceChildren(...FAM_CHIPS.map(([v, l]) =>
    h("button", { class: "pill", "aria-pressed": String(styleFamily === v), dataset: { fam: v } }, l)));
  const list = state.styles.filter(s => styleFamily === "all" || (styleFamily === "user" ? s.source === "user" : s.family === styleFamily));
  const grid = $("#style-grid");
  const focusKey = el => {
    const k = el && el.dataset && Object.keys(el.dataset)[0];
    if (k) return `[data-${k.replace(/[A-Z]/g, m => "-" + m.toLowerCase())}="${CSS.escape(el.dataset[k])}"]`;
    return el && el.id ? "#" + CSS.escape(el.id) : null;
  };
  const cards = list.map(s => {
    const own = s.source === "user";
    const wrap = h("div", { class: "style-wrap" });
    const card = h("button", { type: "button", class: "style-card", dataset: { styleId: s.id }, "aria-pressed": String(state.style === s.id),
      "aria-label": "Seleccionar estilo " + s.name });
    card.append(styleThumbEl(s));
    const text = s.append || s.description || "";
    card.append(h("span", { class: "style-body" },
      h("span", { class: "row" }, h("h3", {}, s.name, s.gem ? h("span", { class: "gem-badge" }, svgUse("i-zap"), "Gem") : null),
        FAM_LABEL[s.family] || s.family ? h("span", { class: "fam" }, FAM_LABEL[s.family] || "Otros") : null),
      text ? h("p", { class: "style-text", title: text }, text) : null,
      h("p", {}, own ? "Tu estilo" : (s.credit || "Estilo incluido"))));
    wrap.append(card);
    // controles hermanos de la tarjeta (nunca anidados), superpuestos a la imagen
    const over = h("div", { class: "style-over" });
    const busy = gemBusy.has(s.id);
    over.append(h("button", { type: "button", class: "gem-sw" + (busy ? " busy" : ""), role: "switch",
      "aria-checked": String(!!s.gem), disabled: busy || null, dataset: { gemSwitch: s.id },
      "aria-label": "Usar Gem en " + s.name, title: "Usar Gem" },
      h("span", { class: "sw-track", "aria-hidden": "true" }, h("i", { class: "sw-knob" })), "Gem"));
    if (s.gem) {
      over.append(h("button", { type: "button", class: "gem-link", dataset: { gemLink: s.id },
        "aria-label": "Instrucciones del Gem de " + s.name, title: "Instrucciones del Gem" }, "Instrucciones"));
    }
    // lápiz (solo estilos propios) y ⋮: visibles al pasar el ratón o con foco, y siempre en táctil
    over.append(h("div", { class: "style-tools" },
      own ? h("button", { type: "button", class: "fab", dataset: { editStyle: s.id }, "aria-label": "Editar estilo " + s.name, title: "Editar estilo" }, svgUse("i-edit")) : null,
      h("button", { type: "button", class: "fab kebab", "aria-haspopup": "menu", "aria-expanded": "false",
        "aria-label": "Más acciones del estilo " + s.name, title: "Más acciones", dataset: { menu: "style:" + s.id } }, svgUse("i-more"))));
    wrap.append(over);
    return wrap;
  });
  cards.push(h("button", { class: "style-new", id: "new-style-card" }, svgUse("i-plus"), "Nuevo estilo"));
  const fk = grid.contains(document.activeElement) ? focusKey(document.activeElement) : null;
  grid.replaceChildren(...cards);
  if (fk) { const el = $(fk, grid); if (el) el.focus(); }   // el refresco por SSE no debe robar el foco
}

function deleteStyle(sid) {
  const s = state.styles.find(x => x.id === sid);
  if (!s) return;
  confirmDlg({ title: `¿Borrar el estilo «${s.name}»?`, text: "Se elimina el estilo y no se puede deshacer.", ok: "Borrar estilo" }).then(yes => {
    if (!yes) return;
    return api.send("DELETE", "/api/styles/" + s.id).then(async () => {
      toast("Estilo borrado.");
      state.styles = await api.get("/api/styles");
      state.stylesLoaded = true;
      renderStyles();
    });
  }).catch(err => toast(errMsg(err), "err"));
}

/* ─── Gem de estilo ─── */
async function refreshStyles() {
  state.styles = await api.get("/api/styles");
  state.stylesLoaded = true;
  renderStyles();
  updateComposer();
}

async function toggleGem(sid) {
  const s = state.styles.find(x => x.id === sid);
  if (!s || gemBusy.has(sid)) return;
  const enable = !s.gem;
  gemBusy.add(sid);
  renderStyles();
  try {
    await api.send("PUT", `/api/styles/${sid}/gem`, { enabled: enable });
    await refreshStyles();
    toast(enable ? `Gem activado en «${s.name}».` : `Gem desactivado en «${s.name}».`);
  } catch (e) { toast(errMsg(e), "err"); }
  finally { gemBusy.delete(sid); renderStyles(); updateComposer(); }
}

function openGemDialog(sid) {
  const s = state.styles.find(x => x.id === sid);
  if (!s) return;
  gemDlgStyle = sid;
  $("#gem-style-name").textContent = s.name;
  api.get(`/api/styles/${sid}/gem/instructions`).then(d => {
    $("#gem-text").value = d.instructions;
    $("#gem-dialog").showModal();
    $("#gem-text").focus();
  }).catch(e => toast(errMsg(e), "err"));
}

async function saveGemInstructions(e) {
  e.preventDefault();
  if (!gemDlgStyle) return;
  const btn = $("#gem-save");
  btn.disabled = true;
  try {
    await api.send("PUT", `/api/styles/${gemDlgStyle}/gem`, { enabled: true, instructions: $("#gem-text").value });
    await refreshStyles();
    $("#gem-dialog").close();
    toast("Instrucciones del Gem guardadas.");
  } catch (err) { toast(errMsg(err), "err"); }
  finally { btn.disabled = false; }
}

async function restoreGemTemplate() {
  if (!gemDlgStyle) return;
  const ok = await confirmDlg({ title: "¿Restaurar la plantilla?", text: "Se sustituyen las instrucciones actuales del Gem por la plantilla original.", ok: "Restaurar plantilla", icon: "i-reuse" });
  if (!ok || !gemDlgStyle) return;
  const btn = $("#gem-restore");
  btn.disabled = true;
  try {
    await api.send("PUT", `/api/styles/${gemDlgStyle}/gem`, { enabled: true, instructions: "" });
    const d = await api.get(`/api/styles/${gemDlgStyle}/gem/instructions`);
    $("#gem-text").value = d.instructions;
    await refreshStyles();
    toast("Plantilla restaurada.");
  } catch (err) { toast(errMsg(err), "err"); }
  finally { btn.disabled = false; }
}

/* ─── diálogo de estilo: crear, editar o ver (estilos incluidos) ─── */
const sd = { id: null, readOnly: false, file: null, fromKey: null, fromThumb: null, objUrl: null,
  existingUrl: null, removeImage: false, busy: false, prevT: 0, prevSeq: 0, prefix: null, origFamily: "", openerSel: null };
const PREFIX_FALLBACK = "Generate an image (do not search the web): ";
const PREV_MARK = "@@EJEMPLO@@";

// Selector que devuelve el foco al botón que abrió el diálogo, aunque se repinte la lista.
function openerSelector(el) {
  if (!el || el === document.body) return null;
  if (el.dataset && el.dataset.editStyle) return `[data-edit-style="${CSS.escape(el.dataset.editStyle)}"]`;
  if (el.dataset && el.dataset.menu) return `[data-menu="${CSS.escape(el.dataset.menu)}"]`;
  if (el.id) return "#" + CSS.escape(el.id);
  return null;
}
function syncStyleTest() {
  if (!$("#style-dialog").open) return;
  const canTest = quotaOk();
  $("#st-test").disabled = sd.busy || !canTest;
  $("#st-test-note").textContent = canTest ? "Probar gasta 1 imagen de tu cuota." : "Probar no está disponible: no hay datos de cuota.";
}

function openStyleDialog({ styleId = null, fromKey = null } = {}) {
  const s = styleId ? state.styles.find(x => x.id === styleId) : null;
  if (styleId && !s) return;
  sd.openerSel = openerSelector(document.activeElement);
  Object.assign(sd, { id: s ? s.id : null, readOnly: !!s && s.source !== "user", file: null, fromKey: null, fromThumb: null,
    existingUrl: s ? coverSrc(s) : null, removeImage: false, busy: false, origFamily: s ? (s.family || "") : "" });
  if (sd.objUrl) { URL.revokeObjectURL(sd.objUrl); sd.objUrl = null; }
  $("#style-form").reset();
  $$("#st-family option[data-tmp]").forEach(o => o.remove());
  $("#st-pick-grid").hidden = true;
  $("#st-gallery").setAttribute("aria-expanded", "false");
  $("#style-title").textContent = !s ? "Nuevo estilo" : sd.readOnly ? "Estilo incluido" : "Editar estilo";
  $("#st-example").value = "un retrato";
  if (s) {
    // familia libre (creada por MCP/CLI): se conserva como opción temporal para no perderla al guardar
    if (s.family && !FAM_LABEL[s.family]) $("#st-family").append(h("option", { value: s.family, dataset: { tmp: "1" } }, s.family));
    $("#st-name").value = s.name;
    $("#st-append").value = s.append ?? s.description ?? "";
    $("#st-desc").value = s.description || "";
    $("#st-family").value = s.family || "";
  } else if (fromKey) {
    const item = state.itemsByKey.get(fromKey);
    if (item) {
      sd.fromKey = fromKey; sd.fromThumb = item.thumb_url;
      $("#st-name").value = item.prompt.split(/\s+/).slice(0, 3).join(" ");
      $("#st-append").value = item.prompt;
    }
  }
  for (const id of ["#st-name", "#st-append", "#st-desc", "#st-family"]) $(id).disabled = sd.readOnly;
  $("#style-submit").hidden = sd.readOnly;
  $("#style-cancel-txt").textContent = sd.readOnly ? "Cerrar" : "Cancelar";
  $("#st-more").open = !!($("#st-desc").value || ($("#st-family").value && sd.readOnly));
  $("#style-dialog").showModal();
  syncStyleTest();
  renderStyleRef();
  updateStyleCount();
  runStylePreview();
  $(sd.readOnly ? "#st-example" : "#st-name").focus();
}

function styleImageSrc() {
  return sd.objUrl || sd.fromThumb || (sd.removeImage ? null : sd.existingUrl);
}
function renderStyleRef() {
  const src = styleImageSrc();
  $("#st-thumb").replaceChildren(src ? h("img", { src, alt: "Imagen de referencia del estilo" })
    : h("span", { class: "st-empty" }, sd.readOnly ? "Sin imagen" : "Arrastra o pega una imagen"));
  $("#st-upload").lastChild.textContent = src ? "Cambiar imagen" : "Subir imagen";
  $("#st-upload").disabled = $("#st-gallery").disabled = sd.readOnly;
  $("#st-remove").hidden = sd.readOnly || !src;
}
function updateStyleCount() {
  const n = $("#st-append").value.length;
  $("#st-count").textContent = `${n} ${n === 1 ? "carácter" : "caracteres"}`;
}
function setStyleFile(file) {
  if (sd.readOnly || !file || !file.type.startsWith("image/")) return;
  if (sd.objUrl) URL.revokeObjectURL(sd.objUrl);
  sd.file = file; sd.fromKey = null; sd.fromThumb = null; sd.removeImage = false;
  sd.objUrl = URL.createObjectURL(file);
  renderStyleRef(); scheduleStylePreview();
}
function pickStyleGallery(key) {
  const it = state.itemsByKey.get(key);
  if (!it) return;
  if (sd.objUrl) { URL.revokeObjectURL(sd.objUrl); sd.objUrl = null; }
  sd.file = null; sd.fromKey = key; sd.fromThumb = it.thumb_url; sd.removeImage = false;
  $("#st-pick-grid").hidden = true; $("#st-gallery").setAttribute("aria-expanded", "false");
  renderStyleRef(); scheduleStylePreview();
}
async function toggleStylePicker() {
  const grid = $("#st-pick-grid"), open = grid.hidden;
  grid.hidden = !open;
  $("#st-gallery").setAttribute("aria-expanded", String(open));
  if (!open) return;
  if (!state.gallery.items.length && !state.recentItems.length) {
    try { const r = await api.get("/api/generations?limit=24"); rememberItems(r.items); state.recentItems = r.items; } catch (e) { toast(errMsg(e), "err"); }
  }
  const items = [...state.recentItems, ...state.gallery.items].filter((it, i, a) => a.findIndex(x => x.key === it.key) === i);
  grid.replaceChildren(...(items.length ? items.map(it =>
    h("button", { type: "button", class: "ref-pick", "aria-pressed": String(sd.fromKey === it.key),
      "aria-label": "Usar como referencia: " + short(it.prompt, 40), dataset: { pick: it.key } },
      h("img", { src: it.thumb_url, alt: "", loading: "lazy", decoding: "async" })))
    : [h("p", { class: "none" }, "Aún no hay imágenes en la galería.")]));
  const first = grid.querySelector("button"); if (first) first.focus();
}

function scheduleStylePreview() { clearTimeout(sd.prevT); sd.prevT = setTimeout(runStylePreview, 300); }
async function runStylePreview() {
  clearTimeout(sd.prevT);
  const seq = ++sd.prevSeq;
  // El servidor (/preview) aporta el prefijo y la frase de proporción; el texto escrito aún no está guardado,
  // así que se quita el «append» del estilo consultado y se pone el del formulario.
  const probe = sd.id || (state.styles[0] && state.styles[0].id);
  let prefix = sd.prefix || PREFIX_FALLBACK, aspect = "";
  if (probe) {
    try {
      const r = await api.get(`/api/styles/${encodeURIComponent(probe)}/preview?prompt=${encodeURIComponent(PREV_MARK)}&aspect=${encodeURIComponent(state.cmp.ratio)}`);
      const at = r.final_prompt.indexOf(PREV_MARK);
      if (at >= 0) {
        prefix = r.final_prompt.slice(0, at); sd.prefix = prefix;
        let tail = r.final_prompt.slice(at + PREV_MARK.length);
        const saved = ((state.styles.find(x => x.id === probe) || {}).append || "").trim();
        if (saved) tail = tail.replace(saved, "");
        aspect = tail.trim();
      }
    } catch (e) { /* se usa el prefijo conocido */ }
  }
  if (seq !== sd.prevSeq) return;
  const example = $("#st-example").value.trim() || "un retrato";
  const text = prefix + [example, $("#st-append").value.trim(), aspect].filter(Boolean).join(" ");
  $("#st-prev-text").textContent = text + (styleImageSrc() ? "\n+ imagen de referencia del estilo (se envía la primera)" : "");
}

function styleFields() {
  return { name: $("#st-name").value.trim(), description: $("#st-desc").value.trim(),
    append: $("#st-append").value.trim(), family: $("#st-family").value || "otros" };
}
// Guarda (crea o actualiza) y devuelve el estilo; null si falla (ya avisó con un toast).
async function saveStyleFromDialog() {
  const f = styleFields();
  if (!f.name) { toast("Escribe un nombre para el estilo.", "err"); $("#st-name").focus(); return null; }
  // al editar, la familia solo se envía si el usuario la cambió
  if (sd.id && $("#st-family").value === sd.origFamily) delete f.family;
  try {
    let body;
    if (sd.file) {
      body = new FormData();
      for (const [k, v] of Object.entries(f)) body.append(k, v);
      body.append("file", sd.file);
    } else {
      body = { ...f };
      if (sd.fromKey) body.from_key = sd.fromKey;
      if (sd.id && sd.removeImage && !sd.fromKey) body.remove_image = true;
    }
    const s = sd.id ? await api.send("PUT", "/api/styles/" + encodeURIComponent(sd.id), body)
      : await api.send("POST", "/api/styles", body);
    // la URL de la portada no cambia al sustituir la imagen: versión para saltar la caché del navegador
    coverVer[s.id] = Date.now();
    state.styles = await api.get("/api/styles");
    state.stylesLoaded = true;
    renderStyles(); renderToolRow(); updateComposer();
    try { const st = await api.get("/api/state"); state.counts = st.counts || {}; updateCounts(); } catch (e) {}
    return s;
  } catch (e) { toast(errMsg(e), "err"); return null; }
}
function setStyleBusy(on) {
  sd.busy = on;
  $("#style-submit").disabled = on;
  syncStyleTest();
}
async function submitStyle(e) {
  e.preventDefault();
  if (sd.busy || sd.readOnly) return;
  setStyleBusy(true);
  const s = await saveStyleFromDialog();
  setStyleBusy(false);
  if (!s) return;
  const created = !sd.id;
  $("#style-dialog").close();
  toast((created ? "Estilo creado: " : "Estilo guardado: ") + s.name + ".");
}
async function testStyle() {
  if (sd.busy || !quotaOk()) return;
  const example = $("#st-example").value.trim() || "un retrato";
  const ok = await confirmDlg({ title: "¿Probar este estilo?", text: `Gasta 1 imagen de tu cuota. Se generará «${short(example, 60)}» con este estilo.`, ok: "Probar (1 imagen)", danger: false, icon: "i-play" });
  if (!ok) return;
  setStyleBusy(true);
  let sid = sd.id;
  if (!sd.readOnly) { const s = await saveStyleFromDialog(); if (!s) { setStyleBusy(false); return; } sid = s.id; sd.id = s.id; sd.origFamily = $("#st-family").value; }
  try {
    const r = await api.send("POST", "/api/jobs", { prompt: example, style: sid, n: 1, model: state.cmp.model, ratio: state.cmp.ratio });
    state.testBatch = r.batch;
    $("#style-dialog").close();
    toast("Prueba enviada. Se abrirá al terminar.");
    await loadJobs();
  } catch (e) { toast(errMsg(e), "err"); }
  finally { setStyleBusy(false); }
}

function wireStyleDialog() {
  const dlg = $("#style-dialog"), zone = $("#st-drop");
  $("#style-form").addEventListener("submit", submitStyle);
  $("#style-cancel").addEventListener("click", () => dlg.close());
  $("#st-upload").addEventListener("click", () => $("#st-file").click());
  $("#st-file").addEventListener("change", e => { setStyleFile(e.target.files[0]); e.target.value = ""; });
  $("#st-gallery").addEventListener("click", toggleStylePicker);
  $("#st-pick-grid").addEventListener("click", e => { const b = e.target.closest("[data-pick]"); if (b) pickStyleGallery(b.dataset.pick); });
  $("#st-remove").addEventListener("click", () => {
    if (sd.objUrl) { URL.revokeObjectURL(sd.objUrl); sd.objUrl = null; }
    sd.file = null; sd.fromKey = null; sd.fromThumb = null; sd.removeImage = true;
    renderStyleRef(); scheduleStylePreview(); $("#st-upload").focus();
  });
  $("#st-append").addEventListener("input", () => { updateStyleCount(); scheduleStylePreview(); });
  $("#st-example").addEventListener("input", scheduleStylePreview);
  $("#st-test").addEventListener("click", testStyle);
  dlg.addEventListener("dragover", e => { if (sd.readOnly) return; e.preventDefault(); zone.classList.add("drop"); });
  dlg.addEventListener("dragleave", e => { if (!dlg.contains(e.relatedTarget)) zone.classList.remove("drop"); });
  dlg.addEventListener("drop", e => { e.preventDefault(); zone.classList.remove("drop"); setStyleFile([...e.dataTransfer.files].find(f => f.type.startsWith("image/"))); });
  dlg.addEventListener("paste", e => {
    const f = [...(e.clipboardData?.files || [])].find(x => x.type.startsWith("image/"));
    if (f) { e.preventDefault(); setStyleFile(f); }
  });
  dlg.addEventListener("close", () => {
    if (sd.objUrl) { URL.revokeObjectURL(sd.objUrl); sd.objUrl = null; }
    sd.file = null;
    // la lista se repintó al guardar: el botón original ya es otro nodo, se busca de nuevo
    const sel = sd.openerSel;
    if (sel) requestAnimationFrame(() => {
      const a = document.activeElement;
      if (a && a !== document.body && !dlg.contains(a)) return;
      const el = $(sel); if (el && el.offsetParent !== null) el.focus();
    });
  });
}

/* ─── límites de uso ─── */
const fmtBytes = n => {
  if (n == null || !isFinite(n)) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = n, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return new Intl.NumberFormat("es-ES", { maximumFractionDigits: 1 }).format(v) + " " + units[i];
};
const RTF = new Intl.RelativeTimeFormat("es", { numeric: "auto" });
function relTime(ts) {
  const t = new Date(ts).getTime();
  if (isNaN(t)) return String(ts || "");
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return "hace un momento";
  const m = Math.floor(s / 60);
  if (m < 60) return RTF.format(-m, "minute");
  const hh = Math.floor(m / 60);
  if (hh < 24) return RTF.format(-hh, "hour");
  const d = Math.floor(hh / 24);
  if (d < 30) return RTF.format(-d, "day");
  return new Date(ts).toLocaleDateString("es-ES", { dateStyle: "medium" });
}
const fmtDateShort = ts => { try { return new Date(ts).toLocaleDateString("es-ES", { month: "short", year: "numeric" }); } catch (e) { return String(ts || ""); } };
const fmtDateLong = ts => { try { return new Date(ts).toLocaleDateString("es-ES", { dateStyle: "medium" }); } catch (e) { return String(ts || ""); } };

async function renderLimites() {
  try { state.quota = await api.get("/api/quota"); } catch (e) { state.quota = null; }
  let usage = null;
  try { usage = await api.get("/api/usage"); } catch (e) {}
  renderQuota();
  renderLimitsScreen(usage);
}

function renderLimitsScreen(usage) {
  const q = state.quota, ok = quotaOk();
  // /api/quota devuelve `code` en las respuestas available:false (auth_expired / quota_unavailable)
  const nextAction = q && q.available === false
    ? (ERR_ES[q.code] ? errMsg(q) : "Sin datos de cuota. Espera un momento y recarga.")
    : "";

  // estado del servicio
  const st = $("#limits-status");
  st.classList.remove("warn", "err");
  if (!q || q.available === false) {
    const noCookie = q && q.cookie_present === false;
    st.textContent = noCookie ? "Sesión caducada · ejecuta «musa auth»" : "Sin datos de cuota";
    st.classList.add("warn");
  } else if (q.account_status === "AVAILABLE") {
    st.textContent = ACCOUNT_ES.AVAILABLE;
  } else {
    st.textContent = ACCOUNT_ES[q.account_status] || "Servicio no disponible"; st.classList.add("warn");
  }

  // anillo 5 h
  const free = quotaFree();
  $("#ring-val").setAttribute("stroke-dasharray", "0 100");
  $("#ring-txt").textContent = ok ? free + "%" : "—";
  $("#lim-free").textContent = ok ? free + "% libre" : "Sin datos de cuota";
  $("#ring-val").classList.toggle("warn", ok && free <= 20);
  $("#lim-window").textContent = ok
    ? [q.resets_at_5h ? "Se renueva a las " + fmtHM(q.resets_at_5h) : null,
       q.est_images_left_5h != null ? `≈ ${q.est_images_left_5h} imágenes libres` : null].filter(Boolean).join(" · ") + "."
    : (nextAction || "Sin datos de cuota.");
  if (ok) requestAnimationFrame(() => requestAnimationFrame(() =>
    $("#ring-val").setAttribute("stroke-dasharray", free + " 100")));

  // generaciones: una a la vez + cola actual
  const queued = (state.jobs || []).filter(j => j.status === "queued").length;
  $("#lim-queue").textContent = queued ? `${queued} en cola ahora.` : "Nada en cola. Se procesan una a una.";

  // almacenamiento
  const storage = usage && usage.storage;
  if (storage) {
    $("#lim-storage").textContent = fmtBytes(storage.output_bytes) + " usados";
    const total = storage.disk_total || 1;
    const usedPct = Math.min(100, storage.output_bytes / total * 100);
    $("#lim-storage-bar").style.width = usedPct + "%";
    $("#lim-storage-bar").classList.toggle("warn", usedPct >= 80);
    $("#lim-storage-sub").textContent = `de ${fmtBytes(total)} en disco · ${fmtBytes(storage.disk_free)} libres`;
  } else {
    $("#lim-storage").textContent = "—";
    $("#lim-storage-bar").style.width = "0";
    $("#lim-storage-bar").classList.remove("warn");
    $("#lim-storage-sub").textContent = "Sin datos de uso.";
  }

  // gráfica 7 días
  const chart = $("#chart");
  const perDay = (usage && usage.per_day) || [];
  const max = Math.max(1, ...perDay.map(x => x.images));
  const anyDay = perDay.some(x => x.images > 0);
  $("#chart-empty").hidden = anyDay;
  chart.replaceChildren(...(anyDay ? perDay : []).map(x => {
    const day = (() => { try { return new Date(x.date + "T00:00:00").toLocaleDateString("es-ES", { weekday: "short" }); } catch (e) { return x.date; } })();
    const label = `${day}: ${x.images} ${x.images === 1 ? "imagen" : "imágenes"}`;
    return h("div", { class: "b", role: "img", "aria-label": label, title: label },
      h("i", { dataset: { h: String(Math.round(x.images / max * 100)) } }),
      h("span", { "aria-hidden": "true" }, day));
  }));
  $$("#chart i").forEach((el, idx) => { el.style.height = "0"; setTimeout(() => { el.style.height = el.dataset.h + "%"; }, 150 + idx * 70); });

  // panel de plan
  if (ok) {
    const weekFree = Math.max(0, 100 - Math.round(q.weekly_pct || 0));
    $("#plan-big").replaceChildren("Plan ", h("span", { class: "upper" }, String(q.tier || "PRO")));
    $("#plan-desc").textContent = `${free}% libre en 5 h`;
    $("#plan-week-bar").style.width = weekFree + "%";
    $("#plan-week-bar").classList.toggle("warn", weekFree <= 20);
    $("#plan-week").textContent = `${weekFree}% libre esta semana` + (q.resets_at_weekly ? " · se renueva el " + fmtDateLong(q.resets_at_weekly) : "") + ".";
  } else {
    $("#plan-big").textContent = "Sin datos de cuota";
    $("#plan-desc").textContent = nextAction;
    $("#plan-week-bar").style.width = "0";
    $("#plan-week-bar").classList.remove("warn");
    $("#plan-week").textContent = "";
  }

  // tabla de actividad
  const body = $("#activity-body");
  const acts = (usage && usage.activity) || [];
  const stateTxt = s => s === "ok" ? ["Completado", ""] : s === "not_saved" ? ["Sin guardar", "warn"] : ["Con error", "err"];
  $("#limit-activity table").hidden = !acts.length;
  $("#activity-empty").hidden = acts.length > 0;
  body.replaceChildren(...acts.map(a => {
    const [txt, cls] = stateTxt(a.status);
    return h("tr", {},
      h("td", {}, short(a.prompt, 48)),
      h("td", {}, modelName(a.model)),
      h("td", { class: "col-2" }, a.style ? (a.style_name || "Estilo eliminado") : "Sin estilo"),
      h("td", { class: "num" }, String(a.images)),
      h("td", { class: "num col-2" }, a.elapsed_s != null ? `${Math.round(a.elapsed_s)} s` : "—"),
      h("td", {}, h("span", { class: "status" + (cls ? " " + cls : "") }, txt)));
  }));
}

/* ─── colecciones ─── */
function renderCollections() {
  const grid = $("#coll-grid");
  $("#coll-list-view").hidden = false;
  $("#coll-detail-view").hidden = true;
  $("#coll-empty").hidden = state.collections.length > 0;
  const cards = state.collections.map(c => {
    const card = h("button", { class: "coll", dataset: { coll: c.id }, "aria-label": "Abrir " + c.name });
    const box = h("div", { class: "stackbox" });
    if (c.covers && c.covers.length) (c.covers).slice(0, 3).forEach(u => box.append(h("img", { src: u, alt: "", loading: "lazy", decoding: "async" })));
    else box.append(h("span", { class: "ph" }, svgUse("i-folder")));
    card.append(box);
    card.append(h("div", { class: "info" },
      h("div", {}, h("h3", {}, c.name), c.description ? h("p", {}, c.description) : null),
      h("span", { class: "cnt" }, String(c.count))));
    return card;
  });
  grid.replaceChildren(...cards);
}

async function renderCollection(id) {
  const c = state.collections.find(x => x.id === id);
  if (!c) { toast("La colección ya no existe.", "err"); setHash("#colecciones"); return; }
  $("#coll-list-view").hidden = true;
  const view = $("#coll-detail-view");
  view.hidden = false;
  setTitle(c.name);

  state.gallery.collection = id;
  state.gallery.items = [];
  await fetchCollectionItems();
  // miembros reales de la colección (máx. 200 por petición; con más imágenes se omite «Estilos»)
  let members = state.gallery.items;
  if (isFiltered(state.gallery) || state.gallery.sort !== "newest") {
    try { members = (await api.get(`/api/generations?collection=${encodeURIComponent(id)}&limit=200`)).items; } catch (e) {}
  }
  rememberItems(members);

  let acts = [];
  try { acts = (await api.get(`/api/collections/${id}/activity`)).activity || []; } catch (e) {}
  const created = acts.filter(a => a.kind === "created").pop();
  const lastAct = acts[0];
  const stylesN = new Set(members.map(m => m.style).filter(Boolean)).size;
  const showStyles = (c.count || members.length) <= 200;
  const tab = state.collTab === "act" ? "act" : "imgs";

  const hasCover = !!(c.covers && c.covers.length);
  const banner = h("div", { class: "banner" + (hasCover ? "" : " no-cover") });
  if (hasCover) {
    banner.append(h("img", { src: c.covers[0], alt: "Portada de " + c.name }));
    if (members.length) banner.append(h("span", { class: "credit-chip" }, short(members[0].prompt, 90)));
  } else {
    banner.append(svgUse("i-folder"), h("p", { class: "t-sm" }, "Sin portada todavía. Añade imágenes y aparecerá aquí."));
  }

  const side = h("aside", { class: "side" },
    h("div", {}, h("h2", {}, c.name), c.description ? h("p", { class: "desc" }, c.description) : null),
    h("div", { class: "badges" },
      h("span", { class: "badge" }, created ? "Creada " + fmtDateShort(created.ts) : "Creada —")),
    h("div", { class: "mini-card" }, h("span", { class: "tnum" }, String(c.count ?? members.length)), h("span", { class: "muted t-xs" }, "Imágenes")),
    h("div", { class: "kv-list" },
      ...(showStyles ? [h("div", { class: "kv" }, h("span", {}, "Estilos"), h("span", { class: "tnum" }, String(stylesN)))] : []),
      h("div", { class: "kv" }, h("span", {}, "Última actualización"), h("span", {}, lastAct ? relTime(lastAct.ts) : "—"))),
    h("div", { class: "row-wrap" },
      h("button", { class: "btn btn-sm btn-primary", id: "coll-add" }, svgUse("i-plus"), "Añadir imágenes"),
      h("button", { class: "btn btn-sm", id: "coll-back" }, svgUse("i-prev"), "Volver")),
    h("div", { class: "row-wrap" },
      h("button", { class: "btn btn-sm", id: "coll-rename" }, svgUse("i-edit"), "Renombrar"),
      h("button", { class: "btn btn-sm danger", id: "coll-delete" }, svgUse("i-trash"), "Borrar")));

  const panelImgs = h("div", { id: "panel-imgs", role: "tabpanel", "aria-labelledby": "tab-imgs", dataset: { tabPanel: "imgs" }, hidden: tab !== "imgs" },
    h("div", { class: "fbar", id: "coll-toolbar" }), h("div", { id: "coll-results" }));
  const feed = acts.map(a => h("div", { class: "feed-row" },
    h("span", { class: "dot" }), h("span", {}, actText(a)), h("time", {}, relTime(a.ts))));
  const panelAct = h("div", { id: "panel-act", role: "tabpanel", "aria-labelledby": "tab-act", dataset: { tabPanel: "act" }, hidden: tab !== "act" },
    feed.length ? feed : h("div", { class: "empty" }, "Sin actividad todavía."));

  view.replaceChildren(
    banner,
    h("div", { class: "detail" },
      h("div", { class: "col" },
        h("div", { class: "tabs", role: "tablist" },
          h("button", { role: "tab", id: "tab-imgs", "aria-selected": String(tab === "imgs"), "aria-controls": "panel-imgs", dataset: { detailTab: "imgs" } }, "Imágenes"),
          h("button", { role: "tab", id: "tab-act", "aria-selected": String(tab === "act"), "aria-controls": "panel-act", dataset: { detailTab: "act" } }, "Actividad")),
        panelImgs, panelAct),
      side));
  paintCollectionGrid();

  view.querySelectorAll("[data-detail-tab]").forEach(b => b.addEventListener("click", () => { state.collTab = b.dataset.detailTab; if (state.collTab !== "imgs") setSelectMode(false); renderCollection(id); }));
  // «Añadir imágenes» abre la galería completa (sin el filtro de esta colección) para elegir de ahí
  $("#coll-add").addEventListener("click", () => { clearGalleryFilters(); state.gallery.collection = null; setHash("#galeria"); });
  $("#coll-back").addEventListener("click", () => { state.gallery.collection = null; setHash("#colecciones"); });
  $("#coll-rename").addEventListener("click", () => startRename(c, view));
  $("#coll-delete").addEventListener("click", async () => {
    if (!await confirmDlg({ title: `¿Borrar la colección «${c.name}»?`, text: "Las imágenes se conservan en la galería.", ok: "Borrar colección" })) return;
    try {
      await api.send("DELETE", `/api/collections/${c.id}`);
      toast("Colección borrada.");
      state.gallery.collection = null;
      setHash("#colecciones");
    } catch (e) { toast(errMsg(e), "err"); }
  });
}

function actText(a) {
  if (a.kind === "created") return "Colección creada";
  if (a.kind === "renamed") return `Renombrada a «${a.name || ""}»`;
  if (a.kind === "added") {
    const it = a.key && state.itemsByKey.get(a.key.replace(/^gen:/, ""));
    return it ? `«${short(it.prompt, 40)}» añadida a la colección` : "Imagen añadida a la colección";
  }
  if (a.kind === "removed") return "Imagen quitada de la colección";
  return a.kind || "Actividad";
}

function startRename(c, view) {
  const h2 = view.querySelector(".side h2");
  const input = h("input", { class: "input", value: c.name, "aria-label": "Nuevo nombre de la colección" });
  const save = h("button", { class: "btn btn-sm btn-primary" }, svgUse("i-check"), "Guardar");
  const cancel = h("button", { type: "button", class: "btn btn-sm" }, svgUse("i-close"), "Cancelar");
  const form = h("form", { class: "rename-form" }, input, save, cancel);
  h2.replaceWith(form);
  input.focus(); input.select();
  const done = () => { h2.textContent = c.name; form.replaceWith(h2); };
  cancel.addEventListener("click", done);
  form.addEventListener("submit", async e => {
    e.preventDefault();
    const name = input.value.trim();
    if (!name || name === c.name) { done(); return; }
    try {
      const r = await api.send("PATCH", `/api/collections/${c.id}`, { name });
      c.name = r.name;
      done();
      setTitle(c.name);
      state.collections = await api.get("/api/collections");
      toast("Colección renombrada.");
    } catch (err) { toast(errMsg(err), "err"); }
  });
}


async function submitNewCollection(e) {
  e.preventDefault();
  const name = $("#coll-name").value.trim();
  if (!name) return;
  try {
    await api.send("POST", "/api/collections", { name, description: $("#coll-desc").value.trim() });
    $("#coll-dialog").close();
    toast("Colección creada: " + name + ".");
    state.collections = await api.get("/api/collections");
    const s = await api.get("/api/state"); state.counts = s.counts || {}; updateCounts();
    if (state.route && state.route.name === "colecciones") {
      if (state.route.id) await renderCollection(state.route.id); else renderCollections();
    }
  } catch (err) { toast(errMsg(err), "err"); }
}

/* ─── eventos globales ─── */
function connectEvents() {
  wireSelection();
  document.addEventListener("click", e => {
    if (e.target.closest("[data-sel-toggle]")) { setSelectMode(!sel.on); return; }
    const nav = e.target.closest("[data-nav]");
    if (nav) {
      e.preventDefault(); closePops();
      if (nav.dataset.nav === "galeria") state.gallery.collection = null;   // no arrastrar el filtro de una colección
      setHash("#" + nav.dataset.nav); return;
    }
    const kb = e.target.closest("[data-menu]");
    if (kb) { e.stopPropagation(); openImgMenu(kb); return; }
    const fav = e.target.closest("[data-fav]");
    if (fav) { e.stopPropagation(); const it = state.itemsByKey.get(fav.dataset.fav); if (it) toggleFavorite(it); return; }
    const gsw = e.target.closest("[data-gem-switch]");
    if (gsw) { e.stopPropagation(); toggleGem(gsw.dataset.gemSwitch); return; }
    const glk = e.target.closest("[data-gem-link]");
    if (glk) { e.stopPropagation(); openGemDialog(glk.dataset.gemLink); return; }
    const open = e.target.closest("[data-open]");
    if (open && sel.on && open.closest(".tile")) { e.preventDefault(); toggleSel(open.dataset.open, { range: e.shiftKey }); return; }
    if (open) { const [gen, i] = open.dataset.open.split("#"); openDetail(gen, i); return; }
    const gf = e.target.closest("[data-gf]");
    if (gf) { applyFilters({ filter: gf.dataset.gf }); return; }
    const chip = e.target.closest("[data-chip]");
    if (chip) { focusFbtn(); applyFilters(CHIP_CLEAR[chip.dataset.chip]); return; }
    if (e.target.closest("[data-clear-chips]")) { focusFbtn(); applyFilters({ ...CHIP_CLEAR_ALL, query: "" }); return; }
    if (e.target.closest("[data-clear-filters]")) { clearGalleryFilters(); applyFilters(); return; }
    const fbtn = e.target.closest("[data-filters]");
    if (fbtn) { toggleFilterPop(fbtn); return; }
    const fam = e.target.closest("[data-fam]");
    if (fam) { styleFamily = fam.dataset.fam; renderStyles(); return; }
    const sid = e.target.closest("[data-style-id]");
    if (sid) { selectStyle(sid.dataset.styleId); return; }
    const start = e.target.closest("[data-start]");
    if (start) { selectStyle(start.dataset.start); return; }
    if (e.target.closest("#new-style-card")) { openStyleDialog(); return; }
    const est = e.target.closest("[data-edit-style]");
    if (est) { openStyleDialog({ styleId: est.dataset.editStyle }); return; }
    const coll = e.target.closest("[data-coll]");
    if (coll) { setHash("#colecciones/" + coll.dataset.coll); return; }
    if (e.target.closest("#new-coll-btn")) { $("#coll-form").reset(); $("#coll-dialog").showModal(); $("#coll-name").focus(); return; }
    const tog = e.target.closest("[data-toggle-coll]");
    if (tog && detailItem) { toggleCollection(detailItem, tog.dataset.toggleColl); return; }
  });

  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && !$("#filter-pop").hidden) { e.preventDefault(); closeFilterPop(true); return; }
    const dlg = $("#detail-dialog");
    if (dlg.open && !$("#confirm-dialog").open && !$("#prompt-dialog").open && !$("#anim-dialog").open) {
      if (e.target.closest("input, textarea, select")) return;   // ←/→ mueven el cursor en los campos de texto
      if (e.key === "ArrowLeft") { e.preventDefault(); stepDetail(-1); }
      else if (e.key === "ArrowRight") { e.preventDefault(); stepDetail(1); }
    }
  });

  $("#detail-dialog").addEventListener("click", e => { if (e.target === $("#detail-dialog")) $("#detail-dialog").close(); });
  $("#detail-dialog").addEventListener("close", () => {
    const key = detailItem && detailItem.key;
    detailItem = null;
    killDetailAnim();
    // si el foco no se movió a otro sitio (p. ej. al compositor), vuelve a la tarjeta que abrió el detalle
    const a = document.activeElement;
    if (key && (!a || a === document.body || a.matches(".tile-open") || $("#detail-dialog").contains(a))) { pendingFocusKey = key; focusTile(key); }
    if (state.route && state.route.name === "imagen") setHash(state.baseHash || "#galeria");
  });
  $("#dlg-x").addEventListener("click", () => $("#detail-dialog").close());
  $("#dlg-prev").addEventListener("click", () => stepDetail(-1));
  $("#dlg-next").addEventListener("click", () => stepDetail(1));
  $("#dlg-fav").addEventListener("click", () => detailItem && toggleFavorite(detailItem));
  $("#dlg-copyref").addEventListener("click", () => { if (detailItem) copyItemId(detailItem); });
  $("#dlg-download").addEventListener("click", () => {
    if (!detailItem) return;
    const a = h("a", { href: detailItem.image_url, download: "musa-" + detailItem.key.replace(/[^a-z0-9]+/gi, "-") + ".jpg" });
    document.body.append(a); a.click(); a.remove();
    toast("Descarga iniciada.");
  });
  // Finder en macOS, Explorador en Windows (el servidor es local: misma máquina que el navegador)
  const revealIn = /Mac|iPhone|iPad/i.test(navigator.platform || navigator.userAgent) ? "el Finder" : "el Explorador";
  $("#dlg-reveal").lastChild.textContent = `Mostrar en ${revealIn}`;
  $("#dlg-reveal").addEventListener("click", async () => {
    if (!detailItem) return;
    try {
      const r = await doFetch(`/api/images/${detailItem.gen_id}/${detailItem.index}/reveal`, { method: "POST", credentials: "same-origin" });
      if (!r.ok) { toast(errMsg(await parseBody(r)), "err"); return; }
      toast(`Mostrando en ${revealIn}…`);
    } catch (e) { toast(errMsg(e), "err"); }
  });
  $("#dlg-style").addEventListener("click", () => detailItem && openStyleDialog({ fromKey: detailItem.key }));
  $("#dlg-edit").addEventListener("click", () => { if (detailItem) startEdit(detailItem); });
  $("#dlg-delete").addEventListener("click", () => { if (detailItem) deleteItem(detailItem); });
  $("#dlg-anim").addEventListener("click", () => { if (detailItem) openAnimDialog(detailItem); });
  $("#dlg-anim-export").addEventListener("click", () => {
    if (!detailItem || !detailItem.animation) return;
    const a = h("a", { href: `/api/animations/${detailItem.gen_id}/${detailItem.index}/export.zip`, download: "" });
    document.body.append(a); a.click(); a.remove();
    toast("Descarga iniciada.");
  });
  $("#anim-dialog").addEventListener("click", e => { if (e.target === $("#anim-dialog")) $("#anim-dialog").close(); });
  $("#ad-cancel").addEventListener("click", () => $("#anim-dialog").close());
  $("#ad-form").addEventListener("submit", submitAnimDialog);
  $("#ad-form").addEventListener("input", syncAnimDialog);
  $("#ad-form").addEventListener("change", syncAnimDialog);
  $("#ad-remove").addEventListener("click", removeAnimation);
  $("#anim-dialog").addEventListener("close", () => {
    const o = adState && adState.opener; adState = null;
    const back = o && o.isConnected && !o.closest("[hidden]") ? o : ($("#detail-dialog").open ? $("#dlg-anim") : null);
    if (back) back.focus();
  });
  $("#dlg-new-coll").addEventListener("submit", async e => {
    e.preventDefault();
    if (!detailItem) return;
    const name = $("#dlg-new-coll-name").value.trim();
    if (!name) return;
    try {
      await addToNewCollection(detailItem, name);
      $("#dlg-new-coll-name").value = "";
    } catch (err) { toast(errMsg(err), "err"); }
  });

  $("#coll-dialog").addEventListener("click", e => { if (e.target === $("#coll-dialog")) $("#coll-dialog").close(); });
  $("#coll-cancel").addEventListener("click", () => $("#coll-dialog").close());
  $("#coll-form").addEventListener("submit", submitNewCollection);
  $("#prompt-dialog").addEventListener("click", e => { if (e.target === $("#prompt-dialog")) $("#prompt-dialog").close(); });
  $("#pd-cancel").addEventListener("click", () => $("#prompt-dialog").close());
  $("#pd-form").addEventListener("submit", submitPromptDialog);
  $("#prompt-dialog").addEventListener("close", () => {
    const o = pdState && pdState.opener; pdState = null;
    if (o && o.isConnected && !o.closest("[hidden]")) o.focus();
  });
  $("#dlg-save-prompt").addEventListener("click", () => { if (detailItem) openPromptDialog({ text: detailItem.prompt }); });

  $("#style-dialog").addEventListener("click", e => { if (e.target === $("#style-dialog")) $("#style-dialog").close(); });
  $("#gem-dialog").addEventListener("click", e => { if (e.target === $("#gem-dialog")) $("#gem-dialog").close(); });
  $("#confirm-dialog").addEventListener("click", e => { if (e.target === $("#confirm-dialog")) $("#confirm-dialog").close(); });
  $("#confirm-dialog").addEventListener("close", () => settleConfirm(false));
  $("#cf-cancel").addEventListener("click", () => $("#confirm-dialog").close());
  $("#cf-form").addEventListener("submit", e => { e.preventDefault(); settleConfirm(true); $("#confirm-dialog").close(); });
  $("#st-family").append(...FAMILIES.map(([v, l]) => h("option", { value: v }, l)));
  $("#skip-link").addEventListener("click", e => { e.preventDefault(); $("#main").focus(); });
  $("#gem-cancel").addEventListener("click", () => $("#gem-dialog").close());
  $("#gem-form").addEventListener("submit", saveGemInstructions);
  $("#gem-restore").addEventListener("click", restoreGemTemplate);
  wireStyleDialog();

  let searchT = 0;
  $("#global-search").addEventListener("input", e => {
    clearTimeout(searchT);
    searchT = setTimeout(() => {
      const q = e.target.value.trim(), r = state.route;
      if (r && (r.name === "galeria" || (r.name === "colecciones" && r.id))) { applyFilters({ query: q }, { push: false }); return; }
      if (!q) return;
      clearGalleryFilters();
      Object.assign(state.gallery, { query: q, collection: null, sort: "newest" });
      setHash(galleryHash());
    }, 250);
  });
  document.addEventListener("change", e => { const s = e.target.closest("[data-sort]"); if (s) applyFilters({ sort: s.value }); });
  document.addEventListener("click", e => {
    if (!$("#filter-pop").hidden && !e.target.closest("#filter-pop, [data-filters]")) closeFilterPop(false);
  });

  window.addEventListener("hashchange", () => {
    if (location.hash === lastHash) return;
    render();
  });
}

/* ─── composiciones ─── */
const comp = { id: null, data: null, sig: "", listSig: "", seq: 0, showOriginal: false, open: new Set(), timer: 0, busy: false,
  mode: new Map(), draft: new Map(), openAnim: new Map(), stage: null, wantPlay: !reducedMotion(), webpBusy: false, mT: 0, mPending: null };
const COMP_STATUS = { pending: "En espera", queued: "En cola", running: "Generando", done: "Lista", error: "Error" };
const compBusy = s => s === "queued" || s === "running";
const compErrText = e => e ? (ERR_ES[e.code] || e.message || "No se pudo completar la acción.") : "";
const compUrl = (id, tail = "") => "/api/compositions/" + encodeURIComponent(id) + tail;

async function renderComposiciones(id) {
  comp.seq++;
  $("#comp-list-view").hidden = !!id;
  $("#comp-edit-view").hidden = !id;
  if (id) {
    if (comp.id !== id) {
      compDropStage();
      clearTimeout(comp.mT); comp.mPending = null;
      Object.assign(comp, { id, data: null, sig: "", showOriginal: false });
      comp.open.clear(); comp.mode.clear(); comp.draft.clear(); comp.openAnim.clear();
      $("#comp-edit-view").replaceChildren();
    } else if (comp.data) setTitle(comp.data.name);
    await loadCompEditor();
    compResumeStage();
  } else {
    compDropStage();
    if (comp.id) {
      comp.id = null; comp.listSig = "";
      clearTimeout(comp.mT); comp.mPending = null;
      comp.open.clear(); comp.mode.clear(); comp.draft.clear(); comp.openAnim.clear();
    }
    await loadCompList();
  }
}

/* lista */
async function loadCompList() {
  const seq = ++comp.seq;
  let items;
  try { items = (await api.get("/api/compositions")).items || []; }
  catch (e) { if (seq === comp.seq) toast(errMsg(e), "err"); return; }
  if (seq !== comp.seq) return;
  const sig = JSON.stringify(items);
  if (sig === comp.listSig) return;
  comp.listSig = sig;
  $("#comp-empty").hidden = items.length > 0;
  $("#comp-grid").replaceChildren(...items.map(c => {
    const p = c.progress || { done: 0, total: 0, errors: 0 };
    const errs = p.errors > 0;
    return h("button", { type: "button", class: "coll comp-card", dataset: { comp: c.id }, "aria-label": "Abrir " + c.name },
      h("div", { class: "comp-cover" }, c.cover_url ? h("img", { src: c.cover_url, alt: "", loading: "lazy", decoding: "async" }) : h("span", { class: "ph" }, svgUse("i-compose"))),
      h("div", { class: "info" }, h("div", { class: "minw-0" },
        h("h3", {}, c.name),
        h("p", { class: errs ? "comp-state err" : "comp-state" }, errs ? "con errores" : `${p.done}/${p.total} capas listas`))));
  }));
}

/* editor */
async function loadCompEditor(force = false) {
  const id = comp.id, seq = ++comp.seq;
  if (!id) return;
  if (comp.mPending && !force) return;   // hay un ajuste de movimiento sin guardar: no pisarlo con el estado del servidor
  let d;
  try { d = await api.get(compUrl(id)); }
  catch (e) {
    if (seq !== comp.seq || id !== comp.id) return;
    toast(errMsg(e), "err");
    if (e && e.code === "unknown_composition") setHash("#composiciones");
    return;
  }
  if (seq !== comp.seq || id !== comp.id) return;
  const sig = JSON.stringify(d);
  if (!force && sig === comp.sig && $("#comp-edit-view").firstChild) return;
  comp.sig = sig; comp.data = d;
  paintCompEditor();
}

/* escenario: MusaScene montado con scene.json; se conserva el nodo mientras no cambie rev ni la animación de ninguna capa */
const compAnType = l => (l.animation && l.animation.type) || "static";
const compStageKey = d => String(d.rev ?? "") + "|" + JSON.stringify(d.layers.map(l => [l.id, l.visible, l.status, l.offset, l.bbox, l.image_url, l.animation]));
function compPauseStage() { const st = comp.stage; if (st && st.player && st.player.playing) st.player.pause(); }
function compResumeStage() {
  const st = comp.stage;
  if (st && st.player && comp.wantPlay && !comp.showOriginal && !st.player.playing) { st.player.play(); syncCompPlay(); }
}
function compDropStage() { if (comp.stage) { if (comp.stage.player) comp.stage.player.destroy(); comp.stage = null; } }
function syncCompPlay() {
  const b = $("#comp-play");
  if (!b) return;
  const on = !!(comp.stage && comp.stage.player && comp.stage.player.playing);
  b.setAttribute("aria-pressed", String(on));
  b.title = on ? "Pausar animación" : "Reproducir animación";
  b.querySelector("use").setAttribute("href", on ? "#i-pause" : "#i-play");
}
function compMountScene(st, scene) {
  if (st.player) st.player.destroy();
  st.player = window.MusaScene.mountScene(st.inner, scene, { autoplay: comp.wantPlay });
  syncCompPlay();
}

function compStage(d) {
  const [rw, rh] = d.width && d.height ? [d.width, d.height] : String(d.ratio || "4:3").split(":").map(Number);
  const mk = () => {
    const s = h("div", { class: "comp-stage" });
    s.style.aspectRatio = `${rw} / ${rh}`;
    s.style.setProperty("--ar", String(rw / rh));
    return s;
  };
  if (comp.showOriginal && d.master_url) {
    compPauseStage();
    const s = mk();
    s.append(h("img", { class: "full", src: d.master_url, alt: "Imagen completa original" }));
    return s;
  }
  const shown = d.layers.filter(l => l.visible && l.image_url && l.bbox);
  if (!shown.length) {
    compDropStage();
    const s = mk();
    s.append(h("p", { class: "comp-stage-empty muted t-sm" }, "Aún no hay capas listas."));
    return s;
  }
  const key = compStageKey(d);
  if (comp.stage && comp.stage.key === key) { compResumeStage(); return comp.stage.node; }
  compDropStage();
  const node = mk(), inner = h("div", { class: "comp-scene" });
  node.append(inner);
  const st = comp.stage = { key, node, inner, scene: null, player: null };
  const fail = () => { if (comp.stage === st) node.append(h("p", { class: "comp-stage-empty muted t-sm", role: "status" }, "No se pudo cargar la animación.")); };
  if (!window.MusaScene) { fail(); return node; }
  api.get(compUrl(d.id, "/scene.json")).then(scene => {
    if (comp.stage !== st) return;
    st.scene = scene;
    compMountScene(st, scene);
    // las miniaturas de frames pueden salir de la escena si la ficha no viene en la capa
    if (comp.data && comp.data.layers.some(l => compAnType(l) === "frames" && !(l.animation && l.animation.sheet_url))) paintCompEditor();
  }).catch(fail);
  return node;
}

/* bloque «Animación» de cada capa de pieza */
const MOTION = {
  bob: { label: "Flotar", amp: 6, min: 1, max: 60, period: 2.0, unit: "px" },
  sway: { label: "Balanceo", amp: 4, min: 1, max: 30, period: 2.4, unit: "°" },
  pulse: { label: "Latido", amp: 0.04, min: 0.01, max: 0.3, period: 1.6, unit: "" },
  fall: { label: "Caer", amp: null, min: 10, max: 2000, period: 1.2, unit: "px" },
};
const COMP_MODES = [["static", "Ninguna"], ["motion", "Movimiento"], ["frames", "Frames"], ["states", "Estados"]];
const COMP_MODE_LABEL = { static: "Ninguna", motion: "Movimiento", frames: "Frames", states: "Estados" };
const compMode = l => comp.mode.get(l.id) || compAnType(l);
const compAutoAmp = l => l.bbox ? Math.floor(1.2 * (l.bbox[3] - l.bbox[1]) + 0.5) : 100;
function compDraft(lid) {
  let dr = comp.draft.get(lid);
  if (!dr) { dr = { action: "", grid: "4x2", prompt: "" }; comp.draft.set(lid, dr); }
  return dr;
}

// Resumen de la ficha para la miniatura animada: lo que trae la capa o, si falta, lo que dice scene.json.
function compFramesAnim(l) {
  const an = l.animation || {};
  if (an.sheet_url && an.frames) return an;
  const st = comp.stage, sl = st && st.scene && st.scene.layers.find(x => x.id === l.id), sa = sl && sl.animation;
  if (sa && sa.type === "frames") return { frames: sa.frames, fps: sa.fps, loop: sa.loop, frame_w: sa.w, frame_h: sa.h, sheet_url: sa.file };
  return null;
}

function compAnimBlock(d, l, lock) {
  const k = s => l.id + ":" + s;
  const type = compAnType(l), mode = compMode(l), an = l.animation || {};
  const dis = lock || !l.image_url;
  const enc = encodeURIComponent(l.id);
  const field = (key, label, input, attrs = {}) => h("div", attrs, h("label", { class: "label", for: input.id }, label), input);
  const body = [];

  if (an.broken) body.push(h("p", { class: "comp-warn t-xs", role: "status" }, svgUse("i-alert"), "La hoja de frames ya no existe."));

  if (mode === "motion") {
    const preset = MOTION[an.preset] ? an.preset : "bob", P = MOTION[preset];
    const fall = preset === "fall", auto = fall && an.amplitude == null;
    const amp = auto ? compAutoAmp(l) : (an.amplitude ?? P.amp);
    const id = key => `comp-an-${key}-${l.id}`;
    const dd = key => ({ can: key, lid: l.id, fk: k("a-" + key) });
    const sel = h("select", { class: "input", id: id("preset"), disabled: dis, dataset: dd("preset") },
      ...Object.entries(MOTION).map(([v, m]) => h("option", { value: v, selected: v === preset }, m.label)));
    const ampIn = h("input", { class: "input", id: id("amp"), type: "number", min: String(P.min), max: String(P.max), step: "any", inputmode: "decimal",
      value: String(amp), disabled: dis || auto, dataset: dd("amp") });
    const autoIn = h("input", { type: "checkbox", id: id("auto"), checked: auto, disabled: dis, dataset: dd("auto") });
    body.push(
      h("div", { class: "comp-adj-grid" },
        field("preset", "Movimiento", sel),
        h("div", {},
          h("label", { class: "label", for: id("amp") }, "Amplitud ", h("span", { class: "muted", dataset: { canUnit: l.id } }, P.unit ? "(" + P.unit + ")" : "")),
          ampIn,
          h("label", { class: "comp-auto t-xs", hidden: !fall, dataset: { canAutoWrap: l.id } }, autoIn, " Automática")),
        field("period", "Periodo (s, 0,3–10)", h("input", { class: "input", id: id("period"), type: "number", min: "0.3", max: "10", step: "any", inputmode: "decimal",
          value: String(an.period ?? P.period), disabled: dis, dataset: dd("period") })),
        field("phase", "Desfase (0–1)", h("input", { class: "input", id: id("phase"), type: "number", min: "0", max: "1", step: "any", inputmode: "decimal",
          value: String(an.phase ?? 0), disabled: dis, dataset: dd("phase") })),
        field("copies", "Copias (1–5)", h("input", { class: "input", id: id("copies"), type: "number", min: "1", max: "5", step: "1", inputmode: "numeric",
          value: String(an.copies ?? 3), disabled: dis, dataset: dd("copies") }), { hidden: !fall, dataset: { canCopiesWrap: l.id } })),
      h("p", { class: "muted t-xs" }, "Se ve en vivo en el escenario y se guarda al dejar de editar."));
  }

  if (mode === "frames") {
    if (type === "frames") {
      const host = h("div", { class: "comp-athumb", role: "img", "aria-label": "Vista previa de los frames de «" + l.name + "»" });
      const sum = compFramesAnim(l);
      if (sum) createSpritePlayer(host, sum, { autoplay: !reducedMotion() });
      else host.append(h("span", { class: "muted t-xs" }, "Cargando…"));
      body.push(
        h("div", { class: "comp-arow" }, host,
          h("div", { class: "comp-lacts" },
            h("button", { type: "button", class: "btn btn-sm", dataset: { ca: "aopen", lid: l.id, fk: k("aopen") } }, svgUse("i-image"), "Abrir en la galería"),
            h("button", { type: "button", class: "btn btn-sm", disabled: lock, dataset: { ca: "aremove", lid: l.id, fk: k("aremove") } }, svgUse("i-trash"), "Quitar"))));
    } else {
      const dr = compDraft(l.id);
      body.push(
        h("div", {}, h("label", { class: "label", for: "comp-an-action-" + l.id }, "Acción (1–500)"),
          h("textarea", { class: "input", id: "comp-an-action-" + l.id, rows: "2", maxlength: "500", disabled: dis, value: dr.action,
            placeholder: "p. ej. parpadea y mueve la cabeza", dataset: { cdraft: "action", lid: l.id, fk: k("action") } })),
        h("div", {}, h("label", { class: "label", for: "comp-an-grid-" + l.id }, "Rejilla de la hoja"),
          h("select", { class: "input", id: "comp-an-grid-" + l.id, disabled: dis, dataset: { cdraft: "grid", lid: l.id, fk: k("grid") } },
            ...[["4x2", "4×2 (8 frames)"], ["3x3", "3×3 (9 frames)"], ["2x2", "2×2 (4 frames)"]].map(([v, t]) => h("option", { value: v, selected: v === dr.grid }, t)))),
        h("div", { class: "comp-lacts" },
          h("button", { type: "button", class: "btn btn-sm", disabled: dis, dataset: { ca: "frames", lid: l.id, fk: k("frames") } }, svgUse("i-film"), "Generar frames (1 imagen)")));
    }
  }

  if (mode === "states") {
    const sts = type === "states" && Array.isArray(an.states) ? an.states : [];
    const dr = compDraft(l.id), full = sts.length >= 6;
    const item = (src, label, rm) => h("li", { class: "comp-state" },
      h("span", { class: "comp-thumb" }, h("img", { src, alt: "", loading: "lazy", decoding: "async" })), h("span", { class: "comp-state-name t-sm" }, label), rm);
    const list = h("ol", { class: "comp-states", "aria-label": "Estados de «" + l.name + "»" },
      item(l.image_url, "Estado 0 · imagen de la capa", null),
      ...sts.map((s, i) => item(compUrl(d.id, `/layers/${enc}/states/${i + 1}.png`) + (s && s.rev != null ? "?rev=" + encodeURIComponent(s.rev) : ""), "Estado " + (i + 1),
        h("button", { type: "button", class: "btn btn-sm", disabled: lock, "aria-label": `Quitar el estado ${i + 1} de «${l.name}»`,
          dataset: { ca: "sdel", lid: l.id, k: String(i + 1), fk: k("sdel" + (i + 1)) } }, svgUse("i-trash"), "Quitar"))));
    body.push(list);
    if (type === "states") {
      body.push(h("div", { class: "comp-adj-grid" },
        field("hold", "Cada estado (s, 0,2–10)", h("input", { class: "input", id: "comp-an-hold-" + l.id, type: "number", min: "0.2", max: "10", step: "any", inputmode: "decimal",
          value: String(an.hold ?? 0.8), disabled: dis, dataset: { cst: "hold", lid: l.id, fk: k("hold") } })),
        field("loop", "Repetición", h("select", { class: "input", id: "comp-an-loop-" + l.id, disabled: dis, dataset: { cst: "loop", lid: l.id, fk: k("loopsel") } },
          ...[["loop", "Bucle"], ["pingpong", "Ida y vuelta"], ["once", "Una vez"]].map(([v, t]) => h("option", { value: v, selected: v === (an.loop || "loop") }, t))))));
    }
    body.push(
      h("div", {}, h("label", { class: "label", for: "comp-an-prompt-" + l.id }, "Nuevo estado (1–2000)"),
        h("textarea", { class: "input", id: "comp-an-prompt-" + l.id, rows: "2", maxlength: "2000", disabled: dis || full, value: dr.prompt,
          placeholder: "p. ej. el tarro lleno de monedas hasta arriba", dataset: { cdraft: "prompt", lid: l.id, fk: k("prompt") } })),
      h("div", { class: "comp-lacts" },
        h("button", { type: "button", class: "btn btn-sm", disabled: dis || full, dataset: { ca: "sadd", lid: l.id, fk: k("sadd") } }, svgUse("i-plus"), "Añadir estado…")),
      h("p", { class: "muted t-xs" }, full ? "Ya tienes los 6 estados que admite una capa." : "Cada estado gasta 1 imagen de tu cuota. Máximo 6."));
  }

  const modes = h("div", { class: "pills comp-modes", role: "group", "aria-label": "Tipo de animación de «" + l.name + "»" },
    ...COMP_MODES.map(([m, label]) => h("button", { type: "button", class: "pill", "aria-pressed": String(mode === m), disabled: dis,
      dataset: { ca: "amode", lid: l.id, mode: m, fk: k("m-" + m) } }, label)));
  const open = comp.openAnim.has(l.id) ? comp.openAnim.get(l.id) : type !== "static";
  return h("details", { class: "comp-adj comp-anim", open, dataset: { alid: l.id } },
    h("summary", { dataset: { fk: k("asum") } }, "Animación" + (type !== "static" ? " · " + COMP_MODE_LABEL[type] : "")),
    h("div", { class: "comp-adj-body" }, modes, ...body));
}


function compLayerRow(d, l, i) {
  const n = d.layers.length, bg = l.role === "background";
  const lock = compBusy(l.status) || l.status === "pending";
  const k = s => l.id + ":" + s;
  const eye = bg ? h("span", { class: "comp-eye-sp", "aria-hidden": "true" })
    : h("button", { type: "button", class: "btn icon-btn comp-eye", "aria-pressed": String(!!l.visible), "aria-label": "Mostrar «" + l.name + "»",
        title: l.visible ? "Ocultar capa" : "Mostrar capa", dataset: { ca: "eye", lid: l.id, fk: k("eye") } }, svgUse(l.visible ? "i-eye" : "i-eye-off"));
  const thumb = h("span", { class: "comp-thumb" }, l.image_url ? h("img", { src: l.image_url, alt: "", loading: "lazy", decoding: "async" }) : svgUse("i-image"));
  const status = l.status === "error" ? "Error · " + compErrText(l.error) : (COMP_STATUS[l.status] || l.status);
  const move = (dir, label, ok) => h("button", { type: "button", class: "btn icon-btn comp-move comp-" + dir, "aria-label": `${label} «${l.name}»`, title: label,
    disabled: !ok, dataset: { ca: dir, lid: l.id, fk: k(dir) } }, svgUse("i-chev"));
  const head = h("div", { class: "comp-lhead" }, eye, thumb,
    h("div", { class: "comp-linfo" }, h("b", {}, l.name), h("span", { class: "comp-lstatus t-xs" }, status)),
    bg ? null : h("div", { class: "comp-moves" }, move("up", "Subir", i < n - 1), move("down", "Bajar", i > 1)));
  const warn = l.status === "done" && l.error
    ? h("p", { class: "comp-warn t-xs", role: "status" }, svgUse("i-alert"), "Aviso: " + compErrText(l.error)) : null;
  const acts = h("div", { class: "comp-lacts" },
    h("button", { type: "button", class: "btn btn-sm", disabled: lock, dataset: { ca: "regen", lid: l.id, fk: k("regen") } }, svgUse("i-reuse"), "Regenerar"),
    h("button", { type: "button", class: "btn btn-sm", disabled: lock, dataset: { ca: "fix", lid: l.id, fk: k("fix") } }, svgUse("i-edit"), "Corregir…"));
  let adj = null;
  if (!bg) {
    const off = l.offset || [0, 0], dis = lock || !l.image_url;
    const field = (key, label, val, min, max) => h("div", {},
      h("label", { class: "label", for: `comp-${key}-${l.id}` }, label),
      h("input", { class: "input", id: `comp-${key}-${l.id}`, type: "number", min: String(min), max: String(max), step: "1", inputmode: "numeric",
        value: String(val), disabled: dis, dataset: { cadj: key, lid: l.id, fk: k(key) } }));
    const other = l.key_color === "magenta" ? "green" : "magenta";
    adj = h("details", { class: "comp-adj", open: comp.open.has(l.id), dataset: { lid: l.id } },
      h("summary", { dataset: { fk: k("sum") } }, "Ajustes"),
      h("div", { class: "comp-adj-body" },
        field("tol", "Tolerancia del recorte (5–80)", l.tolerance, 5, 80),
        h("div", { class: "comp-adj-grid" }, field("dx", "Desplazar X (px)", off[0], -200, 200), field("dy", "Desplazar Y (px)", off[1], -200, 200)),
        h("p", { class: "muted t-xs" }, "Cambiar la tolerancia vuelve a recortar la pieza desde su imagen original."),
        h("div", { class: "comp-lacts" },
          h("button", { type: "button", class: "btn btn-sm", disabled: dis || (!off[0] && !off[1]), dataset: { ca: "reset", lid: l.id, fk: k("reset") } }, "Restablecer a 0"),
          h("button", { type: "button", class: "btn btn-sm", disabled: lock, dataset: { ca: "key", lid: l.id, key: other, fk: k("key") } },
            svgUse("i-reuse"), other === "magenta" ? "Regenerar con fondo magenta" : "Regenerar con fondo verde")),
        h("p", { class: "muted t-xs" }, "Regenerar gasta 1 imagen de tu cuota. Úsalo si el recorte se come parte de la pieza.")));
  }
  const anim = bg ? null : compAnimBlock(d, l, lock);
  return h("li", { class: "comp-layer", dataset: { state: l.status, lid: l.id } }, head, warn, acts, adj, anim);
}

function paintCompEditor() {
  const d = comp.data, view = $("#comp-edit-view");
  if (!d) return;
  const act = document.activeElement, inView = !!act && view.contains(act);
  const fk = inView ? act.dataset.fk : null;
  const typed = inView && act.id === "comp-name" ? { v: act.value, a: act.selectionStart, b: act.selectionEnd } : null;
  setTitle(d.name);

  const masterErr = d.master && d.master.status === "error";
  const bar = h("div", { class: "comp-bar" },
    h("button", { type: "button", class: "btn", dataset: { nav: "composiciones" } }, svgUse("i-prev"), "Composiciones"),
    h("div", { class: "comp-name" },
      h("label", { class: "sr", for: "comp-name" }, "Nombre de la composición"),
      h("input", { class: "input", id: "comp-name", maxlength: "80", autocomplete: "off", value: typed ? typed.v : d.name, dataset: { fk: "name" } })),
    h("div", { class: "comp-acts" },
      masterErr ? h("button", { type: "button", class: "btn", dataset: { ca: "retry", fk: "retry" } }, svgUse("i-reuse"), "Reintentar") : null,
      h("button", { type: "button", class: "btn danger", dataset: { ca: "delete", fk: "delete" } }, svgUse("i-trash"), "Borrar composición")));

  const mst = d.master ? d.master.status : "done";
  const notice = masterErr
    ? h("div", { class: "anotice", role: "status" }, h("p", {}, "La imagen completa falló: " + (compErrText(d.master.error) || "error desconocido") + " Pulsa «Reintentar» para generarla de nuevo."))
    : (mst === "queued" || mst === "running")
      ? h("p", { class: "muted t-sm", role: "status" }, "Generando la imagen completa. Las capas empiezan cuando termine.") : null;

  const viewBtn = (orig, label) => h("button", { type: "button", class: "pill", "aria-pressed": String(comp.showOriginal === orig),
    disabled: orig && !d.master_url, dataset: { ca: "view", orig: orig ? "1" : "0", fk: orig ? "view-orig" : "view-comp" } }, label);
  const animated = d.layers.some(l => l.visible && compAnType(l) !== "static");
  const play = h("button", { type: "button", id: "comp-play", class: "btn icon-btn", "aria-pressed": "false", "aria-label": "Reproducir animación", title: "Reproducir animación",
    disabled: comp.showOriginal || !animated, dataset: { ca: "play", fk: "play" } }, svgUse("i-play"));
  const exportBox = h("div", { class: "comp-export" },
    h("a", { class: "btn", href: compUrl(d.id, "/export.zip"), download: "", dataset: { fk: "export" } }, svgUse("i-download"), "Exportar web (.zip)"),
    h("button", { type: "button", class: "btn", "aria-busy": comp.webpBusy ? "true" : null, "aria-disabled": comp.webpBusy ? "true" : null, "aria-describedby": "comp-webp-note",
      dataset: { ca: "webp", fk: "webp" } }, svgUse("i-download"), comp.webpBusy ? "Generando WebP…" : "Exportar WebP animado"),
    h("p", { class: "muted t-xs", id: "comp-webp-note" }, "En el WebP las velocidades pueden ajustarse hasta ±25 % para que el bucle cierre."));
  const left = h("div", { class: "comp-stage-wrap" },
    h("div", { class: "comp-viewbar" },
      h("div", { class: "pills comp-view", role: "group", "aria-label": "Vista" }, viewBtn(false, "Composición"), viewBtn(true, "Original")), play),
    compStage(d), notice, exportBox);

  const rows = d.layers.map((l, i) => compLayerRow(d, l, i)).reverse();
  const right = h("aside", { class: "comp-layers", "aria-label": "Capas" },
    h("h2", { class: "comp-h" }, "Capas"),
    h("ol", { class: "comp-llist" }, ...rows));

  view.replaceChildren(bar, h("div", { class: "comp-edit" }, left, right));
  if (fk) {
    const t = view.querySelector(`[data-fk="${CSS.escape(fk)}"]`);
    if (t && !t.disabled) {
      t.focus({ preventScroll: true });
      if (typed && t.id === "comp-name") t.setSelectionRange(typed.a, typed.b);
    }
  }
}

// Llama a la API y repinta; un error se muestra y el editor se recarga igualmente (el estado real manda). Devuelve true si salió bien.
async function compCall(method, path, body, okMsg) {
  if (comp.busy) return false;
  comp.busy = true;
  let ok = false;
  try {
    await api.send(method, path, body);
    ok = true;
    if (okMsg) toast(okMsg);
  } catch (e) {
    toast(errMsg(e), "err");
    if (e && e.code === "unknown_composition") { comp.busy = false; setHash("#composiciones"); return false; }
  } finally { comp.busy = false; }
  if (comp.id && !(ok && method === "DELETE")) await loadCompEditor(true);
  return ok;
}

async function compAct(act, lid, btn) {
  const d = comp.data;
  if (!d) return;
  const i = d.layers.findIndex(x => x.id === lid), l = d.layers[i];
  const lbase = compUrl(d.id, "/layers/" + encodeURIComponent(lid));
  if (act === "view") { comp.showOriginal = btn.dataset.orig === "1"; paintCompEditor(); return; }
  if (act === "play") {
    const p = comp.stage && comp.stage.player;
    if (!p) return;
    if (p.playing) { p.pause(); comp.wantPlay = false; } else { p.play(); comp.wantPlay = true; }
    syncCompPlay();
    return;
  }
  if (act === "webp") { await compWebp(d); return; }
  if (act === "retry") { await compCall("POST", compUrl(d.id, "/retry"), undefined, "Reintentando la imagen completa."); return; }
  if (act === "delete") {
    if (!await confirmDlg({ title: "¿Borrar la composición?", text: `Se borrarán «${short(d.name, 40)}» y sus capas. Las imágenes que se generaron se quedan en la galería.`, ok: "Borrar composición" })) return;
    const r = await compCall("DELETE", compUrl(d.id));
    if (r) { toast("Composición borrada."); comp.id = null; comp.data = null; comp.sig = ""; comp.listSig = ""; setHash("#composiciones"); }
    return;
  }
  if (!l) return;
  if (act === "amode") { await compSetMode(l, btn.dataset.mode); return; }
  if (act === "aopen") {
    const [gen, gi] = String((l.animation || {}).image || "").split("#");
    if (gen && gi !== undefined) setHash(`#imagen/${gen}/${gi}`);
    return;
  }
  if (act === "aremove") {
    comp.mode.delete(l.id);
    await compCall("PATCH", lbase, { animation: { type: "static" } });
    return;
  }
  if (act === "frames") {
    const dr = compDraft(l.id), action = dr.action.trim();
    if (!action || action.length > 500) { toast("Describe la acción: de 1 a 500 caracteres.", "err"); return; }
    if (!await confirmDlg({ title: `¿Generar frames para «${short(l.name, 40)}»?`, text: `Se generará una hoja de frames (${dr.grid}) con la acción indicada. Gasta 1 imagen de tu cuota.`, ok: "Generar frames (1 imagen)", danger: false, icon: "i-film" })) return;
    if (await compCall("POST", lbase + "/frames", { action, grid: dr.grid }, `Generando frames de «${short(l.name, 30)}».`)) dr.action = "";
    return;
  }
  if (act === "sadd") {
    const dr = compDraft(l.id), prompt = dr.prompt.trim();
    if (!prompt || prompt.length > 2000) { toast("Describe el estado: de 1 a 2000 caracteres.", "err"); return; }
    if (!await confirmDlg({ title: `¿Añadir un estado a «${short(l.name, 40)}»?`, text: "Se generará la imagen del estado a partir del anterior. Gasta 1 imagen de tu cuota.", ok: "Añadir estado (1 imagen)", danger: false, icon: "i-plus" })) return;
    if (await compCall("POST", lbase + "/states", { prompt }, `Generando el estado de «${short(l.name, 30)}».`)) dr.prompt = "";
    return;
  }
  if (act === "sdel") {
    const sk = Number(btn.dataset.k);
    if (!await confirmDlg({ title: `¿Quitar el estado ${sk}?`, text: `Se quita de «${short(l.name, 40)}». La imagen del estado se pierde de la composición.`, ok: "Quitar estado" })) return;
    if (await compCall("DELETE", lbase + "/states/" + sk)) await loadCompEditor(true);
    return;
  }
  if (act === "eye") { await compCall("PATCH", lbase, { visible: !l.visible }); return; }
  if (act === "up" || act === "down") {
    const ids = d.layers.map(x => x.id), j = act === "up" ? i + 1 : i - 1;
    if (i < 1 || j < 1 || j >= ids.length) return;
    [ids[i], ids[j]] = [ids[j], ids[i]];
    await compCall("PATCH", compUrl(d.id), { order: ids });
    return;
  }
  if (act === "reset") { await compCall("PATCH", lbase, { offset: [0, 0] }); return; }
  if (act === "fix") { openLayerEdit(l); return; }
  if (act === "regen" || act === "key") {
    const key = act === "key" ? btn.dataset.key : null;
    const how = key ? ` con fondo ${key === "magenta" ? "magenta" : "verde"}` : "";
    if (!await confirmDlg({ title: `¿Regenerar «${short(l.name, 40)}»?`, text: `Se volverá a generar la capa${how}. Gasta 1 imagen de tu cuota.`, ok: "Regenerar", danger: false, icon: "i-reuse" })) return;
    await compCall("POST", lbase + "/regenerate", key ? { key_color: key } : {}, `Regenerando «${short(l.name, 30)}».`);
  }
}

async function compAdjust(input) {
  const d = comp.data, lid = input.dataset.lid;
  const l = d && d.layers.find(x => x.id === lid);
  if (!l) return;
  const inR = (v, a, b) => Number.isInteger(v) && v >= a && v <= b;
  const read = key => { const el = $(`[data-cadj="${key}"][data-lid="${CSS.escape(lid)}"]`, $("#comp-edit-view")); return el ? Number(el.value) : NaN; };
  const base = compUrl(d.id, "/layers/" + encodeURIComponent(lid));
  if (input.dataset.cadj === "tol") {
    const v = read("tol");
    if (!inR(v, 5, 80)) { toast("La tolerancia va de 5 a 80.", "err"); input.value = l.tolerance; return; }
    if (v === l.tolerance) return;
    await compCall("PATCH", base, { tolerance: v });
    return;
  }
  const dx = read("dx"), dy = read("dy"), off = l.offset || [0, 0];
  if (!inR(dx, -200, 200) || !inR(dy, -200, 200)) {
    toast("El desplazamiento va de −200 a 200 píxeles.", "err");
    const x = $(`[data-cadj="dx"][data-lid="${CSS.escape(lid)}"]`), y = $(`[data-cadj="dy"][data-lid="${CSS.escape(lid)}"]`);
    if (x) x.value = off[0]; if (y) y.value = off[1];
    return;
  }
  if (dx === off[0] && dy === off[1]) return;
  await compCall("PATCH", base, { offset: [dx, dy] });
}

async function compRename(input) {
  const d = comp.data;
  if (!d) return;
  const v = input.value.trim();
  if (!v) { input.value = d.name; toast("El nombre no puede estar vacío.", "err"); return; }
  if (v === d.name) { input.value = v; return; }
  await compCall("PATCH", compUrl(d.id), { name: v });
}

/* animación: cambio de tipo, movimiento en vivo, estados, WebP */
async function compSetMode(l, mode) {
  const cur = compAnType(l), eff = compMode(l);
  if (mode === eff) return;
  const lbase = compUrl(comp.id, "/layers/" + encodeURIComponent(l.id));
  const n = Array.isArray(l.animation && l.animation.states) ? l.animation.states.length : 0;
  const drops = (cur === "frames" && mode !== "frames") || (cur === "states" && mode !== "states");
  if (drops && !await confirmDlg({
    title: cur === "frames" ? "¿Quitar los frames?" : "¿Quitar los estados?",
    text: cur === "frames" ? `Cambiar a «${COMP_MODE_LABEL[mode]}» quita los frames de «${short(l.name, 40)}». La hoja se queda en la galería.`
      : `Cambiar a «${COMP_MODE_LABEL[mode]}» quita ${n === 1 ? "el estado guardado" : `los ${n} estados guardados`} de «${short(l.name, 40)}».`,
    ok: "Cambiar" })) return;
  const prev = comp.mode.get(l.id);
  let body = null;
  if (mode === "static") { if (cur !== "static") body = { type: "static" }; comp.mode.delete(l.id); }
  else if (mode === "motion") { if (cur !== "motion") body = { type: "motion", preset: "bob" }; comp.mode.delete(l.id); }
  else {
    if (cur === mode) comp.mode.delete(l.id);
    else { comp.mode.set(l.id, mode); if (cur === "frames" || cur === "states") body = { type: "static" }; }
  }
  if (!body) { paintCompEditor(); return; }
  if (!await compCall("PATCH", lbase, { animation: body })) {
    if (prev) comp.mode.set(l.id, prev); else comp.mode.delete(l.id);
    paintCompEditor();
  }
}

function compPreviewMotion(l, a) {
  const st = comp.stage, sl = st && st.scene && st.scene.layers.find(x => x.id === l.id);
  if (!sl || !window.MusaScene) return;
  const { x, y, w, h: hh } = sl;
  sl.animation = { type: "motion", preset: a.preset, amplitude: a.amplitude == null ? Math.floor(1.2 * hh + 0.5) : a.amplitude, period: a.period, phase: a.phase,
    pivot: a.preset === "sway" ? [x + w / 2, y + hh] : a.preset === "pulse" ? [x + w / 2, y + hh / 2] : [0, 0] };
  if (a.preset === "fall") sl.animation.copies = a.copies;
  // varias teclas seguidas se agrupan en un solo remontaje por fotograma
  if (st.raf) return;
  st.raf = requestAnimationFrame(() => { st.raf = 0; if (comp.stage === st && st.scene) compMountScene(st, st.scene); });
}

function compMotionInput(el) {
  const d = comp.data, lid = el.dataset.lid, l = d && d.layers.find(x => x.id === lid);
  if (!l) return;
  const root = $("#comp-edit-view");
  const q = key => $(`[data-can="${key}"][data-lid="${CSS.escape(lid)}"]`, root);
  const f = { preset: q("preset"), auto: q("auto"), amp: q("amp"), period: q("period"), phase: q("phase"), copies: q("copies") };
  if (!f.preset || !f.amp) return;
  const wrap = key => $(`[data-${key}="${CSS.escape(lid)}"]`, root);
  if (el === f.preset) {
    const P = MOTION[f.preset.value];
    if (!P) return;
    const fall = f.preset.value === "fall";
    f.auto.checked = fall;
    f.amp.min = String(P.min); f.amp.max = String(P.max);
    f.amp.value = String(fall ? compAutoAmp(l) : P.amp);
    f.amp.disabled = fall;
    f.period.value = String(P.period);
    wrap("can-auto-wrap").hidden = !fall;
    wrap("can-copies-wrap").hidden = !fall;
    wrap("can-unit").textContent = P.unit ? "(" + P.unit + ")" : "";
  } else if (el === f.auto) {
    f.amp.disabled = f.auto.checked;
    if (f.auto.checked) f.amp.value = String(compAutoAmp(l));
  }
  const preset = f.preset.value, P = MOTION[preset], fall = preset === "fall", auto = fall && f.auto.checked;
  const rd = inp => inp.value.trim() === "" ? NaN : Number(inp.value);
  const inR = (v, a, b) => Number.isFinite(v) && v >= a && v <= b;
  const a = { type: "motion", preset, amplitude: auto ? null : rd(f.amp), period: rd(f.period), phase: rd(f.phase) };
  if (fall) a.copies = rd(f.copies);
  const checks = [[f.amp, auto || inR(a.amplitude, P.min, P.max)], [f.period, inR(a.period, 0.3, 10)], [f.phase, inR(a.phase, 0, 1)],
    [f.copies, !fall || (Number.isInteger(a.copies) && inR(a.copies, 1, 5))]];
  let ok = true;
  for (const [inp, v] of checks) { inp.setAttribute("aria-invalid", v ? "false" : "true"); if (!v) ok = false; }
  clearTimeout(comp.mT);
  if (!ok) { comp.mPending = null; return; }
  comp.mPending = { cid: comp.id, lid, body: a };
  comp.mT = setTimeout(compFlushMotion, 400);
  compPreviewMotion(l, a);
}

async function compFlushMotion() {
  const p = comp.mPending;
  if (!p) return;
  if (comp.busy) { comp.mT = setTimeout(compFlushMotion, 200); return; }
  comp.mPending = null;
  if (p.cid !== comp.id) return;
  await compCall("PATCH", compUrl(p.cid, "/layers/" + encodeURIComponent(p.lid)), { animation: p.body });
}

async function compStatesAdjust(el) {
  const d = comp.data, lid = el.dataset.lid, l = d && d.layers.find(x => x.id === lid);
  if (!l) return;
  const root = $("#comp-edit-view");
  const hold = $(`[data-cst="hold"][data-lid="${CSS.escape(lid)}"]`, root), loop = $(`[data-cst="loop"][data-lid="${CSS.escape(lid)}"]`, root);
  const hv = hold.value.trim() === "" ? NaN : Number(hold.value);
  if (!(hv >= 0.2 && hv <= 10)) { toast("Cada estado dura de 0,2 a 10 segundos.", "err"); hold.value = String((l.animation && l.animation.hold) ?? 0.8); return; }
  await compCall("PATCH", compUrl(d.id, "/layers/" + encodeURIComponent(lid)), { animation: { type: "states", hold: hv, loop: loop.value } });
}

async function compWebp(d) {
  if (comp.webpBusy) return;
  comp.webpBusy = true;
  paintCompEditor();
  try {
    const r = await doFetch(compUrl(d.id, "/scene.webp"), { credentials: "same-origin" });
    if (!r.ok) throw await parseBody(r);
    const blob = await r.blob();
    const m = /filename="?([^";]+)"?/.exec(r.headers.get("content-disposition") || "");
    const url = URL.createObjectURL(blob), a = h("a", { href: url, download: m ? m[1] : "scene.webp" });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    toast("WebP animado descargado.");
  } catch (e) { toast(errMsg(e), "err"); }
  finally { comp.webpBusy = false; if (comp.data) paintCompEditor(); }
}

// Los eventos «jobs» llegan cuando cambia el estado de la cola: el editor abierto o la lista se recargan.
function compJobsTick() {
  const r = state.route;
  if (!r || r.name !== "composiciones") return;
  clearTimeout(comp.timer);
  comp.timer = setTimeout(() => {
    const cur = state.route;
    if (!cur || cur.name !== "composiciones") return;
    if (cur.id) { if (cur.id === comp.id) loadCompEditor(); } else loadCompList();
  }, 500);
}

/* corregir capa */
const le = { cid: null, lid: null };
function openLayerEdit(l) {
  le.cid = comp.id; le.lid = l.id;
  $("#le-title").textContent = `Corregir «${short(l.name, 40)}»`;
  $("#le-prompt").value = "";
  $("#le-ok").disabled = true;
  $("#layer-edit-dialog").showModal();
  $("#le-prompt").focus();
}
async function submitLayerEdit(e) {
  e.preventDefault();
  const prompt = $("#le-prompt").value.trim();
  if (!prompt || prompt.length > 2000 || !le.cid) return;
  const btn = $("#le-ok");
  btn.disabled = true;
  const r = await compCall("POST", compUrl(le.cid, "/layers/" + encodeURIComponent(le.lid) + "/correct"), { prompt }, "Corrección enviada.");
  if (!r) { btn.disabled = false; return; }
  $("#layer-edit-dialog").close();
}

/* nueva composición */
function readCompForm() {
  const pieces = $("#cd-pieces").value.split("\n").map(s => s.trim()).filter(Boolean);
  const prompt = $("#cd-prompt").value.trim();
  let err = "";
  if (!pieces.length) err = "Escribe de 1 a 6 piezas, una por línea.";
  else if (pieces.length > 6) err = `Máximo 6 piezas (llevas ${pieces.length}).`;
  else if (new Set(pieces.map(p => p.toLowerCase())).size !== pieces.length) err = "Hay piezas repetidas.";
  else if (pieces.some(p => p.length > 80)) err = "Cada pieza admite hasta 80 caracteres.";
  return { pieces, prompt, err, valid: !!prompt && !err };
}
function syncCompDialog() {
  const f = readCompForm(), refs = state.cmp.refs.length;
  $("#cd-pieces-count").textContent = f.err || (f.pieces.length ? plural(f.pieces.length, "pieza", "piezas") + "." : "");
  $("#cd-pieces-count").classList.toggle("err", !!f.err && !!$("#cd-pieces").value.trim());
  $("#cd-pieces").setAttribute("aria-invalid", String(!!f.err && !!$("#cd-pieces").value.trim()));
  $("#cd-refs-txt").textContent = `Usar las referencias del compositor (${refs})`;
  $("#cd-refs").disabled = !refs;
  if (!refs) $("#cd-refs").checked = false;
  $("#cd-note").textContent = `Gastará ${2 + f.pieces.length} imágenes de tu cuota.`;
  $("#cd-ok").disabled = !f.valid || cdBusy;
}
let cdBusy = false;
async function openCompDialog() {
  closePops();
  try { await ensureStyles(); } catch (e) {}
  $("#cd-form").reset();
  $("#cd-ratio").replaceChildren(...RATIOS.map(x => h("option", { value: x.r, selected: x.r === "4:3" }, `${x.r} · ${x.n}`)));
  $("#cd-model").replaceChildren(...state.models.map(m => h("option", { value: m.id, selected: m.id === state.cmp.model }, modelLabel(m.id))));
  $("#cd-style").replaceChildren(h("option", { value: "", selected: !state.style }, "Sin estilo"),
    ...state.styles.map(s => h("option", { value: s.id, selected: s.id === state.style }, s.name)));
  cdBusy = false;
  syncCompDialog();
  $("#comp-dialog").showModal();
  $("#cd-name").focus();
}
async function submitComp(e) {
  e.preventDefault();
  if (cdBusy) return;
  const f = readCompForm();
  if (!f.valid) { (f.prompt ? $("#cd-pieces") : $("#cd-prompt")).focus(); return; }
  const cost = 2 + f.pieces.length;
  if (!await confirmDlg({ title: "¿Crear la composición?", text: `Se generarán la imagen completa, el fondo y ${plural(f.pieces.length, "pieza", "piezas")}. Gastará ${cost} imágenes de tu cuota.`,
    ok: "Crear composición", danger: false, icon: "i-compose" })) return;
  const body = { prompt: f.prompt, pieces: f.pieces, ratio: $("#cd-ratio").value };
  const name = $("#cd-name").value.trim(), model = $("#cd-model").value, style = $("#cd-style").value;
  if (name) body.name = name;
  if (model && model !== "auto") body.model = model;
  if (style) body.style = style;
  if ($("#cd-refs").checked && state.cmp.refs.length) body.refs = state.cmp.refs.slice(0, MAX_REFS).map(r => r.kind === "upload" ? { upload: r.id } : { gen: r.ref });
  cdBusy = true; syncCompDialog();
  try {
    const c = await api.send("POST", "/api/compositions", body);
    $("#comp-dialog").close();
    toast("Composición creada. Generando la imagen completa.");
    loadJobs();
    setHash("#composiciones/" + c.id);
  } catch (err) { toast(errMsg(err), "err"); }
  finally { cdBusy = false; if ($("#comp-dialog").open) syncCompDialog(); }
}

function wireComp() {
  const dlg = $("#comp-dialog"), led = $("#layer-edit-dialog"), view = $("#comp-edit-view");
  $("#new-comp-btn").addEventListener("click", openCompDialog);
  $("#comp-grid").addEventListener("click", e => { const c = e.target.closest("[data-comp]"); if (c) setHash("#composiciones/" + c.dataset.comp); });
  $("#cd-form").addEventListener("submit", submitComp);
  $("#cd-form").addEventListener("input", syncCompDialog);
  $("#cd-form").addEventListener("change", syncCompDialog);
  $("#cd-cancel").addEventListener("click", () => dlg.close());
  dlg.addEventListener("click", e => { if (e.target === dlg) dlg.close(); });
  $("#le-form").addEventListener("submit", submitLayerEdit);
  $("#le-prompt").addEventListener("input", () => { $("#le-ok").disabled = !$("#le-prompt").value.trim(); });
  $("#le-cancel").addEventListener("click", () => led.close());
  led.addEventListener("click", e => { if (e.target === led) led.close(); });
  view.addEventListener("click", e => { const b = e.target.closest("[data-ca]"); if (b && !b.disabled) compAct(b.dataset.ca, b.dataset.lid, b); });
  view.addEventListener("input", e => {
    const t = e.target;
    if (t.dataset.can) compMotionInput(t);
    else if (t.dataset.cdraft) compDraft(t.dataset.lid)[t.dataset.cdraft] = t.value;
  });
  view.addEventListener("change", e => {
    if (e.target.id === "comp-name") compRename(e.target);
    else if (e.target.dataset.cadj) compAdjust(e.target);
    else if (e.target.dataset.cst) compStatesAdjust(e.target);
  });
  view.addEventListener("keydown", e => { if (e.key === "Enter" && e.target.id === "comp-name") { e.preventDefault(); e.target.blur(); } });
  view.addEventListener("toggle", e => {
    const t = e.target;
    if (t.tagName !== "DETAILS" || !t.dataset) return;
    if (t.dataset.lid) { if (t.open) comp.open.add(t.dataset.lid); else comp.open.delete(t.dataset.lid); }
    else if (t.dataset.alid) comp.openAnim.set(t.dataset.alid, t.open);
  }, true);
}

/* ─── SSE: refresco ante cambios ─── */
function connectSSE() {
  let t = 0, tp = 0, last = null, needPrompts = false;   // `last`: último payload de «changed» (mtimes por almacén)
  const reloadPrompts = () => { if (Date.now() - promptsLocalAt < 2500) return; clearTimeout(tp); tp = setTimeout(loadPrompts, 150); };
  const refresh = promptsToo => {
    clearTimeout(t);
    needPrompts = needPrompts || promptsToo;
    t = setTimeout(async () => {
      if (needPrompts) { needPrompts = false; loadPrompts(); }
      try { const s = await api.get("/api/state"); state.counts = s.counts || {}; updateCounts(); } catch (e) {}
      try { state.collections = await api.get("/api/collections"); renderDest(); } catch (e) {}
      loadQuota();
      try { state.styles = await api.get("/api/styles"); state.stylesLoaded = true; updateComposer(); } catch (e) {}
      const route = state.route || parseRoute();
      try {
        if (route.name === "inicio") {
          await renderInicio();
        } else if (route.name === "estilos") {
          state.styles = await api.get("/api/styles");
          state.stylesLoaded = true;
          renderStyles();
        } else if (route.name === "limites") {
          await renderLimites();
        } else if (route.name === "colecciones") {
          if (route.id) await renderCollection(route.id); else renderCollections();
        } else if (route.name === "composiciones") {
          await renderComposiciones(route.id);
        } else {
          const keep = route.name === "imagen" ? route : null;
          const keepN = Math.min(200, Math.max(60, state.gallery.items.length));
          await loadGallery({ limit: keepN });
          renderGallery();
          if (keep) {
            const it = await findItem(keep.gen, keep.i);
            if (it) { detailItem = it; buildDetail(it); }
            else { closeDetail(); setHash(state.baseHash || "#galeria"); }
          }
        }
      } catch (e) { /* la siguiente pasada lo reintenta */ }
    }, 300);
  };
  // Si solo cambió «prompts» (p. ej. al insertar uno, que llama a /use) se recarga esa lista y nada más.
  const onChanged = ev => {
    let cur = null;
    try { cur = JSON.parse(ev.data); } catch (err) {}
    const prev = last; last = cur;
    if (!cur || !prev) { refresh(true); return; }
    const diff = [...new Set([...Object.keys(cur), ...Object.keys(prev)])].filter(k => cur[k] !== prev[k]);
    if (!diff.length) return;
    if (diff.length === 1 && diff[0] === "prompts") { reloadPrompts(); return; }
    refresh(diff.includes("prompts"));
  };
  const es = new EventSource("/api/events");
  es.addEventListener("changed", onChanged);
  es.addEventListener("jobs", e => { try { applyJobs(JSON.parse(e.data)); } catch (err) {} });
  es.onerror = () => { /* EventSource reconecta solo */ };
}

/* ─── pantalla de carga ─── */
// Obras de dominio público (Wikimedia Commons), servidas en /static/assets/arte/
const A = (f, w, h, by, work, year) => ({ src: "/static/assets/arte/" + f + ".jpg", w, h, by, work, year });
const ART = {
  starry:     A("starry", 820, 649, "Vincent van Gogh", "La noche estrellada", "1889"),
  sunflowers: A("sunflowers", 650, 820, "Vincent van Gogh", "Los girasoles", "1888"),
  wave:       A("wave", 820, 551, "Katsushika Hokusai", "La gran ola de Kanagawa", "c. 1831"),
  redfuji:    A("redfuji", 820, 547, "Katsushika Hokusai", "Fuji rojo", "c. 1831"),
  lilies:     A("lilies", 820, 787, "Claude Monet", "Nenúfares", "1906"),
  impression: A("impression", 820, 636, "Claude Monet", "Impresión, sol naciente", "1872"),
  jatte:      A("jatte", 820, 546, "Georges Seurat", "Tarde de domingo en La Grande Jatte", "1884–1886"),
  pearl:      A("pearl", 692, 820, "Johannes Vermeer", "La joven de la perla", "c. 1665"),
  milkmaid:   A("milkmaid", 731, 820, "Johannes Vermeer", "La lechera", "c. 1658"),
  venus:      A("venus", 820, 515, "Sandro Botticelli", "El nacimiento de Venus", "c. 1485"),
  monalisa:   A("monalisa", 550, 820, "Leonardo da Vinci", "La Gioconda", "c. 1503–1519"),
  wanderer:   A("wanderer", 640, 820, "Caspar David Friedrich", "El caminante sobre el mar de nubes", "c. 1818"),
  temeraire:  A("temeraire", 820, 609, "J. M. W. Turner", "El Temerario remolcado a su último atraque", "1839"),
  kiss:       A("kiss", 817, 820, "Gustav Klimt", "El beso", "1907–1908"),
  mondrian:   A("mondrian", 809, 820, "Piet Mondrian", "Composición II en rojo, azul y amarillo", "1930")
};
// Figuras: láminas «Dessein» de la Encyclopédie (Diderot y d'Alembert, 1762–1772), dominio público.
const FIGS = [
  { f: "hygieia",  w: 330, alt: "Estatua de Higía",               box: { l: -6, t: 5,  w: 112, h: 28 }, tint: "#ececec" },
  { f: "apollo",   w: 510, alt: "Apolo Pitio",                     box: { l: 44, t: 55, w: 42,  h: 36 }, tint: "#ececec" },
  { f: "hercules", w: 450, alt: "Hércules Farnesio",               box: { l: 14, t: 22, w: 66,  h: 40 }, tint: "#ececec" },
  { f: "antinous", w: 378, alt: "Antínoo" },
  { f: "juno",     w: 336, alt: "Figura drapeada con corona",      box: { l: 8,  t: 0,  w: 84,  h: 23 }, tint: "#ececec" }
];
const FIG_CREDIT = "Figuras: Encyclopédie de Diderot y d'Alembert";
const LOOP = ["wave", "starry", "pearl", "kiss", "mondrian", "lilies", "venus", "wanderer", "redfuji", "sunflowers", "monalisa", "impression", "milkmaid", "temeraire", "jatte"];
const credit = a => `${a.by} · ${a.work}, ${a.year}`;
let ldTimers = [], ldRaf = 0, ldIdx = 0;

function setBox(box, key, animate) {
  const a = ART[key], img = new Image();
  img.src = a.src; img.alt = ""; if (animate) img.className = "in";
  box.appendChild(img);
  img.addEventListener("animationend", () => { while (box.children.length > 1) box.firstElementChild.remove(); img.className = ""; });
  if (!animate) while (box.children.length > 1) box.firstElementChild.remove();
  $("#ld-credit").textContent = credit(a) + "  ·  " + FIG_CREDIT;
}
const preload = src => new Promise(res => { const i = new Image(); i.onload = i.onerror = () => res(); i.src = src; });
// opts.boot: primer arranque de la sesión (breve, espera a que los datos estén listos); sin boot es la repetición desde el logo.
function runLoader(opts = {}) {
  const ld = $("#loader"), row = $("#ld-row");
  ldTimers.forEach(clearTimeout); ldTimers = []; cancelAnimationFrame(ldRaf);
  delete document.documentElement.dataset.nols;   // la repetición desde el logo debe verse aunque la sesión ya la haya mostrado
  ld.hidden = false; ld.classList.remove("out");
  try { sessionStorage.setItem("musa-loader", "1"); } catch (e) {}
  setLoading(true);
  $("#ld-title").textContent = "Cada imagen empieza en la historia del arte.";
  row.replaceChildren(...FIGS.map(g => {
    const fig = h("div", { class: "ld-fig" });
    fig.style.aspectRatio = g.w + "/900";
    fig.append(h("img", { src: `/static/assets/figuras/${g.f}.jpg`, width: g.w, height: 900, alt: g.alt }));
    return fig;
  }));
  const boxes = FIGS.map((g, i) => [g, row.children[i]]).filter(([g]) => g.box).map(([g, fig], i) => {
    const b = h("div", { class: "ld-box" });
    const p = g.box;
    b.style.cssText = `left:${p.l}%;top:${p.t}%;width:${p.w}%;height:${p.h}%;--tint:${g.tint};animation-delay:${500 + i * 160}ms`;
    fig.appendChild(b); setBox(b, LOOP[i], false);
    return b;
  });
  ldIdx = boxes.length;
  const tick = n => {
    setBox(boxes[n % boxes.length], LOOP[ldIdx++ % LOOP.length], true);
    ldTimers.push(setTimeout(() => tick(n + 1), 680));
  };
  ldTimers.push(setTimeout(() => tick(0), 1100));
  // El progreso refleja trabajo real: láminas y cuadros precargados (y, al arrancar, la lista de modelos).
  const jobs = [...FIGS.map(g => preload(`/static/assets/figuras/${g.f}.jpg`)), ...LOOP.map(k => preload(ART[k].src))];
  if (opts.boot && opts.ready) jobs.push(Promise.resolve(opts.ready).catch(() => {}));
  let done = 0; jobs.forEach(p => p.then(() => { done++; }));
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const minMs = reduced ? 500 : (opts.boot ? 1800 : 2800), maxMs = 4500, t0 = performance.now(), bar = $("#ld-bar");
  const finish = () => {
    bar.style.width = "100%";
    $("#ld-title").textContent = "Tu estudio está listo."; $("#ld-status").textContent = "Listo";
    ldTimers.push(setTimeout(closeLoader, 450));
  };
  const step = now => {
    const el = now - t0, tp = Math.min(1, el / minMs), te = 1 - Math.pow(1 - tp, 2.2);
    bar.style.width = (Math.min(te, done / jobs.length) * 100) + "%";
    if ((tp >= 1 && done >= jobs.length) || el >= maxMs) finish();
    else ldRaf = requestAnimationFrame(step);
  };
  ldRaf = requestAnimationFrame(step);
}
function closeLoader() {
  const ld = $("#loader");
  ldTimers.forEach(clearTimeout); ldTimers = []; cancelAnimationFrame(ldRaf);
  ld.classList.add("out");
  $("#ld-status").textContent = "Listo";
  setLoading(false);
  const a = document.activeElement;
  if (!a || a === document.body || ld.contains(a)) $("#main").focus({ preventScroll: true });
  ldTimers.push(setTimeout(() => { ld.hidden = true; }, 800));
}
// Mientras carga, la app queda inerte: ni foco ni lectores de pantalla llegan a ella.
function setLoading(on) {
  $(".stage").inert = on;
  $("#skip-link").inert = on;
  if (on) { $("#ld-status").textContent = "Cargando…"; $("#ld-skip").focus(); }
}
$("#ld-skip").addEventListener("click", closeLoader);
$("#logo").addEventListener("click", runLoader);
$("#replay").addEventListener("click", runLoader);
document.addEventListener("keydown", e => { if (e.key === "Escape" && !$("#loader").hidden) closeLoader(); });

// El contenido reserva el alto del compositor (--dock-h) para que «Recientes» y el resto no queden tapados.
function watchDock() {
  const dock = $("#dock"), main = $("#main");
  const sync = () => main.style.setProperty("--dock-h", dock.offsetHeight + "px");
  new ResizeObserver(sync).observe(dock);
  sync();
}

/* ─── paleta de comandos (⌘K / Ctrl+K) ─── */
const IS_MAC = /Mac|iPhone|iPad/i.test(navigator.platform || navigator.userAgent);
const ck = { open: false, origin: null, q: "", sel: 0, flat: [], seq: 0, timer: 0, img: { q: null, items: [], error: null } };
const ckNorm = s => String(s || "").normalize("NFD").replace(/\p{M}/gu, "").toLowerCase();
// Puntuación difusa sin mayúsculas ni tildes: subcadena (mejor al inicio o en inicio de palabra); con 3+ letras también subsecuencia. -1 = no coincide.
function ckScore(q, text) {
  if (!q) return 0;
  const t = ckNorm(text);
  let total = 0;
  for (const term of q.split(/\s+/).filter(Boolean)) {
    const i = t.indexOf(term);
    if (i >= 0) { total += 100 - Math.min(i, 50) + (i === 0 || t[i - 1] === " " ? 20 : 0); continue; }
    if (term.length < 3) return -1;
    let p = 0, gaps = 0, last = -1;
    for (const ch of term) {
      const j = t.indexOf(ch, p);
      if (j < 0) return -1;
      if (last >= 0) gaps += j - last - 1;
      last = j; p = j + 1;
    }
    if (gaps > term.length) return -1;   // letras casi contiguas: evita coincidencias dispersas
    total += Math.max(1, 40 - gaps);
  }
  return total;
}
const visibleFbtn = () => $$(".fbtn").find(b => b.offsetParent !== null) || null;
async function ckToggleFilters() {
  const r = state.route;
  if (!(r && (r.name === "galeria" || (r.name === "colecciones" && r.id)))) {
    setHash("#galeria");
    for (let i = 0; i < 40 && !visibleFbtn(); i++) await new Promise(res => setTimeout(res, 50));
  }
  const b = visibleFbtn();
  if (b) toggleFilterPop(b);
}
function ckCommands() {
  const screens = [["Inicio", "i-home", "#inicio"], ["Estilos", "i-spark", "#estilos"], ["Galería", "i-grid", "#galeria"],
    ["Colecciones", "i-folder", "#colecciones"], ["Composiciones", "i-compose", "#composiciones"], ["Límites de uso", "i-gauge", "#limites"]]
    .map(([label, icon, hash]) => ({ id: "s:" + hash, icon, label, run: () => setHash(hash) }));
  screens.push({ id: "s:loader", icon: "i-play", label: "Pantalla de carga", run: () => $("#replay").click() });
  const styles = state.styles.map(s => ({ id: "y:" + s.id, icon: "i-spark", label: "Usar " + s.name, kw: s.name + " estilo",
    sub: state.style === s.id ? "Seleccionado" : (FAM_LABEL[s.family] || null),
    run: () => { setSelectMode(false); selectStyle(s.id); $("#prompt").focus(); } }));
  const colls = state.collections.map(c => ({ id: "c:" + c.id, icon: "i-folder", label: "Abrir " + c.name, kw: c.name + " coleccion",
    sub: `${c.count ?? 0} ${c.count === 1 ? "imagen" : "imágenes"}`, run: () => setHash("#colecciones/" + c.id) }));
  const actions = [
    { id: "a:style", icon: "i-plus", label: "Nuevo estilo", run: () => openStyleDialog() },
    { id: "a:coll", icon: "i-folder-plus", label: "Nueva colección", run: () => $("#new-coll-btn").click() },
    { id: "a:comp", icon: "i-compose", label: "Nueva composición", kw: "escena capas piezas fondo", run: () => openCompDialog() },
    { id: "a:prompts", icon: "i-book", label: "Prompts guardados", run: () => { setSelectMode(false); $("#prompts-btn").click(); } },
    { id: "a:filters", icon: "i-sliders", label: "Alternar filtros", kw: "filtrar ordenar modelo fecha", run: ckToggleFilters }];
  return { screens, styles, colls, actions };
}
function ckSections() {
  const q = ckNorm(ck.q).trim(), raw = ck.q.trim(), sections = [];
  const pick = (title, list, cap) => {
    let rows = list.map(it => ({ it, s: ckScore(q, [it.label, it.kw].filter(Boolean).join(" ")) })).filter(x => x.s >= 0);
    if (q) rows.sort((a, b) => b.s - a.s);
    rows = rows.slice(0, q ? 8 : cap).map(x => x.it);
    if (rows.length) sections.push({ title, items: rows });
  };
  const c = ckCommands();
  pick("Pantallas", c.screens, 6);
  pick("Estilos", c.styles, 5);
  pick("Colecciones", c.colls, 5);
  pick("Acciones", c.actions, 6);
  if (raw) {
    const ready = ck.img.q === raw;
    const items = ready ? ck.img.items.map(it => ({ id: "i:" + it.key, icon: "i-image", label: short(it.prompt, 70),
      sub: [modelName(it.model), it.style ? (it.style_name || "Estilo eliminado") : null].filter(Boolean).join(" · "),
      run: () => openDetail(it.gen_id, it.index) })) : [];
    const note = !ready ? "Buscando imágenes…" : ck.img.error;
    if (items.length || note) sections.push({ title: "Imágenes", items, note });
  }
  return sections;
}
function ckRow(it, n) {
  return h("div", { class: "ck-opt", role: "option", id: "ck-opt-" + n, "aria-selected": String(n === ck.sel), dataset: { ck: String(n) } },
    svgUse(it.icon),
    h("span", { class: "ck-txt" }, h("span", { class: "ck-lab" }, it.label), it.sub ? h("small", {}, it.sub) : null));
}
function ckRender(reset) {
  const prev = !reset && ck.flat[ck.sel] ? ck.flat[ck.sel].id : null;
  const sections = ckSections();
  ck.flat = sections.flatMap(s => s.items);
  const idx = prev ? ck.flat.findIndex(x => x.id === prev) : -1;
  ck.sel = idx >= 0 ? idx : 0;
  let n = 0;
  const kids = sections.map((s, i) => h("div", { role: "group", "aria-labelledby": "ck-h-" + i },
    h("div", { class: "ck-head", id: "ck-h-" + i }, s.title),
    ...s.items.map(it => ckRow(it, n++)),
    s.note ? h("div", { class: "ck-note" }, s.note) : null));
  if (!ck.flat.length && !sections.length) kids.push(h("div", { class: "ck-empty" }, `Sin resultados para «${ck.q.trim()}»`));
  $("#ck-list").replaceChildren(...kids);
  const input = $("#ck-input");
  input.setAttribute("aria-activedescendant", ck.flat.length ? "ck-opt-" + ck.sel : "");
  $("#ck-status").textContent = ck.flat.length ? `${ck.flat.length} ${ck.flat.length === 1 ? "resultado" : "resultados"}` : "Sin resultados";
}
function ckSelect(n, scroll = true) {
  if (!ck.flat.length) return;
  ck.sel = n;
  $$(".ck-opt").forEach(o => o.setAttribute("aria-selected", String(+o.dataset.ck === n)));
  $("#ck-input").setAttribute("aria-activedescendant", "ck-opt-" + n);
  const el = $("#ck-opt-" + n);
  if (scroll && el) el.scrollIntoView({ block: "nearest" });
}
// Imágenes: una consulta por pausa de tecleo; una respuesta vieja (seq distinto) nunca pinta.
function ckSearchImages() {
  clearTimeout(ck.timer);
  const seq = ++ck.seq, q = ck.q.trim();
  ck.img = { q: null, items: [], error: null };
  if (!q) return;
  ck.timer = setTimeout(async () => {
    try {
      const r = await api.get("/api/generations?query=" + encodeURIComponent(q) + "&limit=8");
      if (seq !== ck.seq || !ck.open) return;
      rememberItems(r.items);
      ck.img = { q, items: r.items, error: null };
    } catch (e) {
      if (seq !== ck.seq || !ck.open) return;
      ck.img = { q, items: [], error: errMsg(e) };
    }
    ckRender(false);
  }, 200);
}
function openCmdk() {
  if (ck.open || !$("#loader").hidden || $$("dialog[open]").length) return;
  closePops(); closeImgMenu(false); closeFilterPop(false);
  ck.origin = document.activeElement;
  ck.q = ""; ck.seq++; clearTimeout(ck.timer);
  ck.img = { q: null, items: [], error: null };
  $("#ck-input").value = "";
  ckRender(true);
  ck.open = true;
  $("#cmdk").showModal();
  $("#ck-input").focus();
}
function ckRun(it) {
  if (!it) return;
  $("#cmdk").close();   // devuelve el foco al origen; la acción puede moverlo después
  it.run();
}
function connectPalette() {
  const mod = IS_MAC ? "⌘K" : "Ctrl K";
  $("#ck-btn-kbd").textContent = mod;
  $("#ck-foot-kbd").textContent = mod;
  $("#ck-btn").setAttribute("aria-label", `Abrir la paleta de comandos (${IS_MAC ? "⌘K" : "Ctrl+K"})`);
  const dlg = $("#cmdk"), input = $("#ck-input"), list = $("#ck-list");
  document.addEventListener("keydown", e => {
    if (!(IS_MAC ? e.metaKey && !e.ctrlKey : e.ctrlKey && !e.metaKey) || e.altKey || e.shiftKey || e.key.toLowerCase() !== "k") return;
    if (ck.open) { e.preventDefault(); dlg.close(); return; }
    if (!$("#loader").hidden || $$("dialog[open]").length) return;
    e.preventDefault();
    openCmdk();
  }, true);
  $("#ck-btn").addEventListener("click", openCmdk);
  $("#ck-close").addEventListener("click", () => dlg.close());
  dlg.addEventListener("click", e => { if (e.target === dlg) dlg.close(); });
  dlg.addEventListener("close", () => {
    ck.open = false; ck.seq++; clearTimeout(ck.timer);
    const o = ck.origin, a = document.activeElement;
    ck.origin = null;
    if (o && o.isConnected && o !== document.body && (!a || a === document.body || dlg.contains(a))) o.focus();
  });
  input.addEventListener("input", () => { ck.q = input.value; ckSearchImages(); ckRender(true); });
  input.addEventListener("keydown", e => {
    if (e.isComposing) return;
    const n = ck.flat.length;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); if (n) ckSelect((ck.sel + (e.key === "ArrowDown" ? 1 : -1) + n) % n); }
    else if (e.key === "Home") { e.preventDefault(); ckSelect(0); }
    else if (e.key === "End") { e.preventDefault(); ckSelect(n - 1); }
    else if (e.key === "Enter") { e.preventDefault(); ckRun(ck.flat[ck.sel]); }
  });
  list.addEventListener("mousedown", e => e.preventDefault());
  list.addEventListener("click", e => { const o = e.target.closest("[data-ck]"); if (o) { e.stopPropagation(); ckRun(ck.flat[+o.dataset.ck]); } });
  list.addEventListener("mousemove", e => { const o = e.target.closest("[data-ck]"); if (o && +o.dataset.ck !== ck.sel) ckSelect(+o.dataset.ck, false); });
}

async function checkUpdates() {
  try {
    const res = await fetch("/api/update");
    if (!res.ok) return;
    const data = await res.json();
    if (data && data.has_update) {
      const pill = document.getElementById("update-pill");
      if (pill) {
        pill.hidden = false;
        pill.title = "Nueva versión disponible: " + (data.message || data.latest_sha);
        pill.addEventListener("click", () => {
          alert("Hay una nueva versión disponible de Musa.\n\nPara actualizar, ejecuta en tu terminal:\n  musa update");
        });
      }
    }
  } catch (e) {}
}

/* ─── init ─── */
connectEvents();
connectSSE();
connectPalette();
connectComposer();
wireComp();
const modelsReady = loadModels();
loadTemplates();
loadJobs();
renderRefs();
updateComposer();
watchDock();
checkUpdates();
if (!location.hash) {
  let saved = null;
  try { saved = localStorage.getItem("musa-screen"); } catch (e) {}
  history.replaceState(null, "", "#" + (saved || "inicio"));
}
if (document.documentElement.dataset.nols) { $("#loader").hidden = true; setLoading(false); }
else runLoader({ boot: true, ready: modelsReady });
render();   // el primer pintado no espera a /api/models; al llegar la lista se repinta con los nombres reales
modelsReady.then(() => { if (state.models !== FALLBACK_MODELS) render(); });
