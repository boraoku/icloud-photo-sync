"""The two browser pages ``video-optimise`` serves — rendering only.

Companion to :mod:`icloud_photo_sync.optimise_review`, which owns the routes,
the token protocol and the payload dicts. This module only turns those dicts
into HTML: a module-level template per screen, with ``__TOKEN__`` and friends
replaced by :func:`render_select_page` / :func:`render_compare_page`. The idiom
— and the CSS, the poster-retry backoff, the video modal — is copied from
:mod:`icloud_photo_sync.video_review` on purpose: these are siblings of that
page, not strangers.

Screen one (:func:`render_select_page`) is a poster grid where nothing starts
selected — the user opts videos *into* conversion, and a skipped video stays
visible (greyed out, with its skip reason) rather than vanishing, because "why
is my biggest clip missing" has to be answerable from the page itself.

Screen two (:func:`render_compare_page`) has two faces controlled by
``review_all``: the top-N summary with its three commit buttons (review
everything / cancel / finish — nothing here is irreversible any more, since
uploading now happens by hand outside this tool), or the full deselect grid
where everything starts ticked and unticking means "discard this conversion,
don't upload it".
"""

from __future__ import annotations

import json


def _embed(payload: list[dict]) -> str:
    """JSON for a ``<script>`` block: no ``</script>`` can ever appear inside it.

    ``ensure_ascii=False`` keeps filenames readable in view-source; the explicit
    ``</`` escape is what actually protects the script block — non-ASCII text
    plays no part in that (json.dumps already escapes quotes/backslashes).
    """
    return json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")


def render_select_page(token: str, items: list[dict]) -> str:
    """Screen one: the poster grid the user ticks videos to convert from."""
    return (_SELECT_TEMPLATE
            .replace("__TOKEN__", token)
            .replace("__ITEMS_JSON__", _embed(items)))


def render_compare_page(
    token: str, pairs: list[dict], *, review_all: bool, total: int
) -> str:
    """Screen two: either the top-N summary or the full deselect grid.

    The two are genuinely separate templates (not one page branching at
    runtime): the deselect grid must never even ship the approve-all button's
    markup or its "approve-all" choice string, since that page's only
    reachable action is choosing what to swap, not approving anything.
    """
    template = _COMPARE_GRID_TEMPLATE if review_all else _COMPARE_SUMMARY_TEMPLATE
    # __ITEMS_JSON__ goes last: a pathological filename that happened to
    # contain the literal text of another placeholder must not be mangled by
    # a .replace() call that runs after it lands in the template.
    return (template
            .replace("__TOKEN__", token)
            .replace("__REVIEW_ALL__", "true" if review_all else "false")
            .replace("__TOTAL__", str(total))
            .replace("__ITEMS_JSON__", _embed(pairs)))


# --- shared style, copied from video_review.py's idiom -----------------------

