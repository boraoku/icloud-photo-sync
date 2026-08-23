# icloud-photo-sync

A single-user **macOS command-line** tool that downloads your **entire iCloud
Photos library** into the current folder, organised by capture date:

```
./YYYY/MM/<filename>      e.g.  ./2026/07/IMG_1234.HEIC
```

Syncing is **one-way only** — it downloads and adds, and **never deletes or
modifies** anything, locally or in iCloud. You can stop it any time and resume;
interrupted files (large videos) resume at the byte level when the server allows
it, and it can keep the folder up to date by pulling only newly-added items.

The clean-up commands are the one exception, and only when you ask for them:
`local-clean` and `video-clean` move files you pick to the macOS Trash, and with
`--icloud-delete` they can also move the matching iCloud assets to *Recently
Deleted* — after showing you exactly what matched and asking you to confirm.

> Mental model: **`sync` = start · Ctrl-C = stop · `sync` again = resume ·
> `sync --watch N` = stay current.**

---

## Account preconditions (read this first)

This tool uses Apple's iCloud **web/CloudKit** services (via `pyicloud`). Your
Apple Account must be set up so those are reachable, or Apple returns
`ACCESS_DENIED` and login fails:

1. **Turn ON “Access iCloud Data on the Web.”**
   On iPhone/iPad: *Settings → [your name] → iCloud → Access iCloud Data on the Web.*
2. **Turn OFF “Advanced Data Protection” (ADP).**
   ADP disables web access to iCloud data, which this approach depends on.
   **There is no workaround** — with ADP on, this tool cannot work.
3. **Have a trusted device or SMS for 2FA.** A hardware security key (FIDO) as
   the *only* second factor is not supported; you need to receive a 6-digit code.

The tool detects the `ACCESS_DENIED` symptom and prints these steps.

---

## Install

```bash
git clone <this repo>            # or copy the folder
cd icloud-photo-sync
./bootstrap.sh                   # creates .venv and installs everything
source .venv/bin/activate
```

Requires Python ≥ 3.11 on macOS (Apple Silicon or Intel).

### Running multiple copies on one machine

You can keep several independent copies of this repo (e.g. one per drive) and run
each on its own. The one rule: **a virtualenv is not relocatable.** Its
`.venv/bin/activate` and `pyvenv.cfg` hardcode the absolute path where it was
created, so if you `cp -R` a folder that already contains `.venv`, the copy's
`activate` still points PATH back at the *original* copy — the classic symptom is
`which icloud-photo-sync` showing a different drive than the one you're in.

So for each copy, give it its own environment:

```bash
cp -R /Volumes/A/icloud-photo-sync /Volumes/B/icloud-photo-sync   # copy anywhere
cd /Volumes/B/icloud-photo-sync
./bootstrap.sh          # rebuilds .venv for THIS path if it was copied in
source .venv/bin/activate
```

`bootstrap.sh` detects a `.venv` that belongs to a different path and rebuilds it
automatically, so re-running it after a copy is always safe. Always `source` the
`activate` from the copy you're standing in (or just call `./.venv/bin/icloud-photo-sync`
directly, which never depends on PATH). `.venv` is git-ignored, so `git clone`
never carries a foreign one — only a manual folder copy does.

## Quick start

```bash
# 1) One-time sign-in (handles 2FA, then persists the session ~60 days).
icloud-photo-sync login

# 2) Go to the folder you want photos in, and start.
cd /Volumes/MyDrive/iCloudPhotos
icloud-photo-sync sync           # downloads everything into ./YYYY/MM/

# Stop with Ctrl-C at any time. Re-run to resume — nothing already done is re-fetched.

# 3) Later, keep it current (only new photos):
icloud-photo-sync sync --update          # one quick incremental pass
icloud-photo-sync sync --watch 1800      # or loop every 30 min

# Anytime:
icloud-photo-sync status
```

