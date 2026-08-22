"""The two browser screens ``video-optimise`` uses, and the server behind them.

Screen one picks what to convert. Screen two shows what came out, original
beside converted, and asks whether to put it back into iCloud. Both are the same
local HTTP server as ``video-clean`` — see :mod:`icloud_photo_sync.video_review`
for why a browser is involved at all: a ``file://`` page cannot stream a
multi-hundred-megabyte video with the ranged requests Safari requires outright.

Nothing here can modify a file. Both screens subclass
:class:`~icloud_photo_sync.review.TrashSession` for its lifecycle and token
guard, but pass a ``trash_fn`` that *raises* — neither page has a route that
would reach it, and if a future edit adds one, the seam fails loudly rather than
quietly deleting something. What these screens return is a set of indices; every
irreversible act happens later, in the terminal, behind a typed confirmation.

The comparison screen is the one that has to be honest. It labels both sides of
each pair with their colour space, so an HDR clip that came back SDR is visible
rather than inferred — and it plays the two files side by side in one browser,
through one display pipeline, which is the only comparison that means anything.
"""

from __future__ import annotations

import json
import secrets
import threading
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import urlsplit

from .logutil import get_logger
from .poster import PosterCache, format_duration
from .review import CLIENT_GONE, TrashSession, _BaseHandler, _human_size
from .video_review import _guess_video_type, _parse_range, _STREAM_CHUNK

logger = get_logger(__name__)

# What the comparison screen came back with.
CHOICE_PENDING = "pending"
CHOICE_APPROVE_ALL = "approve-all"
CHOICE_REVIEW_ALL = "review-all"
CHOICE_CANCEL = "cancel"
CHOICE_DONE = "done"          # the review-all pass finished with a selection


def _never(paths):  # noqa: ANN001, ANN201 - deliberately unusable
    raise AssertionError(
        "video-optimise review screens must never trash anything; this seam "
        "exists so that a route which tried to would fail loudly"
    )


@dataclass
class SelectItem:
    """One row of the selection grid.

    A skipped video still appears, greyed out and carrying ``skip_reason``:
    "why is my biggest clip not in this list" is a question the page should
    answer, and a video that silently vanishes invites the user to go looking
    for a bug that is not there.
    """

    index: int
    path: Path
    rel: str
    size: int
    mtime_ns: int
    duration: float | None = None

    predicted_size: int | None = None
    out_width: int = 0
    out_height: int = 0
    src_width: int = 0
    src_height: int = 0
    fps: float = 0.0
    hdr: bool = False
    slow_motion: bool = False
    keeps_frame_rate: bool = False
    skip_reason: str = ""

    @property
    def selectable(self) -> bool:
        return not self.skip_reason and self.predicted_size is not None

    @property
    def saving(self) -> int:
        if self.predicted_size is None:
            return 0
        return max(0, self.size - self.predicted_size)


@dataclass
class ComparePair:
    """One original/converted pair on the comparison screen."""

    index: int
    rel: str
    src_path: Path
    out_path: Path
    src_size: int
    out_size: int
    src_label: str          # "3840x2160 · 48 Mbps"
    out_label: str          # "1920x1080 · 5.8 Mbps"
    colour_label: str       # "HLG HDR → HLG HDR"
    duration: float | None = None
    hdr: bool = False
    slow_motion: bool = False

    @property
    def saving(self) -> int:
        return max(0, self.src_size - self.out_size)

    @property
    def percent(self) -> int:
        if self.src_size <= 0:
            return 0
        return round(100 * (1 - self.out_size / self.src_size))


@dataclass
class SelectionOutcome:
    """What a screen came back with. ``choice`` is only set by the comparison."""

    selected: set[int] = field(default_factory=set)
    choice: str = CHOICE_PENDING


class _SelectionSession(TrashSession):
    """Server lifecycle and token guard, with selection instead of trashing."""

    def __init__(self, handler_cls, token: str, host: str = "127.0.0.1",
                 port: int = 0) -> None:
        super().__init__(handler_cls, _never, token, host, port, icloud_armed=False)
        self.selection = SelectionOutcome()
        self._sel_lock = threading.Lock()

    def set_selected(self, ids: Sequence[int]) -> None:
        with self._sel_lock:
            self.selection.selected = {int(i) for i in ids}

    def set_choice(self, choice: str) -> None:
        with self._sel_lock:
            self.selection.choice = choice

    def finish_payload(self) -> dict:
        with self._sel_lock:
            return {"selected": sorted(self.selection.selected),
                    "choice": self.selection.choice}


# --- shared request handling -------------------------------------------------


