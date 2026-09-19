"""Runtime patch bringing the pyatv play-queue protocol to stock pyatv 0.18.0.

Why this exists
---------------
Stock PyPI ``pyatv 0.18.0`` cannot deliver *video* to Apple TVs on tvOS 26.x /
27.x. Its AirPlay-v2 player starts playback with the legacy ``POST /play``
handshake; modern receivers drive video through a play queue over
``POST /command`` with PTP-timed frames instead, so the RTSP session activates
but media never reaches the TV.

The fix lives in an unreleased upstream pull request (PR #2774 / #2899, the
"play-queue protocol rewrite"). This project used to pin that work as a git
dependency on a fork, which meant a git submodule, a non-PyPI install, and a
version of pyatv that could silently drift. This module instead applies the same
change at runtime, on top of whatever stock ``pyatv 0.18.0`` is installed, so the
project depends only on the published package.

What it patches
---------------
Four modules differ between stock ``pyatv 0.18.0`` and the fork:

``pyatv.protocols.airplay.channels``
    Adds the ``EventChannelListener`` interface and makes ``EventChannel``
    decode pushed events and hand them to a listener. Modern receivers report
    playback state on the event channel instead of answering
    ``GET /playback-info``.

``pyatv.protocols.raop.protocols``
    Adds ``StreamProtocol.wait_for_media_end()`` (default: ``False``).

``pyatv.protocols.raop.protocols.airplayv2``
    The bulk of the change: a PTP-timed video session, a remote control session
    (RCS) that unlocks ``POST /command``, the play-queue commands themselves, and
    event-driven end-of-media detection. Legacy receivers that reject the PTP
    setup still get the original ``POST /play`` path.

``pyatv.protocols.airplay.player``
    Stops polling ``GET /playback-info`` when the protocol reports media end
    itself.

Apply it via :func:`apply` (idempotent) before connecting. :mod:`airplay_yt.airplay`
does this automatically, and :mod:`airplay_yt.monkey_patches.tvos_patch` then layers on the one
gap the fork leaves open (the ``psi`` a tvOS 26.6/27 receiver no longer reports).

The patched code is a faithful copy of the upstream pull request; it is kept
verbatim (including comments) so it can be deleted outright once a released pyatv
contains it.

MAINTENANCE -- READ BEFORE CHANGING THE pyatv VERSION
----------------------------------------------------
This patch reaches into pyatv internals and is pinned to the shape of ``pyatv
0.18.0``. ``pyproject.toml`` therefore pins ``pyatv==0.18.0``. On every pyatv
upgrade:

1. First check whether the play-queue fix (upstream PR #2774 / #2899) is in the
   new release. If it is, **delete this module** and remove the call to
   :func:`apply` in :mod:`airplay_yt.airplay` -- keeping it would patch over a
   fix that is already present.
2. If it is still needed, re-verify this patch against the new pyatv. The
   patched modules, method names, signatures, and class attributes are listed
   under "What it patches" above; diff the installed pyatv against that list and
   update the patch for anything that moved or was renamed.
3. Re-test against a real Apple TV on tvOS 26/27. A patch that stops applying is
   silent at the API level: the receiver accepts the legacy ``POST /play`` path
   and simply plays no video.

:func:`apply` logs an error and returns ``False`` when patching fails, rather
than raising, so after an upgrade check the logs instead of assuming silence
means success.

Some of this is checked automatically at runtime by :mod:`airplay_yt.monkey_patches._pyatv_guard`:
the installed pyatv version is compared against the expected one (a mismatch logs
a warning), and :func:`already_fixed_upstream` detects a pyatv that already ships
the play-queue protocol, in which case this module skips patching and says so.
Neither check ever blocks a run, so they are a prompt to re-verify, not a
substitute for steps 1-3 above.
"""

from __future__ import annotations

import logging
import plistlib
from typing import Any, Dict, Mapping, Optional, cast
from uuid import uuid4

from . import _pyatv_guard

_LOGGER = logging.getLogger(__name__)

_APPLIED = False
# Set when the installed pyatv already implements the play-queue protocol, so no
# patching is needed. Kept separate from _APPLIED so the logs and return value
# distinguish "we patched it" from "upstream already had it".
_NOT_NEEDED = False

# Identifies the sender as wanting a remote control session (type 130,
# controlType 1). Distinct from the MRP data channel, which uses controlType 2
# and clientTypeUUID 1910A70F-DBC0-4242-AF95-115DB30604E1.
RCS_CLIENT_TYPE_UUID = "A6B27562-B43A-4F2D-B75F-82391E250194"

