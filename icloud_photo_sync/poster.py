"""What the ``video-clean`` grid needs to know about a video: poster and length.

Drawing the grid with real ``<video>`` elements cost the browser a decoder and
a full-resolution decoded frame *per card* — tens of MB for a 4K clip shown in
a 240 px tile, and nothing released it — so the grid shows small JPEG posters
instead and keeps ``<video>`` for the one clip open in the modal.

Everything here shells out to tools that ship with macOS, falling back rather
than failing, which mirrors :func:`icloud_photo_sync.classifier.prepare_image`:
use the system tools, add no media dependency. Posters come from ``ffmpeg``
when it is on PATH (fast, exact seek) and otherwise QuickLook + ``sips``;
durations from Spotlight (``mdls``, one batched call for the whole library) and
otherwise ``ffprobe``.

Posters render in the background — never on the request thread, since a browser
allows only a handful of connections per host and a 200 ms thumbnail would
stall the video the user just clicked. :meth:`PosterCache.get_cached` answers
from disk or not at all; a miss queues the render (newest first, so the cards
on screen win) and the page retries. They are cached under
``(path, size, mtime_ns)`` so re-running over the same tree is instant; a
poster is a few tens of KB, so a fully-browsed library of a few thousand videos
costs well under a hundred MB.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
import threading
from collections import deque
from pathlib import Path
from typing import Callable, Iterable, Protocol, Sequence

from .logutil import get_logger

logger = get_logger(__name__)

DEFAULT_POSTER_MAX_DIM = 320     # px on the longest side; grid tiles are ~240
DEFAULT_POSTER_WORKERS = 4       # concurrent extractions
DEFAULT_POSTER_TIMEOUT = 60.0    # seconds per extraction attempt
_SEEK_SECONDS = "0.5"            # skip the fade-in/black first frame
_MAX_PENDING = 96                # queued renders; a fast scroll drops the stale tail
_PROBE_BATCH = 200               # files per mdls call


class _HasFileIdentity(Protocol):
    """What :class:`PosterCache` needs of an item (``VideoItem`` satisfies it)."""

    path: Path
    size: int
    mtime_ns: int


def _extract_ffmpeg(src: Path, dest: Path, max_dim: int, timeout: float) -> bool:
    """Grab one frame with ffmpeg. False if ffmpeg is absent or produces nothing."""
    if shutil.which("ffmpeg") is None:
        return False
    scale = f"scale=w={max_dim}:h={max_dim}:force_original_aspect_ratio=decrease"
    # Clips shorter than the seek point yield no frame, so fall back to frame 0.
    for seek in (_SEEK_SECONDS, "0"):
        try:
            proc = subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-ss", seek, "-i", str(src),
                 "-frames:v", "1", "-vf", scale, "-q:v", "4", str(dest)],
                capture_output=True,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("ffmpeg poster failed for %s: %s", src, exc)
            return False
        if proc.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
            return True
    return False


def _extract_quicklook(src: Path, dest: Path, max_dim: int, timeout: float) -> bool:
    """Ask QuickLook for the file's thumbnail, then convert it with ``sips``.

    Slower than ffmpeg on big files but needs nothing installed, and it renders
    anything macOS itself can preview.
    """
    if shutil.which("qlmanage") is None or shutil.which("sips") is None:
        return False
    try:
        with tempfile.TemporaryDirectory(prefix="video-poster-") as tmp:
            tmp_dir = Path(tmp)
            proc = subprocess.run(
                ["qlmanage", "-t", "-s", str(max_dim), "-o", str(tmp_dir), str(src)],
                capture_output=True,
                timeout=timeout,
            )
            # qlmanage names its output after the source and reports success on
            # stdout even when it wrote nothing, so trust the directory instead.
            rendered = sorted(tmp_dir.glob("*.png"))
            if proc.returncode != 0 or not rendered:
                return False
            conv = subprocess.run(
                ["sips", "-Z", str(max_dim), "-s", "format", "jpeg",
                 str(rendered[0]), "--out", str(dest)],
                capture_output=True,
                timeout=timeout,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("quicklook poster failed for %s: %s", src, exc)
        return False
    return conv.returncode == 0 and dest.exists() and dest.stat().st_size > 0


def extract_poster(
    src: Path,
    dest: Path,
    max_dim: int = DEFAULT_POSTER_MAX_DIM,
    timeout: float = DEFAULT_POSTER_TIMEOUT,
) -> bool:
    """Write a JPEG poster for ``src`` to ``dest``. False if no tool could.

    Formats macOS cannot decode at all (some ``.avi``/``.wmv``) legitimately
    return False; the caller renders a placeholder tile for those.
    """
    for extract in (_extract_ffmpeg, _extract_quicklook):
        if extract(src, dest, max_dim, timeout):
            return True
        dest.unlink(missing_ok=True)   # clear a partial write before the next try
    logger.debug("no poster could be extracted for %s", src)
    return False


def format_duration(seconds: float | None) -> str:
    """``HH:MM:SS`` for the grid label, or ``""`` when the length is unknown."""
    if not seconds or seconds < 0:
        return ""
    total = int(round(seconds))
    return f"{total // 3600:02d}:{total // 60 % 60:02d}:{total % 60:02d}"


def _durations_from_spotlight(paths: Sequence[Path]) -> dict[Path, float]:
    """Batch-ask Spotlight for durations. Empty dict if it knows none of them.

    One ``mdls`` call covers hundreds of files in milliseconds, which is what
    makes probing the whole library at scan time affordable. Files outside the
    index (or on a volume with Spotlight disabled) come back ``(null)`` and are
    simply absent from the result.
    """
    if not paths or shutil.which("mdls") is None:
        return {}
    found: dict[Path, float] = {}
    for start in range(0, len(paths), _PROBE_BATCH):
        batch = paths[start:start + _PROBE_BATCH]
        try:
            proc = subprocess.run(
                ["mdls", "-name", "kMDItemDurationSeconds", "-raw", *map(str, batch)],
                capture_output=True, text=True, timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("mdls duration probe failed: %s", exc)
            return found
        if proc.returncode != 0:
            continue
        # -raw emits one NUL-separated value per file, in the order given.
        values = proc.stdout.split("\0")
        for path, raw in zip(batch, values):
            try:
                found[path] = float(raw)
            except ValueError:
                continue                     # "(null)": not indexed
    return found


def _duration_from_ffprobe(path: Path) -> float | None:
    if shutil.which("ffprobe") is None:
        return None
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return float(proc.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def probe_durations(
    paths: Iterable[Path], workers: int = 8
) -> dict[Path, float]:
    """Duration in seconds per path, for those that can be determined.

    Spotlight answers for the whole library in one shot; only the stragglers
    cost an ``ffprobe`` each, spread over a small thread pool. Callers treat a
    missing entry as "unknown" and show no length — never an error.
    """
    paths = list(paths)
    known = _durations_from_spotlight(paths)
    missing = [p for p in paths if p not in known]
    if not missing or shutil.which("ffprobe") is None:
        return known

    lock = threading.Lock()
    queue = deque(missing)

    def drain() -> None:
        while True:
            with lock:
                if not queue:
                    return
                path = queue.popleft()
            value = _duration_from_ffprobe(path)
            if value is not None:
                with lock:
                    known[path] = value

    threads = [threading.Thread(target=drain, daemon=True)
               for _ in range(min(workers, len(missing)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return known


class PosterCache:
    """On-demand, disk-cached poster frames, rendered off the request thread.

    :meth:`get_cached` never blocks: it answers from disk, and a miss queues a
    background render (newest request first, so the cards on screen are served
    before a scrolled-past backlog). The page retries the image until it
    appears. :meth:`get` is the blocking form, for callers that want the poster
    now rather than the connection back.
    """

    def __init__(
        self,
        cache_dir: Path,
        max_dim: int = DEFAULT_POSTER_MAX_DIM,
        workers: int = DEFAULT_POSTER_WORKERS,
        extract: Callable[[Path, Path, int, float], bool] = extract_poster,
        timeout: float = DEFAULT_POSTER_TIMEOUT,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.max_dim = max_dim
        self.timeout = timeout
        self._extract = extract
        self._workers = workers
        self._slots = threading.Semaphore(workers)
        self._lock = threading.Lock()
        self._per_key: dict[str, threading.Lock] = {}

        self._queue: deque[_HasFileIdentity] = deque()
        self._queued: set[str] = set()
        self._ready = threading.Condition()
        self._threads: list[threading.Thread] = []
        self._closed = False

    def path_for(self, item: _HasFileIdentity) -> Path:
        """Cache path for ``item``. Size+mtime in the key: an edited file re-renders."""
        raw = f"{item.path}|{item.size}|{item.mtime_ns}|{self.max_dim}"
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
        return self.cache_dir / f"{digest}.jpg"

    def get_cached(self, item: _HasFileIdentity) -> bytes | None:
        """Poster bytes if rendered, ``b""`` if known-unrenderable, else None."""
        return self._read(self.path_for(item))

    def request(self, item: _HasFileIdentity) -> None:
        """Queue a background render. Cheap and idempotent; returns immediately."""
        name = self.path_for(item).name
        with self._ready:
            if self._closed or name in self._queued:
                return
            self._queued.add(name)
            self._queue.append(item)
            while len(self._queue) > _MAX_PENDING:
                # Oldest first: those cards are long gone, and a card still on
                # screen re-queues itself on its next retry.
                self._queued.discard(self.path_for(self._queue.popleft()).name)
            self._ready.notify()
        self._ensure_workers()

    def get(self, item: _HasFileIdentity) -> bytes | None:
        """Return poster bytes, rendering on a miss. None if none can be made.

        An empty cache file is the negative result: a video whose poster failed
        once would otherwise re-run the (slow) extraction on every scroll past.
        """
        dest = self.path_for(item)
        cached = self._read(dest)
        if cached is not None:
            return cached or None

        with self._key_lock(dest.name):
            cached = self._read(dest)          # another thread may have just made it
            if cached is not None:
                return cached or None
            with self._slots:
                self._render(item, dest)
            return self._read(dest) or None

    def close(self) -> None:
        """Stop the workers. Idempotent; safe if none were ever started."""
        with self._ready:
            self._closed = True
            self._queue.clear()
            self._queued.clear()
            self._ready.notify_all()
        for t in self._threads:
            t.join(timeout=self.timeout + 5)

    # --- internals -----------------------------------------------------------

    def _ensure_workers(self) -> None:
        with self._lock:
            if self._threads or self._closed:
                return
            self._threads = [
                threading.Thread(target=self._drain, daemon=True,
                                 name=f"poster-{i}")
                for i in range(self._workers)
            ]
            for t in self._threads:
                t.start()

    def _drain(self) -> None:
        while True:
            with self._ready:
                while not self._queue and not self._closed:
                    self._ready.wait()
                if self._closed:
                    return
                item = self._queue.pop()        # newest: what the user is looking at
                self._queued.discard(self.path_for(item).name)
            try:
                self.get(item)
            except Exception as exc:            # a worker must outlive one bad file
                logger.debug("poster render failed for %s: %s", item.path, exc)

    def _render(self, item: _HasFileIdentity, dest: Path) -> None:
        """Extract into ``dest``, publishing atomically; empty file marks failure."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".part")
        try:
            ok = self._extract(item.path, tmp, self.max_dim, self.timeout)
            if not ok:
                tmp.unlink(missing_ok=True)
                tmp.touch()                    # negative result
            tmp.replace(dest)                  # atomic: readers never see a partial
        except OSError as exc:
            logger.debug("poster cache write failed for %s: %s", item.path, exc)
            tmp.unlink(missing_ok=True)

    @staticmethod
    def _read(dest: Path) -> bytes | None:
        """Cached bytes, ``b""`` for a known failure, None if not cached yet."""
        try:
            return dest.read_bytes()
        except OSError:
            return None

    def _key_lock(self, name: str) -> threading.Lock:
        with self._lock:
            return self._per_key.setdefault(name, threading.Lock())
