"""Runtime monkey-patches for pyatv, kept together so they are easy to audit.

Every module here exists for the same reason: the released ``pyatv`` cannot
deliver video to an Apple TV on tvOS 26/27, and the fixes live in unreleased
upstream work. Rather than depend on a fork, this project applies those fixes to
the installed pyatv at runtime. Modules:

``play_queue_patch``
    The play-queue protocol rewrite (upstream PR #2774 / #2899): video is driven
    through ``POST /command`` with PTP timing, instead of the legacy
    ``POST /play`` the modern receivers reject.

``tvos_patch``
    The ``psi`` injection those receivers additionally need: tvOS 26.6/27 no
    longer reports it on ``GET /info``, which blocks the remote control session
    the play queue depends on. Layers on top of ``play_queue_patch``.

``_pyatv_guard``
    Shared version check and upstream-fix detection, so an unexpected pyatv
    version is reported and a patch that is no longer needed skips itself.

Order matters: ``play_queue_patch`` must be applied before ``tvos_patch``, because
the latter reuses ``RCS_CLIENT_TYPE_UUID`` and the remote-control-session method
defined by the former. :func:`airplay_yt.airplay._stream` applies both in the
correct order before connecting.

These are patches, not a library: they reach into pyatv internals and are pinned
to one pyatv release (see ``_pyatv_guard.EXPECTED_PYATV_VERSION`` and the
``pyatv==`` pin in ``pyproject.toml``). Each module carries a MAINTENANCE section
describing what to check when pyatv is upgraded, and each is written so it can be
deleted outright once a released pyatv contains the fix.
"""

from __future__ import annotations

__all__ = ["play_queue_patch", "tvos_patch"]