_SHARED_STYLE = """
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 14px/1.4 -apple-system, system-ui, sans-serif; background: Canvas; color: CanvasText; }
  header {
    position: sticky; top: 0; z-index: 10; padding: 12px 16px;
    background: Canvas; border-bottom: 1px solid rgba(128,128,128,.3);
    display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
  }
  header .count { font-weight: 600; font-size: 15px; }
  .prog { font-size: 13px; opacity: .7; }
  .notice {
    margin: 12px 16px; padding: 10px 14px; border-radius: 8px; font-size: 13px;
    background: rgba(128,128,128,.10); border: 1px solid rgba(128,128,128,.25);
  }
  .spacer { margin-left: auto; }
  button {
    font: inherit; padding: 6px 12px; border-radius: 6px;
    border: 1px solid rgba(128,128,128,.4); background: Canvas; color: inherit;
    cursor: pointer;
  }
  button:hover { background: rgba(128,128,128,.12); }
  button:disabled { opacity: .5; cursor: default; }
  button:focus-visible { outline: 2px solid #0a7cff; outline-offset: 2px; }
  button.primary {
    background: #0a7cff; border-color: #0865cc; color: #fff; font-weight: 600;
  }
  button.primary:hover:not(:disabled) { background: #0865cc; }
  button.danger {
    background: #d13438; border-color: #b02a2e; color: #fff; font-weight: 600;
  }
  button.danger:hover:not(:disabled) { background: #b02a2e; }
  button.ghost { background: transparent; border-color: transparent; opacity: .75; }
  button.ghost:hover { opacity: 1; background: rgba(128,128,128,.12); }
  [hidden] { display: none !important; }
  .grid {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
    gap: 12px; padding: 16px;
  }
  .card {
    border: 2px solid transparent; border-radius: 10px; overflow: hidden;
    background: rgba(128,128,128,.08); cursor: pointer; display: flex;
    flex-direction: column;
    content-visibility: auto;
    contain-intrinsic-size: auto 220px;
  }
  .card.selected { border-color: #0a7cff; background: rgba(10,124,255,.10); }
  .card.skip { cursor: default; opacity: .55; }
  .thumb {
    position: relative; width: 100%; aspect-ratio: 16 / 10;
    background: rgba(128,128,128,.15); display: flex; overflow: hidden;
  }
  .thumb img { width: 100%; height: 100%; object-fit: cover; display: block; background: #000; }
  .thumb.noposter { background: rgba(128,128,128,.22); }
  .thumb .play {
    position: absolute; inset: 0; display: flex; align-items: center;
    justify-content: center; font-size: 30px; color: #fff;
    text-shadow: 0 1px 6px rgba(0,0,0,.6); pointer-events: none;
  }
  .thumb .check {
    position: absolute; left: 6px; top: 6px; width: 22px; height: 22px;
    border-radius: 6px; background: #0a7cff; color: #fff; font-size: 14px;
    display: none; align-items: center; justify-content: center; pointer-events: none;
  }
  .card.selected .thumb .check { display: flex; }
  .badges {
    position: absolute; right: 6px; top: 6px; display: flex; flex-direction: column;
    gap: 4px; align-items: flex-end; pointer-events: none;
  }
  .badge {
    font-size: 10px; font-weight: 700; letter-spacing: .03em; padding: 2px 6px;
    border-radius: 4px; background: rgba(0,0,0,.6); color: #fff;
  }
  .badge.hdr { background: rgba(255,159,10,.85); color: #1a1200; }
  .badge.slomo { background: rgba(10,124,255,.85); }
  .dur {
    position: absolute; right: 6px; bottom: 6px; font-size: 11px;
    font-variant-numeric: tabular-nums; padding: 1px 6px; border-radius: 4px;
    background: rgba(0,0,0,.6); color: #fff; pointer-events: none;
  }
  .meta { padding: 8px 10px; display: flex; flex-direction: column; gap: 3px; }
  .name { font-size: 12px; font-weight: 600; word-break: break-all; }
  .sub { font-size: 11px; opacity: .7; }
  .saving { font-size: 11px; color: #1f9d55; font-weight: 600; }
  .fps-note { font-size: 11px; color: #a06b00; }
  .skip-reason { font-size: 11px; opacity: .8; font-style: italic; }
  .done { padding: 40px 20px; text-align: center; max-width: 640px; margin: 0 auto; }
  .done h2 { font-size: 20px; }
  /* video modal, copied wholesale from video_review.py */
  .modal { position: fixed; inset: 0; z-index: 50; display: flex;
    align-items: center; justify-content: center; }
  .modal[hidden] { display: none; }
  .modal .backdrop { position: absolute; inset: 0; background: rgba(0,0,0,.8); }
  .modal .dialog {
    position: relative; z-index: 1; max-width: min(96vw, 1400px);
    max-height: 90vh; display: flex; flex-direction: column; gap: 8px;
  }
  .modal video { max-width: 100%; max-height: 78vh; background: #000; border-radius: 8px; }
  .modal .pair { display: flex; gap: 10px; flex-wrap: wrap; justify-content: center; }
  .modal .pair > div { flex: 1 1 320px; display: flex; flex-direction: column; gap: 4px; }
  .modal .pair video { max-height: 60vh; width: 100%; }
  .modal .pair-label { color: #fff; font-size: 12px; opacity: .8; text-align: center; }
  .modal .caption { color: #fff; font-size: 13px; word-break: break-all; text-align: center; }
  .modal .close {
    position: absolute; top: -14px; right: -14px; width: 34px; height: 34px;
    border-radius: 50%; background: #fff; color: #000; font-weight: 700;
    border: none; padding: 0;
  }
"""


