"""Fetch a video from an input URL (e.g. YouTube) in the highest available quality.

Unimplemented. Planned to wrap yt-dlp / youtube-dl.
"""

from __future__ import annotations


def download(url: str, dest_dir: str) -> str:
    """Download the highest-quality rendition of `url` into `dest_dir`.

    Returns the local path to the downloaded file.
    """
    raise NotImplementedError
