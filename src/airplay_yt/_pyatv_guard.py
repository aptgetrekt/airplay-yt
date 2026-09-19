"""Version and ownership guards shared by the pyatv runtime patches.

Both patches in this package (:mod:`airplay_yt.play_queue_patch` and
:mod:`airplay_yt.tvos_patch`) reach into pyatv internals, so each is written
against one specific pyatv release. Two things can go wrong on an upgrade:

* the patch no longer applies because the internals it touches moved, or
* the patch still applies but now *overrides a fix upstream already provides*.

Neither is visible from the AirPlay side: the receiver just accepts the legacy
``POST /play`` path and plays no video. This module exists so both failures are
loud. It states the expected version once, warns when the installed pyatv is not
that version, and offers a way to tell this package's own patch apart from a
native pyatv implementation.
"""

from __future__ import annotations

import importlib.metadata
import logging

# The pyatv release both patches were written against. This must match the
# `pyatv==` pin in pyproject.toml; changing one without the other is a bug, and
# `uv.lock` records the pin that actually gets installed.
EXPECTED_PYATV_VERSION = "0.18.0"

# Every patch in this package is attributed to one of these module prefixes, so
# `owned_by_this_package` can distinguish our monkeypatch from pyatv's own code.
_OWN_MODULE_PREFIX = "airplay_yt."


def installed_version() -> str | None:
    """Return the installed pyatv distribution version, or None if absent."""
    try:
        return importlib.metadata.version("pyatv")
    except importlib.metadata.PackageNotFoundError:
        return None


def warn_if_unexpected(logger: logging.Logger, patch_name: str) -> bool:
    """Warn when the installed pyatv is not the version a patch targets.

    Returns True when the version is unexpected (including unverifiable), False
    when it matches. Purely advisory: it never blocks the patch, because a patch
    that still applies is usually better than no patch at all. Callers should
    treat a True result as "re-verify before trusting this run".
    """
    actual = installed_version()
    if actual is None:
        logger.warning(
            "%s was written against pyatv %s, but no pyatv distribution is "
            "installed; cannot verify that the patch still applies. Re-check the "
            "patch against whatever pyatv is in use.",
            patch_name, EXPECTED_PYATV_VERSION)
        return True
    if actual != EXPECTED_PYATV_VERSION:
        logger.warning(
            "%s was written against pyatv %s but pyatv %s is installed. Before "
            "trusting this run: check whether upstream now provides the fix (in "
            "which case delete the patch) and re-verify the patch against the new "
            "release. See pyproject.toml and the patch module docstring.",
            patch_name, EXPECTED_PYATV_VERSION, actual)
        return True
    return False


def owned_by_this_package(obj: object) -> bool:
    """Return True when ``obj`` (a function or class) comes from this package.

    Used to distinguish a monkeypatch applied earlier in this process from a
    native pyatv implementation of the same thing.
    """
    module = getattr(obj, "__module__", "") or ""
    return module.startswith(_OWN_MODULE_PREFIX)
