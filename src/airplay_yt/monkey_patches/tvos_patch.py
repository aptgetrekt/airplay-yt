"""Runtime patch that lets pyatv (>=0.18 / modern play-queue builds) stream video
to Apple TVs on tvOS 26.x-27.x.

Why this exists
---------------
pyatv's AirPlay v2 stream protocol starts a "remote control session" (RCS) via
RTSP SETUP with stream type 130. That session needs an Apple "psi" (the
companion / OUTPUT_DEVICE_ID, a UUID like ``1E5EC627-380B-4157-8D56-...``).
pyatv normally learns it from ``GET /info``.

On tvOS 26.6 and 27 the ``GET /info`` endpoint answers ``403 Forbidden``
(no longer populated in the session), so pyatv cannot obtain the psi, the RCS
setup fails, and playback silently falls back to the legacy ``POST /play`` path,
which does not actually deliver video on modern receivers. The media never
reaches the TV.

The psi is in fact already known locally: it is the device's *companion* service
identifier (which equals the MRP ``OUTPUT_DEVICE_ID``). This module therefore
(1) injects that psi into the AirPlay v2 StreamContext, and (2) makes the RCS
setup tolerate a 403 on ``/info`` instead of aborting.

This is a monkeypatch because the fix is not yet in any released pyatv. Apply it
via :func:`apply` (idempotent) before connecting.

MAINTENANCE -- READ BEFORE CHANGING THE pyatv VERSION
----------------------------------------------------
This patch is pinned to the shape of ``pyatv 0.18.0`` (see the pin in
``pyproject.toml``) and depends on :mod:`airplay_yt.monkey_patches.play_queue_patch` being
applied first, because that module owns ``RCS_CLIENT_TYPE_UUID`` and
``_setup_remote_control_session``, which this one layers on. On every pyatv
upgrade:

1. First check whether the psi fix has been released upstream. If so, delete
   this module and remove the call to :func:`apply` in
   :mod:`airplay_yt.airplay`.
2. If it is still needed, re-verify it against the new pyatv. This patch reads
   ``AirPlayStream.create_airplay_protocol``, the ``AirPlayV2`` class, its
   ``context`` / ``rtsp`` attributes, and ``decode_bplist_from_body``; confirm
   those still exist and behave the same. It also supplies the RCS session
   method that a tvOS 26.6/27 receiver needs, so the play-queue patch must still
   be applied (and still apply cleanly) for this one to work.
3. Re-test against a real Apple TV on tvOS 26/27. If the psi is not injected the
   remote control session fails and no video is delivered, even though the
   stream call returns normally.

:func:`apply` returns ``False`` (and logs why) when the pyatv pieces it needs are
absent, so check its return value or the logs after an upgrade.

Some of this is checked automatically at runtime by :mod:`airplay_yt.monkey_patches._pyatv_guard`:
the installed pyatv version is compared against the expected one (a mismatch logs
a warning), and :func:`already_handled_upstream` detects a pyatv that injects the
psi itself, in which case this module skips patching and says so. That detection
is a heuristic on the installed method's source, so treat a skip as informative
rather than proof -- step 3 (re-test on the device) is still the real check.
"""

from __future__ import annotations

import inspect
import logging
import uuid
from typing import Optional

from . import _pyatv_guard

try:  # pragma: no cover - import guard for older trees
    from pyatv.const import Protocol
    from pyatv.protocols.airplay import AirPlayStream
    from pyatv.protocols.raop.protocols.airplayv2 import AirPlayV2
    HAVE_RCS = True
except Exception:  # pragma: no cover
    HAVE_RCS = False

_LOGGER = logging.getLogger(__name__)
_APPLIED = False


def _companion_psi(config) -> Optional[str]:
    """Return the device's psi (companion / OUTPUT_DEVICE_ID) from config."""
    try:
        for svc in config.services:
            if svc.protocol == Protocol.Companion and svc.identifier:
                return svc.identifier
    except Exception:  # pragma: no cover
        pass
    return None


