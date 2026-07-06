"""Browser review page + one-shot local server for ``local-clean``.

A static HTML file cannot move files to Trash, so ``local-clean`` runs a tiny
local HTTP server: it renders a grid of the flagged images (all pre-selected
for deletion), the user deselects any keepers in the browser, and a single
token-guarded POST tells the still-running command which files to trash. The
server then shuts itself down and the command reports the outcome.

Thumbnails are served (``/thumb/<n>``) rather than inlined as base64: the
flagged set can be thousands of images, and a multi-hundred-MB self-contained
document would be unusable. The server has to exist for the POST anyway, so
serving thumbnails lazily costs nothing extra.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Sequence

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


def render_page(items: Sequence[FlaggedItem], token: str) -> str:
    """Return the self-contained review page (inline CSS/JS, no templating dep)."""
    payload = [
        {
            "index": it.index,
            "rel": it.rel,
            "category": it.category,
            "confidence": round(it.confidence, 2),
            "reason": it.reason,
            "size": _human_size(it.size),
        }
        for it in items
    ]
    # Embed as JSON; neutralize any "</script>" that could break out of the tag.
    items_json = json.dumps(payload).replace("</", "<\\/")
    return _TEMPLATE.replace("__ITEMS_JSON__", items_json).replace("__TOKEN__", token)


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

    def do_GET(self) -> None:
        srv: "ReviewServer" = self.server.review  # type: ignore[attr-defined]
        if self.path == "/" or self.path.startswith("/?"):
            self._send(200, srv.page.encode("utf-8"), "text/html; charset=utf-8")
            return
        if self.path.startswith("/thumb/"):
            self._serve_thumb(srv)
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def _serve_thumb(self, srv: "ReviewServer") -> None:
        raw = self.path[len("/thumb/") :]
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
        if self.path != "/trash":
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        if self.headers.get("X-Clean-Token") != srv.token:
            self._send(403, b'{"error":"bad token"}', "application/json")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            ids = [int(i) for i in body.get("ids", [])]
        except (ValueError, TypeError):
            self._send(400, b'{"error":"bad request"}', "application/json")
            return

        outcome = srv.do_trash(ids)
        result = {
            "moved": outcome.moved,
            "failed": [{"rel": r, "error": e} for r, e in outcome.failed],
        }
        self._send(200, json.dumps(result).encode("utf-8"), "application/json")
        # Let the response flush, then stop serve_forever from another thread.
        threading.Thread(target=self.server.shutdown, daemon=True).start()


class ReviewServer:
    """Serves the review page once and applies the user's trash decision."""

    def __init__(
        self,
        items: Sequence[FlaggedItem],
        thumbs_dir: Path,
        trash_fn: Callable[[list[Path]], list[TrashResult]],
        token: str,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self.items = list(items)
        self._by_index = {it.index: it for it in self.items}
        self.thumbs_dir = Path(thumbs_dir)
        self.trash_fn = trash_fn
        self.token = token
        self.page = render_page(self.items, token)
        self.outcome: TrashOutcome | None = None

        self._httpd = ThreadingHTTPServer((host, port), _Handler)
        self._httpd.review = self  # type: ignore[attr-defined]

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}/"

    def thumb_bytes(self, index: int) -> bytes | None:
        p = self.thumbs_dir / f"{index}.jpg"
        if not p.exists():
            return None
        return p.read_bytes()

    def do_trash(self, ids: Sequence[int]) -> TrashOutcome:
        wanted = [self._by_index[i] for i in ids if i in self._by_index]
        results = self.trash_fn([it.path for it in wanted])
        by_path = {str(r.path): r for r in results}
        outcome = TrashOutcome()
        for it in wanted:
            r = by_path.get(str(it.path))
            if r is not None and r.ok:
                outcome.moved.append(it.rel)
            else:
                err = r.error if r is not None else "no result"
                outcome.failed.append((it.rel, err or "unknown error"))
        self.outcome = outcome
        return outcome

    def serve(self) -> TrashOutcome | None:
        """Block until the user submits (or Ctrl-C). None ⇒ nothing trashed."""
        try:
            self._httpd.serve_forever()
        except KeyboardInterrupt:
            return None
        finally:
            self._httpd.server_close()
        return self.outcome


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
  header .count { font-weight: 600; font-size: 15px; margin-right: auto; }
  button {
    font: inherit; padding: 6px 12px; border-radius: 6px;
    border: 1px solid rgba(128,128,128,.4); background: Canvas; color: inherit;
    cursor: pointer;
  }
  button:hover { background: rgba(128,128,128,.12); }
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
  .card .check { position: absolute; }
  .done { padding: 40px 20px; text-align: center; max-width: 640px; margin: 0 auto; }
  .done h2 { font-size: 20px; }
  .fail { color: #d13438; text-align: left; }
</style>
</head>
<body>
<header>
  <span class="count" id="count"></span>
  <button id="all">Select all</button>
  <button id="none">Deselect all</button>
  <span id="chips"></span>
  <button class="danger" id="trash"></button>
</header>
<main id="main"><div class="grid" id="grid"></div></main>
<script>
const ITEMS = __ITEMS_JSON__;
const TOKEN = "__TOKEN__";
const selected = new Set(ITEMS.map(it => it.index));

function esc(t) { const d = document.createElement("div"); d.textContent = t; return d.innerHTML; }

function buildChips() {
  const counts = {};
  for (const it of ITEMS) counts[it.category] = (counts[it.category] || 0) + 1;
  const wrap = document.getElementById("chips");
  wrap.innerHTML = "";
  for (const cat of Object.keys(counts)) {
    const b = document.createElement("button");
    b.className = "chip";
    b.textContent = cat + " (" + counts[cat] + ")";
    b.onclick = () => {
      const cards = ITEMS.filter(it => it.category === cat);
      const anyOff = cards.some(it => !selected.has(it.index));
      for (const it of cards) { anyOff ? selected.add(it.index) : selected.delete(it.index); }
      render();
    };
    wrap.appendChild(b);
  }
}

function updateCount() {
  document.getElementById("count").textContent =
    selected.size + " of " + ITEMS.length + " selected for deletion";
  document.getElementById("trash").textContent = "Move " + selected.size + " to Trash";
  document.getElementById("trash").disabled = selected.size === 0;
}

function render() {
  const grid = document.getElementById("grid");
  grid.innerHTML = "";
  for (const it of ITEMS) {
    const card = document.createElement("div");
    card.className = "card" + (selected.has(it.index) ? " selected" : "");
    card.onclick = () => {
      selected.has(it.index) ? selected.delete(it.index) : selected.add(it.index);
      render();
    };
    const img = document.createElement("img");
    img.loading = "lazy";
    img.src = "/thumb/" + it.index;
    card.appendChild(img);
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.innerHTML =
      '<span class="badge">' + esc(it.category) + "</span>" +
      '<span class="rel">' + esc(it.rel) + "</span>" +
      '<span class="reason">' + esc(it.reason) + "</span>" +
      '<span class="sub">' + Math.round(it.confidence * 100) + "% · " + esc(it.size) + "</span>";
    card.appendChild(meta);
    grid.appendChild(card);
  }
  updateCount();
}

document.getElementById("all").onclick = () => { ITEMS.forEach(it => selected.add(it.index)); render(); };
document.getElementById("none").onclick = () => { selected.clear(); render(); };
document.getElementById("trash").onclick = async () => {
  if (!selected.size) return;
  if (!confirm("Move " + selected.size + " file(s) to the Trash?")) return;
  const btn = document.getElementById("trash");
  btn.disabled = true; btn.textContent = "Moving…";
  try {
    const resp = await fetch("/trash", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Clean-Token": TOKEN },
      body: JSON.stringify({ ids: [...selected] }),
    });
    const data = await resp.json();
    showDone(data);
  } catch (e) {
    btn.disabled = false; btn.textContent = "Move to Trash";
    alert("Failed: " + e);
  }
};

function showDone(data) {
  document.querySelector("header").style.display = "none";
  const moved = (data.moved || []).length;
  const failed = data.failed || [];
  let html = "<div class='done'><h2>Moved " + moved + " file(s) to the Trash.</h2>";
  if (failed.length) {
    html += "<p class='fail'><strong>" + failed.length + " failed:</strong></p><ul class='fail'>";
    for (const f of failed) html += "<li>" + esc(f.rel) + " — " + esc(f.error) + "</li>";
    html += "</ul>";
  }
  html += "<p>You can close this tab. The command has finished.</p></div>";
  document.getElementById("main").innerHTML = html;
}

buildChips();
render();
</script>
</body>
</html>
"""