# --- screen one: pick what to convert -----------------------------------------

_SELECT_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>video-optimise: choose</title>
<style>""" + _SHARED_STYLE + """
</style>
</head>
<body>
<header>
  <span class="count" id="count">Loading…</span>
  <span class="prog" id="progress"></span>
  <span class="spacer"></span>
  <button id="all">Select all</button>
  <button id="fourk">4K only</button>
  <button id="clear">Clear</button>
  <button class="primary" id="done" disabled>Done — 0 videos</button>
</header>
<main id="main"><div class="grid" id="grid"></div></main>
<div class="modal" id="modal" hidden>
  <div class="backdrop" id="backdrop"></div>
  <div class="dialog">
    <button class="close" id="close">✕</button>
    <video id="player" controls playsinline preload="auto"></video>
    <div class="caption" id="caption"></div>
  </div>
</div>
<script>
const TOKEN = "__TOKEN__";
const ITEMS = __ITEMS_JSON__;
const cards = new Map();     // index -> card element
const byIndex = new Map();   // index -> item
const selected = new Set();  // nothing starts selected
let finished = false;

function humanBytes(n) {
  let f = n;
  for (const u of ["B", "KB", "MB", "GB", "TB"]) {
    if (f < 1024 || u === "TB") return (u === "B" ? Math.round(f) : f.toFixed(1)) + " " + u;
    f /= 1024;
  }
}
function freedFor(it) { return Math.round(it.bytes * (it.percent || 0) / 100); }
function longSide(dims) {
  if (!dims) return 0;
  const parts = dims.split("\\u00d7").map(Number);
  return Math.max(0, ...parts.filter((n) => !Number.isNaN(n)));
}

function addCard(it) {
  byIndex.set(it.index, it);
  const skipped = !!it.skip;
  const card = document.createElement("div");
  card.className = "card" + (skipped ? " skip" : "");
  card.dataset.index = it.index;

  const thumb = document.createElement("div");
  thumb.className = "thumb";
  const shot = document.createElement("img");
  shot.loading = "lazy";
  shot.decoding = "async";
  shot.alt = "";
  shot.src = "/poster/" + it.index;
  // A 404 means "queued, not rendered yet" as often as "can't be rendered" — an
  // <img> can't tell them apart, so back off a few times, then keep the tile.
  let tries = 0;
  shot.onerror = () => {
    if (++tries > 6) { shot.remove(); thumb.classList.add("noposter"); return; }
    setTimeout(() => { shot.src = "/poster/" + it.index + "?try=" + tries; },
               Math.min(400 * tries, 2500));
  };
  const play = document.createElement("span"); play.className = "play"; play.textContent = "▶";
  const check = document.createElement("span"); check.className = "check"; check.textContent = "✓";
  const badges = document.createElement("div"); badges.className = "badges";
  if (it.hdr) {
    const b = document.createElement("span"); b.className = "badge hdr"; b.textContent = "HDR";
    badges.appendChild(b);
  }
  if (it.slomo) {
    const b = document.createElement("span"); b.className = "badge slomo";
    b.textContent = "SLO-MO" + (it.fps ? " " + it.fps : "");
    badges.appendChild(b);
  }
  thumb.append(shot, play, check, badges);
  if (it.dur) {
    const d = document.createElement("span"); d.className = "dur"; d.textContent = it.dur;
    thumb.appendChild(d);
  }
  // The poster opens the preview, independent of the card's own click target.
  thumb.onclick = (ev) => { ev.stopPropagation(); openModal(it); };

  const meta = document.createElement("div");
  meta.className = "meta";
  const name = document.createElement("span"); name.className = "name"; name.textContent = it.name;
  const sizeLine = document.createElement("span"); sizeLine.className = "sub";
  sizeLine.textContent = it.size + (it.dims ? "  ·  " + it.dims : "");
  meta.append(name, sizeLine);
  if (skipped) {
    const reason = document.createElement("span");
    reason.className = "skip-reason"; reason.textContent = it.skip;
    meta.appendChild(reason);
  } else {
    if (it.out) {
      const saving = document.createElement("span"); saving.className = "saving";
      saving.textContent = "→ " + it.out + "  ·  −" + it.percent + "%";
      meta.appendChild(saving);
    }
    if (it.keepsFps) {
      const note = document.createElement("span"); note.className = "fps-note";
      note.textContent = "keeps " + it.fps + " fps";
      meta.appendChild(note);
    }
  }
  card.append(thumb, meta);
  if (!skipped) {
    card.onclick = () => toggle(it.index);
  }
  document.getElementById("grid").appendChild(card);
  cards.set(it.index, card);
}