def already_handled_upstream() -> bool:
    """Return True when pyatv already injects the psi itself.

    If a release learns the psi from the companion service and feeds it to the
    remote control session (the same trick this patch performs), there is nothing
    left to patch and this module can go. Detected by checking that the installed
    implementation is pyatv's own and that its source consults a psi carried on
    the stream context. This is a heuristic: a false negative means the patch is
    applied over an equivalent implementation, which is harmless.
    """
    rcs = getattr(AirPlayV2, "_setup_remote_control_session", None)
    if rcs is None or _pyatv_guard.owned_by_this_package(rcs):
        return False
    try:
        source = inspect.getsource(rcs)
    except (OSError, TypeError):  # pragma: no cover - no source available
        return False
    return "context.psi" in source


def apply() -> bool:
    """Apply the patch idempotently. Returns True if the psi fix is in effect.

    :mod:`airplay_yt.monkey_patches.play_queue_patch` supplies the play-queue implementation this
    patch layers on (it owns ``RCS_CLIENT_TYPE_UUID`` and
    ``_setup_remote_control_session``), so it must be applied first.

    Returns True when the psi fix is in effect afterwards, whether from this
    patch or from pyatv itself; False when the pyatv pieces are absent and no
    patch was possible.
    """
    global _APPLIED
    if _APPLIED:
        return True
    if not HAVE_RCS:
        _LOGGER.warning(
            "psi patch cannot be applied: this pyatv does not expose the AirPlay "
            "stream types it patches (expected pyatv %s).",
            _pyatv_guard.EXPECTED_PYATV_VERSION)
        return False

    _pyatv_guard.warn_if_unexpected(_LOGGER, "tvos_patch")

    if already_handled_upstream():
        _LOGGER.info(
            "tvos_patch is not needed: pyatv %s already injects the psi. This "
            "module can be deleted.",
            _pyatv_guard.installed_version() or "(unknown version)")
        _APPLIED = True
        return True

    from pyatv.protocols.raop.protocols.airplayv2 import AirPlayV2 as _V2
    from pyatv.support.http import decode_bplist_from_body

    from . import play_queue_patch

    rcs_client_type_uuid = play_queue_patch.RCS_CLIENT_TYPE_UUID

    orig_create = AirPlayStream.create_airplay_protocol

    def _create(self, service, rtsp):
        proto = orig_create(self, service, rtsp)
        psi = _companion_psi(self.config)
        if isinstance(proto, _V2) and psi:
            proto.context.psi = psi
            _LOGGER.debug("injected AirPlay psi=%s", psi)
        return proto

    AirPlayStream.create_airplay_protocol = _create

    async def _setup_remote_control_session(self):
        psi = getattr(self.context, "psi", None)
        if not psi:
            try:
                resp = await self.rtsp.connection.get("/info", allow_error=True)
                info = (
                    decode_bplist_from_body(resp.body)
                    if resp and getattr(resp, "body", None)
                    else {}
                )
                psi = (info or {}).get("psi")
            except Exception:  # pragma: no cover
                pass
        if not psi:
            _LOGGER.debug("RCS: no psi available, skipping remote control session")
            return False
        try:
            setup_resp = await self.rtsp.setup(
                body={
                    "streams": [
                        {
                             "type": 130,
                             "controlType": 1,
                             "channelID": f"{psi}-RCS-1",
                             "clientUUID": str(uuid.uuid4()).upper(),
                             "clientTypeUUID": rcs_client_type_uuid,
                         }
                     ]
                }
            )
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.debug("RCS setup failed: %s", ex, exc_info=True)
            return False
        resp = decode_bplist_from_body(setup_resp)
        streams = resp.get("streams") or []
        if not streams:
            _LOGGER.debug("RCS: no streams in response: %s", resp)
            return False
        self._rcs_stream_id = streams[0].get("streamID", 1)
        _LOGGER.debug("RCS established (id=%s)", self._rcs_stream_id)
        return True

    AirPlayV2._setup_remote_control_session = _setup_remote_control_session
    _APPLIED = True
    return True
