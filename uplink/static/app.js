/* Uplink workspace.
 *
 * Rendering rule, unchanged from the first version and non-negotiable:
 * document text reaches the DOM through textContent only. There is no
 * innerHTML anywhere in this file, so corpus content can never become markup.
 *
 * GSAP drives motion only. If it fails to load, every function below still
 * works — animations are wrapped so a missing library degrades to no
 * animation rather than a broken page.
 */
"use strict";

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = String(text);
  return n;
};

/* -------------------------------------------------------------- formatting */

function fmtBytes(n) {
  const b = Number(n) || 0;
  if (b < 1024) return b + " B";
  if (b < 1024 * 1024) return Math.round(b / 1024) + " KB";
  return (b / (1024 * 1024)).toFixed(1) + " MB";
}
function fmtPct(x) {
  return x === null || x === undefined ? "—" : Math.round(Number(x) * 100) + "%";
}
function fmtMs(x) {
  return x === null || x === undefined ? "—" : Number(x).toFixed(1) + " ms";
}
function fmtWhen(ts) {
  return String(ts || "").slice(0, 16).replace("T", " ");
}

/* ------------------------------------------------------------------ motion */

const MOTION = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const HAS_GSAP = typeof window.gsap !== "undefined";

function anim(targets, vars) {
  if (!MOTION || !HAS_GSAP || !targets) return null;
  try {
    // clearProps on completion: gsap.from writes inline styles, and a tween
    // interrupted by a re-render (or a backgrounded tab) would otherwise
    // leave rows permanently half-faded.
    return window.gsap.from(targets, Object.assign(
      { clearProps: "opacity,transform" }, vars));
  } catch (e) { return null; }
}
function animTo(targets, vars) {
  if (!MOTION || !HAS_GSAP || !targets) return null;
  try { return window.gsap.to(targets, vars); } catch (e) { return null; }
}

function countUp(node, value) {
  if (!node) return;
  const target = Number(value) || 0;
  if (!MOTION || !HAS_GSAP) { node.textContent = String(target); return; }
  const state = { n: Number(node.textContent.replace(/[^0-9]/g, "")) || 0 };
  window.gsap.to(state, {
    n: target, duration: 0.8, ease: "power2.out",
    onUpdate: () => { node.textContent = String(Math.round(state.n)); },
  });
}

/* ------------------------------------------------------------------- state */

const state = {
  collection: "",
  sources: [],          // [{path, title, chunks, filetype, collection}]
  selected: new Set(),  // docKey()s currently checked
  filter: { text: "", type: "" },  // narrows the VISIBLE list only
  writes: false,
  reports: [],
  pollTimer: null,
  reader: null,         // {path, collection, citedSeq, start, shown, total}
  returnFocus: null,    // element to restore when the reader closes
};

/* --------------------------------------------------------------- API calls */

async function api(path, opts) {
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({ error: "bad response" }));
  if (!r.ok && !data.error) data.error = "request failed (" + r.status + ")";
  return data;
}

const postJSON = (path, body) =>
  api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Uplink": "1" },
    body: JSON.stringify(body),
  });

/* ------------------------------------------------------------------ status */

async function loadStatus() {
  const s = await api("/api/status");
  if (s.error) { $("index-meta").textContent = "index unavailable"; return; }
  state.writes = !!s.writes;
  // Asking is its own capability: the public demo answers questions while
  // every other write stays localhost-only.
  state.asks = !!(s.asks !== undefined ? s.asks : s.writes);

  // A tab keeps running the JavaScript it loaded. If the server has newer
  // assets, say so loudly rather than letting the page behave like an old
  // version while looking current.
  const meta = document.querySelector('meta[name="uplink-build"]');
  const pageBuild = meta ? meta.getAttribute("content") : null;
  if (pageBuild && s.build && pageBuild !== "__BUILD__" && pageBuild !== s.build) {
    $("stale").hidden = false;
  }
  state.reports = Array.isArray(s.reports) ? s.reports : [];
  state.collections = Array.isArray(s.collections) ? s.collections : [];
  state.extensions = Array.isArray(s.extensions) ? s.extensions : [];
  state.maxUpload = Number(s.max_upload_bytes) || 0;
  const types = $("drop-types");
  if (types && state.extensions.length) {
    types.textContent = state.extensions.map((e) => e.replace('.', '')).join(' · ');
  }

  $("index-meta").textContent =
    "last indexed " + (s.last_indexed || "never") +
    (state.writes ? " · writes enabled (localhost)" : " · read-only bind");

  const pill = $("privacy-pill");
  $("privacy-text").textContent = state.writes ? "local & private" : "read-only";
  pill.classList.toggle("pill-warn", !state.writes);

  // Write-gated affordances appear only when the server actually accepts them.
  $("add-source").hidden = !state.writes;
  $("ai").hidden = !state.asks;

  const sel = $("coll");
  const keep = sel.value;
  while (sel.options.length > 1) sel.remove(1);
  (s.collections || []).forEach((c) => {
    const o = document.createElement("option");
    o.value = c.name;
    o.textContent = c.name + " (" + c.documents + ")";
    sel.appendChild(o);
  });
  sel.value = keep;

  const links = $("report-links");
  links.replaceChildren();
  if (!state.reports.length) {
    links.appendChild(el("p", "micro", "Run `uplink report all` to generate reports."));
  } else {
    state.reports.forEach((name) => {
      const a = document.createElement("a");
      a.href = "/reports/" + name;
      a.textContent = name.replace(".html", "");
      links.appendChild(a);
    });
  }
}

/* ----------------------------------------------------------------- sources */

// A document is (collection, path): the schema keys them that way and the
// same filename legitimately exists in several collections. Keying the
// selection on path alone made one checkbox silently control two documents.
const docKey = (src) => String(src.collection || "") + "/" + String(src.path || "");

async function loadSources() {
  const q = state.collection ? "?collection=" + encodeURIComponent(state.collection) : "";
  const data = await api("/api/sources" + q);
  if (data.error) {
    const list = $("source-list");
    list.replaceChildren();
    list.appendChild(el("p", "micro", "sources unavailable"));
    return;
  }
  state.sources = data.sources || [];
  state.selected = new Set(state.sources.map(docKey));

  renderTypeFilter();
  renderSourceList();
  renderSuggestions(data.suggestions || []);
}

// True when a source survives the current search text and type filter.
// Every typed word must appear somewhere in the readable label, the real
// filename, or the path — so word order never matters and a partial word
// matches anywhere across the whole title.
function sourceMatches(src) {
  if (state.filter.type &&
      String(src.filetype || "").toLowerCase() !== state.filter.type) return false;
  const text = state.filter.text.trim().toLowerCase();
  if (!text) return true;
  const hay = [src.label, src.title, src.filename, src.path]
    .map((v) => String(v || "").toLowerCase()).join(" ");
  return text.split(/\s+/).every((word) => hay.indexOf(word) >= 0);
}

function visibleSources() {
  return state.sources.filter(sourceMatches);
}

// One chip per file type present in the current collection, with counts.
// The chips describe what is actually there, so an empty filter can never
// be built by clicking.
function renderTypeFilter() {
  const box = $("type-filter");
  box.replaceChildren();
  const counts = {};
  state.sources.forEach((s) => {
    const t = String(s.filetype || "").toLowerCase();
    if (t) counts[t] = (counts[t] || 0) + 1;
  });
  const types = Object.keys(counts).sort();
  if (types.length < 2) { state.filter.type = ""; return; }
  if (state.filter.type && !counts[state.filter.type]) state.filter.type = "";

  const mk = (label, value, count) => {
    const b = el("button", "type-chip" + (state.filter.type === value ? " is-on" : ""));
    b.type = "button";
    b.textContent = label + (count ? " " + count : "");
    b.title = value ? "Show only ." + value + " sources" : "Show every file type";
    b.addEventListener("click", () => {
      state.filter.type = state.filter.type === value ? "" : value;
      renderTypeFilter();
      renderSourceList();
    });
    return b;
  };
  box.appendChild(mk("all", "", 0));
  types.forEach((t) => box.appendChild(mk(t.toUpperCase(), t, counts[t])));
}