function toggle(index) {
  const card = cards.get(index);
  if (!card) return;
  if (selected.has(index)) { selected.delete(index); card.classList.remove("selected"); }
  else { selected.add(index); card.classList.add("selected"); }
  updateCounts();
}

function updateCounts() {
  document.getElementById("count").textContent =
    ITEMS.length + " video" + (ITEMS.length === 1 ? "" : "s");
  let freed = 0;
  for (const i of selected) freed += freedFor(byIndex.get(i));
  document.getElementById("progress").textContent =
    selected.size ? (selected.size + " selected — frees " + humanBytes(freed)) : "";
  const btn = document.getElementById("done");
  btn.textContent = "Done — " + selected.size + " video" + (selected.size === 1 ? "" : "s");
  btn.disabled = selected.size === 0;
}

// --- modal preview -------------------------------------------------------
const modal = document.getElementById("modal");
const player = document.getElementById("player");
function openModal(it) {
  document.getElementById("caption").textContent = it.name + "  ·  " + it.size;
  player.src = "/video/" + it.index;
  player.load();
  modal.hidden = false;
  player.play().catch(() => {});
}
function closeModal() {
  player.pause();
  player.removeAttribute("src");
  player.load();
  modal.hidden = true;
}
document.getElementById("close").onclick = closeModal;
document.getElementById("backdrop").onclick = closeModal;
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !modal.hidden) closeModal();
});

document.getElementById("all").onclick = () => {
  for (const it of ITEMS) if (!it.skip) { selected.add(it.index); cards.get(it.index).classList.add("selected"); }
  updateCounts();
};
document.getElementById("fourk").onclick = () => {
  for (const it of ITEMS) {
    if (!it.skip && longSide(it.dims) >= 3000) {
      selected.add(it.index); cards.get(it.index).classList.add("selected");
    }
  }
  updateCounts();
};
document.getElementById("clear").onclick = () => {
  for (const i of [...selected]) { selected.delete(i); cards.get(i).classList.remove("selected"); }
  updateCounts();
};

async function finishSelection() {
  if (finished) return;
  finished = true;
  closeModal();
  try {
    await fetch("/finish", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Clean-Token": TOKEN },
      body: JSON.stringify({ ids: [...selected], choice: "done" }),
    });
  } catch (e) { /* the terminal has what it needs regardless */ }
  showSent();
}

function showSent() {
  document.querySelector("header").style.display = "none";
  const main = document.getElementById("main");
  main.innerHTML = "";
  const div = document.createElement("div");
  div.className = "done";
  const h = document.createElement("h2");
  h.textContent = "Selection sent — you can close this tab.";
  div.appendChild(h);
  main.appendChild(div);
}

