# airplay-yt

Take a video URL (such as a YouTube link) or a local file, pull it down in the
highest quality **an Apple TV can play natively** (URL case only), and stream it
to an Apple TV on your local network over AirPlay.

The key choice in this project is that the download step already selects a
codec the Apple TV decodes in hardware — **VP9 video with a fallback to
H.264, paired with the best AAC audio, all muxed into an MP4 container**.
Because of that, there is **no separate re-encode / transcode step**: the file
the download produces is handed straight to the Apple TV. On an Apple TV 4K,
a 4K VP9 + AAC file this way plays at full quality.

---

## How it works

```
   input URL    ──►  download      ──►  airplay
  (YouTube, etc.) (highest VP9/   (serve the file to
                    H.264 + AAC,   an Apple TV over
                     MP4, muxed)   AirPlay)

   local file   ──────────────────────►  airplay
  (--file <path>)   (download skipped)
```

1. **Download** — fetch the source URL with `yt-dlp` and choose the
   highest-quality rendition the Apple TV decodes natively (VP9 → H.264 → best
   available combined stream), muxing separate video and audio into a single
   MP4. With `--file` this stage is skipped entirely and a local path is used
   as-is.
2. **AirPlay** — hand that file to the target Apple TV for playback via
   `pyatv`. The library serves the file itself, so no media server is needed.

