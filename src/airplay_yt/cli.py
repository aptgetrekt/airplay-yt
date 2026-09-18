"""Command-line interface: download a URL, then AirPlay it to an Apple TV.

Usage:
    airplay-yt --url <source-url> [--device "<Apple TV name or IP>"] [--pin N]

The URL is handed to :mod:`airplay_yt.downloader`, which downloads the
highest-quality Apple-TV-playable rendition (VP9 with H.264 fallback, AAC audio,
mp4 container) into a fresh temp directory and returns its path. That path is then
handed to :mod:`airplay_yt.airplay`, which serves it to the target Apple TV over
AirPlay. A live download-progress line is printed to stderr by the downloader, and
status messages bracket each step so the user can see exactly what is happening.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

from airplay_yt import airplay, downloader

# Temp dirs the downloader creates when no destination is supplied; we clean these
# up after streaming unless --keep is passed. Caller-supplied destinations are left
# untouched.
_TEMP_PREFIX = "airplay-yt-"

# Where --keep stores the download by default, instead of the throwaway temp dir.
# Overridable with --save-path.
_DEFAULT_SAVE_DIR = "~/Videos/airplay-yt"


def _cleanup(media_path: str, keep: bool) -> None:
    """Remove the temp download dir created for media_path unless keep is set."""
    parent = os.path.dirname(media_path)
    if not keep and os.path.basename(parent).startswith(_TEMP_PREFIX):
        shutil.rmtree(parent, ignore_errors=True)
        print(f"removed temp download dir {parent}", file=sys.stderr, flush=True)
        return
    if keep:
        print(f"kept {media_path}", file=sys.stderr, flush=True)


def run(url: str, device: str | None = None, pin: str | None = None,
        keep: bool = False, save_path: str | None = None) -> int:
    """Download ``url`` then AirPlay it to the selected Apple TV.

    With ``keep`` the download is written to a persistent directory
    (``save_path``, defaulting to :data:`_DEFAULT_SAVE_DIR`) instead of a
    throwaway temp dir; if a cached copy of the same URL is already there the
    download is skipped and the existing file is streamed straight away.

    Returns a process exit code: 0 on success, 130 on user interrupt, 1 on any
    failure. Status is written to stderr so stdout stays pipe-friendly.
    """
    # ---- 1. obtain a local media file --------------------------------------
    media_path: str
    if keep:
        out_dir = os.path.abspath(os.path.expanduser(save_path or _DEFAULT_SAVE_DIR))
        os.makedirs(out_dir, exist_ok=True)
        existing = downloader.find_existing(url, out_dir)
        if existing:
            media_path = existing
            print(f"[1/2] Using existing {os.path.basename(media_path)} "
                  f"(skipping download)", file=sys.stderr, flush=True)
        else:
            print(f"[1/2] Downloading {url} to {out_dir}",
                  file=sys.stderr, flush=True)
            try:
                media_path = downloader.download(url, dest_dir=out_dir)
            except downloader.DownloadError as e:
                print(f"download failed: {e}", file=sys.stderr, flush=True)
                return 1
    else:
        # ---- temp-path pipeline (the downloader prints progress to stderr) ----
        print(f"[1/2] Downloading {url}", file=sys.stderr, flush=True)
        try:
            media_path = downloader.download(url)
        except downloader.DownloadError as e:
            print(f"download failed: {e}", file=sys.stderr, flush=True)
            return 1

    # ---- 2. AirPlay to the target Apple TV ------------------------------------
    target = device or "the first Apple TV on the network"
    print(f"[2/2] Streaming {os.path.basename(media_path)} to {target} ...",
          file=sys.stderr, flush=True)
    try:
        airplay.stream(media_path, target=device, pin=pin)
    except airplay.AirPlayError as e:
        print(f"airplay failed: {e}", file=sys.stderr, flush=True)
        _cleanup(media_path, keep)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted by user; stopping", file=sys.stderr, flush=True)
        _cleanup(media_path, keep)
        return 130

    print("done", file=sys.stderr, flush=True)
    _cleanup(media_path, keep)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    epilog = (
        "Example:\n"
        '  airplay-yt --device "Living Room Apple TV" '
        "--url https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    )
    parser = argparse.ArgumentParser(
        prog="airplay-yt",
        description="Download a video by URL and stream it to an Apple TV over AirPlay.",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--url", required=True, metavar="<source-url>",
        help="Video source URL (YouTube, or anything yt-dlp understands).",
    )
    parser.add_argument(
        "--device", "-D", default=None, metavar="<name/IP>",
        help="Target Apple TV: its name, IP address, or device id. "
             "Omit to auto-select the single device found on the network.",
    )
    parser.add_argument(
        "--pin", "-p", default=None, metavar="<PIN>",
        help="AirPlay pairing PIN shown on the TV on first use (optional).",
    )
    parser.add_argument(
        "--keep", action="store_true",
        help="Keep the downloaded file after streaming instead of deleting it. "
              "The file is stored in --save-path "
              f"(default {_DEFAULT_SAVE_DIR}) instead of a temp dir, and a cached "
              "copy of the same URL is reused rather than re-downloaded.",
    )
    parser.add_argument(
        "--save-path", default=None, metavar="<dir>",
        help="Directory --keep downloads into and keeps files in "
              f"(default: {_DEFAULT_SAVE_DIR}).",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    """CLI entry point: parse args, run the pipeline, exit on the pipeline code."""
    args = _build_parser().parse_args(argv)
    code = run(args.url, device=args.device, pin=args.pin,
               keep=args.keep, save_path=args.save_path)
    raise SystemExit(code)
