# airplay-yt

Take a video URL (such as a YouTube link) or an existing local file, and stream it
to an Apple TV on your local network over AirPlay.

The central design choice is that the download step selects a rendition the Apple
TV decodes in hardware — **VP9 video with an H.264 fallback, paired with AAC
audio, muxed into an MP4 container**. Because of that there is **no separate
re-encode or transcode step**: the file the download produces is handed straight
to the Apple TV. On an Apple TV 4K, a 4K VP9 + AAC file selected this way plays at
full quality.

## Contents

- [Status](#status)
- [Requirements](#requirements)
- [Install](#install)
- [Usage](#usage)
- [How it works](#how-it-works)
- [Why there is no transcode step](#why-there-is-no-transcode-step)
- [Monkey patches for pyatv](#monkey-patches-for-pyatv)
- [Known limitations](#known-limitations)
- [Troubleshooting](#troubleshooting)
- [Project layout](#project-layout)
- [Other documentation](#other-documentation)

---

## Status

The project is a working pipeline. The original plan had three stages; the
transcode stage is not on the active path, and two runtime patches replace what
would otherwise be a forked `pyatv` dependency.

| Component | Status | Notes |
|---|---|---|
| `downloader.py` — fetch a URL at the highest Apple-TV-playable quality | Working | `yt-dlp` with a hard codec cap (`VP9 → H.264 → best combined`, always AAC, always MP4). Temp-dir handling, output resolution, and error wrapping are done. Cached reuse for `--keep` is done, including an older naming scheme. |
| `airplay.py` — stream a file to an Apple TV | Working | Async wrapper over `pyatv`. Discovers the device, applies stored credentials by hand, pairs on first use, and blocks for the full duration of playback. |
| `monkey_patches/` — runtime fixes for `pyatv` | Working, version-pinned | Two patches plus a version guard. Required on `pyatv 0.18.0`; see [Monkey patches for pyatv](#monkey-patches-for-pyatv). |
| `cli.py` — command line that wires the pipeline | Working | `airplay-yt` console script. One status line per stage, `--url` / `--file` / `--device` / `--pin` / `--keep` / `--save-path`, and cleanup. |
| `transcoder.py` — hardware-accelerated ffmpeg re-encode | Placeholder | Not on the active path. The file raises `NotImplementedError` and nothing imports it. |

### Verification status

Stated precisely, because the difference matters:

- **Local-file streaming to a real Apple TV: confirmed.** Repeated runs streaming
  an H.264/AAC MP4 from disk to an Apple TV 4K on tvOS 27 completed with exit
  code 0, with playback events visible in the logs (play-queue commands accepted,
  `playbackState` messages received) and end-of-media detected by the receiver's
  own event.
- **Download and merge: confirmed.** A real YouTube download produced
  `362,111,397` bytes of merged MP4 from a 342 MiB video stream plus a 3.29 MiB
  audio stream.
- **Full URL to playback in a single run: not confirmed.** The run that downloaded
  the file above was interrupted during the streaming stage, so the download half
  is proven and the AirPlay half of *that* run is not. See
  [STREAMING.md](STREAMING.md).
- **No automated test suite.** There are no tests in the repository; everything
  above is from manual runs against real hardware.

---

## Requirements

- **Python 3.13+** (`requires-python = ">=3.13"`).
- **`ffmpeg` on your `PATH`** — used to mux separate video and audio tracks into
  one MP4. Install it with `apt install ffmpeg`, `brew install ffmpeg`,
  `choco install ffmpeg`, or `winget install Gyan.FFmpeg`.
- **`yt-dlp`**, installed automatically as a dependency. It fetches the source.
  The downloader also asks yt-dlp for its JS ("EJS") challenge solver
  (`remote_components = ["ejs:github"]`), which yt-dlp fetches from GitHub at
  download time. Without it, some VP9 streams are dropped and a lower-quality or
  non-native rendition may be selected.
- **An Apple TV on the same local network**, reachable by name or IP. Modern
  receivers (tvOS 26 / 27) are supported through the runtime patches described
  below.
- **AirPlay pairing**, and the PIN the TV displays the first time you connect.

---

## Install

```bash
git clone <repo-url> airplay-yt
cd airplay-yt
uv sync
```

`uv sync` installs everything, including stock `pyatv` from PyPI. There is no git
dependency and no submodule to initialize.

`pyatv` is pinned to `==0.18.0` in `pyproject.toml`. That pin is deliberate: the
runtime patches reach into `pyatv` internals, so a different version could change
or duplicate their behavior. See
[Monkey patches for pyatv](#monkey-patches-for-pyatv) before changing it.

The `airplay-yt` console script is installed by `uv sync`. Use it through the
project environment, or activate that environment first:

```bash
uv run airplay-yt --help

# or
source .venv/bin/activate        # .venv\Scripts\activate on Windows
airplay-yt --help
```

---

## Usage

### 1. Pair the Apple TV (once per device)

AirPlay requires a one-time pairing so the Apple TV trusts this machine. The first
time you play to a given device, `airplay-yt` pairs it automatically and
interactively. Enter the PIN the TV displays, and the credentials are written to
`~/.pyatv.conf`. Later plays to that device connect silently.

```bash
airplay-yt --url "https://www.youtube.com/watch?v=dQw4w9WgXcQ" \
            --device "Living Room Apple TV"
```

The first run looks like this:

```text
[1/2] Downloading https://www.youtube.com/watch?v=dQw4w9WgXcQ
[download] 100% of  342.00MiB in 00:00:06 at 51.65MiB/s
[2/2] Streaming dQw4w9WgXcQ.mp4 to Living Room Apple TV ...
enter PIN shown on Living Room Apple TV (192.168.1.80):
```

While that prompt is open, the TV shows a request to allow the connection and a
PIN. The following is a representation of that screen, not a capture, and the
wording varies by tvOS version:

```text
┌───────────────────────────────────────────────────┐
│                                                   │
│                      AirPlay                      │
│                                                   │
│           "pyatv" wants to play video on          │
│                   this Apple TV                   │
│                                                   │
│                   PIN:   1 2 3 4                  │
│                                                   │
│       To allow, enter this PIN on the device      │
│             you are using to connect.             │
│                                                   │
└───────────────────────────────────────────────────┘
```

Type that PIN at the `enter PIN shown on ...` prompt and press Return. The TV
then plays the file. When playback ends, `airplay-yt` prints `done` and exits `0`.

#### Fallback: pair with `atvremote wizard`

If automatic pairing does not work — no PIN prompt appears, pairing fails, or you
see an authentication error — pair manually with the `atvremote` tool that ships
with `pyatv`. It writes to the same `~/.pyatv.conf` that `airplay-yt` reads, so
pairing this way is equivalent and only has to be done once per device.

```bash
uv run atvremote wizard
```

The wizard scans, lists what it found, and asks you to pick a device by number:

```text
Looking for devices...
Found the following devices:
    Name                  Model                Address
--  --------------------  -------------------  -------------
 1  Living Room Apple TV  Apple TV 4K (gen 3)  192.168.1.129
 2  Bedroom HomePod       HomePod Mini         192.168.1.218
 3  Living Room HomePod   HomePod Mini         192.168.1.223
 4  Office HomePod        HomePod Mini         192.168.1.114
Enter index of device to set up (q to quit):
```

Enter the number next to your Apple TV. The wizard then works through each
protocol the device offers, skipping the ones that need no pairing, and asks for
the on-screen PIN (`Enter PIN on screen:`) where the device provides one. It
finishes by connecting and printing what is currently playing. Afterwards,
re-run `airplay-yt`; it does not need the PIN again.

### 2. Play a video from a URL

```bash
# One Apple TV on the network: selected automatically.
airplay-yt --url "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

# Several devices: choose one by name, IP address, or device id.
airplay-yt --url "https://www.youtube.com/watch?v=dQw4w9WgXcQ" \
            --device "Living Room Apple TV"
```

### 3. Play a local file

`--file` streams a file you already have and skips the download step entirely.
`--url` is not needed.

```bash
airplay-yt --file ~/Videos/airplay-yt/dQw4w9WgXcQ.mp4 \
            --device "Living Room Apple TV"

# Auto-select the only Apple TV on the network.
airplay-yt --file ./some-video.mp4
```

The file is used exactly as given: nothing is copied, downloaded, or deleted. A
missing path is reported before any AirPlay connection is attempted. The file must
already be in a format the receiver plays natively (VP9 or H.264 video with AAC
audio in MP4) — `--file` performs no transcoding.

### 4. Keep downloads and reuse them

Without `--keep`, the download goes into a temporary directory that is removed
after playback.

```bash
airplay-yt --url "https://www.youtube.com/watch?v=dQw4w9WgXcQ" \
            --device "Living Room Apple TV" \
            --keep \
            --save-path "$HOME/Videos/airplay-yt"
```

With `--keep`, the file is written to a persistent directory instead (default
`~/Videos/airplay-yt`, overridden by `--save-path`). If a copy of the same URL is
already there, the download is skipped and the existing file is streamed:

```text
[1/2] Using existing dQw4w9WgXcQ.mp4 (skipping download)
```

### Options

| Flag | Alias | Default | Description |
|---|---|---|---|
| `--url` | — | required unless `--file` | Source URL: any link `yt-dlp` understands (YouTube and many other hosts). |
| `--file` | `-f` | — | Path to an existing local media file. Skips the download; `--url` is then unnecessary. The file is never modified or deleted. |
| `--device` | `-D` | auto-select | Target Apple TV by name, IP address, or device id. Omitted, it auto-selects only when exactly one device is discovered; otherwise it fails and lists the devices found. |
| `--pin` | `-p` | prompt | AirPlay pairing PIN. Used only when the receiver does not display its own PIN; see [Known limitations](#known-limitations). |
| `--keep` | — | off | Keep the download instead of deleting it, and reuse a cached copy of the same URL on later runs. Ignored with `--file`. |
| `--save-path` | — | `~/Videos/airplay-yt` | Directory `--keep` writes into. Ignored unless `--keep` is set. |
| `-h`, `--help` | — | — | Show help. |

Exit codes: `0` on success, `1` on failure, `130` on interrupt (Ctrl-C), `2` on a
usage error.

Status messages and download progress go to stderr, so stdout stays clean.

### Lower-level: AirPlay a local file directly

The AirPlay stage is usable on its own, using the same `~/.pyatv.conf` credentials
and the same automatic pairing:

```bash
uv run python -m airplay_yt.airplay /path/to/somefile.mp4 "Living Room Apple TV"
uv run python -m airplay_yt.airplay /path/to/somefile.mp4 192.168.1.80
```

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

1. **Download** (`downloader.py`) — fetch the URL with `yt-dlp`, choose the
   highest-quality rendition the Apple TV decodes natively, and mux separate video
   and audio into one MP4. With `--file` this stage is skipped.
2. **AirPlay** (`airplay.py`) — hand the file to the target Apple TV via `pyatv`.
   pyatv serves the file from a short-lived local HTTP server, so no external
   media server is needed. The call blocks for the full duration of playback.

The two stages run in sequence: the download must finish before playback starts.
See [STREAMING.md](STREAMING.md) for why, and what early playback would require.

### Rendition selection

The selector is a hard codec cap, not just a preference order
(`downloader.py:47`):

```
bv[vcodec^=vp9]+ba[acodec^=mp4a]      preferred
/ bv[vcodec^=avc1]+ba[acodec^=mp4a]   fallback
/ bv+ba/b                             last resort
```

Within that capped set, the best candidate is picked by
`vcodec:vp9,res,br,acodec:aac,ext:mp4:m4a`. A sort alone is not enough: when
yt-dlp's challenge solver is unavailable some VP9 streams are dropped, and a
higher-ranked AV1/Opus rendition can win. Apple TV decodes H.264 and VP9 with AAC
but **not** AV1 or Opus, so the cap is what guarantees a playable file.

---

## Why there is no transcode step

Apple TV 4K decodes 4K VP9 + AAC natively, so a correctly chosen download is
already the right format. On a 1080p Apple TV the same selector falls back to the
best H.264 + AAC rendition, which that device also plays without re-encoding.

`transcoder.py` remains in the tree as an unimplemented stub so a future version
could add a hardware re-encode path for unusual sources. Nothing imports it.

---

## Monkey patches for pyatv

Released `pyatv` cannot deliver video to Apple TVs on tvOS 26/27. Two fixes exist
in unreleased upstream work. Rather than depend on a fork, this project applies
them to the installed `pyatv` at runtime, from `src/airplay_yt/monkey_patches/`:

| Module | Purpose |
|---|---|
| `play_queue_patch.py` | The play-queue protocol rewrite (upstream PR #2774 / #2899). Modern receivers drive video through `POST /command` with PTP timing; the legacy `POST /play` handshake used by `pyatv 0.18.0` makes the session activate without media ever arriving. |
| `tvos_patch.py` | Injects the Apple `psi` that tvOS 26.6/27 no longer reports on `GET /info`. Without it the remote control session that carries the play queue cannot be registered. |
| `_pyatv_guard.py` | Version check and upstream-fix detection shared by the two patches. |

`airplay.py` applies both before connecting, in that order, and logs a warning if
either fails to apply. Both are idempotent. The guard warns when the installed
`pyatv` is not the expected version, and each patch skips itself when the
installed `pyatv` already provides the fix — so a future release can make these
modules obsolete, and the logs will say so.

### If you change the pyatv version

`pyproject.toml` pins `pyatv==0.18.0` because these patches are written against
that release's internals. Before changing the pin:

1. Check whether the play-queue fix and the psi fix have shipped upstream. If so,
   delete the corresponding patch module and its `apply()` call in `airplay.py`.
2. If a patch is still needed, re-verify it against the new `pyatv`. Each module
   docstring lists the modules, methods, signatures, and attributes it touches.
3. Re-test on a real Apple TV on tvOS 26/27. A patch that stops applying is quiet
   from the receiver's side: it accepts the legacy path and plays no video.

The full detail lives in the `MAINTENANCE` sections of `pyproject.toml` and each
patch module's docstring.

---

## Known limitations

- **`--pin` cannot skip the prompt for TVs that display their own PIN.** When the
  receiver provides the PIN, the program always asks for it interactively, and
  `--pin` is ignored. `--pin` applies only when the receiver does not provide one.
- **Download and playback are sequential.** Playback cannot begin until the file
  has been downloaded and merged. Analyzed in [STREAMING.md](STREAMING.md).
- **No transcoding.** Whatever the download selects is what plays. A source with
  no VP9 and no H.264 + AAC rendition may not play; the last-resort selector
  (`bv+ba/b`) can pick a non-native codec rather than fail.
- **Playback state control is minimal.** There is no pause, resume, seek, or stop
  command; the program blocks until the media ends or you interrupt it.
- **`--file` performs no validation.** A file in an unsupported codec is sent
  as-is and may show a black screen while audio plays.
- **Cached reuse is name-based.** With `--keep`, a cached file is matched by a
  stem derived from the URL, so two different URLs that canonicalize to the same
  name would share an entry.
- **Interrupted `--keep` downloads can leave partial files.** A download that
  fails or is cancelled mid-way leaves `yt-dlp`'s `.part` files in the save
  directory. Only the temporary-directory path is cleaned up automatically.

---

## Troubleshooting

Start with the two patch warnings. `airplay.py` logs one per patch that failed to
apply, and either one means video will not reach the TV:

```
play-queue patch is not active; video playback on tvOS 26/27 will not work. Verify the patch against the installed pyatv version.
psi patch is not active; the remote control session modern receivers need may fail. Verify the patch against the installed pyatv version.
```

If you see those, the installed `pyatv` is not what the patches expect. Check the
pin in `pyproject.toml` and read
[If you change the pyatv version](#if-you-change-the-pyatv-version).

To see the full logs, run the AirPlay stage with debug logging enabled:

```bash
uv run python -c "
import logging
logging.basicConfig(level=logging.DEBUG, format='%(name)s %(message)s')
from airplay_yt import airplay
airplay.stream('/path/to/file.mp4', target='Living Room Apple TV')
"
```

| Symptom | Likely cause | What to do |
|---|---|---|
| `airplay failed: no Apple TV or AirPlay device found on the network` | Discovery found nothing within its 10-second window, or the TV is on another network/VLAN. | Check the TV is awake and on the same subnet. Retry. |
| `airplay failed: multiple devices found, pass a target to select one: ...` | More than one AirPlay device was discovered and no `--device` was given. | Pass `--device` with one of the listed names or addresses. |
| `airplay failed: no discovered device matches target '...'` | The `--device` value matches no discovered device. | Use the exact name, IP address, or device id from the error listing. |
| `airplay failed: media file not found: ...` | The `--file` path does not exist, after expansion of `~`. | Check the path. Relative paths resolve against the current directory. |
| No PIN prompt appears, then pairing fails | The TV never showed the request, or the PIN was not entered in time. | Pair with `uv run atvremote wizard` instead; see [the fallback](#fallback-pair-with-atvremote-wizard). |
| `AuthenticationError: not authenticated`, or a `403` during setup | No usable credentials for this device in `~/.pyatv.conf`, so the receiver rejects the session. | Pair with `uv run atvremote wizard`, then retry. |
| Black screen while audio plays | The selected rendition uses a codec the receiver cannot decode (AV1 or Opus). | Confirm with the debug logs which codec was downloaded. This happens when no VP9 or H.264 + AAC rendition was available. |
| Playback starts, then the picture collapses after roughly twenty seconds | The play-queue patch is not active, so the session fell back to a setup the modern receiver accepts but does not sustain. | Check for the patch warning above and verify the `pyatv` version. |
| Video never appears on the TV, but the command returns normally | The legacy `POST /play` path was used. | Check for the play-queue patch warning; capture debug logs. |
| `download failed: ...` and the log mentions muxing or merging | `ffmpeg` is missing from `PATH`, so separate video and audio streams cannot be merged. | Install `ffmpeg` and retry. |
| A long Python traceback instead of a clean `airplay failed:` line | Errors raised by `pyatv` (pairing, authentication, connection loss) are not wrapped by this project, so they propagate as-is. | Read the last traceback lines for the pyatv exception type; the table above maps the common ones. |

Note on the last row: only discovery, file, and media-path failures are converted
into a clean `airplay failed: ...` message. Anything raised inside `pyatv` itself
surfaces as an unhandled exception with a traceback. The exit code is still `1`
in both cases.

### Resetting pairing

If credentials are stale or the TV was reset, remove the stored entry and pair
again. `~/.pyatv.conf` is shared with the `pyatv` CLI tools, so this also affects
them:

```bash
cp ~/.pyatv.conf ~/.pyatv.conf.bak     # back it up first
rm ~/.pyatv.conf                       # or remove just the stale device entry
uv run atvremote wizard
```

The wizard skips any protocol that already has credentials, so the file has to be
removed (or edited) before it will pair from scratch. Afterwards `airplay-yt` uses
the new credentials with no further setup.

---

## Project layout

```
src/airplay_yt/
├── __init__.py            # package metadata and `main()` delegating to the CLI
├── __main__.py            # `python -m airplay_yt` entry point
├── cli.py                 # console script: download (or --file), then airplay
├── downloader.py          # fetch a URL at the highest Apple-TV-playable quality
├── airplay.py             # stream a file to an Apple TV over AirPlay
├── monkey_patches/        # runtime pyatv fixes for tvOS 26/27
│   ├── _pyatv_guard.py    # version check and upstream-fix detection
│   ├── play_queue_patch.py  # play-queue protocol (POST /command)
│   └── tvos_patch.py      # psi injection for the remote control session
└── transcoder.py          # unimplemented placeholder, not on the active path
```

---

## Other documentation

- [AIRPLAY.md](AIRPLAY.md) — how video playback was made to work on modern Apple
  TV receivers: the diagnosis, the two fixes, and the evidence from the wire.
- [STREAMING.md](STREAMING.md) — whether playback can start before the download
  finishes, with the blockers, the options, and measured numbers.

---

## Notes

- **Credentials live in `~/.pyatv.conf`** — the same file the `pyatv` CLI tools
  (`atvpair`, `atvremote`) use. Pairing through `airplay-yt` and pairing through a
  `pyatv` tool share one store, so you only pair once per device. `airplay-yt`
  reads that file itself and applies the credentials by hand, because `pyatv`'s
  automatic application is unreliable for modern receivers.
- **The download progress line comes from `yt-dlp` directly** (percentage, speed,
  ETA). This project does not print its own progress bar. Because the downloader
  runs yt-dlp with `quiet` set, that output goes to stderr along with this
  project's own status lines, which keeps stdout clean.
- **`ffmpeg` is required**, because sources that expose video and audio as separate
  streams — the common case on YouTube — must be muxed into one file before
  AirPlay. If `ffmpeg` is missing the downloader logs a warning rather than
  failing early, and the merge step then fails.
- **Temp downloads carry the `airplay-yt-` prefix**, which is how the CLI decides
  what it may remove. A `--file` path is never removed, even if it sits in a
  directory with that prefix.
