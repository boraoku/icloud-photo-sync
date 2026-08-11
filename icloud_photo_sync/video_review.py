"""Browser review page + local server for ``video-clean``.

A static HTML file cannot move files to Trash or stream a multi-hundred-MB video
with seek support, so ``video-clean`` runs a tiny local HTTP server (the same
:class:`~icloud_photo_sync.review.TrashSession` machinery ``local-clean`` uses).

Unlike ``local-clean`` there is no streaming/polling: the size-sorted scan is
instant, so the full list is embedded into the page at load. The three things
the server must do that a file:// page cannot are (1) hand the grid a poster
frame per video (see :mod:`icloud_photo_sync.poster` — drawing the grid with
``<video>`` elements cost the browser a decoder per card), (2) serve each
original video with HTTP Range support — mandatory for ``<video>`` scrubbing and
required outright by Safari — and (3) move selected files to the Trash via
Finder.
"""

from __future__ import annotations

import json
import mimetypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from .logutil import get_logger
from .poster import PosterCache, format_duration
from .review import CLIENT_GONE, TrashSession, _BaseHandler, _human_size
from .trash import TrashResult

logger = get_logger(__name__)

_STREAM_CHUNK = 1 << 20  # 1 MiB reads while streaming a video body

# Container types the stdlib doesn't map (or maps inconsistently) to a
# browser-friendly video MIME type.
_VIDEO_TYPES = {
    ".mov": "video/quicktime",
    ".m4v": "video/x-m4v",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".wmv": "video/x-ms-wmv",
    ".mts": "video/mp2t",
    ".m2ts": "video/mp2t",
    ".3gp": "video/3gpp",
    ".3g2": "video/3gpp2",
}


@dataclass
class VideoItem:
    index: int
    path: Path
    rel: str
    size: int
    mtime_ns: int
    duration: float | None = None   # seconds; None when no prober could tell


def _guess_video_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in _VIDEO_TYPES:
        return _VIDEO_TYPES[suffix]
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def _parse_range(header: str | None, size: int):
    """Parse a single-range ``Range`` header against a file of ``size`` bytes.

    Returns ``None`` for "no/served-in-full" (missing header or a non-byte unit),
    the string ``"unsatisfiable"`` for a 416, or an inclusive ``(start, end)``.
    Only the first range of a multi-range request is honoured.
    """
    if not header:
        return None
    header = header.strip()
    if not header.startswith("bytes="):
        return None
    spec = header[len("bytes="):].split(",")[0].strip()
    if "-" not in spec:
        return "unsatisfiable"
    start_s, _, end_s = spec.partition("-")
    try:
        if start_s == "":                       # suffix range: last N bytes
            n = int(end_s)
            if n <= 0:
                return "unsatisfiable"
            start, end = max(0, size - n), size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
    except ValueError:
        return "unsatisfiable"
    if start >= size or start > end:
        return "unsatisfiable"
    return start, min(end, size - 1)


