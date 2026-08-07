"""The interface's rendering contracts, executed as real JavaScript.

The answer card, citation chips, and source reader are where brain-written
and corpus-derived JSON reaches the DOM, so their coercion rules are worth
running rather than eyeballing. Node is used only as a test tool (it ships on
both CI runners); the app itself has no build step. Skipped when node is
absent.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

APP_JS = (Path(__file__).resolve().parents[1] / "uplink" / "static" / "app.js").read_text(
    encoding="utf-8"
)

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

# A DOM small enough to be obviously correct, strict enough to catch the bugs:
# textContent is the ONLY way text enters a node, so an innerHTML regression
# in the page would fail to render at all.
DOM_SHIM = """
class El {
  constructor(tag) { this.tag = tag; this.childNodes = []; this._text = "";
                     this.className = ""; this.hidden = false; this.disabled = false;
                     this.title = ""; this.type = ""; this._on = {}; this.style = {};
                     this.classList = {
                       add: (c) => { this.className = (this.className + " " + c).trim(); },
                       remove: (c) => { this.className =
                         this.className.split(" ").filter((x) => x !== c).join(" "); },
                       toggle: (c, on) => { if (on) this.classList.add(c);
                                            else this.classList.remove(c); },
                     }; }
  getBoundingClientRect() { return {left: 0, right: 0, top: 0, bottom: 0}; }
  set textContent(v) { this._text = String(v); this.childNodes = []; }
  get textContent() {
    return this._text + this.childNodes.map((c) => c.textContent).join("");
  }
  appendChild(c) { this.childNodes.push(c); return c; }
  replaceChildren(...kids) { this.childNodes = kids; this._text = ""; }
  addEventListener(ev, fn) { (this._on[ev] = this._on[ev] || []).push(fn); }
  click() { (this._on.click || []).forEach((f) => f()); }
  scrollIntoView() {}
  setAttribute(k, v) { this[k] = v; }
  removeAttribute(k) { delete this[k]; }
  focus() {}
  querySelector() { return null; }
  querySelectorAll() { return []; }
  find(pred) {
    if (pred(this)) return this;
    for (const c of this.childNodes) { const hit = c.find(pred); if (hit) return hit; }
    return null;
  }
  all(pred, out) { out = out || [];
    if (pred(this)) out.push(this);
    this.childNodes.forEach((c) => c.all(pred, out));
    return out; }
}
const NODES = {};
const nodeFor = (id) => (NODES[id] = NODES[id] || new El("div"));
const document = {
  createElement: (t) => new El(t),
  createTextNode: (t) => { const n = new El("#text"); n.textContent = t; return n; },
  addEventListener: () => {},
};
const $ = (id) => nodeFor(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = String(text);
  return n;
};
// Motion is a no-op under test; the contract is what renders, not how it moves.
const anim = () => null;
const animTo = () => null;
let OPENED = null;
const openDoc = (path, collection, seq) => { OPENED = {call:"openDoc", path, collection, seq}; };
const openDocAt = (path, collection, start, citedSeq) =>
  { OPENED = {call:"openDocAt", path, collection, start, citedSeq}; };
const openReader = () => {};
let TAB = null;
const showTab = (which) => { TAB = which; };
const mountOriginal = () => {};
const scrollThread = () => {};
const postJSON = async () => ({ ok: true });
const loadNotes = () => {};
const state = { writes: true, collection: null, sources: [], selected: new Set(),
                reader: null, pollTimer: null };
