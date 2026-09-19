# AIRPLAY.md — How video streaming was made to work on modern Apple TV

This document records the work that brought AirPlay video playback of a local,
transcoded file (`test.mp4`) to a **Living Room Apple TV 4K on tvOS 27.0** — a
receiver for which stock `pyatv` cannot deliver video.

TL;DR:

1. The actual streaming fix is **not our code**. It is an open, unreleased fix to
   `pyatv` (the `POST /command` *play-queue* protocol rewrite, upstream PR
   **#2774 / #2899**). It used to be linked as a git submodule at `vendor/pyatv/`;
   it is now **applied at runtime** by `src/airplay_yt/play_queue_patch.py` on top
   of stock `pyatv 0.18.0` from PyPI, so no fork or submodule is needed.
2. A **small additional runtime monkey-patch** in our project —
   `src/airplay_yt/tvos_patch.py` — plugs the one remaining gap on tvOS 26.6/27:
   injecting the Apple `psi` that the modern receiver no longer hands back.
3. `airplay.py` wires the two together, loads stored credentials by hand, and
   calls `atv.stream.play_url(...)`, letting `pyatv` stand up a short-lived HTTP
   server so no external media server is needed.

---

## The symptom

With stock `pyatv 0.18.0`, streaming `test.mp4` to the Apple TV **never showed
video**:

* Pair-verify succeeded; the RTSP session activated (`isAirplayActive: true`,
  receiver `com.apple.TVAirPlay`), **but media never flowed**.
* `GET /playback-info` returned **HTTP 500** on every poll, and the TV never
  pulled the file from the HTTP server that serves it.
* The device state on MRP went `Stopped → Idle` — never `Playing`.

The same command works on older receivers; it only fails on **tvOS 26.x / 27.x**.
The test device is `192.168.1.129`, `AppleCoreMedia/1.0.0.24J361` (`CPU OS 27_0`).

---

## Root cause (two independent problems)

### 1. The play-queue protocol (upstream gap)

Modern receivers drive video through `POST /command` with PTP-timed frames. Stock
pyatv's AirPlay-v2 player still uses the **old `POST /play` handshake**, which
these receivers reject part-way through — the session activates but no media is
delivered. The correct protocol was written in an upstream pull request that is
**open and unreleased**:

* `mikelambert/pyatv`, branch `airplay-play-queue-fix`, commit
  **`b248409`** — *"airplay: fix play_url on receivers using the play queue
  protocol"* (2026-07-29), which folds in PR **#2774** ("url playback broken on
  tvOS 26") / **#2899** ("Fix AirPlay to modern Apple TV tvOS").

This is the bulk of the work, and **we did not write it** — it is now carried as
runtime monkey-patch code in `src/airplay_yt/play_queue_patch.py` (see
*How the fix is carried now* below).

### 2. The `psi` on tvOS 26.6/27 (our gap)

The play-queue path starts a **remote control session (RCS)** via an RTSP `SETUP`
with stream type `130`. That session needs an Apple **`psi`** (the companion /
`OUTPUT_DEVICE_ID`, a UUID such as `1E5EC627-380B-4157-8D56-CB6656A4B407`).
`pyatv` normally learns the `psi` from `GET /info`.

On **tvOS 26.6 and 27**, `GET /info` answers **HTTP 403 Forbidden**. Result:
`pyatv` cannot obtain the `psi`, the RCS `SETUP` fails, and playback silently
falls back to the legacy `POST /play` path — which, on modern receivers, delivers
no video. The media never reaches the TV.

The crucial observation is that **the `psi` is already known locally**: it is the
device's **companion service identifier**, which equals the MRP
`OUTPUT_DEVICE_ID`. In `~/.pyatv.conf` for the target device:

```
airplay   1E:5E:C6:27:38:0B
companion 1E5EC627-380B-4157-8D56-CB6656A4B407   <-- this is the psi
raop      1E5EC627380B
```

So `tvos_patch.py` supplies the missing `psi` from the companion identifier
instead of waiting for `GET /info` to provide it.

---

## The fix

### `src/airplay_yt/tvos_patch.py` (our contribution)

A runtime monkey-patch, applied idempotently via `tvos_patch.apply()` at the start
of the connect path (`airplay.py`, `_connect`). Two changes:

1. **Inject the `psi`.** Wrap `AirPlayStream.create_airplay_protocol`: after the
   original builds the AirPlay-v2 protocol, if it is an AirPlay-v2 instance, set
   `proto.context.psi` from the device's **companion** service identifier
   (`_companion_psi`). This is what makes the RCS `SETUP` succeed on 26.6/27.
2. **Tolerate a `403` on `/info`.** Replace `AirPlayV2._setup_remote_control_session`
   so that if the injected `psi` is present it is used; otherwise it tries
   `GET /info` with `allow_error=True` (so a 403 does not abort the whole
   stream), parses the body, and registers the RCS with an RTSP `SETUP`
   (`type: 130`, `clientTypeUUID == RCS_CLIENT_TYPE_UUID`).

The patch is **non-invasive**: it only touches `AirPlayStream` / `AirPlayV2`
symbols, is guarded by an `import` guard (`HAVE_RCS`) so it is a no-op on older
pyatv trees, and returns `False` (skip RCS, not crash) whenever the `psi` is
unavailable.

### `src/airplay_yt/airplay.py`

* Calls `tvos_patch.apply()` before connecting.
* **Loads stored credentials by hand** from `~/.pyatv.conf` and applies them with
  `config.set_credentials(...)` for every protocol that carries an
  Apple-HAP credential (airplay / companion / raop / mrp). `pyatv`'s
  `FileStorage` auto-application of these credentials is **unreliable on modern
  devices** (it returns no credentials for the matched entry), which alone is
  enough to make the receiver reject the video session with a 403. Loading
  explicitly keeps pairing a one-time cost.
* Calls `atv.stream.play_url(...)`. `pyatv` then stands up its own HTTP server to
  serve the local file, so **no external media server is required**.

Evidence from a working run (access log of `pyatv`'s HTTP server):

```
192.168.1.129 ... "GET /test.mp4 HTTP/1.1" 206 286 ... "AppleCoreMedia/1.0.0.24J361 (Apple TV; U; CPU OS 27_0 like Mac OS X; en_us)"
```

…repeated as HTTP **206 partial-content** range requests for the duration of the
clip — definitive proof the TV is fetching and rendering the video.

---

## How the fix is carried now

### Where it came from

The play-queue protocol change was taken from the open-source fork of `pyatv`:

```
https://github.com/mikelambert/pyatv.git
commit  b24840990af7ae5505538d36fc42cef7b3cc348e   (branch airplay-play-queue-fix)
subject "airplay: fix play_url on receivers using the play queue protocol"
date    2026-07-29
```

That commit folds in the upstream play-queue fix (PRs **#2774** and **#2899**).

### How it is applied

`src/airplay_yt/play_queue_patch.py` applies that change at runtime, on top of
whatever stock `pyatv` is installed, so the project depends only on the published
PyPI package. It patches four modules:

| Module | Change |
|---|---|
| `pyatv.protocols.airplay.channels` | `EventChannelListener` interface, and event dispatch on `EventChannel` |
| `pyatv.protocols.raop.protocols` | `StreamProtocol.wait_for_media_end()` default |
| `pyatv.protocols.raop.protocols.airplayv2` | PTP video session, remote control session, play-queue `POST /command` commands, event-driven end of media |
| `pyatv.protocols.airplay.player` | Stop polling `GET /playback-info` when the protocol reports media end itself |

`tvos_patch.py` then layers `psi` injection on top, because the play-queue path
needs a remote control session that the tvOS 26.6/27 `GET /info` failure would
otherwise block.

### Is it needed in the repo?

**Yes — as of this writing, for a real reason, but it is a stopgap.**

* The fix is in an **unreleased fork / open PR**, not in any released `pyatv` on
  PyPI. `pyproject.toml` depends on `pyatv>=0.18.0`, and **0.18.0 is the last
  released version and does *not* contain the play-queue fix** — it is precisely
  the version that fails on tvOS 26/27. So stock `pyatv` needs the runtime patch
  to work.
* Unlike the earlier submodule, the patch does not vendor pyatv source or pin a
  git revision: `uv.lock` resolves the ordinary PyPI release, and the patch is a
  small, reviewable file in this repo that can be deleted once a released `pyatv`
  contains the fix.

Recommended path forward (once the PR merges upstream): delete
`play_queue_patch.py` and `tvos_patch.py`, and depend on the released `pyatv`
that contains both fixes.

### ⚠️ On every pyatv version change

Both patch modules reach into pyatv internals, so they are pinned to the shape of
the release they were written for (`0.18.0`). `pyproject.toml` pins
`pyatv==0.18.0` for that reason. Before changing that version:

1. Check whether the play-queue fix (PRs #2774 / #2899) and the psi fix are now
   in the released pyatv. If so, delete the corresponding patch module
   (`play_queue_patch.py` / `tvos_patch.py`) and its `apply()` call in
   `airplay.py` — do not keep patching a fix that already exists.
2. If a patch is still needed, re-verify it: each module docstring lists every
   pyatv module, method, signature, and attribute it touches. Update the patch
   for anything that moved or was renamed.
3. Re-test on a real tvOS 26/27 Apple TV. A patch that no longer applies is
   quiet from the receiver's side — it accepts the legacy `POST /play` path and
   plays no video.

`apply()` returns `False` and logs the reason when it cannot patch, and
`airplay.py` logs a warning per inactive patch, so check the logs after an
upgrade.

Part of this is automatic: `_pyatv_guard.py` records the expected pyatv version
(must match the `pyatv==` pin in `pyproject.toml`) and warns on a mismatch, and
each patch skips itself, with a log line, when the installed pyatv already
provides the fix it would apply.

---

## How to reproduce / test

From a freshly cloned repository, `uv sync` installs stock `pyatv 0.18.0` from
PyPI; the runtime patches apply themselves on the first stream. No submodule to
initialize.

```bash
cd /home/theron/Git/airplay-yt
uv sync

# one-liner smoke test (blocks for the full clip; Ctrl-C when confirmed)
.venv/bin/python -m airplay_yt.airplay test.mp4 "Living Room Apple TV"
```

To watch the TV actually pull the file (open a second terminal):

```bash
.venv/bin/python -c "
import sys, logging
sys.path.insert(0, 'src')
h = logging.FileHandler('/tmp/fetches.log', 'w')
h.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
for n in ('aiohttp.access', 'aiohttp'):
    g = logging.getLogger(n); g.addHandler(h); g.setLevel(logging.INFO)
from airplay_yt import airplay
try:
    airplay.stream('test.mp4', target='Living Room Apple TV')
except Exception as e:
    logging.getLogger('aiohttp').info('EXC %s %s', type(e).__name__, e)
logging.getLogger('aiohttp').info('DONE')
"
# then, in the other terminal:
grep AppleCoreMedia /tmp/fetches.log      # expect repeated 206 range requests
```

Pair with `atvpair` once per device if the TV shows a PIN prompt; credentials are
stored in `~/.pyatv.conf` and loaded by hand on subsequent runs.