function renderSourceList() {
  const list = $("source-list");
  list.replaceChildren();

  const visible = visibleSources();
  const filtered = visible.length !== state.sources.length;
  const counter = $("source-count");
  if (filtered) counter.textContent = visible.length + "/" + state.sources.length;
  else countUp(counter, state.sources.length);

  if (!state.sources.length) {
    list.appendChild(el("p", "micro",
      state.writes ? "No documents yet — add one below."
                   : "No documents in this collection."));
  } else if (!visible.length) {
    list.appendChild(el("p", "micro", "No sources match this filter."));
    const clear = el("button", "mini", "Clear filter");
    clear.type = "button";
    clear.addEventListener("click", () => {
      state.filter.text = "";
      state.filter.type = "";
      $("source-search").value = "";
      renderTypeFilter();
      renderSourceList();
    });
    list.appendChild(clear);
  }

  visible.forEach((src) => {
    const key = docKey(src);
    const on = state.selected.has(key);
    const row = el("div", "source" + (on ? "" : " off"));

    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = on;
    box.setAttribute("aria-label", "Include " + src.path);
    box.addEventListener("change", () => {
      if (box.checked) state.selected.add(key);
      else state.selected.delete(key);
      row.classList.toggle("off", !box.checked);
      syncScope();
    });
    row.appendChild(box);

    const body = el("div", "source-body");
    const name = el("button", "source-name", src.label || src.title || src.path);
    name.type = "button";
    name.title = "Open " + src.path;
    name.addEventListener("click", () => openDoc(src.path, src.collection, null));
    body.appendChild(name);

    // The real filename stays visible under the readable label: the label
    // is for finding it, the filename is for citing it.
    body.appendChild(el("div", "source-file", src.filename || src.path));

    const sub = el("div", "source-sub");
    sub.appendChild(el("span", "ftype", (src.filetype || "doc").toUpperCase()));
    sub.appendChild(el("span", null, fmtBytes(src.size)));
    sub.appendChild(el("span", null, (src.chunks || 0) + " passages"));
    if (!state.collection && src.collection) {
      sub.appendChild(el("span", "tag-mini", src.collection));
    }
    body.appendChild(sub);
    row.appendChild(body);
    list.appendChild(row);
  });

  syncSelectAllBox();
  syncScope();

  // Deliberately no opacity here: a source list that fades is a source
  // list you cannot read while it settles.
  anim(list.querySelectorAll(".source"),
       { y: 10, duration: 0.3, stagger: 0.02, ease: "power2.out" });
}

// With a filter active, "select all" reads and writes only the sources you
// can SEE — the natural reading of the checkbox next to a filtered list.
function syncSelectAllBox() {
  const visible = visibleSources();
  $("select-all").checked =
    visible.length > 0 && visible.every((s) => state.selected.has(docKey(s)));
}

function syncScope() {
  const total = state.sources.length;
  const on = state.selected.size;
  const note = $("scope-note");
  const filtered = visibleSources().length !== total;
  $("select-all-label").textContent =
    on === total ? (filtered ? "All shown" : "All sources")
                 : on + " of " + total + " selected";
  // What will actually be sent, visible before you ask.
  const hint = $("scope-hint");
  if (hint) {
    if (total && on < total) {
      hint.hidden = false;
      hint.textContent = on === 0 ? "no sources" : on + " of " + total + " sources";
      hint.className = "scope-hint" + (on === 0 ? " scope-none" : " scope-on");
    } else {
      hint.hidden = true;
    }
  }

  if (on === total || total === 0) {
    note.hidden = true;
  } else {
    note.hidden = false;
    note.textContent = on === 0
      ? "No sources selected — searches will return nothing."
      : "Retrieval is limited to " + on + " of " + total + " documents.";
  }
}

// Selection is keyed by docKey — a bare path here once collided across
// collections and made an unchecked box search anyway. With a filter
// active this toggles only the sources currently shown, leaving the
// hidden ones exactly as they were.
function setAllSelected(on) {
  visibleSources().forEach((s) => {
    const key = docKey(s);
    if (on) state.selected.add(key);
    else state.selected.delete(key);
  });
  $("source-list").querySelectorAll(".source").forEach((row) => {
    const box = row.querySelector("input[type=checkbox]");
    if (box) box.checked = on;
    row.classList.toggle("off", !on);
  });
  syncScope();
}

$("select-all").addEventListener("change", (ev) => setAllSelected(ev.target.checked));

$("source-search").addEventListener("input", (ev) => {
  state.filter.text = String(ev.target.value || "");
  renderSourceList();
});

function scopeParams() {
  // Nothing to scope against yet, or everything is selected: search all.
  if (!state.sources.length || state.selected.size === state.sources.length) return "";

  const chosen = Array.from(state.selected);
  const dropped = state.sources.map(docKey).filter((k) => !state.selected.has(k));

  // `scoped=1` must always ride along: without it an EMPTY selection looks
  // identical to "no scoping" and the server would search everything —
  // the exact opposite of what the checkboxes promise.
  if (dropped.length && dropped.length < chosen.length) {
    // Fewer exclusions than inclusions: send the short side, which keeps the
    // request line sane on large corpora.
    return "&scoped=1" + dropped.map((k) => "&xdoc=" + encodeURIComponent(k)).join("");
  }
  return "&scoped=1" + chosen.map((k) => "&doc=" + encodeURIComponent(k)).join("");
}

/* ------------------------------------------------------------- suggestions */

function renderSuggestions(list) {
  const box = $("suggestions");
  box.replaceChildren();
  (Array.isArray(list) ? list : []).forEach((q) => {
    const b = el("button", "chip", q);
    b.type = "button";
    b.addEventListener("click", () => {
      $("q").value = q;
      if (state.asks) askAI(); else runSearch();
    });
    box.appendChild(b);
  });
  const title = $("empty-title");
  title.textContent = state.collection
    ? "Ask the " + state.collection + " collection"
    : "Ask your documents anything";
  anim(box.querySelectorAll(".chip"),
       { opacity: 0, y: 12, scale: 0.94, duration: 0.4, stagger: 0.05, ease: "back.out(1.6)" });
}

/* --------------------------------------------------------- chat history */

// The thread survives a reload. Stored locally in this browser (never sent
// anywhere) and capped, so a long working session cannot fill the quota.
const HISTORY_KEY = "uplink-thread-v1";
const HISTORY_MAX = 40;

function loadHistory() {
  try {
    const raw = localStorage.getItem(HISTORY_KEY);
    const rows = raw ? JSON.parse(raw) : [];
    return Array.isArray(rows) ? rows.slice(-HISTORY_MAX) : [];
  } catch (e) { return []; }
}

function saveHistory(rows) {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(rows.slice(-HISTORY_MAX)));
  } catch (e) { /* quota or private mode: history is a convenience, not state */ }
}

function recordTurn(entry) {
  const rows = loadHistory();
  rows.push(entry);
  saveHistory(rows);
}

function restoreHistory() {
  const rows = loadHistory();
  if (!rows.length) return;
  rows.forEach((row) => {
    if (!row || typeof row !== "object" || !row.q) return;
    const turn = addTurn(String(row.q), true);
    const card = el("div", "card");
    turn.appendChild(card);
    if (row.kind === "answer" && row.answer) {
      renderAnswer(card, { answer: row.answer, citations: row.citations }, String(row.q));
    } else if (row.kind === "search" && Array.isArray(row.hits)) {
      renderHits(card, { hits: row.hits, latency_ms: row.latency_ms || 0 }, String(row.q));
    } else {
      card.appendChild(el("p", "micro", "(result not stored)"));
    }
  });
  $("thread").appendChild(historyDivider());
  scrollThread();
}

function historyDivider() {
  const d = el("div", "divider");
  d.appendChild(el("span", null, "earlier in this session"));
  return d;
}

function clearHistory() {
  try { localStorage.removeItem(HISTORY_KEY); } catch (e) { /* ignore */ }
  $("thread").querySelectorAll(".turn, .divider").forEach((n) => n.remove());
  const empty = $("empty");
  if (empty) empty.hidden = false;
}

/* -------------------------------------------------------------- the thread */

function clearEmptyState() {
  // Hide, never remove: #suggestions and #empty-title are its children, and
  // detaching them makes every later getElementById return null — which
  // threw inside loadSources() and turned successful uploads into
  // "upload failed".
  const empty = $("empty");
  if (empty) empty.hidden = true;
}

function addTurn(question, restoring) {
  clearEmptyState();
  const turn = el("div", "turn");
  turn.appendChild(el("div", "bubble-q", question));
  $("thread").appendChild(turn);
  anim(turn.querySelector(".bubble-q"),
       { opacity: 0, y: 10, scale: 0.96, duration: 0.36, ease: "back.out(1.5)" });
  scrollThread();
  return turn;
}

function scrollThread() {
  const t = $("thread");
  t.scrollTop = t.scrollHeight;
}

function waitingCard(turn, text) {
  const card = el("div", "card");
  const w = el("div", "waiting");
  w.appendChild(el("span", "pulse"));
  w.appendChild(el("span", null, text));
  card.appendChild(w);
  turn.appendChild(card);
  animTo(card.querySelector(".pulse"),
         { scale: 1.6, opacity: 0.35, duration: 0.7, repeat: -1, yoyo: true, ease: "sine.inOut" });
  scrollThread();
  return card;
}

/* --------------------------------------------------------------- rendering */