document.getElementById("done").onclick = finishSelection;

for (const it of ITEMS) addCard(it);
updateCounts();
</script>
</body>
</html>
"""


# --- screen two: original vs. converted ---------------------------------------

_COMPARE_STYLE_EXTRA = """
  .rows { display: flex; flex-direction: column; gap: 18px; padding: 4px 16px 24px; }
  .row { border: 1px solid rgba(128,128,128,.25); border-radius: 10px; padding: 12px; }
  .row-head { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-bottom: 8px; }
  .row-head .name { font-weight: 600; font-size: 13px; word-break: break-all; }
  .pill {
    font-size: 11px; font-weight: 600; padding: 2px 9px; border-radius: 999px;
    background: rgba(128,128,128,.18);
  }
  .row-head .change { font-size: 12px; opacity: .8; margin-left: auto; }
  .panes { display: flex; gap: 12px; flex-wrap: wrap; }
  .pane { flex: 1 1 320px; display: flex; flex-direction: column; gap: 4px; }
  .pane video { width: 100%; max-height: 340px; background: #000; border-radius: 8px; }
  .pane .lbl { font-size: 12px; font-weight: 600; }
  .pane .sub { font-size: 11px; opacity: .65; }
  .row-actions { margin-top: 8px; }
  footer {
    position: sticky; bottom: 0; z-index: 10; padding: 12px 16px;
    background: Canvas; border-top: 1px solid rgba(128,128,128,.3);
    display: flex; flex-wrap: wrap; gap: 10px; justify-content: flex-end;
  }
"""


# Mode A — the top-N summary. Its own template (not a runtime branch of a
# shared one) so that the approve-all button and its "approve-all" choice
# string physically cannot appear in the deselect-grid page's source: a page
# that never offers the point-of-no-return button should not even ship its
# code.
_COMPARE_SUMMARY_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>video-optimise: compare</title>
<style>""" + _SHARED_STYLE + _COMPARE_STYLE_EXTRA + """
</style>
</head>
<body>
<header>
  <span class="count" id="count">Loading…</span>
</header>
<div class="notice">
  These are ready to upload. When you finish here, the terminal will show you
  the folder they're in. Upload them to iCloud Photos yourself — through
  icloud.com, Photos on a Mac, or Files on an iPhone or iPad — then run the
  command again to have the originals they replace deleted.
</div>
<main id="main"></main>
<footer id="footer">
  <button id="reviewAllBtn">Review all __TOTAL__ side by side</button>
  <button class="ghost" id="cancelBtn">Cancel — change nothing</button>
  <button class="primary" id="approveBtn">Finish — these are ready to upload</button>
</footer>
<script>
const TOKEN = "__TOKEN__";
const REVIEW_ALL = __REVIEW_ALL__;
const TOTAL = __TOTAL__;
const ITEMS = __ITEMS_JSON__;
let finished = false;

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function sendChoice(choice) {
  if (finished) return;
  finished = true;
  try {
    await fetch("/finish", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Clean-Token": TOKEN },
      body: JSON.stringify({ choice }),
    });
  } catch (e) { /* the terminal has what it needs regardless */ }
  showSent();
}

function showSent() {
  document.querySelector("header").style.display = "none";
  document.getElementById("footer").hidden = true;
  const main = document.getElementById("main");
  main.innerHTML = "";
  const div = document.createElement("div");
  div.className = "done";
  const h = document.createElement("h2");
  h.textContent = "Choice sent — returning to the terminal. You can close this tab.";
  div.appendChild(h);
  main.appendChild(div);
}

// preload="none" matters: without it, opening the page starts as many
// simultaneous downloads as there are rows. Written as literal markup (rather
// than set via the video element's .preload property) so the attribute is
// unmistakably present in the page whether or not a browser ever loads it.
function buildPane(label, sub, src, index, posterBase) {
  const base = (posterBase || "/poster/") + index;
  const pane = document.createElement("div"); pane.className = "pane";
  pane.innerHTML =
    '<span class="lbl">' + escapeHtml(label) + '</span>' +
    '<video controls preload="none" poster="' + base + '" src="' + escapeHtml(src) + '"></video>' +
    '<span class="sub">' + escapeHtml(sub || "") + '</span>';
  const video = pane.querySelector("video");
  retryPoster(video, base);
  return { pane, video };
}

// The poster is rendered in the background on first request, so the very first
// GET usually 404s — and a <video poster> that failed is never retried by the
// browser. Without this every pane opens as a black rectangle on a screen whose
// whole job is letting you look at the picture. Probe with an Image (which we
// can retry), then hand the video a URL it has not already given up on.
function retryPoster(video, base) {
  let tries = 0;
  const probe = new Image();
  probe.onload = () => { video.poster = probe.src; };
  probe.onerror = () => {
    if (++tries > 6) return;                 // unrenderable: leave it blank
    setTimeout(() => { probe.src = base + "?try=" + tries; }, 250 * tries);
  };
  probe.src = base + "?try=0";
}

function buildSummary() {
  document.getElementById("count").textContent =
    "Showing " + ITEMS.length + " of " + TOTAL + " converted video" + (TOTAL === 1 ? "" : "s");
  const main = document.getElementById("main");
  const rows = document.createElement("div"); rows.className = "rows";
  const allVideos = [];
  for (const it of ITEMS) {
    const row = document.createElement("div"); row.className = "row";
    const head = document.createElement("div"); head.className = "row-head";
    const name = document.createElement("span"); name.className = "name"; name.textContent = it.name;
    const pill = document.createElement("span"); pill.className = "pill"; pill.textContent = it.colour;
    const change = document.createElement("span"); change.className = "change";
    change.textContent = it.srcSize + " → " + it.outSize + "  ·  −" + it.percent + "%";
    head.append(name, pill, change);

    const panes = document.createElement("div"); panes.className = "panes";
    const paneA = buildPane("Original", it.srcLabel, "/original/" + it.index, it.index, "/src-poster/");
    const paneB = buildPane("Optimised", it.outLabel, "/converted/" + it.index, it.index, "/poster/");
    panes.append(paneA.pane, paneB.pane);
    allVideos.push([paneA.video, paneB.video]);

    const actions = document.createElement("div"); actions.className = "row-actions";
    const playBoth = document.createElement("button");
    playBoth.textContent = "▶ play both";
    playBoth.onclick = () => {
      const playing = !paneA.video.paused || !paneB.video.paused;
      if (playing) { paneA.video.pause(); paneB.video.pause(); }
      else { paneA.video.play().catch(() => {}); paneB.video.play().catch(() => {}); }
    };
    actions.appendChild(playBoth);

    row.append(head, panes, actions);
    rows.appendChild(row);
  }
  main.appendChild(rows);

  // Starting any video pauses every video in every *other* row.
  allVideos.forEach((pair, i) => {
    for (const v of pair) {
      v.addEventListener("play", () => {
        allVideos.forEach((other, j) => {
          if (j === i) return;
          for (const ov of other) ov.pause();
        });
      });
    }
  });

  document.getElementById("reviewAllBtn").onclick = () => sendChoice("review-all");
  document.getElementById("cancelBtn").onclick = () => sendChoice("cancel");
  document.getElementById("approveBtn").onclick = () => sendChoice("approve-all");
}

buildSummary();
</script>
</body>
</html>
"""


# Mode B — the deselect grid. Everything starts ticked; unticking means
# "discard this conversion — don't upload it". Its own template, for the same
# reason the summary has one:
# this page must never carry the approve-all button or choice string at all.
# Overrides the shared modal's two-video pane, only on this page. The shared
# ``.modal .dialog`` (in _SHARED_STYLE) has a max-width but no width, so it is
# sized by shrink-to-fit -- and an auto-width flex container with wrapping
# children has no reliably specified intrinsic-width algorithm across browsers.
# For a single video (the select page's modal) that accident lands close enough
# to right; for two side-by-side portrait videos it was observed to collapse to
# one column, at which point the surviving pane's box was pinned to the
# collapsed dialog's full (wrong) width and the video letterboxed inside it.
# Giving the dialog a real ``width`` here removes the ambiguity outright, and
# ``flex-wrap: nowrap`` plus a pure aspect-ratio-bounded video (``width: auto``
# instead of ``100%``) makes the "always side by side, always the true shape"
# outcome the only one the layout can produce.
_GRID_STYLE_EXTRA = """
  .modal .dialog { width: min(96vw, 1400px); }
  .modal .pair { flex-wrap: nowrap; }
  /* align-items defaults to stretch, which forces a flex child's cross-axis
     size (width, since this is a column flex) to fill the container even when
     the child itself says width:auto -- stretch wins over intrinsic sizing.
     Centering here is what actually lets the video size to its own aspect
     ratio instead of being pinned to the pane's full width and letterboxed. */
  .modal .pair > div { flex: 1 1 0; min-width: 0; align-items: center; }
  .modal .pair video { width: auto; height: auto; max-width: 100%; max-height: 60vh; }
"""

_COMPARE_GRID_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>video-optimise: review all</title>
<style>""" + _SHARED_STYLE + _GRID_STYLE_EXTRA + """
</style>
</head>
<body>
<header>
  <span class="count" id="count">Loading…</span>
  <span class="prog" id="progress"></span>
  <span class="spacer"></span>
  <button class="primary" id="done">Done — upload 0</button>
</header>
<div class="notice">
  These are ready to upload. When you finish here, the terminal will show you
  the folder they're in. Upload them to iCloud Photos yourself — through
  icloud.com, Photos on a Mac, or Files on an iPhone or iPad — then run the
  command again to have the originals they replace deleted.
</div>
<main id="main"><div class="grid" id="grid"></div></main>
<div class="modal" id="modal" hidden>
  <div class="backdrop" id="backdrop"></div>
  <div class="dialog">
    <button class="close" id="close">✕</button>
    <div class="pair">
      <div><video id="playerA" controls playsinline preload="auto"></video>
        <div class="pair-label">Original</div></div>
      <div><video id="playerB" controls playsinline preload="auto"></video>
        <div class="pair-label">Optimised</div></div>
    </div>
    <div class="caption" id="caption"></div>
  </div>
</div>
<script>
const TOKEN = "__TOKEN__";
const REVIEW_ALL = __REVIEW_ALL__;
const TOTAL = __TOTAL__;
const ITEMS = __ITEMS_JSON__;
let finished = false;
const selected = new Set();
const byIndex = new Map();
const cards = new Map();

function parseHuman(s) {
  if (!s) return 0;
  const m = /^([\\d.]+)\\s*([A-Za-z]+)$/.exec(String(s).trim());
  if (!m) return 0;
  const mult = { B: 1, KB: 1024, MB: 1024 ** 2, GB: 1024 ** 3, TB: 1024 ** 4 }[m[2].toUpperCase()] || 1;
  return Math.round(parseFloat(m[1]) * mult);
}
function humanBytes(n) {
  let f = n;
  for (const u of ["B", "KB", "MB", "GB", "TB"]) {
    if (f < 1024 || u === "TB") return (u === "B" ? Math.round(f) : f.toFixed(1)) + " " + u;
    f /= 1024;
  }
}
function freedFor(it) { return parseHuman(it.srcSize) - parseHuman(it.outSize); }

// --- modal: check a decision before it's made, both videos side by side ---
const modal = document.getElementById("modal");
const playerA = document.getElementById("playerA");
const playerB = document.getElementById("playerB");
function openModal(it) {
  document.getElementById("caption").textContent = it.name;
  playerA.src = "/original/" + it.index;
  playerB.src = "/converted/" + it.index;
  playerA.load(); playerB.load();
  modal.hidden = false;
}
function closeModal() {
  for (const p of [playerA, playerB]) {
    p.pause(); p.removeAttribute("src"); p.load();
  }
  modal.hidden = true;
}
document.getElementById("close").onclick = closeModal;
document.getElementById("backdrop").onclick = closeModal;
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !modal.hidden) closeModal();
});

