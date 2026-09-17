"""Command-line interface: download -> transcode -> airplay."""

from __future__ import annotations

from airplay_yt import airplay, downloader, transcoder


def run(url: str, target: str | None = None, **_: object) -> None:
    """Run the full pipeline for a single input URL."""
    # local_file = downloader.download(url, ".")
    # airplay_file = transcoder.transcode(local_file, "airplay.m4v")
    # airplay.stream(airplay_file, target)
    raise NotImplementedError


def main() -> None:
    raise NotImplementedError