const uploadQueue = [];
let uploading = false;
const scheduleMetrics = () => {};
const loadStatus = async () => {};
const loadSources = async () => {};
"""


def _extract(name: str) -> str:
    """Pull one top-level definition out of app.js — either a
    `function name(...) {...}` or a `const name = (...) => …;` arrow."""
    decl = f"function {name}("
    if decl in APP_JS:
        start = APP_JS.index(decl)
        depth = 0
        for i in range(start, len(APP_JS)):
            if APP_JS[i] == "{":
                depth += 1
            elif APP_JS[i] == "}":
                depth -= 1
                if depth == 0:
                    return APP_JS[start:i + 1]
        raise AssertionError(f"unterminated function {name}")

    # `const NAME = …;` — scan for the terminating semicolon while tracking
    # string and bracket state, because a naive index(";") stops inside any
    # prose that happens to contain one.
    start = APP_JS.index(f"const {name} = ")
    depth = 0
    quote = None
    i = start
    while i < len(APP_JS):
        ch = APP_JS[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == ";" and depth == 0:
            return APP_JS[start:i + 1]
        i += 1
    raise AssertionError(f"unterminated declaration {name}")


def _run(script: str, functions=("citationList", "renderAnswer", "citedPage",
                                 "renderDoc", "renderSnippet")) -> dict:
    body = "\n".join([DOM_SHIM] + [_extract(f) for f in functions] + [script])
    proc = subprocess.run(
        [NODE, "-e", body], capture_output=True, text=True, timeout=60,
        encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ----------------------------------------------------------- static assets

def test_app_js_has_no_html_sinks():
    """Corpus text must never be able to become markup."""
    code = APP_JS
    # Strip comments so the "no innerHTML here" note doesn't trip the check.
    import re

    code = re.sub(r"/\*[\s\S]*?\*/", "", code)
    code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert sink not in code, sink


def test_app_js_parses():
    proc = subprocess.run([NODE, "--check",
                           str(Path(__file__).resolve().parents[1] / "uplink" / "static" / "app.js")],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr


def test_gsap_is_vendored_not_fetched():
    """A CDN request would break both offline use and the privacy claim."""
    static = Path(__file__).resolve().parents[1] / "uplink" / "static"
    assert (static / "gsap.min.js").is_file()
    html = (static / "index.html").read_text(encoding="utf-8")
    assert "/static/gsap.min.js" in html
    for remote in ("http://", "https://", "//cdn", "unpkg", "jsdelivr", "googleapis"):
        assert remote not in html, remote


def test_ui_degrades_without_gsap():
    """Motion is optional; a missing library must not break rendering."""
    assert "HAS_GSAP" in APP_JS
    assert "typeof window.gsap" in APP_JS


# --------------------------------------------------------------- rendering

@pytest.mark.parametrize(
    "citations",
    ['"runbook.md"', "[null]", "[42]", '[{"section": "no path"}]', "{}", "0", "null"],
)
def test_malformed_citations_still_render_the_answer(citations: str):
    out = _run(textwrap.dedent(f"""
        const card = new El("div");
        renderAnswer(card, {{answer: "The backup window is 0200.", citations: {citations}}},
                     "when do backups run");
        console.log(JSON.stringify({{text: card.textContent}}));
    """))
    assert "The backup window is 0200." in out["text"]


def test_citation_chip_opens_the_cited_chunk():
    """A citation you cannot open is a claim, not evidence."""
    out = _run(textwrap.dedent("""
        const card = new El("div");
        renderAnswer(card, {answer: "A.", citations: [
          {path: "nist.pdf", section: "Page 31", seq: 44, collection: "tech"}]}, "q");
        const chip = card.find((n) => n.className === "cite-btn");
        chip.click();
        console.log(JSON.stringify({label: chip.textContent, opened: OPENED}));
    """))
    assert "nist.pdf · Page 31" in out["label"]
    assert out["opened"] == {"call": "openDoc", "path": "nist.pdf",
                             "collection": "tech", "seq": 44}


def test_citations_are_numbered_in_order():
    out = _run(textwrap.dedent("""
        const card = new El("div");
        renderAnswer(card, {answer: "A.", citations: [
          {path: "a.md", seq: 1}, {path: "b.md", seq: 2}, {path: "c.md", seq: 3}]}, "q");
        const chips = card.all((n) => n.className === "cite-btn");
        console.log(JSON.stringify({ns: chips.map((c) => c.childNodes[0].textContent)}));
    """))
    assert out["ns"] == ["1", "2", "3"]


def test_citation_without_seq_still_opens_the_document():
    out = _run(textwrap.dedent("""
        const card = new El("div");
        renderAnswer(card, {answer: "A.", citations: [
          {path: "apple.txt", section: "p. 21-22"}]}, "q");
        card.find((n) => n.className === "cite-btn").click();
        console.log(JSON.stringify(OPENED));
    """))
    assert out["path"] == "apple.txt"
    assert out["seq"] is None


def test_non_string_answer_is_coerced():
    out = _run(textwrap.dedent("""
        const card = new El("div");
        renderAnswer(card, {answer: {oops: 1}, citations: []}, "q");
        console.log(JSON.stringify({text: card.textContent}));
    """))
    assert "[object Object]" in out["text"]


def test_answer_card_is_labeled_with_its_question():
    """The saved-note title carries the question, so an answer can never be
    read as the answer to a different one."""
    out = _run(textwrap.dedent("""
        const card = new El("div");
        renderAnswer(card, {answer: "A.", citations: []}, "what is the escalation path");
        const save = card.find((n) => n.textContent === "Save to notes");
        console.log(JSON.stringify({hasSave: !!save, text: card.textContent}));
    """))
    assert out["hasSave"] is True


# ------------------------------------------------------------ source reader

def test_doc_viewer_renders_chunks_and_marks_the_cited_one():
    out = _run(textwrap.dedent("""
        renderDoc({path: "a.md", collection: "ops", total_chunks: 30, start: 18,
                   chunks: [{seq: 18, section: "S18", text: "eighteen"},
                            {seq: 19, section: "S19", text: "nineteen"}]}, 19);
        const cited = $("reader-body").find((n) => n.className.indexOf("cited") >= 0);
        console.log(JSON.stringify({all: $("reader-body").textContent,
                                    cited: cited ? cited.textContent : null,
                                    pos: $("reader-pos").textContent}));
    """))
    assert "eighteen" in out["all"] and "nineteen" in out["all"]
    assert "showing 19–20 of 30" in out["pos"]
    assert "nineteen" in out["cited"] and "eighteen" not in out["cited"]


def test_citation_without_seq_does_not_falsely_highlight_chunk_zero():
    """Number(null) === 0 would mark the cover page as 'the cited passage'."""
    out = _run(textwrap.dedent("""
        renderDoc({path: "a.md", total_chunks: 2, start: 0,
                   chunks: [{seq: 0, section: "Cover", text: "CHUNK-ZERO"},
                            {seq: 1, section: "Body", text: "CHUNK-ONE"}]}, null);
        const cited = $("reader-body").find((n) => n.className.indexOf("cited") >= 0);
        console.log(JSON.stringify({cited: cited ? cited.textContent : null}));
    """))
    assert out["cited"] is None


def test_doc_viewer_survives_malformed_payload():
    out = _run(textwrap.dedent("""
        renderDoc({path: "a.md", chunks: "not-a-list"}, null);
        renderDoc({path: "b.md", chunks: [null, 42, {seq: 1, text: "ok"}]}, null);
        console.log(JSON.stringify({text: $("reader-body").textContent}));
    """))
    assert "ok" in out["text"]


def test_reader_paging_state_uses_absolute_offsets():
    """Paging must not re-centre on a seq — that silently skipped chunks."""
    out = _run(textwrap.dedent("""
        renderDoc({path: "a.md", collection: "ops", total_chunks: 60, start: 36,
                   chunks: Array.from({length: 9}, (_, i) => ({seq: 36 + i, text: "t"}))}, 40);
        console.log(JSON.stringify(state.reader));
    """))
    assert out["start"] == 36 and out["shown"] == 9 and out["total"] == 60
    assert out["citedSeq"] == 40


# ------------------- pins from the v0.5 workspace adversarial review

def test_empty_state_is_hidden_not_detached():
    """#suggestions and #empty-title are children of #empty. Removing #empty
    made every later getElementById return null, which threw inside
    loadSources() and turned successful uploads into 'upload failed'."""
    src = _extract("clearEmptyState")
    assert ".remove()" not in src
    assert "hidden = true" in src


def test_scope_params_always_signal_an_active_selection():
    """The critical one: an empty selection must not look identical to
    'no scoping', or the server searches the whole corpus."""
    out = _run(
        textwrap.dedent("""
            state.sources = [
              {collection: "ops", path: "a.md"},
              {collection: "ops", path: "b.md"},
              {collection: "fin", path: "a.md"},
            ];
            const all = new Set(state.sources.map(docKey));

            state.selected = new Set(all);                    // everything
            const whenAll = scopeParams();

            state.selected = new Set();                       // nothing
            const whenNone = scopeParams();

            state.selected = new Set(["ops/a.md"]);           // one of three
            const whenOne = scopeParams();

            state.selected = new Set(["ops/a.md", "ops/b.md"]); // two of three
            const whenMost = scopeParams();

            console.log(JSON.stringify({whenAll, whenNone, whenOne, whenMost}));
        """),
        functions=("docKey", "scopeParams"),
    )
    assert out["whenAll"] == ""                       # no scoping needed
    assert out["whenNone"] == "&scoped=1"             # selection active, nothing in it
    assert out["whenOne"] == "&scoped=1&doc=ops%2Fa.md"
    # Two of three: the shorter side is the single exclusion.
    assert out["whenMost"] == "&scoped=1&xdoc=fin%2Fa.md"


def test_doc_key_separates_same_filename_across_collections():
    out = _run(
        textwrap.dedent("""
            console.log(JSON.stringify({
              a: docKey({collection: "alpha", path: "shared.md"}),
              b: docKey({collection: "bravo", path: "shared.md"}),
            }));
        """),
        functions=("docKey",),
    )
    assert out["a"] != out["b"]


def test_source_filter_matches_words_across_the_whole_title():
    """Typed words must match anywhere in the readable label, filename, or
    path, in any order — and the type chips narrow by extension."""
    out = _run(
        textwrap.dedent("""
            state.filter = { text: "", type: "" };
            const src = {label: "Apple 10-K FY 2024", title: "apple-10k-fy2024",
                         filename: "apple-10k-fy2024.xls",
                         path: "sec-filings/apple-10k-fy2024.xls", filetype: "xls"};
            const at = (text, type) => {
              state.filter.text = text; state.filter.type = type || "";
              return sourceMatches(src);
            };
            console.log(JSON.stringify({
              reversedWords: at("2024 apple"),
              partialWord: at("10-k"),
              pathOnly: at("sec-filings"),
              noMatch: at("banana"),
              rightType: at("", "xls"),
              wrongType: at("", "pdf"),
              typeAndText: at("apple", "pdf"),
            }));
        """),
        functions=("sourceMatches",),
    )
    assert out["reversedWords"] is True
    assert out["partialWord"] is True
    assert out["pathOnly"] is True
    assert out["noMatch"] is False
    assert out["rightType"] is True
    assert out["wrongType"] is False
    assert out["typeAndText"] is False


def test_select_all_is_keyed_by_docKey():
    """Regression: select-all once rebuilt the selection from bare paths, so
    a later single uncheck deleted a docKey that was never in the set — the
    row looked excluded while the search quietly used every source."""
    out = _run(
        textwrap.dedent("""
            state.filter = { text: "", type: "" };
            state.sources = [
              {collection: "ops", path: "a.md", filetype: "md"},
              {collection: "fin", path: "sec/a.md", filetype: "md"},
            ];
            state.selected = new Set();
            setAllSelected(true);
            const keys = Array.from(state.selected).sort();
            // Uncheck one document the way a row checkbox does.
            state.selected.delete(docKey(state.sources[0]));
            console.log(JSON.stringify({keys, scoped: scopeParams()}));
        """),
        functions=("docKey", "sourceMatches", "visibleSources", "setAllSelected",
                   "syncScope", "scopeParams"),
    )
    assert out["keys"] == ["fin/sec/a.md", "ops/a.md"]
    assert out["scoped"].startswith("&scoped=1")
    assert "fin%2Fsec%2Fa.md" in out["scoped"]


def test_select_all_with_filter_only_touches_visible_sources():
    """With a filter active the checkbox governs what you can SEE; sources
    hidden by the filter keep their selection state untouched."""
    out = _run(
        textwrap.dedent("""
            state.filter = { text: "", type: "" };
            state.sources = [
              {collection: "ops", path: "a.md", filetype: "md", label: "Alpha"},
              {collection: "ops", path: "b.pdf", filetype: "pdf", label: "Bravo"},
            ];
            state.selected = new Set(state.sources.map(docKey));
            state.filter.type = "pdf";
            setAllSelected(false);
            console.log(JSON.stringify({left: Array.from(state.selected)}));
        """),
        functions=("docKey", "sourceMatches", "visibleSources", "setAllSelected",
                   "syncScope", "scopeParams"),
    )
    assert out["left"] == ["ops/a.md"]


def test_ask_carries_the_source_selection():
    """The conversation must use exactly what the checkboxes say: the ask
    request carries the scoped flag and the selected doc keys, because the
    brain answers later and cannot see the interface."""
    src = _extract("askAI")
    assert "scopeParams()" in src
    assert "Array.from(state.selected)" in src
    assert "/api/ask" in src


def test_dictation_button_appears_only_when_the_browser_supports_it():
    """The mic ships hidden and is revealed by feature detection — a browser
    without a speech engine must never show a button that does nothing."""
    html = (Path(__file__).resolve().parents[1] / "uplink" / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    import re
    assert re.search(r'id="mic"[^>]*\bhidden\b', html)
    assert '$("mic").hidden = !SpeechRec' in APP_JS
    # The tooltip must be honest about where the audio goes.
    assert "speech service" in html


def test_dictation_appends_to_the_question_box_and_cleans_up():
    """Spoken words land in the input via .value (never markup), append to
    what was already typed, and the listening state fully unwinds."""
    out = _run(
        textwrap.dedent("""
            const navigator = { language: "en-US" };
            let started = 0;
            const SpeechRec = class {
              start() { started += 1; SpeechRec.last = this; }
              stop() { if (this.onend) this.onend(); }
            };
            let dictation = null;

            $("q").value = "apple";
            $("q").placeholder = "Ask a question";
            toggleDictation();
            const rec = SpeechRec.last;
            rec.onresult({results: [[{transcript: "total net sales "}],
                                    [{transcript: "in 2024"}]]});
            const during = { value: $("q").value, ph: $("q").placeholder,
                             cls: $("mic").className };
            rec.onend();
            console.log(JSON.stringify({during, started,
              after: { ph: $("q").placeholder, cls: $("mic").className,
                       value: $("q").value }}));
        """),
        functions=("transcriptOf", "toggleDictation"),
    )
    assert out["started"] == 1
    assert out["during"]["value"] == "apple total net sales in 2024"
    assert out["during"]["ph"] == "Listening…"
    assert "listening" in out["during"]["cls"]
    assert out["after"]["ph"] == "Ask a question"
    assert "listening" not in out["after"]["cls"]
    assert out["after"]["value"] == "apple total net sales in 2024"


def test_dictation_errors_name_the_actual_failure():
    """One generic 'could not hear that' hid four different problems with
    four different fixes. Each error code gets its own message, and an
    unknown code is shown verbatim rather than swallowed."""
    out = _run(
        textwrap.dedent("""
            const navigator = { language: "en-US" };
            const SpeechRec = class { start() { SpeechRec.last = this; } stop() {} };
            let dictation = null;

            const at = (code) => {
              dictation = null;
              $("q").placeholder = "Ask";
              toggleDictation();
              SpeechRec.last.onerror({error: code});
              const ph = $("q").placeholder;
              SpeechRec.last.onend();
              return ph;
            };
            console.log(JSON.stringify({
              silence: at("no-speech"),
              offline: at("network"),
              device: at("audio-capture"),
              unknown: at("weird-new-code"),
              aborted: at("aborted"),
            }));
        """),
        functions=("transcriptOf", "toggleDictation"),
    )
    assert "microphone" in out["silence"]
    assert "internet" in out["offline"]
    assert "input device" in out["device"]
    assert "weird-new-code" in out["unknown"]
    assert out["aborted"] == "Listening…"   # user cancel is not an error


def test_hidden_attribute_beats_author_display_rules():
    """Every panel toggled with `hidden` must actually hide.

    `.reader` sets `display: flex`, which outranks the UA stylesheet's
    `[hidden] { display: none }` — the source panel rendered on page load
    and its close button did nothing. A global override fixes the whole
    class of bug, so assert it exists and that nothing later re-breaks it.
    """
    import re

    static = Path(__file__).resolve().parents[1] / "uplink" / "static"
    css = (static / "app.css").read_text(encoding="utf-8")
    html = (static / "index.html").read_text(encoding="utf-8")

    override = re.search(r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important", css)
    assert override, "app.css must force [hidden] to win over author display rules"

    # Anything that ships hidden in the markup depends on that rule.
    assert re.search(r'id="reader"[^>]*\bhidden\b', html)
    assert re.search(r'id="scrim"[^>]*\bhidden\b', html)

    # And the override must come before the rules it has to beat, since
    # !important ties would otherwise fall back to source order.
    assert css.index("[hidden]") < css.index(".reader {")


def test_entrance_tweens_clean_up_after_themselves():
    """gsap.from writes inline styles; a tween interrupted by a re-render
    left source rows permanently half-faded and unreadable."""
    assert 'clearProps: "opacity,transform"' in _extract("anim")


def test_source_list_does_not_fade_in():
    """A source list you cannot read while it settles is worse than no
    animation at all."""
    src = _extract("renderSourceList")
    stagger = src[src.index('anim(list.querySelectorAll(".source")'):]
    stagger = stagger[:stagger.index(");")]
    assert "opacity" not in stagger


def test_excluded_sources_stay_readable():
    """Deselected rows are recoloured, not faded into illegibility."""
    css = (Path(__file__).resolve().parents[1] / "uplink" / "static" / "app.css").read_text(
        encoding="utf-8"
    )
    block = css[css.index(".source.off"):css.index(".source-body")]
    assert "opacity" not in block


def test_source_and_hit_openers_are_focusable_buttons():
    """Opening a source to verify a claim is the point of the product; it
    must not be mouse-only."""
    assert 'el("button", "source-name"' in APP_JS
    assert 'el("button", "hit-path"' in APP_JS


# --------------------------------------------------------------- snippets

def test_snippet_highlights_between_control_markers():
    out = _run(textwrap.dedent("""
        const node = new El("div");
        renderSnippet(node, "see the \\u0001install guide\\u0002 in [docs](a.md) then restart");
        const marks = node.all((n) => n.tag === "mark").map((m) => m.textContent);
        console.log(JSON.stringify({marks: marks, text: node.textContent}));
    """))
    assert out["marks"] == ["install guide"]
    assert "[docs](a.md)" in out["text"]


# ------------------------- pins for the original-file reader

def test_pdf_citation_resolves_the_cited_page():
    """PDF sections are labelled 'Page 31' — enough to open the original at
    the cited page rather than at page one."""
    out = _run(textwrap.dedent("""
        const doc = {chunks: [{seq: 4, section: "Page 12"}, {seq: 5, section: "Page 31"}]};
        console.log(JSON.stringify({
          cited: citedPage(doc, 5),
          other: citedPage(doc, 4),
          none: citedPage(doc, null),
          missing: citedPage({chunks: [{seq: 1, section: "Overview"}]}, 1),
        }));
    """), functions=("citedPage",))
    assert out == {"cited": 31, "other": 12, "none": None, "missing": None}


def test_pdf_opens_on_the_original_tab_text_stays_on_passages():
    out = _run(textwrap.dedent("""
        renderDoc({path: "a.pdf", filetype: "pdf", collection: "tech",
                   has_original: true, viewable: true, total_chunks: 2, start: 0,
                   chunks: [{seq: 0, section: "Page 1", text: "x"}]}, 0);
        const forPdf = TAB;
        renderDoc({path: "b.md", filetype: "md", collection: "ops",
                   has_original: true, viewable: true, total_chunks: 1, start: 0,
                   chunks: [{seq: 0, section: "Intro", text: "y"}]}, 0);
        console.log(JSON.stringify({forPdf: forPdf, forText: TAB}));
    """))
    assert out == {"forPdf": "original", "forText": "text"}


def test_reader_records_original_availability():
    """An upload-only collection has no source folder — the Original tab
    must know that rather than showing a broken frame."""
    out = _run(textwrap.dedent("""
        renderDoc({path: "a.md", collection: "notes", filetype: "md",
                   has_original: false, viewable: true, total_chunks: 1, start: 0,
                   chunks: [{seq: 0, text: "z"}]}, 0);
        console.log(JSON.stringify(state.reader));
    """))
    assert out["hasOriginal"] is False
    assert out["viewable"] is True


# ---------------------------- pins for the metrics panel and tooltips

def test_every_metric_label_has_an_explanation():
    """A panel of unexplained jargon is a panel nobody trusts. Every label
    rendered by a metric row must resolve in the glossary."""
    out = _run(textwrap.dedent("""
        const labels = Object.keys(GLOSSARY);
        console.log(JSON.stringify({
          count: labels.length,
          mrr: GLOSSARY["ranking quality"].length,
          hasCommand: GLOSSARY["first answer correct"][2].indexOf("uplink") >= 0,
          verified: GLOSSARY["answers verified"][0],
          // Panel labels are plain English; the industry term must survive
          // in the tooltip so experts can still map the number.
          techTermKept: GLOSSARY["ranking quality"][0].indexOf("MRR") >= 0 &&
                        GLOSSARY["first answer correct"][0].indexOf("hit@1") >= 0,
        }));
    """), functions=("GLOSSARY",))
    assert out["count"] >= 15
    assert out["mrr"] == 3, "definition, explanation, reproducing command"
    assert out["hasCommand"] is True
    assert out["verified"] == "Verification rate"
    assert out["techTermKept"] is True


def test_tooltip_targets_are_keyboard_reachable():
    """Hover-only explanations are unreachable without a mouse."""
    src = _extract("attachTip")
    assert 'setAttribute("tabindex", "0")' in src
    assert '"focus"' in src and '"blur"' in src
    assert 'aria-describedby' in src


def test_unknown_label_does_not_get_a_dead_tooltip():
    out = _run(textwrap.dedent("""
        const node = el("span", "mrow-l", "not a metric");
        attachTip(node, "not a metric");
        console.log(JSON.stringify({cls: node.className, tabindex: node.tabindex}));
    """), functions=("GLOSSARY", "attachTip", "showTip", "hideTip"))
    assert "has-tip" not in out["cls"]


def test_accuracy_card_shows_its_confidence_interval():
    """A rate without its interval is an overclaim when n is small."""
    out = _run(textwrap.dedent("""
        renderAccuracy({accuracy: {available: true, hit_at_1: 0.923, hit_at_k: 0.923,
          hit_at_1_ci: [0.667, 0.986], hit_at_k_ci: [0.667, 0.986], mrr: 0.923,
          questions: 13, k: 5, runs: 1, label: "baseline", ts: "2026-08-06T00:00",
          fixtures: "industry-golden.jsonl", series: [], delta: null}});
        console.log(JSON.stringify({text: $("accuracy").textContent}));
    """), functions=("GLOSSARY", "attachTip", "showTip", "hideTip", "sparkline",
                     "cardTitle", "metricRow", "heroBlock", "fmtDelta",
                     "fmtPct", "fmtWhen", "renderAccuracy"))
    assert "92%" in out["text"]
    assert "confidence range 67%-99%" in out["text"]
    assert "13 test questions" in out["text"]


def test_unmeasured_accuracy_says_so_instead_of_showing_a_number():
    out = _run(textwrap.dedent("""
        renderAccuracy({accuracy: {available: false}});
        console.log(JSON.stringify({text: $("accuracy").textContent}));
    """), functions=("GLOSSARY", "attachTip", "showTip", "hideTip", "sparkline",
                     "cardTitle", "metricRow", "heroBlock", "fmtDelta",
                     "fmtPct", "fmtWhen", "renderAccuracy"))
    assert "Not measured yet" in out["text"]
    assert "%" not in out["text"]


def test_failing_questions_are_listed_by_name():
    out = _run(textwrap.dedent("""
        renderFailing({available: true, hit_at_1: 0.66, mrr: 0.7,
          failing: [{q: "when is hand hygiene required"}],
          weak: [{q: "what are the phases", rank: 3}]});
        console.log(JSON.stringify({text: $("failing").textContent}));
    """), functions=("GLOSSARY", "attachTip", "showTip", "hideTip",
                     "cardTitle", "metricRow", "fmtPct", "renderFailing"))
    assert "MISS" in out["text"]
    assert "when is hand hygiene required" in out["text"]
    assert "rank 3" in out["text"]


# ------------------------- pins for live metric refreshing

def test_metrics_refresh_after_every_action_that_moves_them():
    """Numbers that go stale the moment you use the app are worse than no
    numbers: search, answers, and source opens all change what the panel
    reports, so each must schedule a refresh."""
    for fn, why in (
        ("runSearch", "latency, zero-hit rate, search count"),
        ("askAI", "answers, time-to-answer, citations per answer"),
        ("fetchDoc", "source opens and verification rate"),
    ):
        assert "scheduleMetrics()" in _extract(fn), f"{fn} must refresh: {why}"


def test_metrics_refresh_is_debounced_and_not_self_scheduling():
    """/api/metrics re-scores the live index, so a burst of actions must
    collapse to one refresh — and the timer must call loadMetrics, never
    itself, or it re-arms forever."""
    src = _extract("scheduleMetrics")
    assert "clearTimeout" in src, "a burst must collapse to one refresh"
    assert "loadMetrics()" in src
    body = src[src.index("setTimeout"):]
    assert "scheduleMetrics(" not in body, "the timer must not re-schedule itself"


# --------------------------------- multi-file upload queue

def test_upload_accepts_multiple_files():
    html = (Path(__file__).resolve().parents[1] / "uplink" / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    assert 'id="file"' in html and "multiple" in html.split('id="file"')[1][:60]


def test_queue_dedupes_and_reports_count():
    out = _run(textwrap.dedent("""
        const files = [{name:"a.pdf", size:10}, {name:"b.pdf", size:20},
                       {name:"a.pdf", size:10}];
        const added = queueFiles(files);
        console.log(JSON.stringify({added: added, queued: uploadQueue.length,
                                    msg: $("analysis").textContent}));
    """), functions=("extOf", "fmtBytes", "analyse", "renderAnalysis", "renderQueue", "queueFiles"))
    assert out["added"] == 2, "the same file picked twice must queue once"
    assert out["queued"] == 2
    assert "2 files" in out["msg"]


def test_queue_renders_a_row_per_file_with_state():
    out = _run(textwrap.dedent("""
        queueFiles([{name:"one.pdf", size:1}, {name:"two.pdf", size:2}]);
        uploadQueue[0].state = "done"; uploadQueue[0].note = "12 passages";
        uploadQueue[1].state = "failed"; uploadQueue[1].note = "unsupported file type";
        renderQueue();
        const rows = $("upqueue").all((n) => n.className.indexOf("upitem ") === 0
                                             || /^upitem-/.test(n.className) === false
                                                && n.className.indexOf("upitem") === 0);
        console.log(JSON.stringify({text: $("upqueue").textContent}));
    """), functions=("extOf", "fmtBytes", "analyse", "renderAnalysis", "renderQueue", "queueFiles"))
    assert "one.pdf" in out["text"] and "12 passages" in out["text"]
    assert "two.pdf" in out["text"] and "unsupported file type" in out["text"]


def test_uploads_are_sequential_not_parallel():
    """Each upload re-indexes and takes the database write lock, so firing
    them together would only produce lock contention."""
    src = _extract("processQueue")
    assert "for (const item of pending)" in src
    assert "await uploadOne(" in src


def test_a_failed_file_does_not_abort_the_batch():
    src = _extract("processQueue")
    assert "catch" in src, "one bad file must not stop the rest"
    assert 'item.state = "failed"' in src


def test_failed_files_are_retried_on_the_next_run():
    src = _extract("processQueue")
    assert '"waiting" || q.state === "failed"' in src, (
        "pressing Index them again should retry what failed"
    )


def test_batch_refreshes_once_not_per_file():
    """A refresh per file would re-score the live index dozens of times."""
    src = _extract("processQueue")
    body = src[src.index("for (const item of pending)"):]
    loop = body[:body.index("uploading = false")]
    assert "refreshAfterImport()" not in loop, "refreshing per file re-scores repeatedly"
    assert "refreshAfterImport()" in src, "the batch must refresh when it finishes"


def test_busy_index_is_retried_once():
    """503 means a CLI index run holds the write lock — worth one retry
    rather than failing the file."""
    src = _extract("uploadOne")
    assert "503" in src and "await send()" in src


def test_queue_shows_the_real_label_once_indexed():
    """While a batch runs, rows should read as documents rather than as
    vendor asset codes."""
    out = _run(textwrap.dedent("""
        queueFiles([{name:"ma658_macbook_air_late2008_userguide.pdf", size:1}]);
        renderQueue();
        const before = $("upqueue").textContent;
        uploadQueue[0].state = "done";
        uploadQueue[0].label = "MacBook Air User Guide";
        uploadQueue[0].note = "75 passages";
        renderQueue();
        console.log(JSON.stringify({before: before, after: $("upqueue").textContent}));
    """), functions=("extOf", "fmtBytes", "analyse", "renderAnalysis", "renderQueue", "queueFiles"))
    # Before indexing there is no title yet, so the filename stands in.
    assert "ma658_macbook_air_late2008_userguide.pdf" in out["before"]
    # After, the document's real name leads and the filename stays visible.
    assert "MacBook Air User Guide" in out["after"]
    assert "ma658_macbook_air_late2008_userguide.pdf" in out["after"]
    assert "75 passages" in out["after"]


def test_an_already_indexed_file_does_not_look_like_a_failure():
    """Re-uploading an unchanged file produces zero new passages; reporting
    '0 passages' reads as a failure when nothing went wrong."""
    src = _extract("uploadOne")
    assert "already indexed" in src
    assert "data.indexed === 0" in src


# ------------------------------- the add-sources dialog

def test_files_are_analysed_before_anything_is_sent():
    """An unsupported or oversized file should be visible immediately, not
    discovered when it fails mid-batch."""
    out = _run(textwrap.dedent("""
        state.extensions = [".pdf", ".md", ".txt"];
        state.maxUpload = 1000;
        queueFiles([
          {name: "good.pdf", size: 500},
          {name: "installer.exe", size: 500},
          {name: "huge.pdf", size: 5000},
          {name: "empty.md", size: 0},
        ]);
        console.log(JSON.stringify({
          states: uploadQueue.map((q) => q.state),
          notes: uploadQueue.map((q) => q.note),
          summary: $("analysis").textContent,
        }));
    """), functions=("extOf", "fmtBytes", "analyse", "renderAnalysis",
                     "renderQueue", "queueFiles"))
    assert out["states"] == ["waiting", "blocked", "blocked", "blocked"]
    assert out["notes"][1] == "unsupported type"
    assert "over" in out["notes"][2]
    assert out["notes"][3] == "file is empty"
    assert "1 file" in out["summary"] and "3 cannot be added" in out["summary"]


def test_analysis_summarises_types_and_size():
    out = _run(textwrap.dedent("""
        state.extensions = [".pdf", ".md"];
        state.maxUpload = 0;
        queueFiles([{name:"a.pdf", size:1024}, {name:"b.pdf", size:1024},
                    {name:"c.md", size:2048}]);
        console.log(JSON.stringify({summary: $("analysis").textContent}));
    """), functions=("extOf", "fmtBytes", "analyse", "renderAnalysis",
                     "renderQueue", "queueFiles"))
    assert "3 files" in out["summary"]
    assert "2 PDF" in out["summary"] and "1 MD" in out["summary"]
    assert "4 KB" in out["summary"]


def test_collection_choice_offers_existing_and_new():
    out = _run(textwrap.dedent("""
        state.collections = [{name:"apple", documents:247}, {name:"ops", documents:5}];
        state.collection = "ops";
        fillCollectionChoices();
        const sel = $("add-coll");
        console.log(JSON.stringify({
          options: sel.childNodes.map((o) => o.value),
          chosen: sel.value,
          newHidden: $("add-newcoll").hidden,
        }));
    """), functions=("fillCollectionChoices", "syncCollectionChoice"))
    assert out["options"] == ["apple", "ops", "__new__"]
    assert out["chosen"] == "ops", "the collection in view should be preselected"
    assert out["newHidden"] is True


def test_choosing_new_collection_reveals_the_name_field():
    out = _run(textwrap.dedent("""
        state.collections = [{name:"apple", documents:1}];
        fillCollectionChoices();
        $("add-coll").value = "__new__";
        syncCollectionChoice();
        console.log(JSON.stringify({hidden: $("add-newcoll").hidden,
                                    note: $("coll-note").textContent}));
    """), functions=("fillCollectionChoices", "syncCollectionChoice"))
    assert out["hidden"] is False
    assert "new collection" in out["note"].lower()


def test_chosen_collection_prefers_the_typed_name_when_new():
    out = _run(textwrap.dedent("""
        state.collections = [{name:"apple", documents:1}];
        fillCollectionChoices();
        $("add-coll").value = "apple";
        const existing = chosenCollection();
        $("add-coll").value = "__new__";
        $("add-newcoll").value = "  Legal Docs  ";
        console.log(JSON.stringify({existing: existing, made: chosenCollection()}));
    """), functions=("fillCollectionChoices", "syncCollectionChoice", "chosenCollection"))
    assert out["existing"] == "apple"
    assert out["made"] == "legal docs", "trimmed and lowercased before validation"


def test_pasted_text_becomes_a_markdown_source():
    """Typing a note must go through the same indexing path as a file, so it
    is chunked, cited and verifiable like any other source."""
    src = _extract("importPastedText")
    assert 'new File(' in src and '".md"' in src
    assert '"# " + title' in src, "the title becomes the document heading"
    assert "uploadOne(" in src, "it takes the ordinary upload path"


def test_dialog_will_not_close_mid_batch():
    src = _extract("closeAddBox")
    assert "if (uploading) return" in src