function citationList(container, citations, onOpen) {
  const list = Array.isArray(citations) ? citations : [];
  if (!list.length) return;
  const wrap = el("div", "cites");
  let n = 0;
  list.forEach((c) => {
    if (!c || typeof c !== "object") return;
    const path = String(c.path || "");
    if (!path) return;
    n += 1;
    const b = el("button", "cite-btn");
    b.type = "button";
    b.appendChild(el("span", "n", n));
    b.appendChild(el("span", null, path + (c.section ? " · " + String(c.section) : "")));
    b.title = "Open this passage";
    const seq = Number.isInteger(c.seq) ? c.seq : null;
    b.addEventListener("click", () => onOpen(path, c.collection || null, seq));
    wrap.appendChild(b);
  });
  if (n) {
    container.appendChild(wrap);
    anim(wrap.querySelectorAll(".cite-btn"),
         { opacity: 0, scale: 0.8, duration: 0.34, stagger: 0.04, ease: "back.out(2)" });
  }
}

function renderAnswer(card, resp, question) {
  card.replaceChildren();
  const label = el("div", "card-label");
  label.appendChild(el("span", "spark"));
  label.appendChild(el("span", null, "answer from your documents"));
  card.appendChild(label);
  card.appendChild(el("div", "answer-text", String(resp.answer || "")));

  // Provenance the reader can see, so drift is visible rather than silent.
  const used = Array.isArray(resp.sources_used) ? resp.sources_used.length : 0;
  const passages = Number(resp.passages) || 0;
  if (used || passages) {
    const prov = el("div", "provenance");
    prov.appendChild(el("span", resp.grounding_verified ? "prov-ok" : "prov-warn",
      resp.grounding_verified ? "✓ grounded" : "! unverified"));
    prov.appendChild(el("span", null,
      passages + (passages === 1 ? " passage" : " passages") +
      " from " + used + (used === 1 ? " document" : " documents") +
      (resp.scoped ? " · within your selection" : "")));
    card.appendChild(prov);
  }

  citationList(card, resp.citations, openDoc);

  const actions = el("div", "card-actions");
  const save = el("button", "mini", "Save to notes");
  save.type = "button";
  save.addEventListener("click", async () => {
    save.disabled = true;
    const out = await postJSON("/api/notes", {
      title: question,
      body: String(resp.answer || ""),
      citations: resp.citations,
      collection: state.collection || null,
    });
    save.textContent = out.error ? "could not save" : "Saved ✓";
    if (!out.error) { save.classList.add("on-good"); loadNotes(); scheduleMetrics(); }
  });
  actions.appendChild(save);

  // Rating the ANSWER, not just a search hit. Without this the feedback
  // loop has no intake on the path people actually use, and an upvote here
  // becomes a fixture whose expected documents are the ones it cited.
  if (state.writes) {
    const cites = Array.isArray(resp.citations) ? resp.citations : [];
    const paths = cites
      .filter((c) => c && typeof c === "object" && c.path)
      .map((c) => String(c.path));
    if (paths.length) {
      const up = el("button", "mini", "\uD83D\uDC4D accurate");
      const down = el("button", "mini", "\uD83D\uDC4E wrong");
      const rate = async (verdict, btn, other) => {
        const out = await postJSON("/api/feedback", {
          q: question, path: paths[0], paths: paths, kind: "answer",
          vote: verdict,
          collection: cites[0].collection || state.collection || null,
        });
        if (!out.error) {
          btn.className = "mini " + (verdict === "up" ? "on-good" : "on-bad");
          other.className = "mini";
          scheduleMetrics();
        }
      };
      up.addEventListener("click", () => rate("up", up, down));
      down.addEventListener("click", () => rate("down", down, up));
      actions.appendChild(up);
      actions.appendChild(down);
    }
  }
  card.appendChild(actions);

  anim(card, { opacity: 0, y: 16, duration: 0.45, ease: "power3.out" });
  scrollThread();
}

function renderHits(card, data, question) {
  card.replaceChildren();
  const label = el("div", "card-label");
  label.appendChild(el("span", "spark"));
  label.appendChild(el("span", null,
    data.hits.length + " passages · " + data.latency_ms + " ms"));
  card.appendChild(label);

  data.hits.forEach((h) => {
    const hit = el("div", "hit");
    const head = el("div", "hit-head");
    const p = el("button", "hit-path", h.path);
    p.type = "button";
    p.title = "Open this passage";
    p.addEventListener("click", () => openDoc(h.path, h.collection, h.seq));
    head.appendChild(p);
    if (h.collection) head.appendChild(el("span", "tag", h.collection));
    if (h.section) head.appendChild(el("span", "micro", h.section));
    head.appendChild(el("span", "hit-score", Number(h.score).toFixed(2)));
    hit.appendChild(head);

    const snip = el("div", "snippet");
    renderSnippet(snip, String(h.snippet || ""));
    hit.appendChild(snip);

    const actions = el("div", "card-actions");
    const up = el("button", "mini", "👍 helpful");
    const down = el("button", "mini", "👎 off-target");
    if (state.writes) {
      up.addEventListener("click", () => vote(h, "up", up, down, question));
      down.addEventListener("click", () => vote(h, "down", down, up, question));
      actions.appendChild(up);
      actions.appendChild(down);
      hit.appendChild(actions);
    }
    card.appendChild(hit);
  });

  anim(card.querySelectorAll(".hit"),
       { opacity: 0, y: 14, duration: 0.4, stagger: 0.05, ease: "power3.out" });
  scrollThread();
}

function renderSnippet(node, snippet) {
  // Matches arrive delimited by U+0001 / U+0002 - — non-printable, so corpus
  // text containing [ ] survives intact.
  snippet.split(/[\u0001\u0002]/).forEach((part, i) => {
    if (!part) return;
    if (i % 2 === 1) node.appendChild(el("mark", null, part));
    else node.appendChild(document.createTextNode(part));
  });
}

async function vote(hit, verdict, btn, other, question) {
  const out = await postJSON("/api/feedback", {
    q: question, path: hit.path, seq: hit.seq,
    vote: verdict, collection: hit.collection,
  });
  if (!out.error) {
    btn.className = "mini " + (verdict === "up" ? "on-good" : "on-bad");
    other.className = "mini";
    anim(btn, { scale: 0.82, duration: 0.3, ease: "back.out(3)" });
    scheduleMetrics();
  }
}

/* ---------------------------------------------------------- speech to text */

// Dictation rides on the browser's own speech engine (Web Speech API), so
// there is nothing to install and no key to manage. The mic button appears
// only when the browser actually provides one. Honesty note: the SPOKEN
// AUDIO is transcribed by the browser's speech service (in Chrome and Edge
// that service is remote) — the corpus never goes anywhere.
const SpeechRec = (typeof window !== "undefined" &&
  (window.SpeechRecognition || window.webkitSpeechRecognition)) || null;
let dictation = null;   // the active recognition session, or null

// Results arrive cumulatively: every event carries all segments so far,
// interim ones included, so the box can show words as they are recognised.
function transcriptOf(ev) {
  let text = "";
  for (let i = 0; i < ev.results.length; i++) {
    text += ev.results[i][0].transcript;
  }
  return text.trim();
}

function toggleDictation() {
  if (!SpeechRec) return;
  if (dictation) {
    // Second press stops listening; onend does the cleanup.
    try { dictation.stop(); } catch (e) { /* already stopping */ }
    return;
  }
  const q = $("q");
  const mic = $("mic");
  // Dictating never erases what is already typed — it appends.
  const before = q.value ? q.value.replace(/\s+$/, "") + " " : "";
  const placeholder = q.placeholder;
  const rec = new SpeechRec();
  dictation = rec;
  rec.lang = navigator.language || "en-US";
  rec.interimResults = true;
  rec.maxAlternatives = 1;
  mic.classList.add("listening");
  q.placeholder = "Listening…";

  rec.onresult = (ev) => { q.value = before + transcriptOf(ev); };
  rec.onerror = (ev) => {
    if (ev.error === "aborted") return;   // the user stopped it; not a failure
    // Name the actual failure: "could not hear that" once hid four different
    // problems with four different fixes behind one shrug.
    q.placeholder = {
      "not-allowed": "microphone blocked — allow it for this site and try again",
      "service-not-allowed": "the browser's speech service is turned off — check browser settings",
      "no-speech": "heard silence — is Windows sending the right microphone to this browser?",
      "audio-capture": "no working microphone found — check the input device in Windows",
      "network": "the browser's speech service is unreachable — it needs an internet connection",
      "language-not-supported": "this browser's speech engine does not support " + rec.lang,
    }[ev.error] || "dictation failed (" + String(ev.error) + ") — try again";
  };
  rec.onend = () => {
    dictation = null;
    mic.classList.remove("listening");
    if (q.placeholder === "Listening…") q.placeholder = placeholder;
    else setTimeout(() => { q.placeholder = placeholder; }, 4000);
    q.focus();
  };
  try { rec.start(); } catch (e) {
    dictation = null;
    mic.classList.remove("listening");
    q.placeholder = placeholder;
  }
}

