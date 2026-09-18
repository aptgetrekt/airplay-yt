"""Fetch a video from an input URL (e.g. YouTube) in the highest available quality.

Downloads a single, self-contained media file into a temporary location and
returns the local path to it.

The rendition is chosen to play on Apple TV *without* transcoding: VP9 video (with
automatic fallback to the highest available H.264), paired with the best AAC
audio, all muxed into an mp4 container. The next stage therefore often just needs
to hand this file to ``airplay.py``. The file is left on disk for the caller: it
is not deleted here because the downstream stage still needs it.

By default the file lands in a fresh, unique temp directory. Pass ``dest_dir`` to
place it somewhere else instead.
"""

from __future__ import annotations

import glob
import importlib
import logging
import os
import shutil
import subprocess
import tempfile

log = logging.getLogger(__name__)

# The rendition must play on Apple TV *without* transcoding. Apple TV (tvOS)
# decodes H.264/VP9 with AAC audio but NOT AV1 or Opus -- confirmed on playback:
# an AV1/Opus file showed a black screen while its audio still played, so an
# AV1/Opus render is useless for this pipeline and must never be selected.
#
# A bare format *sort* (the reference AirPlay command) is not enough: a "vcodec:
# vp9" sort term only orders candidates; when yt-dlp's JS ("EJS") challenge solver
# is unavailable some VP9 streams are dropped ("some formats may be missing") and a
# higher-ranked AV1/Opus render can win. So we *cap* the candidate set with a
# hard codec selector that excludes the non-playable codecs, and use the reference
# sort only to *order* within the allowed set:
#
#   1. best VP9 video + best AAC (mp4a) audio                    <- preferred
#     / best H.264 video + best AAC audio                        <- fallback ("h264")
#     / best combined stream                                      <- last resort
#
# This yields the 4K VP9 + AAC file that plays on Apple TV, and falls back to the
# best H.264 + AAC when a source has no VP9 ladder.
_FORMAT_SELECTOR = (
      "bv[vcodec^=vp9]+ba[acodec^=mp4a]"
        "/bv[vcodec^=avc1]+ba[acodec^=mp4a]"
        "/bv+ba/b"
)

# Mirror the reference AirPlay sort so we get the *highest* quality within the
# capped set; the hard codec cap above is what actually guarantees VP9/H.264 + AAC.
_FORMAT_SORT = "vcodec:vp9,res,br,acodec:aac,ext:mp4:m4a"

# yt-dlp's EJS challenge solver keeps the full VP9 ladder available. The Python
# API expects a *list* of component specs (passing a string iterates its
# characters and every char is rejected as an "unsupported component"), so it is
# given as a list here and as a single CLI arg below.
_REMOTE_COMPONENTS = ["ejs:github"]

# Force the merged (or single) output into MP4 so downstream gets one uniform,
# AirPlay-playable container regardless of the source container (webm, etc.).
MERGE_FORMAT = "mp4"

# Extra attempts for flaky networks / CDN fragment fetches.
_RETRIES = 10


class DownloadError(RuntimeError):
    """A source URL could not be downloaded to a local file."""


def _safe_video_id(url: str) -> str:
    """Derive a filesystem-safe, extension-free stem from ``url``."""
    stem = os.path.basename(url.rstrip("/") or "").replace("?", "_").replace("&", "_")
    return os.path.splitext(stem)[0] or "video"


def _progress_hook(d: dict) -> None:
    """Report download progress through the logging layer.

    yt-dlp renders its own live download progress bar, so this hook only records
    coarse milestones at debug level; it does not print its own progress line.
    """
    status = d.get("status")
    fname = os.path.basename(d.get("filename", "") or "?")
    if status == "downloading":
        log.debug("downloading %s", fname)
    elif status == "finished":
        log.debug("downloaded %s", fname)
    elif status == "error":
        log.warning("download error for %s", fname)


def _ensure_ffmpeg() -> None:
    """Warn (do not fail) when ffmpeg is missing -- a merge step would fail."""
    if shutil.which("ffmpeg") is None:
        log.warning(
            "ffmpeg not found on PATH; separate video+audio streams cannot be "
            "merged into one file, and the result may not be transcode-ready.")