# Modern receivers drive video through POST /command rather than POST /play.
COMMAND_HEADERS = {
    "User-Agent": "AirPlay/960.13.1",
    "Content-Type": "application/x-apple-binary-plist",
}


def _patch_raop_protocols() -> None:
    """Add ``wait_for_media_end`` to the ``StreamProtocol`` base interface."""
    from pyatv.protocols.raop.protocols import StreamProtocol

    if hasattr(StreamProtocol, "wait_for_media_end"):
        return

    async def wait_for_media_end(self) -> bool:
        """Wait until the receiver reports that playback finished.

        Returns False if this protocol cannot report it, in which case the
        caller should fall back to polling GET /playback-info.
        """
        return False

    StreamProtocol.wait_for_media_end = wait_for_media_end


def _patch_event_channel() -> None:
    """Add the listener interface and event dispatch to ``EventChannel``.

    The installed pyatv is left in charge of parsing and responding: the fork's
    own ``handle_received`` depends on internal buffer handling that differs
    between pyatv releases, so copying it would tie this patch to one build.
    Instead a thin subclass overrides ``parse_request`` to note each request the
    base parses and dispatches those to the listener afterwards. The base class
    declares ``parse_request`` static, but calls it as ``self.parse_request(...)``,
    so an instance-level override is picked up.
    """
    from abc import ABC

    import pyatv.protocols.airplay.channels as channels_module
    from pyatv.protocols.airplay.channels import EventChannel
    from pyatv.protocols.airplay.utils import decode_plist_body
    from pyatv.support.http import HttpRequest

    if getattr(EventChannel, "_airplay_yt_patched", False):
        return

    class EventChannelListener(ABC):
        """Listener interface for EventChannel."""

        def handle_event(self, event: Mapping[str, Any]) -> None:
            """Handle an event pushed by the receiver."""

    class _DispatchingEventChannel(EventChannel):
        """EventChannel that forwards decoded events to ``listener``."""

        listener: Optional[EventChannelListener] = None
        _pending_requests: list = []

        def parse_request(self, data: bytes):
            request, body, rest = super().parse_request(data)
            if request is not None:
                # The base decides when one request is complete, so only
                # requests it actually parsed end up here.
                self._pending_requests.append(request)
            return request, body, rest

        def handle_received(self) -> None:
            self._pending_requests: list = []
            try:
                super().handle_received()
            finally:
                pending, self._pending_requests = self._pending_requests, []
            for request in pending:
                self._dispatch_event(request)

        def _dispatch_event(self, request: HttpRequest) -> None:
            """Decode an event and hand it to the listener, if any."""
            if self.listener is None:
                return

            body = request.body if isinstance(request.body, bytes) else b""
            if not body.startswith(b"bplist00"):
                return

            # The payload is a plist whose "data" member is itself a plist.
            outer = decode_plist_body(body)
            if not isinstance(outer, dict):
                return
            data = outer.get("params", {}).get("data")
            event = decode_plist_body(data) if isinstance(data, bytes) else outer
            if isinstance(event, dict):
                self.listener.handle_event(event)

    _DispatchingEventChannel.__name__ = "EventChannel"
    _DispatchingEventChannel._airplay_yt_patched = True

    # swap the module attribute so `setup_channel(EventChannel, ...)` builds this
    # one without the call site changing.
    channels_module.EventChannel = _DispatchingEventChannel
    channels_module.EventChannelListener = EventChannelListener


def _patch_player() -> None:
    """Make the player use event-driven end-of-media detection when available."""
    from pyatv import exceptions
    from pyatv.protocols.airplay.player import AirPlayPlayer

    if getattr(AirPlayPlayer, "_airplay_yt_patched", False):
        return

    _original_wait = AirPlayPlayer._wait_for_media_to_end

    async def _wait_for_media_to_end(self) -> None:
        # Receivers that report playback state themselves are not polled: the
        # endpoint below is not part of such a session and answers with an error.
        try:
            if await self.stream_protocol.wait_for_media_end():
                _LOGGER.debug("media playback ended")
                return
        except exceptions.ConnectionLostError:
            _LOGGER.debug("Connection was lost, assuming video playback stopped")
            return

        # Receivers without a remote control session still need the original
        # GET /playback-info polling loop.
        await _original_wait(self)

    AirPlayPlayer._wait_for_media_to_end = _wait_for_media_to_end
    AirPlayPlayer._airplay_yt_patched = True