/* ------------------------------------------------------------ search / ask */

async function runSearch() {
  const q = $("q").value.trim();
  if (!q) return;
  const turn = addTurn(q);
  const card = waitingCard(turn, "searching…");
  $("go").disabled = true;
  $("meta").textContent = "";
  try {
    const url = "/api/search?q=" + encodeURIComponent(q) + "&k=8" +
      (state.collection ? "&collection=" + encodeURIComponent(state.collection) : "") +
      scopeParams();
    const data = await api(url);
    if (data.error) {
      card.replaceChildren(el("p", "err", "error: " + data.error));
    } else if (!data.hits || !data.hits.length) {
      card.replaceChildren(el("p", "micro",
        "Nothing matched. Try fewer or different words" +
        (state.selected.size < state.sources.length ? " — or re-select sources." : ".")));
    } else {
      renderHits(card, data, q);
      recordTurn({
        kind: "search", q: q, ts: Date.now(),
        latency_ms: data.latency_ms,
        // Keep the thread light: the citation coordinates are what make a
        // restored turn still clickable.
        hits: data.hits.slice(0, 8).map((h) => ({
          path: h.path, section: h.section, seq: h.seq,
          collection: h.collection, score: h.score, snippet: h.snippet,
        })),
      });
    }
  } catch (e) {
    card.replaceChildren(el("p", "err", "search failed — is the server running?"));
  } finally {
    $("go").disabled = false;
    $("q").value = "";
    scheduleMetrics();
  }
}

async function askAI() {
  const q = $("q").value.trim();
  if (!q) return;
  const turn = addTurn(q);
  const card = waitingCard(turn, "queued — waiting for the brain session…");
  $("ai").disabled = true;
  if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
  $("q").value = "";
  try {
    // The selection must ride along: the brain answers later and cannot
    // see the checkboxes.
    const scoping = scopeParams();
    const started = await postJSON("/api/ask", {
      q: q, collection: state.collection || null,
      scoped: scoping !== "",
      docs: scoping === "" ? [] : Array.from(state.selected),
    });
    if (started.error) {
      card.replaceChildren(el("p", "err", "error: " + started.error));
      $("ai").disabled = false;
      return;
    }
    const t0 = Date.now();
    state.pollTimer = setInterval(async () => {
      const secs = Math.round((Date.now() - t0) / 1000);
      let st;
      try { st = await api("/api/ask/" + started.id); } catch (e) { return; }
      if (st.state === "answered") {
        clearInterval(state.pollTimer); state.pollTimer = null;
        try {
          renderAnswer(card, st, q);
          recordTurn({ kind: "answer", q: q, ts: Date.now(),
                       answer: st.answer, citations: st.citations });
        } finally { $("ai").disabled = false; scheduleMetrics(); }
      } else if (st.state === "error") {
        clearInterval(state.pollTimer); state.pollTimer = null;
        card.replaceChildren(el("p", "err", "brain error: " + String(st.error || "unknown")));
        $("ai").disabled = false;
      } else if (secs > 180) {
        clearInterval(state.pollTimer); state.pollTimer = null;
        card.replaceChildren(el("p", "micro",
          "No answer after 3 minutes — is a brain session running with its watcher armed?"));
        $("ai").disabled = false;
      } else {
        const w = card.querySelector(".waiting span:last-child");
        if (w) w.textContent = "waiting for the brain session… " + secs + "s";
      }
    }, 2000);
  } catch (e) {
    card.replaceChildren(el("p", "err", "could not reach the server"));
    $("ai").disabled = false;
  }
}

$("composer").addEventListener("submit", (ev) => { ev.preventDefault(); runSearch(); });
$("ai").addEventListener("click", askAI);
$("mic").hidden = !SpeechRec;
$("mic").addEventListener("click", toggleDictation);
$("clear-thread").addEventListener("click", clearHistory);
$("stale-reload").addEventListener("click", () => window.location.reload(true));

/* ------------------------------------------------------------ source reader */

function openReader() {
  state.returnFocus = document.activeElement;
  $("reader").hidden = false;
  $("scrim").hidden = false;
  $("reader-close").focus();
  anim($("reader"), { x: 60, opacity: 0, duration: 0.42, ease: "power3.out" });
  anim($("scrim"), { opacity: 0, duration: 0.3 });
}
function closeReader() {
  $("reader").hidden = true;
  $("scrim").hidden = true;
  state.reader = null;
  // Send focus back where it came from, so keyboard users are not dumped
  // at the top of the document after checking a citation.
  const back = state.returnFocus;
  state.returnFocus = null;
  if (back && typeof back.focus === "function") back.focus();
}
$("reader-close").addEventListener("click", closeReader);
$("tab-original").addEventListener("click", () => showTab("original"));
$("tab-text").addEventListener("click", () => showTab("text"));
$("scrim").addEventListener("click", () => {
  if (!$("addbox").hidden) closeAddBox();
  if (!$("reader").hidden) closeReader();
});
document.addEventListener("keydown", (ev) => {
  if (ev.key !== "Escape") return;
  hideTip();
  if (!$("addbox").hidden) closeAddBox();
  if (!$("reader").hidden) closeReader();
});

async function fetchDoc(path, collection, params, citedSeq) {
  openReader();
  $("reader-path").textContent = String(path);
  $("reader-meta").textContent = "loading…";
  $("reader-body").replaceChildren();
  let url = "/api/doc?path=" + encodeURIComponent(path);
  if (collection) url += "&collection=" + encodeURIComponent(collection);
  url += params;
  const doc = await api(url);
  if (doc.error) {
    let msg = String(doc.error);
    if (Array.isArray(doc.collections) && doc.collections.length) {
      msg += " (" + doc.collections.map(String).join(", ") + ")";
    }
    $("reader-meta").textContent = msg;
    return;
  }
  renderDoc(doc, citedSeq);
  scheduleMetrics();
}

function openDoc(path, collection, seq) {
  const anchored = Number.isInteger(seq);
  return fetchDoc(path, collection,
    anchored ? "&seq=" + encodeURIComponent(seq) : "",
    anchored ? seq : null);
}
function openDocAt(path, collection, start, citedSeq) {
  return fetchDoc(path, collection, "&start=" + encodeURIComponent(start), citedSeq);
}

function citedPage(doc, citedSeq) {
  // PDF sections are labelled "Page 31" — enough to open the original at
  // the cited page rather than at page one.
  if (!Number.isInteger(citedSeq)) return null;
  const chunks = Array.isArray(doc.chunks) ? doc.chunks : [];
  const hit = chunks.find((c) => c && Number(c.seq) === Number(citedSeq));
  const m = hit && /page\s*(\d+)/i.exec(String(hit.section || ""));
  return m ? Number(m[1]) : null;
}

function showTab(which) {
  const original = which === "original";
  $("reader-frame-wrap").hidden = !original;
  $("reader-body").hidden = original;
  $("reader-nav").hidden = original;
  $("tab-original").className = "tab" + (original ? " is-on" : "");
  $("tab-text").className = "tab" + (original ? "" : " is-on");
  if (original && state.reader) mountOriginal();
}

function mountOriginal() {
  const r = state.reader;
  if (!r) return;
  const frame = $("reader-frame");
  const note = $("reader-frame-note");
  if (!r.hasOriginal) {
    frame.removeAttribute("src");
    note.textContent =
      "This collection has no source folder on disk (uploaded documents), " +
      "so the original file cannot be shown. The indexed text is complete.";
    return;
  }
  let src = "/api/file?collection=" + encodeURIComponent(r.collection) +
            "&path=" + encodeURIComponent(r.path);
  if (r.viewable) {
    if (r.page) src += "#page=" + r.page;
    frame.src = src;
    note.textContent = r.page
      ? "Original file, opened at page " + r.page + "."
      : "Original file as stored on disk.";
  } else {
    frame.removeAttribute("src");
    note.textContent =
      "Browsers cannot display this file type inline — use Download to open " +
      "it in its own application. The indexed text tab shows its contents.";
  }
}