The password is stored in the **macOS Keychain** (never on the command line).
You are asked once during `login`; clear it with `--reset-keyring` (this also
clears the entry pyicloud itself keeps under `pyicloud://icloud-password`, which
it would otherwise silently fall back to). If a saved password stops working —
e.g. after an Apple ID password change — `login` prompts you again instead of
failing.

---

## CLI reference

```
icloud-photo-sync [GLOBAL] login
icloud-photo-sync [GLOBAL] sync [--update] [--watch SECONDS] [--until-found N]
icloud-photo-sync [GLOBAL] status
icloud-photo-sync [GLOBAL] local-clean [--max-size SIZE] [--lm-url URL]
                                       [--lm-model NAME] [--flag CATS]
                                       [--limit N] [--reclassify] [--no-browser]
                                       [--icloud-delete] [--icloud-dry-run]
                                       [--max-delete N]
icloud-photo-sync [GLOBAL] video-clean [--min-size SIZE] [--port N] [--no-browser]
                                       [--icloud-delete] [--icloud-dry-run]
                                       [--max-delete N]
icloud-photo-sync [GLOBAL] icloud-delete (--last | --from MANIFEST | --explain RECEIPT
                                        | --scan-trashed)
                                       [--dry-run] [--max-delete N]
                                       [--max-size SIZE] [--min-size SIZE]
                                       [--corroborate-root DIR] [--no-review]

GLOBAL options:
  -u, --username APPLE_ID   Apple ID (else $ICLOUD_SYNC_USERNAME, else prompt)
  -d, --directory PATH      Output root (default: current directory)
  -v, --verbose             Verbose logging
      --reset-keyring       Forget the stored iCloud password
```

* **`login`** — authenticate and persist the session. Run once initially, and
  again only if the session expires (~every 60 days) or 2FA is re-required.
* **`sync`** (no flags) — full, resumable pass: download everything not yet
  downloaded. Stop with Ctrl-C; re-run to resume.
  * `--update` — incremental pass: iterate newest-first and stop after
    `--until-found` (default 50) consecutive already-have items. Fast, because
    new photos are at the top of the library.
  * `--watch N` — repeat the incremental pass every `N` seconds (min 300).
* **`status`** — completed / pending / failed counts, last pass times, total
  bytes downloaded, and any failed assets (which are retried on the next sync).
* **`local-clean`** — find and remove junk images (screenshots, memes, saved
  web graphics) from an already-downloaded tree. No iCloud login required. See
  the section below.
* **`video-clean`** — list downloaded videos largest-first, preview any of them
  in the browser, and move the ones you choose to the Trash to reclaim space. No
  iCloud login and no model required. See the section below.
* **`video-optimise`** — re-encode oversized videos to 1080p HEVC, keeping the
  capture date, location, HDR colour and (for slow motion) the frame rate; you
  upload the results yourself, and it reconciles them back into your library.
  See the section below.
* **`icloud-delete`** — finish or retry deleting already-trashed files from
  iCloud, using the manifest a clean session wrote; or `--scan-trashed` to
  reconstruct a session that ran before the flag existed. See *Deleting from
  iCloud too*.

Exit codes: `2` = account precondition problem, `3` = session expired (run
`login`), `4` = must accept Apple terms, `5` = a deletion could not be verified
(nothing further was attempted), `1` = other error.

---

## Clean out screenshots & memes (`local-clean`)

Over the years an iCloud library accumulates small non-photos: screenshots,
shared memes, saved web graphics. `local-clean` finds them locally and lets you
trash them after a visual review. It never contacts iCloud.

```bash
# From the photo root (or pass -d PATH). Needs a local vision model running.
icloud-photo-sync local-clean
```

How it works:

1. **Scan** — walks the tree for JPG/PNG files at or below `--max-size`
   (default 1 MB — real camera photos are almost always larger).
2. **Classify** — sends each image to a **local** vision model and bins it as
   `screenshot`, `meme`, `photo`, or `other`. Results are cached (keyed by path
   + size + mtime), so re-runs are instant and Ctrl-C is always safe — it
   resumes where it stopped. At ~10-15 s per image, use `--limit N` to work
   through a large library in chunks.
