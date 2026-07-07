"""Browser review page + streaming local server for ``local-clean``.

A static HTML file cannot move files to Trash, so ``local-clean`` runs a tiny
local HTTP server in a background thread while classification proceeds on the
main thread. Newly flagged images are *published* into the server and the open
page polls ``/items`` and appends them live — so review can start within seconds
instead of after the whole (potentially multi-hour) classification finishes.

The user may trash several rounds during a session; each token-guarded
``POST /trash`` moves that selection and the server keeps running. A
``POST /finish`` (the page's Finish button) ends the session; the command then
reports the accumulated outcome. Thumbnails are served (``/thumb/<n>``) rather
than inlined, so the page stays light no matter how many images are flagged.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import parse_qs, urlsplit

from .logutil import get_logger
from .trash import TrashResult

logger = get_logger(__name__)


@dataclass
class FlaggedItem:
    index: int
    path: Path
    rel: str
    category: str
    confidence: float
    reason: str
    size: int


@dataclass
class TrashOutcome:
    moved: list[str] = field(default_factory=list)          # rel paths
    failed: list[tuple[str, str]] = field(default_factory=list)  # (rel, error)


def _human_size(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if f < 1024 or unit == "GB":
            return f"{int(f)} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} GB"


def _item_payload(it: FlaggedItem) -> dict:
    return {
        "index": it.index,
        "rel": it.rel,
        "category": it.category,
        "confidence": round(it.confidence, 2),
        "reason": it.reason,
        "size": _human_size(it.size),
    }


def render_page(token: str) -> str:
    """Return the static review shell (inline CSS/JS, no templating dep).

    Item data is not embedded — the page fetches it from ``/items``.
    """
    return _TEMPLATE.replace("__TOKEN__", token)


class _Handler(BaseHTTPRequestHandler):
    server_version = "LocalCleanReview/1.0"

    def log_message(self, fmt, *args):  # noqa: ANN001 - quiet the default logging
        logger.debug("review: " + fmt, *args)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    def do_GET(self) -> None:
        srv: "ReviewServer" = self.server.review  # type: ignore[attr-defined]
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/":
            self._send(200, srv.page.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/items":
            qs = parse_qs(parsed.query)
            try:
                since = int(qs.get("since", ["0"])[0])
            except (ValueError, IndexError):
                self._json(400, {"error": "bad since"})
                return
            self._json(200, srv.snapshot(since))
            return
        if path.startswith("/thumb/"):
            self._serve_thumb(srv, path)
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def _serve_thumb(self, srv: "ReviewServer", path: str) -> None:
        raw = path[len("/thumb/") :]
        try:
            n = int(raw)  # int-only: no path traversal possible
        except ValueError:
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        data = srv.thumb_bytes(n)
        if data is None:
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        self._send(200, data, "image/jpeg")

    def do_POST(self) -> None:
        srv: "ReviewServer" = self.server.review  # type: ignore[attr-defined]
        path = urlsplit(self.path).path
        if path not in ("/trash", "/finish"):
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        if self.headers.get("X-Clean-Token") != srv.token:
            self._json(403, {"error": "bad token"})
            return
        if path == "/finish":
            srv.request_finish()
            self._json(200, srv.finish_payload())
            return
        # /trash
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            ids = [int(i) for i in body.get("ids", [])]
        except (ValueError, TypeError):
            self._json(400, {"error": "bad request"})
            return
        outcome = srv.do_trash(ids)
        self._json(200, {
            "moved": outcome.moved,
            "failed": [{"rel": r, "error": e} for r, e in outcome.failed],
        })


class ReviewServer:
    """Streams flagged images to a browser and applies trash decisions.

    Runs its HTTP server on a daemon thread (:meth:`start`). The main thread
    publishes items and progress; the server thread only mutates lock-protected
    state and never prints, so terminal output (tqdm/typer) stays clean.
    """

    def __init__(
        self,
        thumbs_dir: Path,
        trash_fn: Callable[[list[Path]], list[TrashResult]],
        token: str,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self.thumbs_dir = Path(thumbs_dir)
        self.trash_fn = trash_fn
        self.token = token
        self.page = render_page(token)

        self._lock = threading.Lock()
        self._items: list[FlaggedItem] = []
        self._by_index: dict[int, FlaggedItem] = {}
        self._trashed: set[int] = set()
        self._classified = 0
        self._total = 0
        self._done = False
        self._finish = threading.Event()
        self.outcome = TrashOutcome()          # accumulated across all trash rounds

        self._thread: threading.Thread | None = None
        self._closed = False

        self._httpd = ThreadingHTTPServer((host, port), _Handler)
        self._httpd.review = self  # type: ignore[attr-defined]

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}/"

    # --- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True, name="review-server"
        )
        self._thread.start()

    def close(self) -> None:
        """Idempotent; safe whether or not :meth:`start` ran."""
        if self._closed:
            return
        self._closed = True
        if self._thread is not None:
            self._httpd.shutdown()
            self._thread.join(timeout=5)
        self._httpd.server_close()

    # --- main-thread producers -----------------------------------------------

    def publish(self, item: FlaggedItem) -> None:
        with self._lock:
            assert item.index == len(self._items), "indices must be monotonic"
            self._items.append(item)
            self._by_index[item.index] = item

    def set_progress(self, classified: int, total: int) -> None:
        with self._lock:
            self._classified = classified
            self._total = total

    def mark_done(self) -> None:
        with self._lock:
            self._done = True

    @property
    def item_count(self) -> int:
        with self._lock:
            return len(self._items)

    @property
    def finish_requested(self) -> bool:
        return self._finish.is_set()

    def wait_finished(self) -> None:
        """Block until the page (or a caller) requests finish. Ctrl-C propagates."""
        self._finish.wait()

    # --- handler-facing ------------------------------------------------------

    def request_finish(self) -> None:
        self._finish.set()

    def snapshot(self, since: int) -> dict:
        with self._lock:
            fresh = [
                _item_payload(it)
                for it in self._items[since:]
                if it.index not in self._trashed
            ]
            return {
                "items": fresh,
                "next": len(self._items),   # explicit cursor; trashed filtering
                "classified": self._classified,   # breaks "cursor == count received"
                "total": self._total,
                "done": self._done,
            }

    def finish_payload(self) -> dict:
        with self._lock:
            return {
                "moved": list(self.outcome.moved),
                "failed": [{"rel": r, "error": e} for r, e in self.outcome.failed],
            }

    def thumb_bytes(self, index: int) -> bytes | None:
        p = self.thumbs_dir / f"{index}.jpg"
        if not p.exists():
            return None
        return p.read_bytes()

    def do_trash(self, ids: Sequence[int]) -> TrashOutcome:
        # (a) Under lock: resolve ids, skip unknown/already-trashed, mark trashed
        # up front so a concurrent/double POST resolves those ids to nothing.
        with self._lock:
            wanted = [
                self._by_index[i]
                for i in ids
                if i in self._by_index and i not in self._trashed
            ]
            self._trashed.update(it.index for it in wanted)

        # (b) OUTSIDE the lock: trashing shells out to Finder and can take
        # minutes for a big selection — holding the lock would stall publish().
        results = self.trash_fn([it.path for it in wanted])
        by_path = {str(r.path): r for r in results}
        round_outcome = TrashOutcome()
        failed_indices: list[int] = []
        for it in wanted:
            r = by_path.get(str(it.path))
            if r is not None and r.ok:
                round_outcome.moved.append(it.rel)
            else:
                err = r.error if r is not None else "no result"
                round_outcome.failed.append((it.rel, err or "unknown error"))
                failed_indices.append(it.index)

        # (c) Re-acquire: un-mark failures (retryable) and fold into the total.
        with self._lock:
            for idx in failed_indices:
                self._trashed.discard(idx)
            self.outcome.moved += round_outcome.moved
            self.outcome.failed += round_outcome.failed
        return round_outcome


_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>local-clean review</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 14px/1.4 -apple-system, system-ui, sans-serif; }
  header {
    position: sticky; top: 0; z-index: 10; padding: 12px 16px;
    background: Canvas; border-bottom: 1px solid rgba(128,128,128,.3);
    display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
  }
  header .count { font-weight: 600; font-size: 15px; }
  .prog { font-size: 13px; opacity: .7; }
  #status { font-size: 12px; color: #2a8a4a; font-weight: 600; }
  .spacer { margin-left: auto; }
  button {
    font: inherit; padding: 6px 12px; border-radius: 6px;
    border: 1px solid rgba(128,128,128,.4); background: Canvas; color: inherit;
    cursor: pointer;
  }
  button:hover { background: rgba(128,128,128,.12); }
  button:disabled { opacity: .5; cursor: default; }
  button.danger {
    background: #d13438; border-color: #b02a2e; color: #fff; font-weight: 600;
  }
  button.danger:hover { background: #b02a2e; }
  .chip { font-size: 12px; padding: 4px 10px; }
  .grid {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: 12px; padding: 16px;
  }
  .card {
    border: 2px solid transparent; border-radius: 10px; overflow: hidden;
    background: rgba(128,128,128,.08); cursor: pointer; display: flex;
    flex-direction: column;
  }
  .card.selected { border-color: #d13438; background: rgba(209,52,56,.10); }
  .card img { width: 100%; aspect-ratio: 1; object-fit: cover; display: block;
    background: rgba(128,128,128,.15); }
  .meta { padding: 8px 10px; display: flex; flex-direction: column; gap: 3px; }
  .badge { align-self: flex-start; font-size: 11px; text-transform: uppercase;
    letter-spacing: .04em; padding: 2px 7px; border-radius: 999px;
    background: rgba(128,128,128,.25); }
  .rel { font-size: 12px; opacity: .75; word-break: break-all; }
  .reason { font-size: 12px; opacity: .9; }
  .sub { font-size: 11px; opacity: .6; }
  .done { padding: 40px 20px; text-align: center; max-width: 640px; margin: 0 auto; }
  .done h2 { font-size: 20px; }
  .fail { color: #d13438; text-align: left; }
</style>
</head>
<body>
<header>
  <span class="count" id="count">Loading…</span>
  <span class="prog" id="progress"></span>
  <span id="status"></span>
  <span class="spacer"></span>
  <span id="chips"></span>
  <button id="all">Select all</button>
  <button id="none">Deselect all</button>
  <button id="finish">Finish</button>
  <button class="danger" id="trash" disabled>Move to Trash</button>
</header>
<main id="main"><div class="grid" id="grid"></div></main>
<script>
const TOKEN = "__TOKEN__";
const cards = new Map();        // index -> card element
const relToIndex = new Map();   // rel -> index
const selected = new Set();     // selected indices (checked = will be trashed)
let since = 0, done = false, finished = false, pollFails = 0;
let lastClassified = 0, lastTotal = 0;
let statusTimer = null;

function setStatus(msg) {
  const el = document.getElementById("status");
  el.textContent = msg;
  if (statusTimer) clearTimeout(statusTimer);
  statusTimer = setTimeout(() => { el.textContent = ""; }, 4000);
}

function addCard(it) {
  if (cards.has(it.index)) return;
  const card = document.createElement("div");
  card.className = "card selected";
  card.dataset.category = it.category;
  card.onclick = () => {
    if (selected.has(it.index)) { selected.delete(it.index); card.classList.remove("selected"); }
    else { selected.add(it.index); card.classList.add("selected"); }
    updateCounts();
  };
  const img = document.createElement("img");
  img.loading = "lazy";
  img.src = "/thumb/" + it.index;
  card.appendChild(img);
  const meta = document.createElement("div");
  meta.className = "meta";
  const badge = document.createElement("span"); badge.className = "badge"; badge.textContent = it.category;
  const rel = document.createElement("span"); rel.className = "rel"; rel.textContent = it.rel;
  const reason = document.createElement("span"); reason.className = "reason"; reason.textContent = it.reason;
  const sub = document.createElement("span"); sub.className = "sub";
  sub.textContent = Math.round(it.confidence * 100) + "% · " + it.size;
  meta.append(badge, rel, reason, sub);
  card.appendChild(meta);
  document.getElementById("grid").appendChild(card);
  cards.set(it.index, card);
  relToIndex.set(it.rel, it.index);
  selected.add(it.index);
}

function updateCounts() {
  document.getElementById("count").textContent =
    selected.size + " of " + cards.size + " selected for deletion";
  document.getElementById("progress").textContent =
    done ? (cards.size + " flagged — classification complete")
         : ("Classifying… " + lastClassified + "/" + lastTotal);
  const btn = document.getElementById("trash");
  btn.textContent = "Move " + selected.size + " to Trash";
  btn.disabled = selected.size === 0;
}

function buildChips() {
  const counts = {};
  for (const card of cards.values()) {
    const cat = card.dataset.category;
    counts[cat] = (counts[cat] || 0) + 1;
  }
  const wrap = document.getElementById("chips");
  wrap.innerHTML = "";
  for (const cat of Object.keys(counts).sort()) {
    const b = document.createElement("button");
    b.className = "chip";
    b.textContent = cat + " (" + counts[cat] + ")";
    b.onclick = () => {
      const catCards = [...cards.entries()].filter(([i, c]) => c.dataset.category === cat);
      const anyOff = catCards.some(([i, c]) => !selected.has(i));
      for (const [i, c] of catCards) {
        if (anyOff) { selected.add(i); c.classList.add("selected"); }
        else { selected.delete(i); c.classList.remove("selected"); }
      }
      updateCounts();
    };
    wrap.appendChild(b);
  }
}

async function poll() {
  if (finished) return;
  try {
    const resp = await fetch("/items?since=" + since);
    if (!resp.ok) throw new Error("status " + resp.status);
    const data = await resp.json();
    pollFails = 0;
    const added = data.items.length;
    for (const it of data.items) addCard(it);
    since = data.next;
    lastClassified = data.classified;
    lastTotal = data.total;
    if (data.done) done = true;
    if (added) buildChips();
    updateCounts();
    if (done) {
      finished = true;
      if (cards.size === 0) showNothingFlagged();
      return;
    }
  } catch (e) {
    pollFails++;
    if (pollFails >= 3) { sessionEnded(); return; }
  }
  setTimeout(poll, 2500);
}

async function doTrash() {
  if (!selected.size) return;
  if (!confirm("Move " + selected.size + " file(s) to the Trash?")) return;
  const btn = document.getElementById("trash");
  btn.disabled = true; btn.textContent = "Moving…";
  const ids = [...selected];
  try {
    const resp = await fetch("/trash", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Clean-Token": TOKEN },
      body: JSON.stringify({ ids }),
    });
    const data = await resp.json();
    for (const rel of (data.moved || [])) {
      const idx = relToIndex.get(rel);
      if (idx !== undefined) {
        const card = cards.get(idx);
        if (card) card.remove();
        cards.delete(idx);
        selected.delete(idx);
        relToIndex.delete(rel);
      }
    }
    const nMoved = (data.moved || []).length;
    const nFailed = (data.failed || []).length;
    buildChips();
    updateCounts();
    setStatus("Moved " + nMoved + " to Trash" + (nFailed ? " — " + nFailed + " failed" : ""));
    if (done && cards.size === 0) { finishSession(); return; }
  } catch (e) {
    setStatus("Trash failed: " + e);
  } finally {
    if (!finished) updateCounts();
  }
}

async function finishSession() {
  if (finished) return;
  finished = true;
  try {
    const resp = await fetch("/finish", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Clean-Token": TOKEN },
      body: "{}",
    });
    const data = await resp.json();
    showDone(data);
  } catch (e) {
    sessionEnded();
  }
}

function showDone(data) {
  document.querySelector("header").style.display = "none";
  const moved = (data.moved || []).length;
  const failed = data.failed || [];
  const main = document.getElementById("main");
  main.innerHTML = "";
  const div = document.createElement("div");
  div.className = "done";
  const h = document.createElement("h2");
  h.textContent = "Moved " + moved + " file(s) to the Trash.";
  div.appendChild(h);
  if (failed.length) {
    const p = document.createElement("p"); p.className = "fail";
    p.textContent = failed.length + " could not be moved:";
    div.appendChild(p);
    const ul = document.createElement("ul"); ul.className = "fail";
    for (const f of failed) {
      const li = document.createElement("li");
      li.textContent = f.rel + " — " + f.error;
      ul.appendChild(li);
    }
    div.appendChild(ul);
  }
  const p2 = document.createElement("p");
  p2.textContent = "You can close this tab. The command has finished.";
  div.appendChild(p2);
  main.appendChild(div);
}

function showNothingFlagged() {
  document.querySelector("header").style.display = "none";
  const main = document.getElementById("main");
  main.innerHTML = "";
  const div = document.createElement("div");
  div.className = "done";
  const h = document.createElement("h2");
  h.textContent = "Nothing flagged.";
  const p = document.createElement("p");
  p.textContent = "All small images look like real photos. You can close this tab.";
  div.append(h, p);
  main.appendChild(div);
}

function sessionEnded() {
  finished = true;
  document.querySelector("header").style.display = "none";
  const main = document.getElementById("main");
  main.innerHTML = "";
  const div = document.createElement("div");
  div.className = "done";
  const h = document.createElement("h2");
  h.textContent = "Session ended.";
  const p = document.createElement("p");
  p.textContent = "The command has exited — check the terminal for the summary. You can close this tab.";
  div.append(h, p);
  main.appendChild(div);
}

document.getElementById("all").onclick = () => {
  for (const [i, c] of cards) { selected.add(i); c.classList.add("selected"); }
  updateCounts();
};
document.getElementById("none").onclick = () => {
  for (const [i, c] of cards) { selected.delete(i); c.classList.remove("selected"); }
  updateCounts();
};
document.getElementById("trash").onclick = doTrash;
document.getElementById("finish").onclick = () => {
  if (confirm("Finish the review session and exit the command?")) finishSession();
};

updateCounts();
poll();
</script>
</body>
</html>
"""
