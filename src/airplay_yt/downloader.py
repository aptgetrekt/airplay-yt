"""Fetch a video from an input URL (e.g. YouTube) in the highest available quality.

Downloads a single, self-contained media file -- best available video plus best
available audio muxed together -- at the maximum quality yt-dlp can obtain, into a
temporary location, and returns the local path to that file.

The next stage (``transcoder.py``) is handed exactly one input, so the download
always resolves to a single MP4 (the most broadly re-encodable container for the
hardware-accelerated ffmpeg step). The file is left on disk for the caller: it is
not deleted here because the transcoder still needs it.

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

# yt-dlp can fetch the "best video" and "best audio" streams separately (YouTube
# never serves one combined stream), so we pick each independently and let yt-dlp
# merge them. The ``/b`` fallback handles sources that offer only a single
# combined rendition.
_BEST_FORMAT = "bv*+ba*/b"

# Force the merged (or single) output into MP4 so the transcoder always sees one
# uniform input, regardless of the source container (webm, etc.).
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
    """Log download progress at a coarse, non-spammy cadence."""
    status = d.get("status")
    if status == "downloading":
        log.debug("downloading %s", d.get("filename", "?"))
    elif status == "finished":
        log.debug("downloaded %s", d.get("filename", "?"))
    elif status == "error":
        log.warning("download error for %s", d.get("filename", "?"))


def _ensure_ffmpeg() -> None:
    """Warn (do not fail) when ffmpeg is missing -- a merge step would fail."""
    if shutil.which("ffmpeg") is None:
        log.warning(
            "ffmpeg not found on PATH; separate video+audio streams cannot be "
            "merged into one file, and the result may not be transcode-ready.")


def _build_opts(url: str, out_dir: str) -> dict:
    """Build yt-dlp options for a full-quality, single-file download into ``out_dir``."""
    base = _safe_video_id(url)
    return {
        "outtmpl": os.path.join(out_dir, f"{base}.%(ext)s"),
        "merge_output_format": MERGE_FORMAT,
        "format": _BEST_FORMAT,
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
        "-f", opts["format"],
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
    log.info("downloading %s (best quality) into %s", url, out_dir)
    ydl = yt_dlp.YoutubeDL(opts)
    try:
        info = ydl.extract_info(url, download=True)
    except Exception as e:  # yt-dlp raises a moving hierarchy; re-wrap all
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
    """Download the highest-quality rendition of ``url`` into a temp location.

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
        Absolute path to the single merged, full-quality MP4 on disk. The file is
        NOT deleted by this call; pass it to ``transcoder.transcode`` next.
    """
    out_dir = os.path.abspath(dest_dir or tempfile.mkdtemp(prefix="airplay-yt-"))
    os.makedirs(out_dir, exist_ok=True)
    base = _safe_video_id(url)
    opts = _build_opts(url, out_dir)

    if _module_available("yt_dlp"):
        return _download_via_module(url, out_dir, base, opts)
    log.info("yt_dlp module unavailable; falling back to yt-dlp CLI")
    return _download_via_cli(url, out_dir, base, opts)