function renderDoc(doc, citedSeq) {
  const total = Number(doc.total_chunks) || 0;
  const start = Number(doc.start) || 0;
  const chunks = Array.isArray(doc.chunks) ? doc.chunks : [];

  state.reader = {
    path: doc.path, collection: doc.collection,
    citedSeq: citedSeq, start: start, shown: chunks.length, total: total,
    viewable: !!doc.viewable, hasOriginal: !!doc.has_original,
    page: citedPage(doc, citedSeq),
  };

  $("reader-path").textContent = String(doc.label || doc.path || "");
  $("reader-meta").textContent =
    String(doc.path || "") + " · " + String(doc.collection || "") +
    " · " + total + " passages";

  const dl = $("reader-download");
  if (doc.has_original) {
    dl.hidden = false;
    dl.href = "/api/file?collection=" + encodeURIComponent(doc.collection) +
              "&path=" + encodeURIComponent(doc.path);
  } else {
    dl.hidden = true;
    dl.removeAttribute("href");
  }
  $("tab-original").disabled = !doc.has_original;

  const body = $("reader-body");
  body.replaceChildren();
  chunks.forEach((c) => {
    if (!c || typeof c !== "object") return;
    const cited = Number.isInteger(citedSeq) && Number(c.seq) === Number(citedSeq);
    const div = el("div", "chunk" + (cited ? " cited" : ""));
    div.appendChild(el("div", "chunk-head",
      "#" + String(c.seq) + (c.section ? " · " + String(c.section) : "") +
      (cited ? "  — cited" : "")));
    div.appendChild(el("div", "chunk-text", String(c.text || "")));
    body.appendChild(div);
  });

  $("reader-pos").textContent = chunks.length
    ? "showing " + (start + 1) + "–" + (start + chunks.length) + " of " + total
    : "no text";
  $("reader-prev").disabled = start <= 0;
  $("reader-next").disabled = start + chunks.length >= total;

  // A PDF is much more readable as itself; text-shaped files read fine as
  // indexed passages and keep the cited highlight.
  showTab(doc.has_original && doc.viewable && doc.filetype === "pdf"
          ? "original" : "text");

  const anchor = body.querySelector(".chunk.cited");
  if (anchor) anchor.scrollIntoView({ block: "center" });
  anim(body.querySelectorAll(".chunk"),
       { opacity: 0, y: 10, duration: 0.34, stagger: 0.03, ease: "power2.out" });
}

$("reader-prev").addEventListener("click", () => {
  const r = state.reader;
  if (!r) return;
  openDocAt(r.path, r.collection, Math.max(0, r.start - r.shown), r.citedSeq);
});
$("reader-next").addEventListener("click", () => {
  const r = state.reader;
  if (!r) return;
  openDocAt(r.path, r.collection, r.start + r.shown, r.citedSeq);
});

/* ----------------------------------------------------------------- metrics */

// Every metric explains itself: what it means, how it is computed, and the
// command that reproduces it — so no number on this panel has to be taken
// on faith. Labels on the panel are plain English; the industry term lives
// in the tooltip title so the panel reads like a report, not a benchmark.
const GLOSSARY = {
  "first answer correct": ["Also known as hit@1",
    "The share of test questions where the very FIRST result was a correct document. The strictest useful measure of search quality.",
    "python -m uplink eval fixtures/<set>.jsonl"],
  "correct in top results": ["Also known as hit@k",
    "The share of questions where a correct document appeared anywhere in the top results. Measures whether search found it at all, ignoring position.",
    "python -m uplink eval fixtures/<set>.jsonl --k 5"],
  "ranking quality": ["Also known as MRR (mean reciprocal rank)",
    "How close to the top the right document lands, from 0 to 1. First place scores 1.0, second 0.5, third 0.33. Rewards ranking correctly, not merely finding.",
    "python -m uplink eval fixtures/<set>.jsonl"],
  "confidence range": ["95% confidence interval",
    "The range the true rate plausibly falls in, given how few questions were scored. A wide range means the sample is small — not that the system is bad.",
    "Wilson score interval, computed from the logged run"],
  "change since baseline": ["Live index vs published score",
    "The published score is a logged run. This re-scores the CURRENT index right now, so a change that quietly hurt search quality shows up here.",
    "python -m uplink eval fixtures/<set>.jsonl"],
  "live check": ["Scored against the index right now",
    "These numbers re-run every test question against the current index, so they move the moment a document is added or removed.",
    "python -m uplink eval fixtures/<set>.jsonl"],
  "answers generated": ["Answers written so far",
    "Questions the AI session has answered from retrieved passages, with citations.",
    "ls data/asks/*.response.json"],
  "time to answer": ["Median time to answer",
    "From asking a question to the answer landing. This depends on an AI session being connected — it measures the workflow, not search speed.",
    "data/asks/*.json timestamps"],
  "citations per answer": ["Average sources cited",
    "How many source passages each answer stood on. An answer with no citations is unsupported and is counted separately.",
    "data/asks/*.response.json"],
  "answers verified": ["Verification rate",
    "The share of answers where a cited source was actually OPENED afterwards. The honest test of whether citations do their job or just decorate the answer.",
    "data/query-log.jsonl (kind=doc|original)"],
  "saved to notes": ["Save rate",
    "Answers kept as notes. A strong satisfaction signal: people save what they intend to reuse.",
    "data/notes.jsonl"],
  "helpful votes": ["Thumbs-up count",
    "Thumbs-up on results and answers. Helpful votes become test questions, which is how the system measures its own improvement.",
    "python -m uplink promote"],
  "helpful rate": ["Helpful share of all votes",
    "Thumbs-up as a share of all votes cast.",
    "data/feedback.jsonl"],
  "added to test set": ["Promoted fixtures",
    "Helpful votes turned into permanent test questions. Once added, a case is scored on every future evaluation run.",
    "python -m uplink promote"],
  "ready to promote": ["Awaiting promotion",
    "Helpful votes not yet converted into test questions — the actionable backlog of the self-improvement loop.",
    "python -m uplink promote"],
  "searches with no results": ["Zero-hit queries",
    "Questions your documents could not answer in the words they were asked. Each one is a concrete gap with a known question.",
    "data/query-log.jsonl"],
  "words not in any source": ["Terms absent from the index",
    "Words from failed searches that match NO passage anywhere. This separates the two causes of failure: the documents lack the topic, or they hold it under different words. The second is fixable.",
    "SELECT ... FROM chunks_fts WHERE chunks_fts MATCH"],
  "typical search time": ["Median search latency",
    "Query time over the search index for a typical search. Search only; it excludes answer generation.",
    "data/query-log.jsonl"],
  "slowest searches": ["95th percentile latency",
    "The slow end: 19 of 20 searches finish faster than this.",
    "data/query-log.jsonl"],
  "no-result rate": ["Zero-hit rate",
    "Searches that returned nothing. Each one is a vocabulary mismatch — your documents likely hold the answer under different words.",
    "data/query-log.jsonl"],
  "documents with no text": ["Unsearchable documents",
    "Indexed but produced no passages, usually a text-extraction failure. They inflate the document count while answering nothing.",
    "python -m uplink status"],
  "passages per document": ["Chunking density",
    "How many searchable passages the average document yields. Very low suggests extraction problems; very high suggests documents that could be split.",
    "python -m uplink status"],
};

function attachTip(node, key) {
  const entry = GLOSSARY[key];
  if (!entry) return node;
  node.setAttribute("tabindex", "0");
  node.setAttribute("aria-describedby", "tip");
  node.classList.add("has-tip");
  const show = () => showTip(node, entry);
  node.addEventListener("mouseenter", show);
  node.addEventListener("focus", show);
  node.addEventListener("mouseleave", hideTip);
  node.addEventListener("blur", hideTip);
  return node;
}

function showTip(node, entry) {
  const tip = $("tip");
  $("tip-title").textContent = entry[0];
  $("tip-body").textContent = entry[1];
  $("tip-cmd").textContent = entry[2];
  tip.hidden = false;
  const r = node.getBoundingClientRect();
  const width = 268;
  let left = r.left - width - 12;
  if (left < 8) left = Math.min(window.innerWidth - width - 8, r.right + 12);
  tip.style.left = Math.max(8, left) + "px";
  tip.style.top = Math.max(8, Math.min(window.innerHeight - 170, r.top - 4)) + "px";
}

function hideTip() { $("tip").hidden = true; }

// A sparkline built from SVG DOM nodes — no library, no innerHTML.
function sparkline(values, width, height) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 " + width + " " + height);
  svg.setAttribute("class", "spark-svg");
  svg.setAttribute("aria-hidden", "true");
  const nums = (values || []).map(Number).filter((n) => !Number.isNaN(n));
  if (nums.length < 2) return svg;
  const lo = Math.min(...nums), hi = Math.max(...nums);
  const span = hi - lo || 1;
  const step = width / (nums.length - 1);
  const pts = nums.map((v, i) =>
    [i * step, height - ((v - lo) / span) * (height - 4) - 2]);
  const line = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
  line.setAttribute("points", pts.map((q) => q[0].toFixed(1) + "," + q[1].toFixed(1)).join(" "));
  line.setAttribute("class", "spark-line");
  svg.appendChild(line);
  const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
  dot.setAttribute("cx", pts[pts.length - 1][0].toFixed(1));
  dot.setAttribute("cy", pts[pts.length - 1][1].toFixed(1));
  dot.setAttribute("r", "2.6");
  dot.setAttribute("class", "spark-dot");
  svg.appendChild(dot);
  return svg;
}

