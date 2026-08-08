"""Uplink web UI — a three-panel workspace with localhost-gated writes.

`python -m uplink serve` starts a stdlib-only HTTP server. The server has no
framework and no build step; the interface lives in `uplink/static/`
(index.html, app.css, app.js) plus one vendored animation library (GSAP —
see static/NOTICE.md). Nothing is fetched from a network at runtime, which
is what keeps the offline and privacy guarantees literal.

    GET  /                     the workspace (Sources / Conversation / Studio)
    GET  /static/<asset>       bundled UI assets (whitelisted names only)
    GET  /api/sources?collection=        documents + opening questions
    GET  /api/search?q=&k=&collection=&path=…   results as JSON (read-only)
    GET  /api/status           index statistics as JSON
    GET  /api/doc?path=&collection=&seq=&start=&limit=   source text (read-only)
    GET  /api/notes?collection=          saved notes
    GET  /api/ask/<id>         poll a queued question
    GET  /reports/<kind>.html  generated reports, if present
    POST /api/upload           add a document to a collection   (localhost only)
    POST /api/feedback         thumbs up/down on a result       (localhost only)
    POST /api/ask              queue a question for the brain   (localhost only)
    POST /api/notes            save an answer                   (localhost only)
    POST /api/notes/delete     tombstone a note                 (localhost only)

Repeated `path=` parameters on /api/search narrow retrieval to those
documents — the source checkboxes are that parameter, so deselecting a
source genuinely removes it from retrieval rather than hiding its results.

Security posture:
- binds to 127.0.0.1 unless --host says otherwise (Tailscale exposure is a
  deliberate operator choice, never a default);
- THE LOCALHOST-ONLY WRITE RULE: the two write endpoints exist only when the
  server is bound to a loopback address. Bound to anything wider, every POST
  returns 403 and the UI never renders the controls — a phone on the
  tailnet is ask-only by construction, not by convention;
- browser-borne attacks on the loopback server are closed off twice: every
  request's Host header must name the server itself (defeats DNS
  rebinding), and write POSTs must carry the custom X-Uplink header and a
  loopback Origin (defeats CSRF — a hostile web page cannot attach custom
  headers cross-origin without a CORS preflight this server never grants);
- search paths still open the database read-only (SQLite mode=ro); the only
  database writes go through the same indexer the CLI uses;
- uploads are constrained: extension whitelist, size cap, sanitized bare
  filename, stored under data/uploads/<collection>/. A collection bound to a
  real folder refuses uploads rather than writing into the operator's files;
- corpus text reaches the page as JSON and is rendered with textContent,
  never innerHTML, so document content cannot inject markup or script;
- the query log and feedback log are local JSONL files next to the database.

Read surface, stated plainly: `/api/doc` serves untruncated chunk text so a
citation can be opened and checked — verification is the point of a RAG UI.
That makes reads wider than `/api/search` alone (which truncates to 1200
chars per hit), so anyone who can reach the port can page a document out in
full. Reads are GETs and therefore unaffected by the localhost-only write
rule: "ask-only" describes writes, not confidentiality. Both search and
document reads are appended to the local query log. Before binding beyond
loopback, decide that corpus reads by anyone on that network are acceptable.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import socket
import sqlite3
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import asks, db, metrics
from .extractors import SUPPORTED_EXTENSIONS
from .feedback import VALID_VOTES, append_jsonl, promote
from .notes import add_note, delete_note, list_notes
from .originals import OriginalUnavailable, is_viewable, resolve_original
from .search import hits_to_dicts, search
from .suggest import suggestions

MAX_QUERY_LEN = 500
MAX_K = 25
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_FEEDBACK_BYTES = 10 * 1024
MAX_NOTE_BYTES = 64 * 1024
MAX_SOURCE_FILTER = 200

STATIC_DIR = Path(__file__).resolve().parent / "static"

# Whitelist, not a directory listing: these are the only files servable.
STATIC_FILES = {
    "index.html": "text/html; charset=utf-8",
    "case-study.html": "text/html; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "gsap.min.js": "text/javascript; charset=utf-8",
}

_BUILD_CACHE: dict = {"key": None, "id": "dev"}


def build_id() -> str:
    """A short fingerprint of the served interface.

    A browser tab keeps running whatever JavaScript it loaded, so after the
    server restarts with new assets an open page can silently behave like an
    older version — which is exactly how a source selection failed to travel
    and the operator was told, wrongly, that they had selected nothing. The
    page carries this stamp and compares it against the live server, so a
    stale tab announces itself instead of misleading.
    """
    key = []
    for name in sorted(STATIC_FILES):
        asset = STATIC_DIR / name
        try:
            stat = asset.stat()
            key.append((name, int(stat.st_mtime_ns), stat.st_size))
        except OSError:
            key.append((name, 0, 0))
    key_tuple = tuple(key)
    if _BUILD_CACHE["key"] != key_tuple:
        digest = hashlib.sha256(repr(key_tuple).encode("utf-8")).hexdigest()[:12]
        _BUILD_CACHE.update({"key": key_tuple, "id": digest})
    return _BUILD_CACHE["id"]


LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._ -]")


# SQLite binds 64-bit integers; anything wider raises OverflowError at query
# time, which would surface as a 500 with a traceback.
_INT64_MAX = 2 ** 63 - 1


def _int_param(params: dict, name: str, default: int | None) -> int | None:
    """Parse one integer query parameter, falling back on anything odd."""
    raw = (params.get(name) or [""])[0].strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if not -_INT64_MAX <= value <= _INT64_MAX:
        return default
    return value


# Terms whose conventional casing a title-caser would get wrong. Generic
# enough to serve any technical corpus; extend rather than special-case.
_TERM_CASING = {
    "macbook": "MacBook", "imac": "iMac", "emac": "eMac", "ipad": "iPad",
    "iphone": "iPhone", "ipod": "iPod", "ibook": "iBook", "isight": "iSight",
    "powerbook": "PowerBook", "powermac": "Power Mac", "macos": "macOS",
    "airport": "AirPort", "firewire": "FireWire", "magsafe": "MagSafe",
    "thunderbolt": "Thunderbolt", "bootcamp": "Boot Camp",
    "pdf": "PDF", "csv": "CSV", "tsv": "TSV", "usb": "USB", "hdmi": "HDMI",
    "ghz": "GHz", "mhz": "MHz", "vesa": "VESA", "faq": "FAQ", "sop": "SOP",
    "kpi": "KPI", "api": "API", "sec": "SEC", "nist": "NIST", "cdc": "CDC",
    "cisa": "CISA", "bls": "BLS", "cui": "CUI", "iot": "IoT", "sp": "SP",
    "ai": "AI", "hr": "HR", "it": "IT", "us": "US", "eu": "EU", "uk": "UK",
    "q1": "Q1", "q2": "Q2", "q3": "Q3", "q4": "Q4", "fy": "FY",
    "10k": "10-K", "10q": "10-Q", "8k": "8-K",
    "4k": "4K", "5k": "5K", "1080p": "1080p", "retina": "Retina",
}

# Abbreviations that appear in documentation filenames and mean nothing to
# a reader as-is.
_DOC_TYPES = {
    "ug": "User Guide", "userguide": "User Guide", "usersguide": "User Guide",
    "qsg": "Quick Start Guide", "qs": "Quick Start", "quickstart": "Quick Start",
    "gettingstarted": "Getting Started", "ipig": "Important Product Information",
    "info": "Information", "essentials": "Essentials", "manual": "Manual",
}

# A leading vendor asset code ("ma658_", "doc12-") identifies a file in some
# catalogue; it tells a reader nothing and only crowds the label.
_ASSET_ID = re.compile(r"^[a-z]{1,3}\d{2,6}$", re.I)
_LETTER_DIGIT = re.compile(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])")


def _cased(word: str) -> str:
    low = word.lower()
    if low in _TERM_CASING:
        return _TERM_CASING[low]
    if low in _DOC_TYPES:
        return _DOC_TYPES[low]
    if word.isdigit():
        return word
    return word[:1].upper() + word[1:]


# Longest-first so "powerbook" wins over any shorter prefix. Restricted to
# terms of four characters or more (plus a couple of explicit units) because
# short ones match inside ordinary words.
_PEELABLE = sorted(
    {t for t in list(_TERM_CASING) + list(_DOC_TYPES) if len(t) >= 4}
    | {"ghz", "mhz", "inch"},
    key=len, reverse=True,
)


def _peel(token: str) -> list[str]:
    """Split a run-together filename token using known terms.

    Filenames like "powerbookg4" and "33ghzgettingstarted" carry no
    separators at all; peeling recognised terms off the front recovers the
    words a reader expects to see.
    """
    low = token.lower()
    parts: list[str] = []
    i = 0
    while i < len(low):
        hit = next((term for term in _PEELABLE if low.startswith(term, i)), None)
        if hit:
            parts.append(low[i:i + len(hit)])
            i += len(hit)
            continue
        j = i + 1
        while j < len(low):
            if low[j].isdigit() != low[i].isdigit():
                break
            if any(low.startswith(term, j) for term in _PEELABLE):
                break
            j += 1
        parts.append(low[i:j])
        i = j
    return parts


def readable_label(title: str | None, path: str) -> str:
    """A human-readable name for a document.

    A real title — a PDF's own /Title, a Markdown H1 — is used as written,
    because the author's name for the document beats anything derived from
    a filename. Only when the extractor could fall back no further than the
    filename is one constructed: vendor asset codes dropped, separators and
    letter/digit runs split, known abbreviations expanded, and terms cased
    the way they are conventionally written.
    """
    stem = Path(path).stem
    clean = (title or "").strip()
    if clean and clean.lower() != stem.lower():
        return clean

    raw = [w for w in re.split(r"[-_.\s]+", stem) if w]
    if raw and _ASSET_ID.match(raw[0]) and len(raw) > 1:
        raw = raw[1:]

    words: list[str] = []
    for token in raw:
        low = token.lower()
        # A token that is itself a known term keeps its shape: splitting
        # "10k" into "10" and "k" would lose the form people recognise.
        if low in _TERM_CASING or low in _DOC_TYPES:
            words.append(token)
            continue
        words.extend(_peel(token))

    out: list[str] = []
    for word in words:
        cased = _cased(word)
        # "g" + "4" -> "G4"; a stray letter before a number is a model code.
        if out and word.isdigit() and len(out[-1]) == 1 and out[-1].isalpha():
            out[-1] = out[-1].upper() + word
            continue
        # "14" + "inch" -> "14-inch", the way specifications are written.
        if cased.lower() == "inch" and out and out[-1].isdigit():
            out[-1] = out[-1] + "-inch"
            continue
        out.append(cased)
    return " ".join(out) or stem


def disambiguate_labels(sources: list[dict]) -> None:
    """Make every label in a listing unique, in place.

    Vendors reuse one title across many documents — a dozen files all called
    "MacBook Pro" is a source list you cannot navigate. Where labels
    collide, the distinguishing part of the filename is appended, so the
    reader can tell which is which without opening them.
    """
    counts: dict[str, int] = {}
    for item in sources:
        counts[item["label"]] = counts.get(item["label"], 0) + 1
    for item in sources:
        if counts.get(item["label"], 0) < 2:
            continue
        hint = readable_label(None, item["path"])
        label_words = {w.lower() for w in item["label"].split()}
        extra = [w for w in hint.split() if w.lower() not in label_words]
        if extra:
            item["label"] = f"{item['label']} — {' '.join(extra)}"

    # Anything still colliding differs only by its asset code; use it rather
    # than leave two rows a reader cannot tell apart.
    still: dict[str, int] = {}
    for item in sources:
        still[item["label"]] = still.get(item["label"], 0) + 1
    for item in sources:
        if still.get(item["label"], 0) > 1:
            stem = Path(item["path"]).stem.split("_")[0].split("-")[0]
            item["label"] = f"{item['label']} ({stem})"


def _doc_pairs(raw: list[str] | None) -> list[tuple[str, str]]:
    """Parse repeated `doc=<collection>/<path>` values into identity pairs.

    A document's identity is (collection, path) — the schema keys them that
    way, and the same filename legitimately exists in several collections.
    Collection slugs never contain '/', so the first slash is an unambiguous
    separator. Malformed values are dropped rather than widening the filter.
    """
    out: list[tuple[str, str]] = []
    for value in raw or []:
        coll, sep, path = value.partition("/")
        if not sep or not path.strip():
            continue
        try:
            db.validate_collection(coll)
        except ValueError:
            continue
        out.append((coll, path.strip()))
    return out


def sanitize_filename(raw: str) -> str:
    """Reduce a client-supplied filename to a safe bare name, or raise.

    Path separators are stripped (basename only), unsafe characters removed,
    and hidden/empty names rejected. The extension must be one the indexer
    supports — anything else has no reason to enter the corpus.
    """
    name = raw.replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = _SAFE_FILENAME_RE.sub("", name)
    if not name or name.startswith(".") or len(name) > 150:
        raise ValueError("invalid filename")
    ext = Path(name).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS or len(Path(name).stem.strip()) == 0:
        allowed = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise ValueError(f"unsupported file type '{ext or '(none)'}' — allowed: {allowed}")
    return name


def parse_multipart(body: bytes, content_type: str) -> dict[str, tuple[str, bytes]]:
    """Minimal multipart/form-data parser (stdlib `cgi` is gone in 3.13).

    Returns {field_name: (filename, value_bytes)}; filename is "" for plain
    fields. Preserves part content byte-exactly: only the single transport
    CRLF before the next boundary is removed, never the payload's own
    trailing newlines (a stripped final 0x0A corrupts uploads and makes the
    stored sha256 hash a file the operator never sent). Raises ValueError on
    anything that does not look like multipart.
    """
    m = re.search(r'boundary="?([^";]+)"?', content_type)
    if not m:
        raise ValueError("missing multipart boundary")
    delim = b"--" + m.group(1).encode("ascii", "replace")
    fields: dict[str, tuple[str, bytes]] = {}
    segments = body.split(delim)
    for seg in segments[1:]:  # segments[0] is the preamble
        if seg.startswith(b"--"):
            break  # closing delimiter
        if seg.startswith(b"\r\n"):
            seg = seg[2:]
        head, sep, payload = seg.partition(b"\r\n\r\n")
        if not sep:
            continue
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]  # the transport CRLF before the delimiter
        disp = ""
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-disposition"):
                disp = line.decode("utf-8", "replace")
        # `name=` must not match inside `filename=` regardless of parameter
        # order (RFC 7578 fixes neither order nor our luck).
        name_m = re.search(r'(?<![a-zA-Z])name="([^"]*)"', disp)
        if not name_m:
            continue
        file_m = re.search(r'filename="([^"]*)"', disp)
        fields[name_m.group(1)] = (file_m.group(1) if file_m else "", payload)
    if not fields:
        raise ValueError("empty multipart body")
    return fields


def make_handler(
    db_path: Path,
    reports_dir: Path | None,
    writes_enabled: bool,
    bind_host: str = "127.0.0.1",
    fixtures_dir: Path | None = None,
    demo_mode: bool = False,
):
    data_dir = db_path.parent
    # Public demo: the one write that stays open on a public bind is asking
    # a question, and each visitor gets a small allowance of those.
    ask_limiter = None
    if demo_mode:
        from .ratelimit import RateLimiter
        ask_limiter = RateLimiter(data_dir / "ratelimit.json")
    uploads_root = data_dir / "uploads"
    query_log = data_dir / "query-log.jsonl"
    feedback_log = data_dir / "feedback.jsonl"
    asks_dir = data_dir / "asks"
    notes_path = data_dir / "notes.jsonl"
    # Eval history belongs to the project, not the CWD: a server on one
    # database must never report another corpus's accuracy numbers.
    fixtures = Path(fixtures_dir) if fixtures_dir else Path("fixtures")
    allowed_hosts = {bind_host.lower(), "127.0.0.1", "localhost", "::1", "[::1]"}
    # Serializes browser uploads: index_folder holds a write transaction for
    # its whole run, so without this a second simultaneous upload waits out
    # busy_timeout and dies with "database is locked".
    write_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "Uplink"

        def _client_ip(self) -> str:
            """The visitor's address, for the demo's per-visitor ask limit.

            Behind the hosting platform's proxy the socket peer is the proxy,
            not the visitor — the platform puts the real address first in
            X-Forwarded-For. That header is only trusted in demo mode, where
            a proxy is guaranteed to be in front of us.
            """
            if demo_mode:
                fwd = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
                if fwd:
                    return fwd
            return str(self.client_address[0])

        def _host_ok(self) -> bool:
            """The Host header must name this server or be an IP literal —
            defeats DNS rebinding, where evil.com re-points to 127.0.0.1 so a
            hostile page's fetches become same-origin and corpus text becomes
            readable off-machine. IP-literal Hosts are safe (rebinding needs
            a DNS name) and keep deliberate wide binds (0.0.0.0, a Tailscale
            IP) reachable by address."""
            if demo_mode:
                # A public demo is reached by whatever hostname the platform
                # assigns; rebinding defends localhost secrets, and a demo
                # holds none.
                return True
            host = (self.headers.get("Host") or "").strip().lower()
            if not host:
                return True  # HTTP/1.0 clients; browsers always send Host
            if host.startswith("["):  # [::1]:8180
                host = host.split("]", 1)[0].lstrip("[")
            else:
                host = host.rsplit(":", 1)[0]
            if host in allowed_hosts or f"[{host}]" in allowed_hosts:
                return True
            try:
                ipaddress.ip_address(host)
                return True
            except ValueError:
                return False

        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            if not self._host_ok():
                self._json(403, {"error": "unrecognized Host header"})
                return
            url = urlparse(self.path)
            try:
                if url.path == "/":
                    self._static("index.html")
                elif url.path == "/case-study":
                    self._static("case-study.html")
                elif url.path.startswith("/static/"):
                    self._static(url.path.removeprefix("/static/"))
                elif url.path == "/api/sources":
                    self._api_sources(url)
                elif url.path == "/api/metrics":
                    self._api_metrics()
                elif url.path == "/api/file":
                    self._api_file(url)
                elif url.path == "/api/notes":
                    self._api_notes(url)
                elif url.path == "/api/search":
                    self._api_search(url)
                elif url.path == "/api/status":
                    self._api_status()
                elif url.path.startswith("/api/ask/"):
                    self._api_ask_status(url.path.removeprefix("/api/ask/"))
                elif url.path == "/api/doc":
                    self._api_doc(url)
                elif url.path.startswith("/reports/"):
                    self._report(url.path)
                else:
                    self._send(404, "text/plain; charset=utf-8", b"not found")
            except FileNotFoundError as exc:
                self._json(404, {"error": str(exc)})
            except ValueError as exc:  # bad parameter (e.g. collection name)
                self._json(400, {"error": str(exc)})
            except Exception as exc:  # never leak a traceback to the client
                self._json(500, {"error": f"{type(exc).__name__}"})
                raise

        def do_POST(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            demo_ask = demo_mode and url.path == "/api/ask"
            if not writes_enabled and not demo_ask:
                # The localhost-only rule. Drain the body first or Windows
                # clients see a connection reset instead of the status.
                # In demo mode exactly one write survives a public bind:
                # asking a question — uploads, feedback, notes, and promote
                # stay localhost-only.
                self._drain()
                self._json(403, {"error": "writes are enabled only when bound to localhost"})
                return
            if not self._host_ok():
                self._drain()
                self._json(403, {"error": "unrecognized Host header"})
                return
            # CSRF defense: a browser will not attach a custom header to a
            # cross-origin request without a CORS preflight, and this server
            # never grants one — so requiring X-Uplink (plus a loopback
            # Origin when one is sent) means a hostile page the operator
            # happens to visit cannot write into the corpus. On the public
            # demo the loopback-origin rule would block every legitimate
            # browser, so X-Uplink alone carries the CSRF defense there.
            origin = (self.headers.get("Origin") or "").strip()
            if origin and not demo_ask:
                o_host = urlparse(origin).hostname or ""
                if o_host not in ("127.0.0.1", "localhost", "::1"):
                    self._drain()
                    self._json(403, {"error": "cross-origin writes are not allowed"})
                    return
            if not self.headers.get("X-Uplink"):
                self._drain()
                self._json(403, {"error": "missing X-Uplink header"})
                return
            if demo_ask and ask_limiter is not None:
                allowed, message = ask_limiter.check(self._client_ip())
                if not allowed:
                    self._drain()
                    self._json(429, {"error": message})
                    return
            try:
                if url.path == "/api/upload":
                    self._api_upload()
                elif url.path == "/api/feedback":
                    self._api_feedback()
                elif url.path == "/api/ask":
                    self._api_ask()
                elif url.path == "/api/promote":
                    self._api_promote()
                elif url.path == "/api/notes":
                    self._api_note_add()
                elif url.path == "/api/notes/delete":
                    self._api_note_delete()
                else:
                    self._drain()
                    self._send(404, "text/plain; charset=utf-8", b"not found")
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            except sqlite3.OperationalError:
                # Another writer (a CLI index run) holds the database lock.
                self._json(503, {"error": "index is busy — try again shortly"})
            except Exception as exc:
                self._json(500, {"error": f"{type(exc).__name__}"})
                raise

        def do_PUT(self) -> None:  # noqa: N802
            self._drain()
            self._send(405, "text/plain; charset=utf-8", b"method not allowed")

        do_DELETE = do_PATCH = do_PUT

        # ------------------------------------------------------------- reads

        def _api_search(self, url) -> None:
            params = parse_qs(url.query)
            query = (params.get("q") or [""])[0].strip()[:MAX_QUERY_LEN]
            collection = (params.get("collection") or [""])[0].strip() or None
            if collection is not None:
                collection = db.validate_collection(collection)
            try:
                k = min(MAX_K, max(1, int((params.get("k") or ["8"])[0])))
            except ValueError:
                k = 8
            if not query:
                self._json(400, {"error": "missing q parameter"})
                return
            # Source checkboxes. `scoped=1` says a selection is in force —
            # without it an empty selection would be indistinguishable from
            # "no scoping" and would silently search the whole corpus, the
            # exact inverse of what the interface promises.
            scoped = bool(params.get("scoped"))
            exclude = _doc_pairs(params.get("xdoc"))
            # Exclusion mode ("everything except these") is not an empty
            # selection: only a scoped request with no exclusions and no
            # inclusions means "nothing is selected".
            include = None if exclude else (_doc_pairs(params.get("doc")) if scoped else None)
            if include is not None and len(include) > MAX_SOURCE_FILTER:
                raise ValueError(
                    f"too many sources selected ({len(include)}); "
                    f"the limit is {MAX_SOURCE_FILTER}"
                )
            if len(exclude) > MAX_SOURCE_FILTER:
                raise ValueError(
                    f"too many sources excluded ({len(exclude)}); "
                    f"the limit is {MAX_SOURCE_FILTER}"
                )
            t0 = time.perf_counter()
            hits = search(
                db_path, query, k=k, collection=collection,
                include=include, exclude=exclude or None,
            )
            latency_ms = round((time.perf_counter() - t0) * 1000, 1)
            append_jsonl(
                query_log,
                {
                    "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "q": query,
                    "collection": collection,
                    "k": k,
                    "hits": len(hits),
                    "latency_ms": latency_ms,
                },
            )
            self._json(
                200,
                {
                    "query": query,
                    "k": k,
                    "collection": collection,
                    "latency_ms": latency_ms,
                    "hits": hits_to_dicts(hits),
                },
            )

        def _api_status(self) -> None:
            conn = db.connect_ro(db_path)
            try:
                docs = conn.execute("SELECT COUNT(*) n FROM documents").fetchone()["n"]
                chunks = conn.execute("SELECT COUNT(*) n FROM chunks").fetchone()["n"]
                latest = conn.execute(
                    "SELECT MAX(indexed_at) t FROM documents"
                ).fetchone()["t"]
                collections = db.list_collections(conn)
            finally:
                conn.close()
            self._json(
                200,
                {
                    "documents": docs,
                    "chunks": chunks,
                    "last_indexed": latest,
                    "collections": collections,
                    "writes": writes_enabled,
                    "asks": writes_enabled or demo_mode,
                    "demo": demo_mode,
                    "build": build_id(),
                    "extensions": sorted(SUPPORTED_EXTENSIONS),
                    "max_upload_bytes": MAX_UPLOAD_BYTES,
                    "reports": _available_reports(reports_dir),
                },
            )

        def _report(self, path: str) -> None:
            name = path.removeprefix("/reports/")
            # Whitelist, not path arithmetic: only the three known pages.
            if reports_dir is None or name not in (
                "health.html", "quality.html", "activity.html",
            ):
                self._send(404, "text/plain; charset=utf-8", b"not found")
                return
            page = reports_dir / name
            if not page.is_file():
                self._send(404, "text/plain; charset=utf-8", b"report not generated yet")
                return
            self._send(200, "text/html; charset=utf-8", page.read_bytes())

        def _static(self, name: str) -> None:
            """Serve a bundled asset. Whitelisted by name — the static dir is
            application code, not user content, and must never become a
            file-read surface."""
            ctype = STATIC_FILES.get(name)
            if ctype is None:
                self._send(404, "text/plain; charset=utf-8", b"not found")
                return
            asset = STATIC_DIR / name
            if not asset.is_file():
                self._send(404, "text/plain; charset=utf-8", b"asset missing")
                return
            if name == "index.html":
                body = asset.read_text(encoding="utf-8").replace("__BUILD__", build_id())
                self._send(200, ctype, body.encode("utf-8"),
                           extra={"Cache-Control": "no-cache, must-revalidate"})
                return
            # Assets are read from disk per request and must never be served
            # from a stale browser cache: a fix that the operator cannot see
            # is indistinguishable from no fix at all.
            self._send(200, ctype, asset.read_bytes(),
                       extra={"Cache-Control": "no-cache, must-revalidate"})

        def _api_sources(self, url) -> None:
            """The documents in a collection, plus opening questions — what
            the Sources panel and the empty state are built from."""
            params = parse_qs(url.query)
            collection = (params.get("collection") or [""])[0].strip() or None
            if collection is not None:
                collection = db.validate_collection(collection)
            conn = db.connect_ro(db_path)
            try:
                where, args = "", []
                if collection:
                    where, args = "WHERE d.collection = ?", [collection]
                rows = conn.execute(
                    f"""
                    SELECT d.path, d.title, d.filetype, d.collection, d.size,
                           d.indexed_at, COUNT(c.id) chunks
                    FROM documents d LEFT JOIN chunks c ON c.doc_id = d.id
                    {where}
                    GROUP BY d.id ORDER BY d.collection, d.path
                    """,
                    args,
                ).fetchall()
                collections = db.list_collections(conn)
                prompts = suggestions(conn, collection)
                roots = {
                    r["key"].removeprefix("corpus_root:"): r["value"]
                    for r in conn.execute(
                        "SELECT key, value FROM meta WHERE key LIKE 'corpus_root:%'"
                    )
                }
            finally:
                conn.close()

            sources = []
            for r in rows:
                item = dict(r)
                item["label"] = readable_label(r["title"], r["path"])
                item["filename"] = Path(r["path"]).name
                item["viewable"] = is_viewable(r["path"])
                # Whether an "Original" tab can work at all: uploads-only
                # collections and moved folders have no readable source.
                item["has_original"] = bool(roots.get(r["collection"]))
                sources.append(item)
            disambiguate_labels(sources)

            self._json(
                200,
                {
                    "collection": collection,
                    "collections": collections,
                    "sources": sources,
                    "suggestions": prompts,
                },
            )

        def _api_metrics(self) -> None:
            """Health, accuracy, latency, and the human feedback loop —
            computed from the index and the local logs, never estimated."""
            conn = db.connect_ro(db_path)
            try:
                payload = metrics.collect(
                    conn,
                    history_path=fixtures / "eval-history.jsonl",
                    query_log=query_log,
                    feedback_log=feedback_log,
                    promoted=fixtures / "promoted.jsonl",
                    db_name=db_path.name,
                    db_path=db_path,
                    fixtures_dir=fixtures,
                    asks_dir=asks_dir,
                    notes_path=notes_path,
                )
            finally:
                conn.close()
            self._json(200, payload)

        def _api_file(self, url) -> None:
            """Serve the ORIGINAL document behind a citation.

            The only request-time corpus file read in the system. The client
            names a document; the path comes from the index and is required
            to resolve inside that collection's own recorded source folder.
            """
            params = parse_qs(url.query)
            path = (params.get("path") or [""])[0].strip()
            collection = (params.get("collection") or [""])[0].strip()
            if not path or not collection:
                self._json(400, {"error": "path and collection are required"})
                return
            collection = db.validate_collection(collection)
            conn = db.connect_ro(db_path)
            try:
                asset, ctype = resolve_original(conn, collection, path)
            except OriginalUnavailable as exc:
                self._json(404, {"error": str(exc)})
                return
            finally:
                conn.close()

            append_jsonl(
                query_log,
                {
                    "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "kind": "original",
                    "path": path,
                    "collection": collection,
                },
            )
            body = asset.read_bytes()
            self._send(
                200, ctype, body,
                extra={
                    # inline so the browser can render it in the reader;
                    # nosniff (already sent) keeps the declared type binding.
                    "Content-Disposition": f'inline; filename="{asset.name}"',
                    "Cache-Control": "no-cache",
                },
            )

        def _api_notes(self, url) -> None:
            params = parse_qs(url.query)
            collection = (params.get("collection") or [""])[0].strip() or None
            if collection is not None:
                collection = db.validate_collection(collection)
            self._json(200, {"notes": list_notes(notes_path, collection)})

        def _api_doc(self, url) -> None:
            """Serve a window of one document's chunks so a citation can be
            opened and checked.

            The text comes from the INDEX, never the filesystem: the path is
            an exact parameterized lookup in `documents`, so there is no file
            read to traverse and nothing outside the corpus is reachable.
            What you see here is literally what retrieval saw.
            """
            params = parse_qs(url.query)
            path = (params.get("path") or [""])[0].strip()
            if not path:
                self._json(400, {"error": "missing path parameter"})
                return
            collection = (params.get("collection") or [""])[0].strip() or None
            if collection is not None:
                collection = db.validate_collection(collection)
            seq = _int_param(params, "seq", default=None)
            explicit_start = _int_param(params, "start", default=None)
            limit = min(40, max(1, _int_param(params, "limit", default=9) or 9))

            conn = db.connect_ro(db_path)
            try:
                if collection:
                    docs = conn.execute(
                        "SELECT id, path, title, filetype, collection FROM documents "
                        "WHERE collection = ? AND path = ?", (collection, path),
                    ).fetchall()
                else:
                    docs = conn.execute(
                        "SELECT id, path, title, filetype, collection FROM documents "
                        "WHERE path = ? ORDER BY collection", (path,),
                    ).fetchall()
                if not docs:
                    self._json(404, {"error": "document not found in the index"})
                    return
                if len(docs) > 1:
                    # Paths are relative to each collection root, so the same
                    # name can exist in several. Guessing would show the
                    # reader unrelated text as the source of a claim — fail
                    # safe and make the caller name the collection.
                    self._json(
                        409,
                        {
                            "error": f"'{path}' exists in several collections — "
                                     "name one with &collection=",
                            "collections": [d["collection"] for d in docs],
                        },
                    )
                    return
                doc = docs[0]
                total = conn.execute(
                    "SELECT COUNT(*) n FROM chunks WHERE doc_id = ?", (doc["id"],)
                ).fetchone()["n"]
                has_root = conn.execute(
                    "SELECT 1 FROM meta WHERE key = ?",
                    (f"corpus_root:{doc['collection']}",),
                ).fetchone() is not None
                # `start` pages absolutely; `seq` centres the window on a
                # cited chunk. Mixing the two silently skipped chunks when
                # paging backward, so `start` wins when both are present.
                if explicit_start is not None:
                    start = max(0, explicit_start)
                elif seq is not None:
                    start = max(0, seq - limit // 2)
                else:
                    start = 0
                rows = conn.execute(
                    "SELECT seq, section, text FROM chunks WHERE doc_id = ? "
                    "AND seq >= ? ORDER BY seq LIMIT ?",
                    (doc["id"], start, limit),
                ).fetchall()
            finally:
                conn.close()
            # Reads are logged like searches: /api/doc returns untruncated
            # text, so it belongs in the same local audit trail.
            append_jsonl(
                query_log,
                {
                    "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "kind": "doc",
                    "path": doc["path"],
                    "collection": doc["collection"],
                    "start": start,
                    "chunks": len(rows),
                },
            )
            self._json(
                200,
                {
                    "path": doc["path"],
                    "title": doc["title"],
                    "label": readable_label(doc["title"], doc["path"]),
                    "filetype": doc["filetype"],
                    "collection": doc["collection"],
                    "viewable": is_viewable(doc["path"]),
                    "has_original": has_root,
                    "total_chunks": total,
                    "start": start,
                    "chunks": [
                        {"seq": r["seq"], "section": r["section"] or "", "text": r["text"]}
                        for r in rows
                    ],
                },
            )

        def _api_ask_status(self, ask_id: str) -> None:
            """Poll one queued question. Read-only: reads the response file
            the brain session wrote (or reports pending/unknown)."""
            self._json(200, asks.get_status(asks_dir, ask_id))

        # ------------------------------------------------------ writes (localhost)

        def _api_ask(self) -> None:
            """Queue a question for the brain session (see AGENT.md).

            Behind the localhost-only write gate like every POST: remote
            ask-only access is exactly what the integration review gates,
            so today the queue adds zero remote surface."""
            body = self._read_body(MAX_FEEDBACK_BYTES)
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ValueError("expected a JSON body") from None
            if not isinstance(payload, dict):
                raise ValueError("expected a JSON object")
            q = str(payload.get("q") or "").strip()[:MAX_QUERY_LEN]
            if not q:
                raise ValueError("missing q")
            collection = payload.get("collection")
            if collection:
                collection = db.validate_collection(str(collection))
            try:
                k = min(MAX_K, max(1, int(payload.get("k") or 8)))
            except (TypeError, ValueError):
                k = 8

            # The source selection must travel WITH the question. The brain
            # answers later and cannot see the checkboxes; without this the
            # answer is composed from documents the operator excluded.
            docs = None
            if payload.get("scoped"):
                raw = payload.get("docs")
                raw = raw if isinstance(raw, list) else []
                pairs = _doc_pairs([str(d) for d in raw[:MAX_SOURCE_FILTER]])
                docs = [f"{coll}/{path}" for coll, path in pairs]

            req = asks.new_ask(asks_dir, q, collection or None, k=k, docs=docs)
            self._json(200, {"id": req["id"], "state": "pending",
                             "scoped_to": len(docs) if docs is not None else None})

        def _read_body(self, cap: int) -> bytes:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise ValueError("invalid Content-Length") from None
            if length < 0:
                # A negative length must not reach rfile.read(): read(-1)
                # reads the socket to EOF, bypassing the cap entirely.
                raise ValueError("invalid Content-Length")
            if length > cap:
                # Discard so the client receives the error instead of a reset.
                self._drain()
                raise ValueError(f"body too large (max {cap} bytes)")
            return self.rfile.read(length) if length else b""

        def _api_upload(self) -> None:
            ctype = self.headers.get("Content-Type") or ""
            if "multipart/form-data" not in ctype:
                self._drain()
                raise ValueError("expected multipart/form-data")
            body = self._read_body(MAX_UPLOAD_BYTES)
            fields = parse_multipart(body, ctype)
            filename_raw, file_bytes = fields.get("file", ("", b""))
            if not filename_raw:
                raise ValueError("missing file field")
            if not file_bytes:
                raise ValueError("the file is empty")
            _, coll_bytes = fields.get("collection", ("", b""))
            collection = db.validate_collection(
                coll_bytes.decode("utf-8", "replace").strip() or db.DEFAULT_COLLECTION
            )
            filename = sanitize_filename(filename_raw)

            target_dir = (uploads_root / collection).resolve()
            # A collection bound to a real folder must not be written to.
            conn = db.connect_ro(db_path)
            try:
                row = conn.execute(
                    "SELECT value FROM meta WHERE key=?", (f"corpus_root:{collection}",)
                ).fetchone()
            finally:
                conn.close()
            if row and Path(row["value"]) != target_dir:
                self._json(
                    409,
                    {
                        "error": (
                            f"collection '{collection}' is bound to folder "
                            f"'{row['value']}' — drop the file there and re-index, "
                            "or upload to a different collection"
                        )
                    },
                )
                return

            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / filename
            with write_lock:
                self._save_and_index(target, file_bytes, target_dir, collection, filename)

        def _save_and_index(
            self, target: Path, file_bytes: bytes, target_dir: Path,
            collection: str, filename: str,
        ) -> None:
            if target.exists() and target.read_bytes() != file_bytes:
                # Sanitization strips directories, so distinct sources can
                # collide on one name — refuse rather than silently replace
                # an already-indexed document.
                self._json(
                    409,
                    {
                        "error": (
                            f"'{filename}' already exists in '{collection}' with "
                            "different content — rename the file or remove the "
                            "existing one first"
                        )
                    },
                )
                return
            target.write_bytes(file_bytes)

            from .indexer import index_folder

            try:
                stats = index_folder(target_dir, db_path, collection=collection)
            except Exception:
                # Never leave an unindexed file behind: it would silently
                # join the corpus on the next unrelated index run.
                target.unlink(missing_ok=True)
                raise
            own_errors = [e for e in stats.errors if e.startswith(filename + ":")]
            if own_errors:
                target.unlink(missing_ok=True)
                self._json(400, {"error": f"file could not be indexed: {own_errors[0]}"})
                return

            # Report the document by the name it will be KNOWN by, not the
            # filename it arrived as: the extractor has just read its real
            # title, so a batch can show what it actually indexed rather
            # than a list of vendor asset codes.
            conn = db.connect_ro(db_path)
            try:
                row = conn.execute(
                    "SELECT title FROM documents WHERE collection = ? AND path = ?",
                    (collection, filename),
                ).fetchone()
            finally:
                conn.close()
            label = readable_label(row["title"] if row else None, filename)

            self._json(
                200,
                {
                    "saved": filename,
                    "label": label,
                    "collection": collection,
                    "indexed": stats.indexed,
                    "unchanged": stats.unchanged,
                    "chunks": stats.chunks,
                    "errors": stats.errors,
                },
            )

        def _json_body(self, cap: int) -> dict:
            body = self._read_body(cap)
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ValueError("expected a JSON body") from None
            if not isinstance(payload, dict):
                raise ValueError("expected a JSON object")
            return payload

        def _api_promote(self) -> None:
            """Turn thumbs-up votes into golden-question fixtures.

            The same operation as `uplink promote`, exposed so the loop can
            be closed without leaving the workspace. Localhost-gated like
            every write: it appends to the project's fixture file.
            """
            self._drain()
            added, skipped = promote(feedback_log, fixtures / "promoted.jsonl")
            self._json(
                200,
                {
                    "added": added,
                    "skipped": skipped,
                    "fixtures": str((fixtures / "promoted.jsonl").as_posix()),
                },
            )

        def _api_note_add(self) -> None:
            payload = self._json_body(MAX_NOTE_BYTES)
            body = str(payload.get("body") or "").strip()
            if not body:
                raise ValueError("a note needs a body")
            collection = payload.get("collection")
            if collection:
                collection = db.validate_collection(str(collection))
            note = add_note(
                notes_path,
                title=str(payload.get("title") or "")[:300],
                body=body,
                citations=payload.get("citations"),
                collection=collection or None,
                kind=str(payload.get("kind") or "answer"),
            )
            self._json(200, {"note": note})

        def _api_note_delete(self) -> None:
            payload = self._json_body(MAX_FEEDBACK_BYTES)
            note_id = str(payload.get("id") or "")
            if not delete_note(notes_path, note_id):
                self._json(404, {"error": "no such note"})
                return
            self._json(200, {"deleted": note_id})

        def _api_feedback(self) -> None:
            body = self._read_body(MAX_FEEDBACK_BYTES)
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ValueError("expected a JSON body") from None
            if not isinstance(payload, dict):
                raise ValueError("expected a JSON object")
            q = str(payload.get("q") or "").strip()[:MAX_QUERY_LEN]
            path = str(payload.get("path") or "").strip()[:500]
            vote = payload.get("vote")
            if not q or not path or vote not in VALID_VOTES:
                raise ValueError("feedback needs q, path, and vote of 'up' or 'down'")
            collection = payload.get("collection")
            if collection:
                collection = db.validate_collection(str(collection))
            # The path must name an indexed document. Unvalidated, a forged
            # short path (e.g. "e") promoted into a fixture matches nearly
            # every result by substring and inflates the quality numbers.
            conn = db.connect_ro(db_path)
            try:
                if collection:
                    known = conn.execute(
                        "SELECT 1 FROM documents WHERE collection = ? AND path = ?",
                        (collection, path),
                    ).fetchone()
                else:
                    known = conn.execute(
                        "SELECT 1 FROM documents WHERE path = ?", (path,)
                    ).fetchone()
            finally:
                conn.close()
            if not known:
                raise ValueError("path does not name an indexed document")
            # An answer-level vote carries every document the answer cited,
            # so promoting it yields a fixture that accepts any of them —
            # which is what "this answer was right" actually means.
            extra_paths = payload.get("paths")
            paths: list[str] = []
            if isinstance(extra_paths, list):
                seen = {path}
                paths = [path]
                for candidate in extra_paths[:MAX_SOURCE_FILTER]:
                    text = str(candidate or "").strip()[:500]
                    if text and text not in seen:
                        seen.add(text)
                        paths.append(text)

            entry = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "q": q,
                "path": path,
                "seq": payload.get("seq") if isinstance(payload.get("seq"), int) else None,
                "vote": vote,
                "kind": "answer" if str(payload.get("kind")) == "answer" else "hit",
                "collection": collection or None,
            }
            if len(paths) > 1:
                entry["paths"] = paths
            append_jsonl(feedback_log, entry)
            self._json(200, {"recorded": vote})

        # ---------------------------------------------------------- plumbing

        def _drain(self) -> None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return
            while length > 0:
                read = self.rfile.read(min(length, 1 << 16))
                if not read:
                    break
                length -= len(read)

        def _json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=True).encode("ascii")
            self._send(code, "application/json", body)

        def _send(self, code: int, ctype: str, body: bytes,
                  extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args) -> None:
            # Quiet by default; the CLI prints the URL once at startup.
            pass

    return Handler


def _available_reports(reports_dir: Path | None) -> list[str]:
    if reports_dir is None:
        return []
    return sorted(
        p.name for p in reports_dir.glob("*.html")
        if p.name in ("health.html", "quality.html", "activity.html")
    )


def serve(db_path: str | Path, host: str, port: int, reports_dir: str | Path | None,
          fixtures_dir: str | Path | None = None) -> None:
    db_file = Path(db_path)
    writes_enabled = host in LOOPBACK_HOSTS
    demo_mode = os.environ.get("UPLINK_DEMO") == "1"
    if writes_enabled and not db_file.exists():
        # First-run flow: serve an empty index so the operator can upload
        # their first document through the browser.
        db.connect_rw(db_file).close()
    # Fail fast with the standard message if the index does not exist.
    probe: sqlite3.Connection = db.connect_ro(db_file)
    probe.close()
    handler = make_handler(
        db_file, Path(reports_dir) if reports_dir else None, writes_enabled, host,
        fixtures_dir=Path(fixtures_dir) if fixtures_dir else None,
        demo_mode=demo_mode,
    )
    if demo_mode:
        # The demo answers its own asks with the Claude API — no human
        # session required. Without a key, search still works and Ask AI
        # reports the brain as unavailable rather than hanging forever.
        from .api_brain import start_thread
        brain = start_thread(db_file, db_file.parent / "asks")
        print("demo mode: ask limit active, API brain "
              + ("running" if brain else "DISABLED (no ANTHROPIC_API_KEY)"))

    class _Server(ThreadingHTTPServer):
        # ThreadingHTTPServer is AF_INET-only by default; an IPv6 bind
        # (::1, or a Tailscale IPv6 address) needs the right family.
        address_family = socket.AF_INET6 if ":" in host else socket.AF_INET

    httpd = _Server((host, port), handler)
    mode = "writes: localhost-enabled" if writes_enabled else "read-only (non-loopback bind)"
    print(f"Uplink serving http://{host}:{port}  (db: {db_file}, {mode})")
    print("Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