3. **Review** — opens a local web page with a grid of every flagged image
   (categories `screenshot,meme,other` by default), all pre-selected for
   deletion. Deselect anything you want to keep, then click **Move to Trash**.
4. **Trash** — the selected files go to the macOS Trash via Finder, so they keep
   *Put Back* and are trivially recoverable. Nothing is ever deleted without
   your click.

The vision model must speak the OpenAI chat API. [LM Studio](https://lmstudio.ai)
with a vision model (e.g. `qwen/qwen3.5-9b`) works out of the box: load the
model, start its local server, and leave the defaults. Point elsewhere with
`--lm-url` / `--lm-model` (or `$ICLOUD_SYNC_LM_URL`).

```
--max-size SIZE    Only images at or below this (e.g. 500KB, 2MB). Default 1MB.
--lm-url URL       Vision model base URL. Default http://127.0.0.1:1234.
--lm-model NAME    Model name. Default qwen/qwen3.5-9b.
--flag CATS        Comma-separated categories to flag. Default screenshot,meme,other.
--limit N          Classify at most N new images this run (resume later).
--reclassify       Ignore the cache and re-classify everything.
--no-browser       Print the review URL instead of opening a browser.
--icloud-delete    Also offer to delete the trashed images from iCloud.
--icloud-dry-run   Show what would be deleted from iCloud; delete nothing.
--max-delete N     Cap iCloud deletions for this run (default 500).
```

**First run trashes via Finder**, so macOS shows a one-time prompt asking to let
your terminal control Finder — approve it (System Settings → Privacy & Security
→ Automation). If denied, trashing fails with a reminder and no files move.

---

## Reclaim space from videos (`video-clean`)

Videos are the heaviest thing in a photo library — a handful of clips can be more
than all your stills combined. `video-clean` lists every downloaded video from
**largest to smallest**, lets you preview any of them, and moves the ones you
pick to the Trash. It needs **no iCloud login and no model** — the scan is an
instant `stat` walk, and every deletion is your explicit choice.

```bash
# From the photo root (or pass -d PATH).
icloud-photo-sync video-clean
```

How it works:

1. **Scan** — walks the tree for video files (`.mov`, `.mp4`, `.m4v`, `.mkv`,
   `.avi`, and more), sorted largest-first. Use `--min-size` to hide small clips.
2. **Review** — opens a local web page listing each video with its size, date and
   length (`HH:MM:SS`). Each card shows a poster frame, rendered in the
   background as you scroll (via `ffmpeg` if you have it, otherwise QuickLook —
   both optional; cards without one still list and play). Posters are cached in
   `.icloud-photo-sync/posters/` inside the photo folder, a few tens of KB each,
   so later runs are instant — deleting that folder just re-renders them, and
   the scan ignores it. **Nothing is pre-selected** — it's entirely up to you.
   Click any card to open a preview player (with seek/scrubbing); tick the
   checkbox on the ones you want gone. The header shows how much space the
   current selection would free.
3. **Trash** — click **Move to Trash** and the selected files go to the macOS
   Trash via Finder, keeping *Put Back*. The terminal reports how many files
   moved and how many bytes were freed. Click **Finish** (or press Ctrl-C) to end.

```
--min-size SIZE    Only list videos at or above this (e.g. 50MB, 1GB). Default 0.
--icloud-delete    Also offer to delete the trashed videos from iCloud.
--icloud-dry-run   Show what would be deleted from iCloud; delete nothing.
--max-delete N     Cap iCloud deletions for this run (default 500).
--port N           Review server port (0 = auto).
--no-browser       Print the review URL instead of opening a browser.
```

Like `local-clean`, the first trash triggers the one-time macOS prompt to let
your terminal control Finder — approve it under Privacy & Security → Automation.

---

## Shrink videos in iCloud (`video-optimise`)

`video-clean` frees space by *removing* clips. `video-optimise` frees space by
making them smaller and putting them back — so the video stays in your library,
in the right place on the timeline, just at a sane bitrate.

On the library this was built against, videos were **6% of the files and 61% of
the bytes**. Re-encoding the big ones to 1080p HEVC gives back about **39 GB**
and takes roughly six hours of (interruptible) encoding.

**Apple has closed every programmatic way to upload into iCloud Photos.** This
was checked against a live account: the legacy `uploadimagews` endpoint returns
HTTP 410 Gone for every request shape tried — raw body under three content
types, multipart, every media type, down to a 247-byte JPEG — and the modern
CloudKit `assets/upload` route returns a permanent `QUOTA_EXCEEDED` policy
refusal, retry timer included, on an account sitting at 21.8 GiB used of 200 GiB
free. The same session's reads and deletes kept working throughout, so this is
an upload-specific wall, not an auth problem — and it's the same wall pyicloud's
own upload helper hits, since it targets the same dead endpoint.

So `video-optimise` no longer uploads anything itself. It converts, **you**
upload — with whatever client Apple still lets do that — and then it
reconciles the results back into your library.

```bash
# From the photo root (or pass -d PATH). Needs ffmpeg.
icloud-photo-sync video-optimise --dry-run          # see the plan, change nothing
icloud-photo-sync video-optimise --offline          # convert locally, no Apple ID
icloud-photo-sync video-optimise                    # convert, then reconcile any uploads found
icloud-photo-sync video-optimise --reconcile-only   # just check iCloud and finish pending swaps
```

### The flow

1. **Scan and probe** — every video is `ffprobe`d, and the terminal prints what
   your library is made of and what could be freed.
2. **Choose** — a browser page lists every video largest-first with the saving
   each would give. **Nothing is pre-selected.** Videos that were skipped are
   shown greyed out *with the reason*, so "why isn't my biggest clip here?" is
   answerable from the page.
3. **Convert** — one file at a time, into a single flat, visible folder,
   `optimised/`, at the top of your photo folder. **Your originals are not
   touched.** When conversion finishes the terminal prints the folder's path
   and opens it in Finder.
4. **You upload.** Drag the folder's contents into icloud.com/photos in a
   browser, into Photos on a Mac (File → Import), or copy them onto an iPhone
   or iPad and add them from Files. Any device, any pace — this is the one step
   Apple now reserves for its own clients, so the tool can't do it for you.
5. **Run `video-optimise` again.** Before it scans anything else, it checks
   iCloud for uploads you've made, matches each one to the conversion it came
   from, and — behind a typed confirmation — deletes the originals those
   uploads replace, offers to move the local originals to the Trash, and slots
   the optimised copies into your library in their place.

Every long phase is interruptible with Ctrl-C and resumes by re-running the same
command. Nothing already done is repeated.

### Matching an upload back to its original

Reconciliation is strict on purpose — a wrong guess here deletes the wrong
video, so an unmatched conversion is left alone rather than resolved by
best guess.

* A match requires the **filename and the byte size to both be exact**, and the
  iCloud asset must not already be the row's own original.
* **If two candidates match one conversion, that conversion is refused** and
  left alone rather than guessed at.
* **Nothing is deleted whose replacement isn't verified present.** The
  replacement is read back from iCloud immediately before its original is
  touched, even if the matching happened days earlier in an interrupted run.

### Filename collisions

Flattening a dated folder tree into one flat `optimised/` folder collides —
cameras reuse names across years. On a real library, 647 candidate videos
shared only 17 basenames; `IMG_0003` alone showed up five times, from five
different years. A colliding conversion is renamed with the source's year and
month, e.g. `IMG_0003-2019-06.mov`, so nothing in the folder overwrites
anything else.

The `optimised/` folder itself is excluded from every scan: `video-clean` won't
offer these files for trashing, and `video-optimise` won't try to convert its
own output on a later run.

### Run it again before your next `sync`

If a `sync` lands between uploading and reconciling, it downloads your uploaded
copies as new files before `video-optimise` gets the chance to match and retire
the originals they replace — harmless duplicates, but confusing to sort out
later. Run `video-optimise` (or `--reconcile-only`) first.

### What it does to your footage

The settings are chosen per file from what `ffprobe` finds, not applied
uniformly:

* **HDR clips stay HDR.** An HLG/BT.2020 10-bit source is re-encoded as HEVC
  10-bit with its colour tags carried through. It is never converted to H.264 or
  dropped to 8-bit, which is what makes HDR footage look washed out.
* **The colour is then checked on the file that came out.** If the transfer,
  primaries, colourspace or bit depth do not match the source, the conversion is
  deleted and your original is kept. No clip is uploaded without passing this.
* **Slow motion keeps its frame rate.** Above 60 fps no frame-rate flag is
  passed at all — forcing 30 fps on a 240 fps clip would drop seven of every
  eight frames and play it back at normal speed. A slow-motion clip that is
  already at the target resolution is skipped entirely.
* **The shorter side is capped at 1080, never a 1920×1080 box.** A portrait
  1080×1920 clip stays 1080×1920. Nothing is ever upscaled.
* **Nothing grows.** If the output is not at least 25% smaller than the input it
  is thrown away and the original kept.
* **Capture date, timezone, GPS location and camera model survive**
  (`com.apple.quicktime.creationdate` and friends), which is what lets Photos
  file the replacement on the right date.

### What a swap costs you

iCloud has no "replace the bytes of this asset" API, so your uploaded copy is a
**new asset**. It keeps its capture date, timezone, location and camera model —
the timeline and Places stay right — but it **permanently loses**:

* album membership, including shared albums
* Favourite and Hidden status
* captions, keywords and edits
* people and face tags
* its place in Memories
* its "Added" date, which becomes whenever you uploaded it, so *Recently Added*
  reorders

For holiday clips that is usually a fair trade. For videos you have curated into
albums it is not. The confirmation screen says so before anything happens, and
asks you to type `swap N videos` and then `YES I AM SURE`.

The original is deleted only after its replacement has been read back from
iCloud and confirmed present — see *Matching an upload back to its original*
above. Originals go to *Recently Deleted* and are recoverable for 30 days.
Local originals go to the macOS Trash, never `unlink`, and only after their
swap is confirmed.

```
--min-size SIZE      Only consider videos at or above this. Default 20MB.
--short-side N       Cap the SHORTER side (default 1080; portrait-safe).
--max-fps N          Frame-rate cap (default 30). Never applied to slow motion.
--hdr-bitrate RATE   Target for HDR/10-bit sources at 1080p (default 8M).
--sdr-bitrate RATE   Target for 8-bit sources at 1080p (default 6M).
--skip-hdr           Leave HDR clips alone entirely.
--hdr-only           Only convert HDR clips (usually where most of the space is).
--limit N            Convert at most N this run; re-run for the rest.
--restart            Throw away the unfinished job and start over.
--dry-run            Print the exact ffmpeg command per file. Change nothing.
--offline            Convert only: resolve no Apple ID, touch no network.
--reconcile-only     Only finish pending uploads: check iCloud, delete, stop.
--port N             Review server port (0 = auto).
--no-browser         Print the review URL instead of opening a browser.
```

**Requires `ffmpeg` with `hevc_videotoolbox`** (`brew install ffmpeg` on Apple
silicon). The command refuses to start without it rather than falling back to a
software encoder that would take days.

---

## Deleting from iCloud too (`--icloud-delete`)

`local-clean` and `video-clean` only move files to the macOS Trash. The photos
themselves stay in iCloud — so the space is never actually reclaimed upstream,
and **the next `sync` downloads them all again**. Passing `--icloud-delete` adds
an opt-in step that moves the matching iCloud assets to *Recently Deleted*.

```bash
icloud-photo-sync video-clean --icloud-delete
```

What happens, in order:

1. **Before the scan**, the terminal checks your iCloud session. If it has
   expired you find out immediately — not after an hour of reviewing.
2. The review page shows a red **iCloud deletion armed** banner and an
   **Also delete from iCloud** checkbox (on by default). Unticking it for a
   round trashes those files locally only. The page can only ever *narrow* the
   decision — it cannot enable something the terminal did not authorise.
3. **When the review ends**, the terminal lists exactly what matched, writes a
   manifest, and asks you to type the number of assets back before anything is
   deleted. Files it cannot match are named, with the reason, and left in iCloud.
4. Each batch is deleted and then **independently re-checked**. If a deletion
   cannot be confirmed the run stops immediately (exit code `5`) rather than
   repeating an operation whose effect it cannot read.

### What it refuses to delete

A trashed file is only eligible when **exactly one** asset in the sync manifest
claims its path, that download completed, the recorded size matches the file as
it was the moment before trashing, and the file really is gone from disk. It
never matches on filename — thousands of assets in a real library share one.
Anything else is listed and skipped, because a file left in iCloud costs one
re-download, and a wrongly deleted one costs a photo.

Immediately before deleting, the live iCloud records are re-read and the
filename, original size and capture date must still agree. Shared-library assets
are always refused.

### Getting them back

Deletion sets iCloud's "Recently Deleted" state — **recoverable for about 30
days** on any device signed into that Apple ID (Photos → Albums → Recently
Deleted → Recover). The tool never touches the permanent-deletion flag. Your
local copies also remain in the macOS Trash until you empty it (Finder → Trash →
Put Back). Every run writes a manifest and an append-only receipt under
`~/Library/Application Support/icloud-photo-sync/deletions/`, and
`icloud-delete --explain <receipt>` re-reads iCloud to tell you the current
state of everything in it.

### If something goes wrong mid-run

The manifest is written *before* anything is deleted, so an expired session, a
dropped connection or a Ctrl-C never loses the work:

```bash
icloud-photo-sync icloud-delete --last       # retry; already-deleted items are skipped
icloud-photo-sync icloud-delete --last --dry-run
```

Without `--icloud-delete`, nothing changes: no Apple ID is resolved, no Keychain
is read, and neither clean command touches the network.

### Already trashed things without the flag? (`--scan-trashed`)

If you cleaned up *before* using `--icloud-delete`, no manifest exists and there
is nothing for `--last` to resume. `--scan-trashed` reconstructs that session
instead: it reconciles the folder against the sync manifest and offers the
tracked files that are no longer on disk.

```bash
icloud-photo-sync icloud-delete --scan-trashed --dry-run   # always start here
icloud-photo-sync icloud-delete --scan-trashed
```

**This evidence is weaker, and the tool says so.** The normal check compares the
manifest's size against the file's size measured the instant before it was
trashed. A file that is already gone cannot be measured, and comparing the
manifest with itself proves nothing — so that check is not weakened here, it is
*replaced*, and the plan is labelled `retrospective` everywhere it appears.

What stands in for it:

* **A verified-complete moment.** `sync` only skips a file without rewriting its
  row when it confirms the file present at its expected size, so a finished full
  pass over a manifest with no pending and no failed rows is a point at which
  every tracked file was provably on disk. That timestamp bounds the window.
* **The scan envelope.** A file no clean command would even list (a `.HEIC`, or a
  JPEG above `--max-size`) cannot be explained by a clean session.
* **The log.** Every browser trash round is logged, so the run can tell whether
  a trash round actually happened in that window — and whether the logs reach
  back far enough to be sure nothing happened unseen.
* **The classification cache.** `local-clean` deletes a file's cache row when it
  trashes it, so a missing file whose row survived was not trashed by it.
* **Your eyes.** iCloud keeps a thumbnail of every asset, so the candidates are
  shown in the usual review page even though the local files are gone. Only what
  you tick is deleted. `--no-review` skips this.

The whole run refuses — deleting nothing, writing no manifest — if the folder is
not readable, if the manifest never completed a full pass, if the logs do not
cover the window, if no trash round is logged in it, or **if any missing file
falls outside every scan envelope**. That last one is strict on purpose: one
hand-deleted file means something other than a clean session removed things, and
the premise, not the file, is what is wrong. Read what it names and fix that.

The whole run is confirmed **once**, in two deliberate steps: type
`delete <n> retrospective`, then `YES I AM SURE`. The count proves the number on
screen was read and cannot be recalled from an ordinary run; the second phrase
cannot be reached by pressing return. Files a concurrent `sync` has restored drop
out before the count is quoted, so what you confirm is what goes.

A retrospective run is bounded by a **2000-asset ceiling** rather than the
measured path's 500-per-run cap — the CloudKit work is batched at 25 per request
either way, so splitting the consent would have added a session resume per slice
while teaching you to type the phrase without reading it. `--max-delete` lowers
that ceiling if you want a smaller bite; above it the run refuses.

Two flags worth knowing: `--max-size` / `--min-size` declare the thresholds your
past sessions used (they were never recorded, so today's defaults are assumed
and printed), and `--corroborate-root DIR` points at another copy of the library
so anything still present there at the same size is left alone.

---

## How it works

* **Folders by capture date.** Each asset goes to `YYYY/MM` derived from its
  capture timestamp. iCloud reports that timestamp in **UTC** and does not
  reliably expose the original local offset, so months are computed in UTC —
  the only rule that maps an asset to the *same* folder on every run. (A few
  photos taken near a month boundary may land in the adjacent month.) Months are
  zero-padded (`01`–`12`). Assets with no date go to `./unknown-date/`.
* **Stop / resume.** A SQLite manifest records every asset's status, destination
  and byte progress. Re-running `sync` skips anything already complete and
  resumes partial files.
* **Interrupted transfers.** Files stream to a `*.part` sibling. On retry the
  tool sends an HTTP `Range` request to **resume** from where it left off; if the
  server won't honour the range (or answers with a mismatched offset) it
  **restarts that file**. The final size is verified against iCloud's reported
  size — or, when that is unavailable, against the size the content server
  advertised — then the file is atomically renamed into place. A half-written
  file is never mistaken for complete.
* **Incremental / watch.** Incremental passes enumerate by the date an item was
  **added to iCloud** (newest first), so imports and AirDrops of old photos are
  found too, and stop once they see a run of items already downloaded. If the
  added-date listing is ever unavailable, the pass falls back to scanning the
  whole library rather than risk missing anything.
* **One-way.** No deletion, no two-way reconciliation. If you remove a photo from
  iCloud, your local copy stays. The tool also **never overwrites** a file it
  didn't write: if an unknown file occupies a destination, the download is
  redirected to a `-1`-suffixed name (or refused, with the reason recorded).
* **Existing exports are adopted.** If a file already sits at the expected
  `YYYY/MM/name` with exactly the size iCloud reports (e.g. you re-point the tool
  at an old export, or the manifest was lost), it is marked complete instead of
  being downloaded again.
* **Missing capture-date metadata is backfilled.** Files shared via WhatsApp (and
  some other apps) strip embedded date metadata before you re-import them, so a
  video or photo that reaches iCloud that way carries no capture date of its own
  — only iCloud's own record does. If a re-import ever happens by hand later
  (e.g. after `video-optimise`), the file's filesystem modified-time is what
  decides the date, so `sync` sets it to the true capture date on every
  download, and — only when the file's own embedded date is genuinely absent —
  writes it in too. A date that's already present, even one that looks wrong,
  is never touched. The embedded-date half needs `exiftool` for photos
  (`brew install exiftool`; videos use the `ffmpeg`/`ffprobe` you already have)
  — without it, download still succeeds and the filesystem date is still fixed,
  you just don't get the embedded stamp. `video-optimise` carries the same date
  forward onto its converted output.

### Where things live

* **Photos:** under the folder you run `sync` in → `./YYYY/MM/…`.
* **Session cookies, state DB, logs:** under
  `~/Library/Application Support/icloud-photo-sync/` — never mixed into your
  photos. The state DB is keyed by `(Apple ID, output folder)`, so each output
  folder keeps its own manifest.
* **Deletion manifests and receipts:** `…/icloud-photo-sync/deletions/` — one
  JSON plan and one JSONL receipt per run that offered to delete from iCloud.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ACCESS_DENIED` / precondition error | Turn ADP **off** and web access **on** (see top). |
| “session expired … run login” | `icloud-photo-sync login` again. |
| “still indexing” | iCloud is preparing the library; it retries automatically. |
| 2FA code never arrives | Known Apple-side flakiness; retry later, or use a trusted device. |
| Must accept terms | Sign in at <https://www.icloud.com> to accept, then re-run. |

Logs (with `-v` for detail) are in
`~/Library/Application Support/icloud-photo-sync/logs/`.

---

## Tested versions

macOS (Apple Silicon), Python 3.13, June 2026:

| Package | Version |
|---|---|
| pyicloud | 2.6.5 |
| keyring | 25.7.0 |
| requests | 2.34.2 |
| typer | 0.26.8 |
| tqdm | 4.68.3 |

Apple periodically changes authentication and breaks these tools. All iCloud
calls are isolated behind a single adapter (`icloud_photo_sync/icloud_client.py`)
so the engine can be swapped without touching the rest of the program. If logins
suddenly fail, the first thing to try is upgrading `pyicloud`.

---

## Fallback: drive `icloudpd` as the engine

If direct `pyicloud` integration becomes unstable, the mature
[`icloudpd`](https://github.com/icloud-photos-downloader/icloud_photos_downloader)
tool implements the same behaviours and can be used directly. It produces the
**exact same folder tree** and one-way semantics:

```bash
pipx install icloud-photos-downloader

# One-way (copy mode = default; do NOT pass --auto-delete), ./YYYY/MM tree:
icloudpd \
  --username you@example.com \
  --directory . \
  --folder-structure '{:%Y/%m}' \
  --until-found 50            # fast incremental; resume of interrupted files is built in

# Keep updated:
icloudpd ... --watch-with-interval 1800
```

The newer `pyicloud`-shipped CLI offers an equivalent path:
`icloud auth login` then `icloud photos sync` / `icloud photos watch`.

Because every iCloud call here lives behind the `ICloudClient` adapter, switching
this tool to shell out to `icloudpd` would only touch that one module.

---

## Run the tests

```bash
source .venv/bin/activate
pytest
```

The suite covers path resolution, the state store, and the downloader's
resume-vs-restart logic against a local mock HTTP server (no iCloud account
needed). The live login + download path requires a real account and is exercised
manually per the steps above.

## Acknowledgments

This tool stands on the shoulders of several open-source projects:

| Project | Role | License |
|---|---|---|
| [pyicloud](https://github.com/picklepete/pyicloud) | iCloud web/CloudKit client (auth, 2FA, photo enumeration) | MIT |
| [keyring](https://github.com/jaraco/keyring) | Storing the iCloud password in the macOS Keychain | MIT |
| [requests](https://github.com/psf/requests) | HTTP transfers, including `Range`-based resume | Apache-2.0 |
| [typer](https://github.com/fastapi/typer) | Command-line interface | MIT |
| [tqdm](https://github.com/tqdm/tqdm) | Progress bars | MPL-2.0 / MIT |
| [pytest](https://github.com/pytest-dev/pytest) | Test suite (dev only) | MIT |

The `local-clean` feature classifies images through any OpenAI-compatible local
vision model, and is tested against [LM Studio](https://lmstudio.ai). The
[`icloudpd`](https://github.com/icloud-photos-downloader/icloud_photos_downloader)
project (MIT) is documented above as a drop-in fallback engine.

Each dependency is used under its own license; this project bundles none of
their source.

## License

Released under the [MIT License](LICENSE). © 2026 Bora Okumusoglu.
