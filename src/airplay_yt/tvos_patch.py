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
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

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


def apply() -> bool:
    """Apply the patch idempotently. Returns True if the patch is active."""
    global _APPLIED
    if not HAVE_RCS or _APPLIED:
        return HAVE_RCS and _APPLIED

    from pyatv.protocols.raop.protocols.airplayv2 import (
        AirPlayV2 as _V2,
        RCS_CLIENT_TYPE_UUID,
    )
    from pyatv.support.http import decode_bplist_from_body

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
                             "clientTypeUUID": RCS_CLIENT_TYPE_UUID,
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