> **Note on transcoding.** An early design planned a third stage
> (`transcoder.py`) to re-encode the download into an AirPlay-friendly file.
> It turns out a correctly-chosen download rendition is already playable, so
> `transcoder.py` is an **unused placeholder** — it is not on the active path.
> See [Status](#status) for the full picture.

---

## Requirements

- **Python 3.13+**.
- **`ffmpeg`** on your `PATH` — used to mux separate video and audio tracks into
  one MP4.
- The **`yt-dlp`** package (installed automatically as a dependency; it fetches
  the source).
- An **Apple TV** on the same local network, reachable by its name or IP.
  Modern Apple TVs (tvOS 26 / 27) are supported; they are handled by runtime
  patches over stock `pyatv` from PyPI, so no fork install is needed (see below).
- AirPlay pairing, and a **PIN** shown on the TV the first time you connect.

---

## Install (with `uv`)

```bash
# 1. Get the source.
git clone <repo-url> airplay-yt
cd airplay-yt

# 2. Create the virtualenv and install everything (project + yt-dlp + pyatv
#    from PyPI). `uv sync` needs no activation.
uv sync
```

`uv sync` installs **stock `pyatv` from PyPI**. Released `pyatv` cannot play video
to Apple TVs running tvOS 26/27, so the play-queue protocol fix is applied at
runtime by `src/airplay_yt/play_queue_patch.py` instead of by pinning a fork. `uv
sync` (and `uv run`) set everything up; there is no submodule to initialize and
no git dependency to fetch.

The `yt-dlp` binary and its optional JS challenge solver are installed as part
of the project's dependencies.

### Using the CLI

`uv sync` installs a console script called `airplay-yt`. Use it either directly
through the project's virtualenv or without activating it:

```bash
# Without activating the virtualenv:
uv run airplay-yt --help

# Or activate the virtualenv created by `uv sync`:
source .venv/bin/activate        # .venv\Scripts\activate on Windows
airplay-yt --help
```

---

## Usage

### 1. Pair the Apple TV (one time per device)

AirPlay requires a one-time **pairing** so the Apple TV trusts this machine.
The first time you play to a given device, `airplay-yt` pairs it **automatically
and interactively**. The PIN is the 4-digit number the Apple TV shows when it
asks you to allow the connection. The resulting credentials are written to
`~/.pyatv.conf`, so pairing happens only **once** — every later play to the same
TV connects silently.

**Interactive (prompts for the PIN on the first play):**

```bash
airplay-yt --url "https://www.youtube.com/watch?v=dQw4w9WgXcQ" \
            --device "Living Room Apple TV"
```

On the first run you will see the download happen, then a prompt:

```text
[1/2] Downloading https://www.youtube.com/watch?v=dQw4w9WgXcQ
[download] 100% of  234.5MiB in 00:00:15 at 15.3MiB/s
[2/2] Streaming dQw4w9WgXcQ.mp4 to Living Room Apple TV ...
enter PIN shown on Living Room Apple TV (192.168.1.80):
```

When the Apple TV prompts to allow the AirPlay connection (this happens right
after the download starts streaming), enter the PIN it shows at the prompt and
press **Return**. The credentials are saved; the video plays afterward.

**Non-interactive (provide the PIN up front):**

```bash
# 1. Trigger pairing (play anything once, or open AirPlay Settings on the TV);
#    read the 4-digit PIN off the TV when it appears.
# 2. Supply that PIN so the program never blocks on a prompt:
airplay-yt --url "https://www.youtube.com/watch?v=dQw4w9WgXcQ" \
            --device "Living Room Apple TV" \
            --pin 1234
```

> If a PIN is required but not supplied with `--pin`, the program prompts for it.

### 2. Play a video on the paired Apple TV

Once paired, the credential in `~/.pyatv.conf` lets later plays connect
without a PIN.

```bash
# Single Apple TV on the network -- auto-selected:
airplay-yt --url "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

# Multiple Apple TVs on the network -- choose one by name or IP:
airplay-yt --url "https://www.youtube.com/watch?v=dQw4w9WgXcQ" \
            --device "Living Room Apple TV"

# Keep the downloaded file on disk after streaming (otherwise it is deleted).
# With --keep the file is stored in ~/Videos/airplay-yt instead of a temp dir
# (override the location with --save-path); re-running the same URL reuses the
# cached copy instead of downloading it again:
airplay-yt --url "https://www.youtube.com/watch?v=x" \
            --device "Dining Room Apple TV" \
            --keep \
            --save-path "$HOME/Videos/airplay-yt"
```

For each play, the program:

1. **Downloads** the URL to a fresh temporary directory, showing YouTube's
   own progress line (percentage, speed, ETA) as it goes. With `--keep` the
   file is written to the persistent save directory (default `~/Videos/airplay-yt`,
   override with `--save-path <dir>`) instead, and if a cached copy of the same
   URL is already there the download is **skipped** and the existing file is
   streamed right away.
2. **Streams** that file to the Apple TV; this call blocks for the full
   duration of the media. Press **Ctrl-C** at any time to stop; the pending
   playback is cancelled and the temporary file is removed.
3. **Cleans up** by removing the temporary download directory — unless you
   passed `--keep`, in which case the file is left on disk in the save
   directory and its location is printed.

### Options

| Flag | Aliases | Default | Description |
|---|---|---|---|
| `--url` | — | *required unless `--file`* | The source URL: any link `yt-dlp` understands (YouTube and many other hosts). Not needed when `--file` is given. |
| `--file` | `-f` | — | Path to an existing local media file. Streams that file directly and **skips the download step**; `--url` is then unnecessary. The file is left on disk afterwards (`--keep` / `--save-path` are ignored). |
| `--device`, `-D` | — | auto-pick | Target Apple TV by **name**, **IP address**, or device id. Omit to auto-select when exactly one device is on the network. |
| `--pin`, `-p` | — | *prompt* | The AirPlay pairing PIN, supplied non-interactively. When omitted, the program prompts for it on first use. |
| `--keep` | — | *off* | Leave the downloaded file on disk after streaming rather than deleting it. The file is written to the persistent save directory (default `~/Videos/airplay-yt`; override with `--save-path`) instead of a temp dir, and a cached copy of the same URL is reused on later runs instead of re-downloaded. Ignored with `--file`. |
| `--save-path` | — | `~/Videos/airplay-yt` | Directory that `--keep` downloads into and keeps files in. Ignored unless `--keep` is set. |
| `-h`, `--help` | — | — | Show the help page. |

### 3. Play a local file (skip the download)

If you already have a file on disk — including one saved earlier with `--keep` —
stream it directly with `--file`. No download runs, so `--url` is not needed:

```bash
airplay-yt --file ~/Videos/airplay-yt/dQw4w9WgXcQ.mp4 \
            --device "Living Room Apple TV"

# Auto-pick the only Apple TV on the network, no URL:
airplay-yt --file ./some-video.mp4
```

The file is used exactly as given: nothing is copied, downloaded, or deleted.
A missing path is reported before any AirPlay connection is attempted. Pairing
works the same as above, so `--pin` applies here too. The file must already be
in a format the Apple TV plays natively (VP9/H.264 + AAC in MP4) — `--file`
performs no transcoding.

### Lower-level: AirPlay a local file directly

The AirPlay stage is usable on its own to stream a file you already have, using
the same `~/.pyatv.conf` credentials (and it pairs on first use, same as the
CLI above):

```bash
uv run python -m airplay_yt.airplay /path/to/somefile.mp4 "Living Room Apple TV"
uv run python -m airplay_yt.airplay /path/to/somefile.mp4 192.168.1.80
```

---

## Status

The project is a working skeleton where **two of the three original pipeline
stages are implemented and tested**, and the third (transcoding) has been
removed from the active path:

| Component | Status | Notes |
|---|---|---|
| `downloader.py` — fetch a URL at the highest Apple-TV-playable quality | ✅ Implemented | `yt-dlp` with a codec cap of `VP9 → H.264 → best combined`, always `AAC` audio, always muxed into `MP4`. No transcode needed. Temp-dir handling, resolution of the produced file, and error wrapping are done. Tested end-to-end with real YouTube and sample URLs. |
| `airplay.py` — stream a file to an Apple TV | ✅ Implemented | Async wrapper over `pyatv`. Applies `play_queue_patch` (the play-queue `POST /command` protocol modern receivers need) and `tvos_patch` (the `psi` those receivers no longer report on `GET /info`) before connecting, so stock PyPI `pyatv` works. Credentials are applied from `~/.pyatv.conf` by hand. Auto-pairs on first use. |
| `cli.py` — command line that wires the pipeline | ✅ Implemented | `airplay-yt` console script. Prints a status line per stage, shows the download progress, handles `--device` / `--pin` / `--keep` / `--save-path` / `--file`, and cleans up. `--file` bypasses the download and streams a local path directly; one of `--url` or `--file` is required. Tested via both `uv run airplay-yt` and `python -m airplay_yt`. |
| `transcoder.py` — hardware-accelerated ffmpeg re-encode | ⚠️ Placeholder | Not on the active path. The download stage already produces a natively-playable rendition, so a re-encode is not needed. The file is a stub that raises `NotImplementedError`. |

Stock PyPI `pyatv 0.18.0` cannot play video to Apple TVs running tvOS 26/27, so
this project carries the fixes as two runtime monkey-patches rather than a forked
dependency:

- `play_queue_patch.py` — the play-queue protocol rewrite from upstream PR
  #2774 / #2899 (video is driven through `POST /command`, not the legacy
  `POST /play` that these receivers reject).