function cardTitle(box, text) {
  box.appendChild(el("p", "card-title", text));
}

function metricRow(label, value, tone, tipKey) {
  const row = el("div", "mrow");
  row.appendChild(attachTip(el("span", "mrow-l", label), tipKey || label));
  row.appendChild(el("span", "mrow-v" + (tone ? " " + tone : ""), value));
  return row;
}

function heroBlock(box, value, label, sub, tipKey) {
  const hero = el("div", "metric-hero");
  hero.appendChild(el("span", "metric-hero-n", value));
  hero.appendChild(attachTip(el("span", "metric-hero-l", label), tipKey || label));
  box.appendChild(hero);
  if (sub) box.appendChild(el("p", "metric-hero-sub", sub));
}

function fmtDelta(n, asPoints) {
  const v = Number(n) || 0;
  const shown = asPoints ? Math.round(v * 100) + "pp" : v.toFixed(3);
  return (v > 0 ? "+" : "") + shown;
}

let metricsTimer = null;

// Every action that moves a number schedules a refresh; the debounce means
// a burst of searches costs one re-score rather than one each.
function scheduleMetrics(delay) {
  if (metricsTimer) clearTimeout(metricsTimer);
  metricsTimer = setTimeout(() => { metricsTimer = null; loadMetrics(); },
                            delay === undefined ? 900 : delay);
}

async function loadMetrics() {
  const m = await api("/api/metrics");
  if (m.error) return;
  renderAccuracy(m);
  renderFailing(m.live || {});
  renderGaps(m.gaps || {});
  renderAnswers(m.answers || {});
  renderFeedback(m.feedback || {});
  renderPerformance(m.performance || {});
  renderHealth(m.health || {});
}

function renderAccuracy(m) {
  const box = $("accuracy");
  box.replaceChildren();
  cardTitle(box, "Search accuracy");
  const a = m.accuracy || {};
  if (!a.available) {
    box.appendChild(el("p", "micro",
      "Not measured yet. Run `uplink eval fixtures/golden.jsonl --log` to record a baseline."));
    return;
  }
  const ci = Array.isArray(a.hit_at_1_ci)
    ? "confidence range " + fmtPct(a.hit_at_1_ci[0]) + "-" + fmtPct(a.hit_at_1_ci[1]) +
      " · " + a.questions + " test questions"
    : a.questions + " test questions";
  heroBlock(box, fmtPct(a.hit_at_1), "first answer correct", ci);

  box.appendChild(metricRow("correct in top " + a.k, fmtPct(a.hit_at_k),
                            "", "correct in top results"));
  if (Array.isArray(a.hit_at_k_ci)) {
    box.appendChild(metricRow("confidence range",
      fmtPct(a.hit_at_k_ci[0]) + "-" + fmtPct(a.hit_at_k_ci[1])));
  }
  box.appendChild(metricRow("ranking quality", Number(a.mrr).toFixed(3)));

  if (a.delta) {
    const d = a.delta;
    const better = (Number(d.mrr) || 0) >= 0;
    box.appendChild(metricRow("change since baseline",
      fmtDelta(d.hit_at_1, true) + " first answer · " + fmtDelta(d.mrr) + " ranking",
      better ? "good" : "warn", "change since baseline"));
    if (!d.comparable) {
      box.appendChild(el("p", "micro",
        "Previous run used a different test set - not directly comparable."));
    }
  }

  if (a.series && a.series.length > 1) {
    const wrap = el("div", "spark-wrap");
    wrap.appendChild(sparkline(a.series.map((s) => s.mrr), 200, 30));
    wrap.appendChild(el("span", "micro",
      "ranking quality across " + a.runs + " logged runs"));
    box.appendChild(wrap);
  }
  box.appendChild(el("p", "micro",
    (a.label || "unlabeled run") + " · " + fmtWhen(a.ts) +
    (a.fixtures ? " · " + a.fixtures : "")));
}

function renderFailing(live) {
  const box = $("failing");
  box.replaceChildren();
  cardTitle(box, "What is failing now");
  if (!live.available) {
    box.appendChild(el("p", "micro", String(live.reason || "live scoring unavailable")));
    return;
  }
  const failing = Array.isArray(live.failing) ? live.failing : [];
  const weak = Array.isArray(live.weak) ? live.weak : [];

  box.appendChild(metricRow("first answer correct now", fmtPct(live.hit_at_1),
                            "", "live check"));
  box.appendChild(metricRow("ranking quality now", Number(live.mrr).toFixed(3),
                            "", "ranking quality"));

  if (!failing.length && !weak.length) {
    box.appendChild(el("p", "micro",
      "Every test question returns a correct document in first place."));
    return;
  }
  failing.forEach((f) => {
    const row = el("div", "issue issue-bad");
    row.appendChild(el("span", "issue-tag", "MISS"));
    row.appendChild(el("span", "issue-q", f.q));
    box.appendChild(row);
  });
  weak.forEach((w) => {
    const row = el("div", "issue");
    row.appendChild(el("span", "issue-tag", "rank " + w.rank));
    row.appendChild(el("span", "issue-q", w.q));
    box.appendChild(row);
  });
  box.appendChild(el("p", "micro",
    "Misses are vocabulary mismatches - the answer is in the corpus under different words."));
}

function renderAnswers(a) {
  const box = $("answer-metrics");
  box.replaceChildren();
  cardTitle(box, "Generated answers");
  if (!a.answered) {
    box.appendChild(el("p", "micro",
      "No answers yet. Ask a question with Ask AI to start measuring the generative side."));
    return;
  }
  heroBlock(box, fmtPct(a.verification_rate), "answers verified",
            a.verified + " of " + a.answered + " answers had a cited source opened",
            "answers verified");
  box.appendChild(metricRow("answers generated", String(a.answered)));
  box.appendChild(metricRow("time to answer",
    a.median_seconds === null ? "—" : Math.round(a.median_seconds) + "s"));
  box.appendChild(metricRow("citations per answer",
    a.avg_citations === null ? "—" : String(a.avg_citations)));
  if (a.uncited_answers) {
    box.appendChild(metricRow("answers without citations", String(a.uncited_answers),
                              "warn", "citations per answer"));
  }
  box.appendChild(metricRow("saved to notes", fmtPct(a.save_rate), "", "saved to notes"));
}

function renderGaps(g) {
  const box = $("gaps");
  box.replaceChildren();
  cardTitle(box, "Knowledge gaps");

  const zero = Array.isArray(g.zero_hit) ? g.zero_hit : [];
  const unknown = Array.isArray(g.unknown_terms) ? g.unknown_terms : [];

  if (!zero.length && !unknown.length) {
    box.appendChild(el("p", "micro",
      "No failed searches recorded. Gaps appear here as soon as a query " +
      "comes back empty."));
    return;
  }

  box.appendChild(metricRow("searches with no results",
                            String(g.zero_hit_total || zero.length),
                            (g.zero_hit_total || 0) > 0 ? "warn" : "",
                            "searches with no results"));

  // The actionable half: a term that matches nothing is the REASON its
  // query matched nothing, and it names the vocabulary to add.
  if (unknown.length) {
    box.appendChild(metricRow("words not in any source", String(unknown.length), "warn",
                              "words not in any source"));
    unknown.forEach((u) => {
      const row = el("div", "issue issue-bad");
      row.appendChild(el("span", "issue-tag", "no match"));
      const body = el("span", "issue-q");
      body.appendChild(el("strong", null, String(u.term)));
      const uses = Array.isArray(u.queries) ? u.queries.length : 0;
      if (uses) body.appendChild(document.createTextNode(
        " \u00B7 used in " + uses + (uses === 1 ? " query" : " queries")));
      row.appendChild(body);
      box.appendChild(row);
    });
  }

  zero.forEach((z) => {
    const row = el("div", "issue");
    row.appendChild(el("span", "issue-tag", z.count > 1 ? z.count + "x" : "empty"));
    const q = el("button", "issue-q issue-link", z.q);
    q.type = "button";
    q.title = "Run this query again";
    q.addEventListener("click", () => { $("q").value = z.q; runSearch(); });
    row.appendChild(q);
    box.appendChild(row);
  });

  box.appendChild(el("p", "micro",
    unknown.length
      ? "A missing word means your documents likely cover the topic under " +
        "other words - add a document, or search the synonym."
      : "Each failed search is a question your documents could not answer as asked."));
}