def _build_opts(url: str, out_dir: str) -> dict:
    """Build yt-dlp options that select the best Apple-TV-playable rendition and
    write a single merged MP4 into ``out_dir`` (see the codec-cap constants above)."""
    base = _safe_video_id(url)
    return {
        "outtmpl": os.path.join(out_dir, f"{base}.%(ext)s"),
        "merge_output_format": MERGE_FORMAT,
        "remote_components": _REMOTE_COMPONENTS,
        "format": _FORMAT_SELECTOR,
        "format_sort_expressions": _FORMAT_SORT,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": False,
        "ignoreerrors": False,
        "skip_download": False,
        "retries": _RETRIES,
        "fragment_retries": _RETRIES,
        "concurrent_fragment_downloads": 4,
        "external_downloader": None,       # yt-dlp downloads natively
        # Prefer ffmpeg for hls/ism: it handles segment + key files more reliably
        # than yt-dlp's own downloader for encrypted VOD/live streams.
        "hls_prefer_native": False,
        "progress_hooks": [_progress_hook],
        "restrictfilenames": False,
    }


def _resolve_output(out_dir: str, base: str, info: dict | None) -> str:
    """Return the single merged media file yt-dlp produced, or raise."""
    preferred = os.path.join(out_dir, f"{base}.{MERGE_FORMAT}")
    if os.path.isfile(preferred):
        return preferred

    # Fall back to whatever media yt-dlp actually left in the destination.
    candidates = [c for c in glob.glob(os.path.join(out_dir, f"{base}*"))
                  if os.path.isfile(c)]
    if not candidates:
        raise DownloadError(f"download finished but no output file found in {out_dir!r}")
    # Most-recently-written last -- prefer the newest, i.e. the final output.
    return max(candidates, key=os.path.getmtime)


def _download_via_cli(url: str, out_dir: str, base: str, opts: dict) -> str:
    """Fallback: shell out to the ``yt-dlp`` binary when the module is absent."""
    _ensure_ffmpeg()
    cmd = [
        "yt-dlp",
        "--remote-components", ",".join(_REMOTE_COMPONENTS),
        "-f", _FORMAT_SELECTOR,
        "-S", _FORMAT_SORT,
        "--merge-output-format", MERGE_FORMAT,
        "--no-part",
        "--retries", str(_RETRIES),
        "--newline",
    ]
    if opts.get("noplaylist"):
        cmd.append("--no-playlist")
    cmd += [url, "-o", os.path.join(out_dir, f"{base}.%(ext)s")]
    log.info("downloading via yt-dlp CLI: %s", " ".join(cmd))
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        raise DownloadError(f"yt-dlp CLI exited with status {result.returncode}")
    return _resolve_output(out_dir, base, None)


def _download_via_module(url: str, out_dir: str, base: str, opts: dict) -> str:
    import yt_dlp

    _ensure_ffmpeg()
    log.info("downloading %s (Apple-TV playable: VP9/H.264 + AAC) into %s", url, out_dir)
    ydl = yt_dlp.YoutubeDL(opts)
    try:
        info = ydl.extract_info(url, download=True)
    except Exception as e:  # yt-dlp raises a moving hierarchy; re-wrap all
        print()                   # clear a leftover progress line first
        raise DownloadError(f"yt-dlp failed to download {url!r}: {e}") from e
    else:
        result_path = _resolve_output(out_dir, base, info)
        log.info("download complete -> %s", result_path)
        return os.path.abspath(result_path)
    finally:
        ydl.close()


def _module_available(name: str) -> bool:
    try:
        importlib.import_module(name)
        return True
    except ImportError:
        return False


def download(url: str, dest_dir: str | None = None) -> str:
    """Download the highest-quality rendition of ``url`` that plays on Apple TV.
    
    Apple TV decodes H.264 + VP9 with AAC audio but not AV1 or Opus, so the
    best such rendition (prefer VP9, fall back to H.264, always AAC) is selected
    and written to a temp location without transcoding.

    Parameters
    ----------
    url:
        A media source URL (YouTube, or any URL yt-dlp understands).
    dest_dir:
        Directory to write into. When ``None`` a fresh, unique temp directory is
        created -- recommended for the download -> transcode -> airplay pipeline,
        since the caller (the transcoder) still owns the file afterward.

    Returns
    -------
    str
        Absolute path to the single merged MP4 on disk. The file is NOT deleted
        by this call; pass it to ``transcoder.transcode`` next.
    """
    out_dir = os.path.abspath(dest_dir or tempfile.mkdtemp(prefix="airplay-yt-"))
    os.makedirs(out_dir, exist_ok=True)
    base = _safe_video_id(url)
    opts = _build_opts(url, out_dir)

    if _module_available("yt_dlp"):
        return _download_via_module(url, out_dir, base, opts)
    log.info("yt_dlp module unavailable; falling back to yt-dlp CLI")
    return _download_via_cli(url, out_dir, base, opts)
