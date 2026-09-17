"""Stream a media file to an AirPlay receiver (Apple TV).

Unimplemented. Planned to use an AirPlay client / HTTP media server that
adverts the transcoded file to a target receiver on the local network.
"""

from __future__ import annotations


def stream(media_path: str, target: str | None = None) -> None:
    """AirPlay `media_path` to `target` (a host/ip/name). Auto-discover if omitted."""
    raise NotImplementedError