class _Handler(_BaseHandler):
    server_version = "VideoCleanReview/1.0"
    protocol_version = "HTTP/1.1"  # keep-alive for smooth range seeking

    def log_message(self, fmt, *args):  # noqa: ANN001 - quiet the default logging
        logger.debug("video-review: " + fmt, *args)

    def _send(
        self,
        code: int,
        body: bytes,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    def do_GET(self) -> None:
        srv: "VideoReviewServer" = self.server.review  # type: ignore[attr-defined]
        path = urlsplit(self.path).path
        if path == "/":
            self._send(200, srv.page.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path.startswith("/video/"):
            self._serve_video(srv, path)
            return
        if path.startswith("/poster/"):
            self._serve_poster(srv, path)
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def _serve_poster(self, srv: "VideoReviewServer", path: str) -> None:
        """Serve the grid thumbnail, rendering it on this thread if it's a miss."""
        raw = path[len("/poster/"):]
        try:
            n = int(raw)  # int-only: no path traversal possible
        except ValueError:
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        data = srv.poster_bytes(n)
        if data is None:
            # Either still rendering (queued by poster_bytes) or unrenderable.
            # Both answer immediately: holding the connection would stall the
            # video the user just clicked. The page retries, then gives up.
            self._send(404, b"not ready", "text/plain; charset=utf-8")
            return
        self._send(200, data, "image/jpeg",
                   {"Cache-Control": "private, max-age=86400"})

    def do_HEAD(self) -> None:
        self.do_GET()

    def _serve_video(self, srv: "VideoReviewServer", path: str) -> None:
        raw = path[len("/video/"):]
        try:
            n = int(raw)  # int-only: no path traversal possible
        except ValueError:
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        item = srv.item_for(n)
        if item is None:
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        try:
            size = item.path.stat().st_size
        except OSError:
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return

        rng = _parse_range(self.headers.get("Range"), size)
        if rng == "unsatisfiable":
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        start, end = (0, size - 1) if rng is None else rng
        length = end - start + 1
        self.send_response(200 if rng is None else 206)
        self.send_header("Content-Type", _guess_video_type(item.path))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if rng is not None:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        self._stream(item, start, length)

    def _stream(self, item: VideoItem, start: int, length: int) -> None:
        try:
            with open(item.path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(_STREAM_CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except CLIENT_GONE:
            pass  # browser aborted the request (normal while scrubbing)
        except OSError as exc:
            logger.debug("video stream error for %s: %s", item.rel, exc)

    def do_POST(self) -> None:
        srv: "VideoReviewServer" = self.server.review  # type: ignore[attr-defined]
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
            icloud = bool(body.get("icloud", False))
        except (ValueError, TypeError):
            self._json(400, {"error": "bad request"})
            return
        outcome = srv.do_trash(ids, icloud=icloud)
        self._json(200, {
            "moved": outcome.moved,
            "failed": [{"rel": r, "error": e} for r, e in outcome.failed],
        })


class VideoReviewServer(TrashSession):
    """Serves the video list, grid posters, ranged originals, and trashes.

    All items are known up front (the scan is instant), so the page embeds the
    full list and there is no streaming/polling — only the poster frames, the
    range-serving of the original files and the inherited
    :meth:`~..review.TrashSession.do_trash`.

    ``posters`` may be omitted (tests, callers that don't want a cache dir); the
    grid then falls back to placeholder tiles.
    """

    def __init__(
        self,
        items: list[VideoItem],
        trash_fn: Callable[[list[Path]], list[TrashResult]],
        token: str,
        host: str = "127.0.0.1",
        port: int = 0,
        posters: PosterCache | None = None,
        icloud_armed: bool = False,
    ) -> None:
        super().__init__(_Handler, trash_fn, token, host, port, icloud_armed)
        self.items = list(items)
        self._by_index = {it.index: it for it in self.items}
        self.posters = posters
        self.page = render_video_page(token, self.items, icloud_armed)

    def item_for(self, index: int) -> VideoItem | None:
        return self._by_index.get(index)

    def poster_bytes(self, index: int) -> bytes | None:
        """Rendered poster for ``index``, or None — queueing a render on a miss.

        Never blocks: see :class:`~icloud_photo_sync.poster.PosterCache`.
        """
        item = self.item_for(index)
        if item is None or self.posters is None:
            return None
        data = self.posters.get_cached(item)
        if data is None:
            self.posters.request(item)
            return None
        return data or None          # b"" means "tried, can't be rendered"

    def close(self) -> None:
        if self.posters is not None:
            self.posters.close()
        super().close()


def _item_payload(it: VideoItem) -> dict:
    try:
        date = datetime.fromtimestamp(it.mtime_ns / 1e9).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        date = ""
    return {
        "index": it.index,
        "rel": it.rel,
        "bytes": it.size,
        "size": _human_size(it.size),
        "date": date,
        "dur": format_duration(it.duration),
    }


def render_video_page(
    token: str, items: list[VideoItem], icloud_armed: bool = False
) -> str:
    """Return the review page with the token and item list embedded."""
    payload = json.dumps([_item_payload(it) for it in items])
    return (_TEMPLATE.replace("__TOKEN__", token)
            .replace("__ITEMS_JSON__", payload)
            .replace("__ICLOUD__", "true" if icloud_armed else "false"))


_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>video-clean review</title>
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
  button.danger:hover:not(:disabled) { background: #b02a2e; }
  .icloud {
    font-size: 12px; font-weight: 600; padding: 4px 10px; border-radius: 6px;
    background: rgba(209,52,56,.14); color: #d13438; border: 1px solid rgba(209,52,56,.4);
  }
  .icloud-pick { font-size: 12px; display: flex; align-items: center; gap: 6px; }
  .icloud-pick input { accent-color: #d13438; }
  [hidden] { display: none !important; }
  .grid {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
    gap: 12px; padding: 16px;
  }
  .card {
    border: 2px solid transparent; border-radius: 10px; overflow: hidden;
    background: rgba(128,128,128,.08); cursor: pointer; display: flex;
    flex-direction: column;
    /* Skip layout/paint for the offscreen majority of a long list. The
       intrinsic size keeps the scrollbar honest before a card is rendered. */
    content-visibility: auto;
    contain-intrinsic-size: auto 190px;
  }
  .card.selected { border-color: #d13438; background: rgba(209,52,56,.10); }
  .thumb {
    position: relative; width: 100%; aspect-ratio: 16 / 10;
    background: rgba(128,128,128,.15); display: flex; overflow: hidden;
  }
  .thumb img { width: 100%; height: 100%; object-fit: cover; display: block;
    background: #000; }
  .thumb.noposter { background: rgba(128,128,128,.22); }
  .thumb .play {
    position: absolute; inset: 0; display: flex; align-items: center;
    justify-content: center; font-size: 34px; color: #fff;
    text-shadow: 0 1px 6px rgba(0,0,0,.6); pointer-events: none;
  }
  .pick {
    position: absolute; left: 6px; top: 6px; width: 26px; height: 26px;
    border-radius: 6px; background: rgba(0,0,0,.5); display: flex;
    align-items: center; justify-content: center;
  }
  .pick input { width: 17px; height: 17px; cursor: pointer; accent-color: #d13438; }
  .meta { padding: 8px 10px; display: flex; flex-direction: column; gap: 3px; }
  .rel { font-size: 12px; opacity: .85; word-break: break-all; }
  .sub { font-size: 11px; opacity: .6; display: flex; gap: 8px;
    justify-content: space-between; align-items: baseline; }
  .sub .dur { font-variant-numeric: tabular-nums; white-space: nowrap; }
  .done { padding: 40px 20px; text-align: center; max-width: 640px; margin: 0 auto; }
  .done h2 { font-size: 20px; }
  .fail { color: #d13438; text-align: left; }
  /* modal lightbox */
  .modal { position: fixed; inset: 0; z-index: 50; display: flex;
    align-items: center; justify-content: center; }
  .modal[hidden] { display: none; }
  .modal .backdrop { position: absolute; inset: 0; background: rgba(0,0,0,.8); }
  .modal .dialog {
    position: relative; z-index: 1; max-width: min(92vw, 1100px);
    max-height: 90vh; display: flex; flex-direction: column; gap: 8px;
  }
  .modal video {
    max-width: 100%; max-height: 78vh; background: #000; border-radius: 8px;
  }
  .modal .caption { color: #fff; font-size: 13px; word-break: break-all;
    text-align: center; }
  .modal .modal-err { color: #ffb4b4; text-align: center; font-size: 13px; }
  .modal .close {
    position: absolute; top: -14px; right: -14px; width: 34px; height: 34px;
    border-radius: 50%; background: #fff; color: #000; font-weight: 700;
    border: none; padding: 0;
  }
</style>
</head>
<body>
<header>
  <span class="count" id="count">Loading…</span>
  <span class="prog" id="progress"></span>
  <span id="status"></span>
  <span class="icloud" id="icloudBanner" hidden>iCloud deletion armed</span>
  <span class="spacer"></span>
  <label class="icloud-pick" id="icloudPick" hidden>
    <input type="checkbox" id="icloudBox" checked> Also delete from iCloud
  </label>
  <button id="all">Select all</button>
  <button id="none">Deselect all</button>
  <button id="finish">Finish</button>
  <button class="danger" id="trash" disabled>Move to Trash</button>
</header>
<main id="main"><div class="grid" id="grid"></div></main>
<div class="modal" id="modal" hidden>
  <div class="backdrop" id="backdrop"></div>
  <div class="dialog">
    <button class="close" id="close">✕</button>
    <video id="player" controls playsinline preload="auto"></video>
    <div class="modal-err" id="modalErr" hidden>This format can't be previewed in the browser.</div>
    <div class="caption" id="caption"></div>
  </div>
</div>
<script>
const TOKEN = "__TOKEN__";
const ICLOUD_ARMED = __ICLOUD__;   // authorised in the terminal, not here
const ITEMS = __ITEMS_JSON__;
const cards = new Map();        // index -> card element
const relToIndex = new Map();   // rel -> index
const byIndex = new Map();      // index -> item
const selected = new Set();     // selected indices (start empty)
let finished = false, freedBytes = 0;
let statusTimer = null;

function humanSize(n) {
  let f = n;
  for (const u of ["B", "KB", "MB", "GB", "TB"]) {
    if (f < 1024 || u === "TB") return (u === "B" ? Math.round(f) : f.toFixed(1)) + " " + u;
    f /= 1024;
  }
}
function totalBytes() {
  let t = 0;
  for (const i of cards.keys()) t += byIndex.get(i).bytes;
  return t;
}
function selectedBytes() {
  let t = 0;
  for (const i of selected) t += byIndex.get(i).bytes;
  return t;
}

function setStatus(msg) {
  const el = document.getElementById("status");
  el.textContent = msg;
  if (statusTimer) clearTimeout(statusTimer);
  statusTimer = setTimeout(() => { el.textContent = ""; }, 4000);
}

function addCard(it) {
  byIndex.set(it.index, it);
  const card = document.createElement("div");
  card.className = "card";
  card.dataset.index = it.index;

  const thumb = document.createElement("div");
  thumb.className = "thumb";
  // A poster image costs a small bitmap; a media element per card cost a
  // decoder and a full-resolution frame. Lazy loading fetches it near the view.
  const shot = document.createElement("img");
  shot.loading = "lazy";
  shot.decoding = "async";
  shot.alt = "";
  shot.src = "/poster/" + it.index;
  // A 404 means "queued, not rendered yet" as often as "can't be rendered", and
  // an <img> can't tell them apart — so back off a few times, then settle for
  // the placeholder tile.
  let tries = 0;
  shot.onerror = () => {
    if (++tries > 6) { shot.remove(); thumb.classList.add("noposter"); return; }
    setTimeout(() => { shot.src = "/poster/" + it.index + "?try=" + tries; },
               Math.min(400 * tries, 2500));
  };
  const play = document.createElement("span"); play.className = "play"; play.textContent = "▶";
  const pick = document.createElement("label"); pick.className = "pick";
  const box = document.createElement("input"); box.type = "checkbox";
  box.onclick = (ev) => {
    ev.stopPropagation();
    if (box.checked) { selected.add(it.index); card.classList.add("selected"); }
    else { selected.delete(it.index); card.classList.remove("selected"); }
    updateCounts();
  };
  pick.onclick = (ev) => ev.stopPropagation();
  pick.appendChild(box);
  thumb.append(shot, play, pick);

  const meta = document.createElement("div");
  meta.className = "meta";
  const rel = document.createElement("span"); rel.className = "rel"; rel.textContent = it.rel;
  const sub = document.createElement("span"); sub.className = "sub";
  const facts = document.createElement("span");
  facts.textContent = it.size + (it.date ? "  ·  " + it.date : "");
  const dur = document.createElement("span"); dur.className = "dur";
  dur.textContent = it.dur || "";
  sub.append(facts, dur);
  meta.append(rel, sub);

  card.append(thumb, meta);
  card.onclick = () => openModal(it);
  document.getElementById("grid").appendChild(card);
  cards.set(it.index, card);
  relToIndex.set(it.rel, it.index);
}

function updateCounts() {
  document.getElementById("count").textContent =
    cards.size + " video" + (cards.size === 1 ? "" : "s") + "  ·  " + humanSize(totalBytes());
  document.getElementById("progress").textContent =
    selected.size ? (selected.size + " selected — frees " + humanSize(selectedBytes())) : "";
  const btn = document.getElementById("trash");
  btn.textContent = selected.size
    ? "Move " + selected.size + " to Trash (" + humanSize(selectedBytes()) + ")"
    : "Move to Trash";
  btn.disabled = selected.size === 0;
}

// --- modal lightbox ---
const modal = document.getElementById("modal");
const player = document.getElementById("player");
function openModal(it) {
  document.getElementById("caption").textContent = it.rel + "  ·  " + it.size;
  document.getElementById("modalErr").hidden = true;
  player.src = "/video/" + it.index;
  // closeModal() leaves the element with no source, so tell it to start
  // loading the new one explicitly rather than relying on preload behaviour —
  // Safari will otherwise sit on a black frame until something forces it.
  player.load();
  modal.hidden = false;
  player.play().catch(() => {});   // blocked autoplay just leaves the controls
}
function closeModal() {
  player.pause();
  player.removeAttribute("src");
  player.load();               // stop the transfer
  modal.hidden = true;
}
player.addEventListener("error", () => {
  if (!modal.hidden) document.getElementById("modalErr").hidden = false;
});
document.getElementById("close").onclick = closeModal;
document.getElementById("backdrop").onclick = closeModal;
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !modal.hidden) closeModal();
});

function wantsICloud() {
  const box = document.getElementById("icloudBox");
  return ICLOUD_ARMED && !!box && box.checked;
}

async function doTrash() {
  if (!selected.size) return;
  const alsoICloud = wantsICloud();
  const question = alsoICloud
    ? "Move " + selected.size + " file(s) to the Trash AND queue them for deletion "
      + "from iCloud?\\n\\nYou will confirm the iCloud part in the terminal before "
      + "anything is deleted there."
    : "Move " + selected.size + " file(s) to the Trash?";
  if (!confirm(question)) return;
  const btn = document.getElementById("trash");
  btn.disabled = true; btn.textContent = "Moving…";
  const ids = [...selected];
  try {
    const resp = await fetch("/trash", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Clean-Token": TOKEN },
      body: JSON.stringify({ ids, icloud: alsoICloud }),
    });
    const data = await resp.json();
    for (const rel of (data.moved || [])) {
      const idx = relToIndex.get(rel);
      if (idx !== undefined) {
        freedBytes += byIndex.get(idx).bytes;
        const card = cards.get(idx);
        if (card) card.remove();
        cards.delete(idx);
        selected.delete(idx);
        relToIndex.delete(rel);
      }
    }
    const nMoved = (data.moved || []).length;
    const nFailed = (data.failed || []).length;
    updateCounts();
    setStatus("Moved " + nMoved + " to Trash — " + humanSize(freedBytes) + " freed"
      + (nFailed ? " (" + nFailed + " failed)" : ""));
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
    showDone(await resp.json());
  } catch (e) {
    sessionEnded();
  }
}

function showDone(data) {
  closeModal();
  document.querySelector("header").style.display = "none";
  const moved = (data.moved || []).length;
  const failed = data.failed || [];
  const main = document.getElementById("main");
  main.innerHTML = "";
  const div = document.createElement("div");
  div.className = "done";
  const h = document.createElement("h2");
  h.textContent = "Moved " + moved + " file(s) to the Trash — " + humanSize(freedBytes) + " freed.";
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
  if (data.icloud_armed && data.icloud) {
    const p1 = document.createElement("p");
    p1.textContent = data.icloud + " file(s) are queued for deletion from iCloud — "
      + "go back to the terminal to confirm. Nothing has been deleted from iCloud yet.";
    div.appendChild(p1);
  }
  const p2 = document.createElement("p");
  p2.textContent = "You can close this tab. The command has finished.";
  div.appendChild(p2);
  main.appendChild(div);
}

function sessionEnded() {
  finished = true;
  closeModal();
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
  for (const [i, c] of cards) {
    selected.add(i); c.classList.add("selected");
    const b = c.querySelector(".pick input"); if (b) b.checked = true;
  }
  updateCounts();
};
document.getElementById("none").onclick = () => {
  for (const [i, c] of cards) {
    selected.delete(i); c.classList.remove("selected");
    const b = c.querySelector(".pick input"); if (b) b.checked = false;
  }
  updateCounts();
};
document.getElementById("trash").onclick = doTrash;
document.getElementById("finish").onclick = () => {
  if (confirm("Finish the review session and exit the command?")) finishSession();
};

if (ICLOUD_ARMED) {
  document.getElementById("icloudBanner").hidden = false;
  document.getElementById("icloudPick").hidden = false;
}

for (const it of ITEMS) addCard(it);
updateCounts();
</script>
</body>
</html>
"""