def _patch_airplay_v2() -> None:
    """Apply the play-queue protocol to ``AirPlayV2``."""
    import asyncio

    from pyatv import exceptions
    from pyatv.auth.hap_channel import setup_channel
    from pyatv.protocols.airplay.auth import verify_connection
    from pyatv.protocols.airplay.channels import EventChannel, EventChannelListener
    from pyatv.protocols.raop.protocols.airplayv2 import (
        AirPlayV2,
        EVENTS_READ_INFO,
        EVENTS_SALT,
        EVENTS_WRITE_INFO,
    )
    from pyatv.support.http import HttpResponse, decode_bplist_from_body

    if getattr(AirPlayV2, "_airplay_yt_patched", False):
        return

    # Stock pyatv only has ``play_url``. Capture that bound implementation before
    # replacing it: the fork kept its body unchanged and just moved it into a
    # ``_play_url_legacy`` method, so it is reused directly instead of copied.
    _original_play_url = AirPlayV2.play_url

    # ---- constructor state the play-queue path needs -----------------------
    orig_init = AirPlayV2.__init__

    def __init__(self, context, rtsp) -> None:
        orig_init(self, context, rtsp)
        # Set once a remote control session has been registered, which is what
        # makes POST /command available.
        self._rcs_stream_id: Optional[int] = None
        self._item_uuid = str(uuid4()).upper()
        # The receiver reports playback state on the event channel rather than
        # answering GET /playback-info, so end of media is signalled here.
        self._media_ended: asyncio.Event = asyncio.Event()

    # ---- PTP-timed video session ------------------------------------------
    async def _setup_video_session(self) -> None:
        """Set up a session for video playback.

        Video sessions are timed with PTP and carry a peer list, unlike the NTP
        based setup used for audio streaming.

        PTP is required, and not only because a peer list is expected. The NTP
        setup is accepted -- SETUP succeeds, a remote control session can be
        registered, the receiver fetches the media and playback starts -- but the
        session does not survive it. No events arrive on the event channel, so
        there is no playback state, and the picture collapses after roughly
        twenty seconds. Measured on tvOS 26.5: PTP is still playing at 60s with
        26 events over a short session; NTP delivers zero events and is gone by
        27s.
        """
        local_ip = self.rtsp.connection.local_ip
        peer = {
            "ID": self.uuid.upper(),
            "Addresses": [local_ip],
            "DeviceType": 0,
            "SupportsClockPortMatchingOverride": True,
        }

        setup_resp = await self.rtsp.setup(
            body={
                "timingProtocol": "PTP",
                "timingPeerInfo": peer,
                "timingPeerList": [peer],
                "sessionUUID": str(uuid4()).upper(),
                "sessionCorrelationUUID": self.uuid.upper(),
                "updateSessionRequest": False,
                "statsCollectionEnabled": False,
                "isMultiSelectAirPlay": False,
                "deviceID": "AA:BB:CC:DD:EE:FF",
                "macAddress": "AA:BB:CC:DD:EE:FF",
                "model": "iPhone14,3",
                "name": "pyatv",
                "osName": "iPhone OS",
                "osVersion": "16.5",
                "osBuildVersion": "20F66",
                "sourceVersion": "690.7.1",
            }
        )
        resp = decode_bplist_from_body(setup_resp)
        _LOGGER.debug("Video setup response: %s", resp)

        event_port = resp.get("eventPort", 0)
        if event_port:
            transport, protocol = await setup_channel(
                EventChannel,
                self._verifier,
                self.rtsp.connection.remote_ip,
                event_port,
                EVENTS_SALT,
                EVENTS_READ_INFO,
                EVENTS_WRITE_INFO,
            )
            cast(EventChannel, protocol).listener = self
            self.event_channel = transport

    # ---- remote control session (unlocks POST /command) --------------------
    async def _setup_remote_control_session(self) -> bool:
        """Register a remote control session, which is what enables /command.

        Returns True if the receiver accepted it. The receiver answers without a
        dataPort, so nothing is ever connected -- asking for the channel is the
        entire purpose of this request.
        """
        try:
            info = decode_bplist_from_body(await self.rtsp.connection.get("/info"))
            psi = info.get("psi")
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("Failed to read /info", exc_info=True)
            return False

        if not psi:
            _LOGGER.debug("Receiver did not report psi, cannot set up RCS")
            return False

        try:
            setup_resp = await self.rtsp.setup(
                body={
                    "streams": [
                        {
                            "type": 130,
                            "controlType": 1,
                            "channelID": f"{psi}-RCS-1",
                            "clientUUID": str(uuid4()).upper(),
                            "clientTypeUUID": RCS_CLIENT_TYPE_UUID,
                        }
                    ]
                }
            )
        except exceptions.HttpError:
            _LOGGER.debug("Receiver rejected remote control session", exc_info=True)
            return False

        resp = decode_bplist_from_body(setup_resp)
        streams = resp.get("streams") or []
        if not streams:
            _LOGGER.debug("No streams in RCS setup response: %s", resp)
            return False

        self._rcs_stream_id = streams[0].get("streamID", 1)
        _LOGGER.debug("Remote control session established (id=%s)", self._rcs_stream_id)
        return True

    # ---- play queue commands ----------------------------------------------
    async def send_command(self, command: Dict[str, Any]) -> HttpResponse:
        """Send a command to the receiver via POST /command.

        The body is a plist whose "data" member is itself a serialized plist.
        """
        headers = dict(COMMAND_HEADERS)
        headers["X-Apple-StreamID"] = str(self._rcs_stream_id or 1)
        inner = plistlib.dumps(command, fmt=plistlib.FMT_BINARY)
        return await self.rtsp.connection.post(
            "/command",
            headers=headers,
            body=plistlib.dumps({"params": {"data": inner}}, fmt=plistlib.FMT_BINARY),
            allow_error=True,
        )

    def _queue_item(self, url: str, position: float) -> Dict[str, Any]:
        """Build a play queue item for the current item uuid."""
        return {
            "uuid": self._item_uuid,
            "Content-Location": url,
            "mediaType": "file",
            "IsTLSEnabled": url.startswith("https://"),
            "playbackRestrictions": 0,
            "referenceRestrictions": 2,
            "supportsIntegratedTimeline": False,
            "snapTimeToPausePlayback": False,
            "clientBundleID": "dev.pyatv",
            "clientProcName": "pyatv",
            # playerLoggingID must be at most 6 characters. A longer value makes
            # the receiver close the connection part way through the play queue
            # commands, with no error response. playerItemLoggingID is not
            # subject to the same limit.
            "playerLoggingID": "P/ATV",
            "playerItemLoggingID": "I/PYATV.01",
            "Start-Position": {
                "flags": 1,
                "value": int(position),
                "epoch": 0,
                "timescale": 1,
            },
        }

    async def _play_url_command(self, url: str, position: float) -> HttpResponse:
        """Start playback using the play queue protocol."""
        resp = await self.send_command(
            {"type": "insertPlayQueueItem", "item": self._queue_item(url, position)}
        )
        if resp.code != 200:
            return resp

        await self.send_command(
            {
                "type": "setProperty",
                "property": "isInterestedInDateRange",
                "value": True,
                "item": {"uuid": self._item_uuid},
            }
        )
        # Playback starts paused without this.
        await self.send_command({"type": "setRate", "rate": 1.0})
        return resp

    # ---- event channel + end of media -------------------------------------
    def handle_event(self, event: Mapping[str, Any]) -> None:
        """Handle an event pushed by the receiver on the event channel."""
        kind = event.get("type")
        if kind == "playbackState":
            state = event.get("name")
            _LOGGER.debug("Playback state: %s", state)
            # A replacement goes playing -> loading -> playing, so "stopped" is
            # only reported when nothing is playing any more.
            if state == "stopped":
                self._media_ended.set()
        elif kind == "notification":
            _LOGGER.debug("Playback notification: %s", event.get("name"))

    async def wait_for_media_end(self) -> bool:
        """Wait until the receiver reports that playback finished."""
        if self._rcs_stream_id is None:
            return False

        await self._media_ended.wait()
        return True

    # ---- entry point ------------------------------------------------------
    async def play_url(self, timing_server_port: int, url: str, position: float = 0.0):
        """Play media from a URL.

        Modern receivers drive video through a play queue over POST /command,
        which only becomes available once a remote control session is
        registered. Receivers that do not offer one still get POST /play.
        """
        # The order below mirrors what a real sender does. It matters: reading
        # /info before pair-verify, or starting feedback before RECORD, makes the
        # receiver drop the connection part way through the play queue commands.
        self._verifier = await verify_connection(
            self.context.credentials, self.rtsp.connection
        )

        try:
            await self._setup_video_session()
        except exceptions.HttpError as ex:
            # Receivers that predate PTP timed video sessions reject this setup.
            # Fall back to the audio style setup, which is what /play expects.
            _LOGGER.debug("Video session setup rejected (%s), using legacy setup", ex)
            await self._setup_base(timing_server_port)
            await self.start_feedback()
            await self.rtsp.record()
            return await self._play_url_legacy(url, position)

        await self.rtsp.record()
        use_command = await self._setup_remote_control_session()

        # Feedback must not start until the play commands have been issued.
        # It shares this connection, and an RTSP feedback request in flight
        # alongside an HTTP /command request makes the receiver drop the
        # connection part way through the play queue.
        if use_command:
            resp = await self._play_url_command(url, position)
            await self.start_feedback()
            return resp

        _LOGGER.debug("No remote control session, falling back to POST /play")
        resp = await self._play_url_legacy(url, position)
        await self.start_feedback()
        return resp

    async def _play_url_legacy(self, url: str, position: float) -> HttpResponse:
        """Start playback using POST /play, for receivers without /command.

        This is the pre-patch ``play_url`` body, reused verbatim.
        """
        return await _original_play_url(self, url, position)

    # ---- teardown ---------------------------------------------------------
    orig_teardown = AirPlayV2.teardown

    def teardown(self) -> None:
        """Teardown resources allocated by setup efter streaming finished."""
        self._media_ended.set()
        orig_teardown(self)

    AirPlayV2.__init__ = __init__
    AirPlayV2._setup_video_session = _setup_video_session
    AirPlayV2._setup_remote_control_session = _setup_remote_control_session
    AirPlayV2.send_command = send_command
    AirPlayV2._queue_item = _queue_item
    AirPlayV2._play_url_command = _play_url_command
    AirPlayV2.handle_event = handle_event
    AirPlayV2.wait_for_media_end = wait_for_media_end
    AirPlayV2.play_url = play_url
    AirPlayV2._play_url_legacy = _play_url_legacy
    AirPlayV2.teardown = teardown

    # The fork declares EventChannelListener as a real base class. Registering it
    # as a virtual base gives the same isinstance/issubclass behavior without
    # rebuilding the class, which would leave the already-installed AirPlayStream
    # holding a stale reference to the old one.
    EventChannelListener.register(AirPlayV2)

    AirPlayV2._airplay_yt_patched = True