class _OptimiseHandler(_BaseHandler):
    """Routes shared by both screens. Subclasses only add their page."""

    protocol_version = "HTTP/1.1"   # keep-alive: ranged seeking needs it

    def log_message(self, fmt, *args):  # noqa: ANN001
        logger.debug("optimise-review: " + fmt, *args)

    def _send(self, code: int, body: bytes, content_type: str,
              extra: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    def _not_found(self) -> None:
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        srv = self.server.review  # type: ignore[attr-defined]
        path = urlsplit(self.path).path
        if path == "/":
            self._send(200, srv.page.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path.startswith("/poster/"):
            self._serve_poster(srv, path[len("/poster/"):], "out")
            return
        if path.startswith("/src-poster/"):
            self._serve_poster(srv, path[len("/src-poster/"):], "src")
            return
        for prefix, which in (("/video/", "src"), ("/original/", "src"),
                              ("/converted/", "out")):
            if path.startswith(prefix):
                self._serve_video(srv, path[len(prefix):], which)
                return
        self._not_found()

    # -- media ---------------------------------------------------------------

    def _index(self, raw: str) -> int | None:
        try:
            return int(raw)      # int-only: no path traversal is expressible
        except ValueError:
            return None

    def _serve_poster(self, srv, raw: str, which: str = "out") -> None:
        index = self._index(raw)
        data = None if index is None else srv.poster_bytes(index, which)
        if data is None:
            # Still rendering, or unrenderable. Answer at once either way:
            # holding the connection would stall the video just clicked.
            self._send(404, b"not ready", "text/plain; charset=utf-8")
            return
        self._send(200, data, "image/jpeg", {"Cache-Control": "private, max-age=86400"})

    def _serve_video(self, srv, raw: str, which: str) -> None:
        index = self._index(raw)
        path = None if index is None else srv.media_path(index, which)
        if path is None:
            self._not_found()
            return
        try:
            size = path.stat().st_size
        except OSError:
            self._not_found()
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
        self.send_header("Content-Type", _guess_video_type(path))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if rng is not None:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        self._stream(path, start, length)

    def _stream(self, path: Path, start: int, length: int) -> None:
        try:
            with open(path, "rb") as handle:
                handle.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = handle.read(min(_STREAM_CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except CLIENT_GONE:
            pass                # the browser aborted; normal while scrubbing
        except OSError as exc:
            logger.debug("stream error for %s: %s", path, exc)

    # -- decisions ------------------------------------------------------------

    def do_POST(self) -> None:
        srv = self.server.review  # type: ignore[attr-defined]
        path = urlsplit(self.path).path
        if path not in ("/select", "/finish"):
            self._not_found()
            return
        if self.headers.get("X-Clean-Token") != srv.token:
            self._json(403, {"error": "bad token"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except (ValueError, TypeError):
            self._json(400, {"error": "bad request"})
            return

        if "ids" in body:
            srv.set_selected([int(i) for i in body.get("ids") or []])
        choice = str(body.get("choice") or "")
        if choice in (CHOICE_APPROVE_ALL, CHOICE_REVIEW_ALL,
                      CHOICE_CANCEL, CHOICE_DONE):
            srv.set_choice(choice)

        if path == "/finish":
            srv.request_finish()
        self._json(200, srv.finish_payload())


class _SelectHandler(_OptimiseHandler):
    server_version = "VideoOptimiseSelect/1.0"


class _CompareHandler(_OptimiseHandler):
    server_version = "VideoOptimiseCompare/1.0"


# --- the two servers ---------------------------------------------------------


class SelectServer(_SelectionSession):
    """Screen one: which videos to convert. Nothing is pre-selected."""

    def __init__(self, items: Sequence[SelectItem], token: str,
                 host: str = "127.0.0.1", port: int = 0,
                 posters: PosterCache | None = None) -> None:
        super().__init__(_SelectHandler, token, host, port)
        self.items = list(items)
        self._by_index = {it.index: it for it in self.items}
        self.posters = posters
        from .optimise_pages import render_select_page
        self.page = render_select_page(token, [select_payload(i) for i in self.items])

    def media_path(self, index: int, which: str) -> Path | None:
        item = self._by_index.get(index)
        return item.path if item is not None else None

    def poster_bytes(self, index: int, which: str = "out") -> bytes | None:
        item = self._by_index.get(index)          # one file per row; `which` is moot
        if item is None or self.posters is None:
            return None
        data = self.posters.get_cached(item)
        if data is None:
            self.posters.request(item)
            return None
        return data or None          # b"" means "tried, cannot be rendered"

    def close(self) -> None:
        if self.posters is not None:
            self.posters.close()
        super().close()


class CompareServer(_SelectionSession):
    """Screen two: original beside converted, with the colour check restated.

    ``review_all`` decides which page is drawn: the top-N summary with its three
    buttons, or the full deselect grid. The server is otherwise identical, so
    the second pass reuses the first one's connection handling and range serving.
    """

    def __init__(self, pairs: Sequence[ComparePair], token: str,
                 host: str = "127.0.0.1", port: int = 0,
                 review_all: bool = False, total: int | None = None,
                 posters: PosterCache | None = None) -> None:
        super().__init__(_CompareHandler, token, host, port)
        self.pairs = list(pairs)
        self._by_index = {p.index: p for p in self.pairs}
        self.posters = posters
        self.review_all = review_all
        # Everything is selected by default in the deselect pass: the user got
        # here by approving, so the question is which ones to hold back.
        if review_all:
            self.set_selected([p.index for p in self.pairs])
        from .optimise_pages import render_compare_page
        self.page = render_compare_page(
            token, [compare_payload(p) for p in self.pairs],
            review_all=review_all, total=total if total is not None else len(self.pairs),
        )

    def media_path(self, index: int, which: str) -> Path | None:
        pair = self._by_index.get(index)
        if pair is None:
            return None
        return pair.out_path if which == "out" else pair.src_path

    def poster_bytes(self, index: int, which: str = "out") -> bytes | None:
        """A frame from whichever side was asked for.

        The two sides must not share a poster. On a screen whose entire job is
        letting someone judge the conversion, showing the converted frame above
        the word "Original" would be quietly answering the question for them.
        """
        pair = self._by_index.get(index)
        if pair is None or self.posters is None:
            return None
        source = which == "src"
        path = pair.src_path if source else pair.out_path
        item = SelectItem(index=index, path=path, rel=pair.rel,
                          size=pair.src_size if source else pair.out_size,
                          mtime_ns=1 if source else 0)
        data = self.posters.get_cached(item)
        if data is None:
            self.posters.request(item)
            return None
        return data or None

    def close(self) -> None:
        if self.posters is not None:
            self.posters.close()
        super().close()


# --- payloads the pages render ----------------------------------------------


def select_payload(item: SelectItem) -> dict:
    return {
        "index": item.index,
        "rel": item.rel,
        "name": item.rel.rsplit("/", 1)[-1],
        "bytes": item.size,
        "size": _human_size(item.size),
        "dur": format_duration(item.duration),
        "dims": f"{item.src_width}×{item.src_height}" if item.src_width else "",
        "out": (f"{item.out_width}×{item.out_height}"
                if item.out_width and item.selectable else ""),
        "saving": _human_size(item.saving) if item.selectable else "",
        "percent": (round(100 * item.saving / item.size)
                    if item.selectable and item.size else 0),
        "hdr": item.hdr,
        "slomo": item.slow_motion,
        "fps": round(item.fps) if item.fps else 0,
        "keepsFps": item.keeps_frame_rate,
        "skip": item.skip_reason,
    }


def compare_payload(pair: ComparePair) -> dict:
    return {
        "index": pair.index,
        "rel": pair.rel,
        "name": pair.rel.rsplit("/", 1)[-1],
        "srcSize": _human_size(pair.src_size),
        "outSize": _human_size(pair.out_size),
        "srcLabel": pair.src_label,
        "outLabel": pair.out_label,
        "colour": pair.colour_label,
        "percent": pair.percent,
        "dur": format_duration(pair.duration),
        "hdr": pair.hdr,
        "slomo": pair.slow_motion,
    }


# --- driving them from the terminal -----------------------------------------


def _run(server, *, open_browser: bool, echo, prompt_text: str) -> SelectionOutcome:
    server.start()
    url = server.url
    echo(f"\nReview page: {url}")
    if open_browser:
        webbrowser.open(url)
    else:
        echo("Open that URL in your browser.")
    echo(prompt_text)
    interrupted = False
    try:
        server.wait_finished()
    except KeyboardInterrupt:
        interrupted = True
    outcome = SelectionOutcome(set(server.selection.selected), server.selection.choice)
    server.close()
    if interrupted:
        # Ctrl-C means "stop", not "do what was ticked". The distinction matters
        # on the comparison screen, where proceeding would upload.
        return SelectionOutcome(set(), CHOICE_CANCEL)
    return outcome


def choose_videos(
    items: Sequence[SelectItem], *, port: int = 0, open_browser: bool = True,
    posters: PosterCache | None = None, echo: Callable[[str], None] = print,
) -> set[int]:
    """Screen one. Returns the indices the user ticked."""
    server = SelectServer(items, secrets.token_urlsafe(16), port=port, posters=posters)
    return _run(server, open_browser=open_browser, echo=echo,
                prompt_text="Tick the videos you want optimised, then click Done — "
                            "or press Ctrl-C here to stop.").selected


def compare_results(
    pairs: Sequence[ComparePair], *, review_all: bool = False, total: int | None = None,
    port: int = 0, open_browser: bool = True, posters: PosterCache | None = None,
    echo: Callable[[str], None] = print,
) -> SelectionOutcome:
    """Screen two. Returns both the choice and, in the deselect pass, the picks."""
    server = CompareServer(pairs, secrets.token_urlsafe(16), port=port,
                           review_all=review_all, total=total, posters=posters)
    prompt = ("Untick anything you would rather keep the original of, then click "
              "Done — or press Ctrl-C here to stop."
              if review_all else
              "Check the conversions, then approve them, review all of them, or "
              "cancel — or press Ctrl-C here to stop.")
    return _run(server, open_browser=open_browser, echo=echo, prompt_text=prompt)
