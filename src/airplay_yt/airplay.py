"""Stream a local media file to an Apple TV over AirPlay.

Uses the async `pyatv` library. The local file is handed to the Apple TV via
`AppleTV.stream.play_url()`, which serves the file over a short-lived HTTP
server on the device's behalf -- so no external media server is needed here.

Modern receivers (Apple TV 4K on tvOS 26.x / 27.x) broke the stock pyatv stream
path: they no longer answer ``GET /info``, so pyatv cannot obtain the Apple psi
needed to register the "remote control session" that carries the play queue, and
playback silently falls back to a legacy path that delivers no video on tvOS 27.
Module :mod:`airplay_yt.tvos_patch` fixes that; :func:`stream` applies it
automatically before connecting.

Credentials are read from the pyatv config file written by the CLI
(``atvpair`` / ``atvremote``) at ``~/.pyatv.conf`` and applied by hand. This is
deliberate: pyatv's ``FileStorage`` auto-application of those credentials is
unreliable for modern devices (it returns no credentials for the matched
device), which alone is enough to make the receiver reject the video session
with a 403. Loading them explicitly keeps pairing a one-time cost.

Public, blocking entry point: `stream(media_path, target=...)`.
Async internals live under `_` prefixes.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pyatv
import pyatv.exceptions
from pyatv import const
from pyatv.const import Protocol

from . import tvos_patch

PROTOCOL = const.Protocol.AirPlay
# Reuse the credentials the pyatv CLI (`atvpair`/`atvremote`) already wrote.
STORAGE_FILE = str(Path.home() / ".pyatv.conf")

# Every pyatv protocol that carries an Apple-HAP credential we may want to apply.
_CRED_PROTOCOLS = (
    Protocol.AirPlay,
    Protocol.Companion,
    Protocol.RAOP,
    Protocol.MRP,
)


class AirPlayError(Exception):
    """A file could not be delivered to an Apple TV."""


async def _discover(target, loop, storage, timeout=10.0):
    """Return the device config to use.

    `target` is `None` to auto-pick (only when exactly one device is found), or
    a host IP / device name / identifier to match a specific device.
    """
    atvs = await pyatv.scan(loop=loop, timeout=timeout, storage=storage)
    if not atvs:
        raise AirPlayError("no Apple TV or AirPlay device found on the network")

    if target is None:
        if len(atvs) != 1:
            names = ", ".join(f"{a.name} ({a.address})" for a in atvs)
            raise AirPlayError(
                f"multiple devices found, pass a target to select one: {names}"
            )
        return atvs[0]

    for atv in atvs:
        if target == atv.address or target == atv.name or target in atv.all_identifiers:
            return atv
    raise AirPlayError(f"no discovered device matches target {target!r}")


def _match_conf_entry(config, conf_data):
    """Find the stored `.pyatv.conf` entry that matches a discovered device.

    Matches on any known identifier (airplay / companion / raop / mrp / dmap).
    """
    identifiers = {ident for ident in config.all_identifiers if ident}
    for entry in conf_data.get("devices", []):
        stored = {
            p.get("identifier")
            for p in entry.get("protocols", {}).values()
            if p.get("identifier")
        }
        if stored & identifiers:
            return entry
    return None


def _apply_stored_credentials(config, conf_entry):
    """Apply credentials from a `.pyatv.conf` entry onto `config`.

    ``FileStorage``'s auto-application is unreliable for modern devices, so we
    set each protocol's credential by hand when the entry has one.
    """
    protocols = conf_entry.get("protocols", {})
    key_by_protocol = {
        Protocol.AirPlay: "airplay",
        Protocol.Companion: "companion",
        Protocol.RAOP: "raop",
        Protocol.MRP: "mrp",
        Protocol.DMAP: "dmap",
    }
    for protocol in _CRED_PROTOCOLS:
        creds = protocols.get(key_by_protocol[protocol], {}).get("credentials")
        if creds:
            config.set_credentials(protocol, creds)


async def _pair(config, loop, storage, pin=None):
    """Interactively pair `config`. Prompts for the PIN shown on the TV.

    Credentials are written to `storage`, so pairing only happens on the first
    run for a given device.
    """
    handler = await pyatv.pair(
        config, protocol=PROTOCOL, loop=loop, storage=storage
    )
    await handler.begin()
    if handler.device_provides_pin:
        entered = input(f"enter PIN shown on {config.name} ({config.address}): ")
    else:
        entered = pin if pin is not None else input(f"PIN for {config.name}: ")
    await handler.pin(entered)
    await handler.finish()
    handler.close()


async def _connect(config, loop, storage, pin=None):
    """Connect a device, pairing on first use and applying stored credentials.

    Returns a connected `AppleTV`.
    """
    try:
        return await pyatv.connect(config, loop=loop, storage=storage)
    except pyatv.exceptions.NoCredentialsError:
        # No stored credentials for this device yet: pair (which writes the
        # credentials to `storage`), then connect again.
        await _pair(config, loop, storage, pin)
        return await pyatv.connect(config, loop=loop, storage=storage)


async def _stream(media_path, target=None, pin=None, storage_file=STORAGE_FILE):
    """Core async pipeline: discover, connect, play the local file."""
    # Patch pyatv for modern receivers before anything touches the protocol.
    tvos_patch.apply()

    path = Path(media_path).expanduser().resolve()
    if not path.is_file():
        raise AirPlayError(f"media file not found: {path}")

    loop = asyncio.get_running_loop()
    conf_data: dict = {}
    if Path(storage_file).is_file():
        conf_data = json.loads(Path(storage_file).read_text())

    # We deliberately do NOT pass a FileStorage to scan/connect on the normal
    # path: pyatv auto-loads ~/.pyatv.conf, and the manual credential application
    # below (from the raw JSON) is what reliably hands credentials to the
    # protocols on modern receivers. The storage file path is reserved for the
    # fresh-pair fallback, which passes the path rather than a FileStorage object.
    config = await _discover(target, loop, storage=None)

    # Apply stored credentials by hand -- FileStorage's auto-application does
    # not reliably hand them to the protocols on modern devices.
    entry = _match_conf_entry(config, conf_data)
    if entry:
        _apply_stored_credentials(config, entry)

    atv = await _connect(config, loop, storage=None, pin=pin)
    try:
        # play_url blocks for the duration of the media and serves the file for
        # us; pass the local path and let pyatv stand up the HTTP server.
        await atv.stream.play_url(str(path))
    finally:
        # close() returns the pending cleanup tasks; drain them so nothing
        # lingers in the background.
        for task in atv.close():
            try:
                # bound each task so a stuck teardown task (e.g. a companion
                # channel that never closes cleanly) can't hang the caller
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            except asyncio.TimeoutError:
                task.cancel()
            except Exception:   # pragma: no cover - best-effort cleanup
                pass


def stream(media_path, target=None, pin=None):
    """AirPlay a local ``media_path`` to an Apple TV.

    The file is served as-is; no transcoding happens here, so it must already be
    in a format the receiver plays natively (VP9 or H.264 video with AAC audio
    in an MP4 container).

    Blocking. Wraps the async core in `asyncio.run` and blocks for the full
    duration of the media.

    `target` selects the device (None = auto-pick a single one, or pass a host
    IP / device name / identifier). `pin` pre-fills the pairing PIN when given.
    """
    try:
        asyncio.run(_stream(media_path, target=target, pin=pin))
    except KeyboardInterrupt:  # pragma: no cover
        raise


if __name__ == "__main__":  # smoke test: python -m airplay_yt.airplay <file>
    import sys

    if len(sys.argv) < 2:
        print(
            "usage: python -m airplay_yt.airplay <media-file> [target]",
            file=sys.stderr,
        )
        raise SystemExit(1)
    stream(sys.argv[1], target=sys.argv[2] if len(sys.argv) > 2 else None)