def already_fixed_upstream() -> bool:
    """Return True when the installed pyatv already has the play-queue protocol.

    Detected by looking for the protocol surface the fix introduces (a
    ``wait_for_media_end`` on ``StreamProtocol`` and a remote-control-session
    method on ``AirPlayV2``) and confirming the implementation does not come from
    this package's own patch -- if it does, the fix is not upstream, it is us.
    """
    try:
        from pyatv.protocols.raop.protocols import StreamProtocol
        from pyatv.protocols.raop.protocols.airplayv2 import AirPlayV2
    except Exception:  # pragma: no cover - pyatv laid out differently
        return False

    if not hasattr(StreamProtocol, "wait_for_media_end"):
        return False
    rcs = getattr(AirPlayV2, "_setup_remote_control_session", None)
    if rcs is None or _pyatv_guard.owned_by_this_package(rcs):
        return False
    return True


def apply() -> bool:
    """Apply the play-queue patch idempotently.

    Returns True when the play-queue protocol is available afterwards -- whether
    because this patch supplied it or because the installed pyatv already had it.
    Returns False only when patching was attempted and failed. Safe to call
    repeatedly.
    """
    global _APPLIED, _NOT_NEEDED
    if _APPLIED or _NOT_NEEDED:
        return True

    # Advisory only: an unexpected version still gets patched (a stale patch that
    # applies is usually better than none), but the warning says to re-verify.
    _pyatv_guard.warn_if_unexpected(_LOGGER, "play_queue_patch")

    if already_fixed_upstream():
        _LOGGER.info(
            "play_queue_patch is not needed: pyatv %s already provides the "
            "play-queue protocol. This module can be deleted.",
            _pyatv_guard.installed_version() or "(unknown version)")
        _NOT_NEEDED = True
        return True

    try:
        _patch_raop_protocols()
        _patch_event_channel()
        _patch_airplay_v2()
        _patch_player()
    except Exception:  # pragma: no cover - a failed patch must not hide itself
        _LOGGER.exception("failed to apply the pyatv play-queue patch")
        return False

    _APPLIED = True
    _LOGGER.debug("pyatv play-queue patch applied")
    return True