async function finishGrid() {
  if (finished) return;
  finished = true;
  closeModal();
  try {
    await fetch("/finish", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Clean-Token": TOKEN },
      body: JSON.stringify({ ids: [...selected], choice: "done" }),
    });
  } catch (e) { /* the terminal has what it needs regardless */ }
  showSent();
}

function showSent() {
  document.querySelector("header").style.display = "none";
  const main = document.getElementById("main");
  main.innerHTML = "";
  const div = document.createElement("div");
  div.className = "done";
  const h = document.createElement("h2");
  h.textContent = "Choice sent — returning to the terminal. You can close this tab.";
  div.appendChild(h);
  main.appendChild(div);
}

function buildCard(it) {
  const card = document.createElement("div");
  card.className = "card selected";
  card.dataset.index = it.index;

  const thumb = document.createElement("div"); thumb.className = "thumb";
  const shot = document.createElement("img");
  shot.loading = "lazy"; shot.decoding = "async"; shot.alt = "";
  shot.src = "/poster/" + it.index;
  let tries = 0;
  shot.onerror = () => {
    if (++tries > 6) { shot.remove(); thumb.classList.add("noposter"); return; }
    setTimeout(() => { shot.src = "/poster/" + it.index + "?try=" + tries; },
               Math.min(400 * tries, 2500));
  };
  const check = document.createElement("span"); check.className = "check"; check.textContent = "✓";
  const badges = document.createElement("div"); badges.className = "badges";
  const pct = document.createElement("span"); pct.className = "badge"; pct.textContent = "−" + it.percent + "%";
  badges.appendChild(pct);
  if (it.hdr) { const b = document.createElement("span"); b.className = "badge hdr"; b.textContent = "HDR"; badges.appendChild(b); }
  if (it.slomo) { const b = document.createElement("span"); b.className = "badge slomo"; b.textContent = "SLO-MO"; badges.appendChild(b); }
  thumb.append(shot, check, badges);
  thumb.onclick = (ev) => { ev.stopPropagation(); openModal(it); };

  const meta = document.createElement("div"); meta.className = "meta";
  const name = document.createElement("span"); name.className = "name"; name.textContent = it.name;
  const sizeLine = document.createElement("span"); sizeLine.className = "sub swap-line";
  sizeLine.textContent = it.srcSize + " → " + it.outSize + "  ·  −" + it.percent + "%";
  meta.append(name, sizeLine);

  card.append(thumb, meta);
  card.onclick = () => toggleCard(it.index);
  cards.set(it.index, card);
  return card;
}

function toggleCard(index) {
  const card = cards.get(index);
  const it = byIndex.get(index);
  const line = card.querySelector(".swap-line");
  if (selected.has(index)) {
    selected.delete(index);
    card.classList.remove("selected");
    line.textContent = "discard — won't upload";
  } else {
    selected.add(index);
    card.classList.add("selected");
    line.textContent = it.srcSize + " → " + it.outSize + "  ·  −" + it.percent + "%";
  }
  updateCounts();
}

function updateCounts() {
  document.getElementById("count").textContent =
    selected.size + " of " + TOTAL + " ready to upload";
  let freed = 0;
  for (const i of selected) freed += freedFor(byIndex.get(i));
  document.getElementById("progress").textContent =
    selected.size ? ("frees " + humanBytes(freed)) : "";
  document.getElementById("done").textContent = "Done — upload " + selected.size;
}

for (const it of ITEMS) { byIndex.set(it.index, it); selected.add(it.index); }
const grid = document.getElementById("grid");
for (const it of ITEMS) grid.appendChild(buildCard(it));
document.getElementById("done").onclick = finishGrid;
updateCounts();
</script>
</body>
</html>
"""