function promoteFixtures(btn) {
  btn.disabled = true;
  btn.textContent = "promoting\u2026";
  postJSON("/api/promote", {}).then((out) => {
    btn.disabled = false;
    if (out.error) { btn.textContent = "promote failed"; return; }
    btn.textContent = out.added
      ? "promoted " + out.added
      : "nothing new to promote";
    scheduleMetrics();
  });
}

function renderFeedback(f) {
  const box = $("feedback-metrics");
  box.replaceChildren();
  cardTitle(box, "Feedback loop");
  if (!f.total) {
    box.appendChild(el("p", "micro",
      "No votes yet. Rate a result or an answer - helpful votes become test " +
      "questions via `uplink promote`, which is how the system improves itself."));
    return;
  }
  box.appendChild(metricRow("helpful votes", String(f.up), "good"));
  box.appendChild(metricRow("off-target votes", String(f.down), f.down ? "warn" : ""));
  box.appendChild(metricRow("helpful rate", fmtPct(f.helpful_rate)));
  box.appendChild(metricRow("added to test set", String(f.promoted_fixtures)));
  if (f.pending_promotion) {
    box.appendChild(metricRow("ready to promote", String(f.pending_promotion), "warn"));
  }
  (f.by_document || []).forEach((d) => {
    const row = el("div", "mrow mrow-sub");
    row.appendChild(el("span", "mrow-l", d.path));
    row.appendChild(el("span", "mrow-v", "+" + d.up + " / -" + d.down));
    box.appendChild(row);
  });

  // Close the loop without leaving the workspace.
  if (state.writes && f.pending_promotion) {
    const act = el("div", "card-actions");
    const btn = el("button", "mini", "Promote " + f.pending_promotion + " to fixtures");
    btn.type = "button";
    btn.addEventListener("click", () => promoteFixtures(btn));
    act.appendChild(btn);
    box.appendChild(act);
  }
}

function renderPerformance(p) {
  const box = $("performance");
  box.replaceChildren();
  cardTitle(box, "Speed");
  box.appendChild(metricRow("typical search time", fmtMs(p.median_ms), "good"));
  box.appendChild(metricRow("slowest searches", fmtMs(p.p95_ms)));
  box.appendChild(metricRow("searches run", String(p.searches || 0)));
  box.appendChild(metricRow("sources opened", String(p.source_opens || 0)));
  box.appendChild(metricRow("no-result rate", fmtPct(p.zero_hit_rate),
                            (p.zero_hit_rate || 0) > 0.25 ? "warn" : ""));
}

function renderHealth(h) {
  const box = $("health-metrics");
  box.replaceChildren();
  cardTitle(box, "Library health");
  box.appendChild(metricRow("documents", String(h.documents || 0)));
  box.appendChild(metricRow("text passages", String(h.chunks || 0)));
  box.appendChild(metricRow("total size", fmtBytes(h.bytes)));
  box.appendChild(metricRow("passages per document",
                            String(h.avg_chunks_per_doc || 0)));
  if (h.documents_without_chunks) {
    box.appendChild(metricRow("documents with no text",
                              String(h.documents_without_chunks), "warn"));
  }
  (h.by_type || []).forEach((r) => {
    const row = el("div", "mrow mrow-sub");
    row.appendChild(el("span", "mrow-l", "." + r.filetype));
    row.appendChild(el("span", "mrow-v", String(r.docs)));
    box.appendChild(row);
  });
  box.appendChild(el("p", "micro", "last indexed " + (h.last_indexed || "never")));
}

/* ------------------------------------------------------------------- notes */

async function loadNotes() {
  const q = state.collection ? "?collection=" + encodeURIComponent(state.collection) : "";
  const data = await api("/api/notes" + q);
  const box = $("notes");
  box.replaceChildren();
  const notes = (data && Array.isArray(data.notes)) ? data.notes : [];
  countUp($("note-count"), notes.length);
  $("notes-empty").hidden = notes.length > 0;

  notes.forEach((n) => {
    const card = el("div", "note");
    card.appendChild(el("div", "note-title", n.title || "(untitled)"));
    card.appendChild(el("div", "note-body", n.body || ""));
    const foot = el("div", "note-foot");
    foot.appendChild(el("span", "micro", String(n.ts || "").slice(0, 16).replace("T", " ")));
    const cites = Array.isArray(n.citations) ? n.citations.length : 0;
    if (cites) {
      const open = el("button", "mini", cites + " source" + (cites > 1 ? "s" : ""));
      open.type = "button";
      open.addEventListener("click", () => {
        const c = n.citations[0];
        openDoc(String(c.path), c.collection || null,
                Number.isInteger(c.seq) ? c.seq : null);
      });
      foot.appendChild(open);
    }
    if (state.writes) {
      const del = el("button", "note-del", "remove");
      del.type = "button";
      del.addEventListener("click", async () => {
        await postJSON("/api/notes/delete", { id: n.id });
        animTo(card, { opacity: 0, x: 20, height: 0, duration: 0.28,
                       ease: "power2.in", onComplete: loadNotes }) || loadNotes();
      });
      foot.appendChild(del);
    }
    card.appendChild(foot);
    box.appendChild(card);
  });
  anim(box.querySelectorAll(".note"),
       { opacity: 0, y: 10, duration: 0.36, stagger: 0.04, ease: "power3.out" });
}

/* ------------------------------------------------------------------ upload */

/* ------------------------------------------------------- add-sources dialog */

const uploadQueue = [];
let uploading = false;

function openAddBox() {
  state.returnFocus = document.activeElement;
  $("addbox").hidden = false;
  $("scrim").hidden = false;
  fillCollectionChoices();
  showAddTab("files");
  anim($("addbox"), { y: 18, opacity: 0, duration: 0.34, ease: "power3.out" });
  $("addbox-close").focus();
}

function closeAddBox() {
  if (uploading) return;   // never vanish mid-batch
  $("addbox").hidden = true;
  if ($("reader").hidden) $("scrim").hidden = true;
  const back = state.returnFocus;
  state.returnFocus = null;
  if (back && typeof back.focus === "function") back.focus();
}

function showAddTab(which) {
  const files = which === "files";
  $("pane-files").hidden = !files;
  $("pane-text").hidden = files;
  $("tab-files").className = "tab" + (files ? " is-on" : "");
  $("tab-text").className = "tab" + (files ? "" : " is-on");
  $("upgo").textContent = files ? "Add to collection" : "Add note";
}

// The collection is chosen here, not typed blind: existing ones come from
// the index, and a new one is created simply by naming it.
function fillCollectionChoices() {
  const sel = $("add-coll");
  const keep = sel.value;
  sel.replaceChildren();
  (state.collections || []).forEach((c) => {
    const o = document.createElement("option");
    o.value = c.name;
    o.textContent = c.name + " (" + c.documents + ")";
    sel.appendChild(o);
  });
  const o = document.createElement("option");
  o.value = "__new__";
  o.textContent = "+ New collection…";
  sel.appendChild(o);
  sel.value = keep || state.collection ||
    ((state.collections || [])[0] || {}).name || "__new__";
  syncCollectionChoice();
}

function syncCollectionChoice() {
  const isNew = $("add-coll").value === "__new__";
  $("add-newcoll").hidden = !isNew;
  const note = $("coll-note");
  if (isNew) {
    note.textContent = "Lowercase letters, digits, - or _. A new collection is " +
      "created the moment the first document lands in it.";
  } else {
    note.textContent = "Documents will join this collection.";
  }
}

function chosenCollection() {
  if ($("add-coll").value !== "__new__") return $("add-coll").value;
  return $("add-newcoll").value.trim().toLowerCase();
}

/* ------------------------------------------------------------ file analysis */

function extOf(name) {
  const i = String(name).lastIndexOf(".");
  return i < 0 ? "" : String(name).slice(i).toLowerCase();
}

// Each file is checked BEFORE anything is sent, so an unsupported or
// oversized file is visible immediately rather than failing mid-batch.
function analyse(file) {
  const ext = extOf(file.name);
  const types = state.extensions || [];
  if (types.length && types.indexOf(ext) < 0) {
    return { ok: false, why: "unsupported type" };
  }
  if (!file.size) return { ok: false, why: "file is empty" };
  const cap = state.maxUpload || 0;
  if (cap && file.size > cap) return { ok: false, why: "over " + fmtBytes(cap) };
  return { ok: true, why: "" };
}

function queueFiles(list) {
  let added = 0;
  for (const f of list || []) {
    if (uploadQueue.some((q) => q.file.name === f.name && q.file.size === f.size)) continue;
    const verdict = analyse(f);
    uploadQueue.push({
      file: f, label: "", note: verdict.ok ? "" : verdict.why,
      state: verdict.ok ? "waiting" : "blocked",
    });
    added += 1;
  }
  renderQueue();
  return added;
}

