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
You are asked once during `login`; clear it with `--reset-keyring`.

---

## CLI reference

```
icloud-photo-sync [GLOBAL] login
icloud-photo-sync [GLOBAL] sync [--update] [--watch SECONDS] [--until-found N]
icloud-photo-sync [GLOBAL] status

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

Exit codes: `2` = account precondition problem, `3` = session expired (run
`login`), `4` = must accept Apple terms, `1` = other error.

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
  server won't honour the range it **restarts that file**. The final size is
  verified against iCloud's, then the file is atomically renamed into place — a
  half-written file is never mistaken for complete.
* **Incremental / watch.** New items appear at the top of “All Photos”
  (newest-first). An incremental pass walks from the top and stops once it sees a
  run of items it already has.
* **One-way.** No deletion, no two-way reconciliation. If you remove a photo from
  iCloud, your local copy stays.

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

## License

MIT.
