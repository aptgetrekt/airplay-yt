"""Transcode a downloaded file into an AirPlay-compatible format using hw-accelerated ffmpeg.

Unimplemented. Planned to probe an ffmpeg hardware encoder (e.g. NVENC / QSV /
AMF / VideoToolbox) and produce an H.264/AAC MP4 (or HEVC for Apple TV 4K).
"""

from __future__ import annotations


def transcode(input_path: str, output_path: str) -> str:
    """Transcode `input_path` to an AirPlay-friendly container and return `output_path`."""
    raise NotImplementedError
