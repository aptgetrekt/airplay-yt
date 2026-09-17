# airplay-yt

Take a video URL (such as a YouTube link), pull it down in the highest available
quality, transcode it for AirPlay with a hardware-accelerated `ffmpeg`, and stream
it to an Apple TV on the local network.

## Pipeline

```
   input URL  ──►  download  ──►  transcode  ──►  airplay
 (YouTube, etc.) (highest qual)  (hw ffmpeg →    (to an Apple
                                H.264/AAC MP4 /   TV receiver)
                                HEVC @ 4K)
```

1. **Download** — fetch the source URL and choose the highest-quality rendition.
2. **Transcode** — re-encode to an AirPlay-compatible file using a hardware
   encoder (NVENC / QSV / AMF / VideoToolbox, as available).
3. **AirPlay** — serve the file and hand it off to an Apple TV receiver.

## Project layout

```
src/airplay_yt/
├── __init__.py      # package metadata
├── __main__.py      # `python -m airplay_yt` entry point
├── cli.py           # wires the pipeline: download -> transcode -> airplay
├── downloader.py    # fetch a URL at highest quality (yt-dlp)
├── transcoder.py    # hw-accelerated ffmpeg re-encode for AirPlay
└── airplay.py       # stream the file to an Apple TV
```

All modules currently contain unimplemented stubs — no implementation yet.

## Setup

```bash
uv sync          # create the virtualenv and install the project
uv run airplay-yt   # (stub - not wired up yet)
```

## Status

Skeleton only. The three pipeline stages are placeholders awaiting implementation.