function renderQueue() {
  const box = $("upqueue");
  box.replaceChildren();
  uploadQueue.forEach((item, idx) => {
    const row = el("div", "upitem upitem-" + item.state);
    row.appendChild(el("span", "upitem-type", (extOf(item.file.name) || "?").replace(".", "")));

    const body = el("div", "upitem-body");
    body.appendChild(el("div", "upitem-name", item.label || item.file.name));
    const meta = el("div", "upitem-file");
    meta.textContent = (item.label ? item.file.name + " · " : "") + fmtBytes(item.file.size);
    body.appendChild(meta);
    row.appendChild(body);

    row.appendChild(el("span", "upitem-note", item.note || item.state));

    if (!uploading) {
      const drop = el("button", "upitem-x", "✕");
      drop.type = "button";
      drop.title = "Remove from this batch";
      drop.addEventListener("click", () => {
        uploadQueue.splice(idx, 1);
        renderQueue();
      });
      row.appendChild(drop);
    }
    box.appendChild(row);
  });
  renderAnalysis();
}

function renderAnalysis() {
  const note = $("analysis");
  if (!uploadQueue.length) { note.hidden = true; return; }
  const ready = uploadQueue.filter((q) => q.state !== "blocked");
  const blocked = uploadQueue.length - ready.length;
  const bytes = ready.reduce((sum, q) => sum + (q.file.size || 0), 0);
  const kinds = {};
  ready.forEach((q) => {
    const k = (extOf(q.file.name) || "?").replace(".", "");
    kinds[k] = (kinds[k] || 0) + 1;
  });
  const parts = Object.keys(kinds).sort().map((k) => kinds[k] + " " + k.toUpperCase());
  note.hidden = false;
  note.textContent =
    ready.length + (ready.length === 1 ? " file" : " files") +
    (parts.length ? " (" + parts.join(", ") + ")" : "") +
    " · " + fmtBytes(bytes) +
    (blocked ? " · " + blocked + " cannot be added" : "");
}

/* ---------------------------------------------------------------- importing */

async function uploadOne(item, collection) {
  const fd = new FormData();
  fd.append("collection", collection);
  fd.append("file", item.file);
  const send = () => fetch("/api/upload", {
    method: "POST", headers: { "X-Uplink": "1" }, body: fd,
  });
  let r = await send();
  if (r.status === 503) {
    await new Promise((done) => setTimeout(done, 1500));
    r = await send();
  }
  const data = await r.json().catch(() => ({ error: "bad response" }));
  if (data.error) {
    item.state = "failed";
    item.note = String(data.error).slice(0, 90);
  } else {
    item.state = "done";
    item.label = data.label || item.file.name;
    item.note = (data.indexed === 0 && data.unchanged > 0)
      ? "already indexed"
      : data.chunks + (data.chunks === 1 ? " passage" : " passages");
  }
}

async function importPastedText(collection) {
  const title = $("text-title").value.trim();
  const body = $("text-body").value.trim();
  if (!body) { $("upmsg").textContent = "nothing to add — the text is empty"; return; }

  // A pasted note becomes a Markdown file and takes the ordinary upload
  // path, so it is chunked, cited and verified like any other source.
  const safe = (title || "note").toLowerCase().replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "").slice(0, 60) || "note";
  const text = (title ? "# " + title + "\n\n" : "") + body;
  const file = new File([text], safe + ".md", { type: "text/markdown" });

  const item = { file: file, label: "", note: "", state: "waiting" };
  uploadQueue.length = 0;
  uploadQueue.push(item);
  uploading = true;
  $("upgo").disabled = true;
  item.state = "indexing";
  renderQueue();
  try { await uploadOne(item, collection); }
  catch (e) { item.state = "failed"; item.note = "upload failed"; }
  uploading = false;
  $("upgo").disabled = false;
  renderQueue();
  $("upmsg").textContent = item.state === "done"
    ? "added to '" + collection + "'" : "could not add: " + item.note;
  if (item.state === "done") {
    $("text-title").value = ""; $("text-body").value = "";
    await refreshAfterImport();
  }
}

async function refreshAfterImport() {
  await loadStatus();
  await loadSources();
  fillCollectionChoices();
  scheduleMetrics(200);
}

async function processQueue() {
  const collection = chosenCollection();
  if (!collection) {
    $("upmsg").textContent = "name the new collection first";
    $("add-newcoll").focus();
    return;
  }
  if (!/^[a-z0-9][a-z0-9_-]{0,49}$/.test(collection)) {
    $("upmsg").textContent =
      "'" + collection + "' is not a valid name — lowercase letters, digits, - or _";
    return;
  }
  if (!$("pane-text").hidden) { await importPastedText(collection); return; }

  const pending = uploadQueue.filter((q) => q.state === "waiting" || q.state === "failed");
  if (!pending.length) {
    $("upmsg").textContent = uploadQueue.length
      ? "nothing left to add" : "choose some files first";
    return;
  }

  uploading = true;
  $("upgo").disabled = true;
  let done = 0, failed = 0;
  for (const item of pending) {
    item.state = "indexing";
    item.note = "";
    renderQueue();
    $("upmsg").textContent = "adding " + (done + failed + 1) + " of " + pending.length +
      " to '" + collection + "'\u2026";
    try { await uploadOne(item, collection); }
    catch (e) { item.state = "failed"; item.note = "upload failed"; }
    if (item.state === "done") done += 1; else failed += 1;
    renderQueue();
  }
  uploading = false;
  $("upgo").disabled = false;
  $("upmsg").textContent = done + " added to '" + collection + "'" +
    (failed ? ", " + failed + " failed — press again to retry those" : "");
  await refreshAfterImport();
}

/* ------------------------------------------------------------------ wiring */

$("add-source").addEventListener("click", openAddBox);
$("addbox-close").addEventListener("click", closeAddBox);
$("addbox-cancel").addEventListener("click", closeAddBox);
$("tab-files").addEventListener("click", () => showAddTab("files"));
$("tab-text").addEventListener("click", () => showAddTab("text"));
$("add-coll").addEventListener("change", syncCollectionChoice);
$("upgo").addEventListener("click", processQueue);

$("drop").addEventListener("dragover", (ev) => {
  ev.preventDefault();
  $("drop").classList.add("hot");
});
$("drop").addEventListener("dragleave", () => $("drop").classList.remove("hot"));
$("drop").addEventListener("drop", (ev) => {
  ev.preventDefault();
  $("drop").classList.remove("hot");
  if (ev.dataTransfer && ev.dataTransfer.files.length) queueFiles(ev.dataTransfer.files);
});
$("file").addEventListener("change", () => {
  queueFiles($("file").files);
  $("file").value = "";   // so re-picking the same file still fires
});

/* ------------------------------------------------------------------- chrome */

$("coll").addEventListener("change", async (ev) => {
  state.collection = ev.target.value;
  // Refresh status as well: the collection counts in this very dropdown go
  // stale otherwise, which is how a page silently disagrees with the index.
  await loadStatus();
  await loadSources();
  await loadNotes();
  await loadMetrics();
});

const THEME_KEY = "uplink-theme";
function applyTheme(mode) {
  if (mode) document.documentElement.setAttribute("data-theme", mode);
  else document.documentElement.removeAttribute("data-theme");
}
$("theme-toggle").addEventListener("click", () => {
  const current = document.documentElement.getAttribute("data-theme");
  const next = current === "dark" ? "light" : "dark";
  applyTheme(next);
  try { localStorage.setItem(THEME_KEY, next); } catch (e) { /* private mode */ }
  anim($("theme-toggle"), { rotate: -180, duration: 0.5, ease: "back.out(2)" });
});
try {
  const saved = localStorage.getItem(THEME_KEY);
  if (saved) applyTheme(saved);
} catch (e) { /* ignore */ }

/* ------------------------------------------------------------------- start */

function driftOrbs() {
  if (!MOTION || !HAS_GSAP) return;
  const g = window.gsap;
  g.to(".orb-a", { x: 60, y: 40, duration: 18, repeat: -1, yoyo: true, ease: "sine.inOut" });
  g.to(".orb-b", { x: -50, y: 60, duration: 22, repeat: -1, yoyo: true, ease: "sine.inOut" });
  g.to(".orb-c", { x: 40, y: -50, duration: 26, repeat: -1, yoyo: true, ease: "sine.inOut" });
}

function introduce() {
  anim(".topbar", { opacity: 0, y: -14, duration: 0.5, ease: "power3.out" });
  anim(".panel", { opacity: 0, y: 22, duration: 0.6, stagger: 0.08, ease: "power3.out" });
}

async function boot() {
  driftOrbs();
  introduce();
  await loadStatus();
  await loadSources();
  await loadNotes();
  await loadMetrics();
  restoreHistory();
}

boot();