- `tvos_patch.py` — the `psi` injection those receivers additionally need.

Both apply automatically on every stream. `pyproject.toml` depends only on the
published `pyatv` package, so `uv sync` needs no `[tool.uv.sources]` override, no
git fetch, and no submodule. Once a released `pyatv` contains the play-queue fix,
both patch modules can be deleted.

### Maintenance: what to do when the pyatv version changes

The two patch modules reach into pyatv internals, so they are **pinned to the shape
of the pyatv release they were written against**. `pyproject.toml` therefore pins
`pyatv==0.18.0` exactly, and the pin is deliberate — do not relax it to `>=`
without doing the work below.

On every pyatv upgrade:

1. **Decide whether the patches are still needed at all.** Check whether the
   play-queue fix (upstream PR #2774 / #2899) and the psi fix have shipped in the
   new release. If they have, **delete the corresponding patch module**
   (`play_queue_patch.py` / `tvos_patch.py`) and remove its `apply()` call from
   `airplay.py` and its mention in `pyproject.toml`. Leaving a patch in place for
   a fix that already exists is a bug.
2. **If a patch is still needed, re-verify it against the new pyatv.** Each module
   docstring lists the exact modules, method names, signatures, and class
   attributes it touches. Diff those against the installed pyatv and update the
   patch for anything that moved, was renamed, or changed behavior.
3. **Re-test on a real Apple TV on tvOS 26/27.** A patch that stops applying
   fails quietly from the receiver's point of view: it accepts the legacy
   `POST /play` path and plays no video at all.

Both `apply()` functions return `False` and log the reason when they cannot patch,
rather than raising, and `airplay.py` logs a warning for each inactive patch. After
an upgrade, read those warnings — silence there is not proof of success.

The guards are partly automatic. `_pyatv_guard.py` holds the expected pyatv
version (`EXPECTED_PYATV_VERSION`, which must match the pin in `pyproject.toml`);
a mismatch logs a warning, and each patch **skips itself when the installed pyatv
already provides the fix it would apply**, saying so in the log. Neither check
blocks a run, so they are a prompt to re-verify — step 3 above is still the real
test.

---

## Project layout

```
src/airplay_yt/
├── __init__.py       # package metadata + `main()` delegating to the CLI
├── __main__.py       # `python -m airplay_yt` entry point
├── cli.py            # console script: download (or --file) -> airplay
├── downloader.py     # fetch a URL in the highest Apple-TV-playable quality (yt-dlp)
├── airplay.py        # stream that file to an Apple TV over AirPlay (pyatv)
├── _pyatv_guard.py  # version check + upstream-fix detection for the patches
├── play_queue_patch.py  # runtime play-queue protocol fix for tvOS 26/27
├── tvos_patch.py     # runtime psi-injection fix for tvOS 26/27 receivers
└── transcoder.py     # unused placeholder: hardware-accelerated re-encode (see Status)
```

## Notes

- **Credentials live in `~/.pyatv.conf`** — the same file `pyatv`'s `atvscripts` CLI (`atvremote` and friends) uses. Pairing through `airplay-yt` and pairing through a `pyatv` tool share the store, so you do not have to pair more than once.
- **Why no transcode?** Apple TV 4K decodes 4K VP9 + AAC natively, so a correctly-chosen download is already the right format for that device. On a 1080p Apple TV the same selector falls back to the highest H.264 + AAC rendition available, which is again something that device plays without a re-encode. The `transcoder.py` stage remains in the tree so a future version can add a hardware re-encode path for unusual cases, but it is not needed today.
- **`ffmpeg` is required on your `PATH`**, because when a source exposes its video and audio as separate streams — the common case on YouTube — those must be muxed together into one file before AirPlay, and that mux uses `ffmpeg`. Install it with `apt install ffmpeg`, `brew install ffmpeg`, `choco install ffmpeg`, or `winget install Gyan.FFmpeg`.
- **`yt-dlp`'s EJS challenge solver** is used for the full high-quality VP9 stream ladder; without it, some modern YouTube URLs fall back to lower-quality or non-VP9 renditions. The solver ships with `yt-dlp`, and `uv sync` installs it.
- **`play_queue_patch` and `tvos_patch` are applied automatically.** `stream()` in `airplay.py` patches `pyatv` so it can deliver video to modern tvOS receivers; you do not need to do anything to enable them, and they are reapplied on every stream, so a fresh `pyatv` install works with no extra setup. Both patch modules are self-contained and can be deleted once a released `pyatv` includes the fixes.
- **Upgrading `pyatv` requires checking these patches.** They are pinned to `pyatv 0.18.0` internals (hence the exact pin in `pyproject.toml`). Before changing that version, confirm whether each patch is still needed and whether it still applies — see [Maintenance](#maintenance-what-to-do-when-the-pyatv-version-changes). `airplay.py` logs a warning when a patch is inactive.
