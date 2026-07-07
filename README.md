# icloud-photo-sync

A single-user **macOS command-line** tool that downloads your **entire iCloud
Photos library** into the current folder, organised by capture date:

```
./YYYY/MM/<filename>      e.g.  ./2026/07/IMG_1234.HEIC
```

It is **one-way only** — it downloads and adds, and **never deletes or modifies**
anything, locally or in iCloud. You can stop it any time and resume; interrupted
files (large videos) resume at the byte level when the server allows it, and it
can keep the folder up to date by pulling only newly-added items.

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

Exit codes: `2` = account precondition problem, `3` = session expired (run
`login`), `4` = must accept Apple terms, `1` = other error.

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
```

**First run trashes via Finder**, so macOS shows a one-time prompt asking to let
your terminal control Finder — approve it (System Settings → Privacy & Security
→ Automation). If denied, trashing fails with a reminder and no files move.

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

### Where things live

* **Photos:** under the folder you run `sync` in → `./YYYY/MM/…`.
* **Session cookies, state DB, logs:** under
  `~/Library/Application Support/icloud-photo-sync/` — never mixed into your
  photos. The state DB is keyed by `(Apple ID, output folder)`, so each output
  folder keeps its own manifest.

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
