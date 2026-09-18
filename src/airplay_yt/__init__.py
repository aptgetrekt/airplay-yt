"""airplay-yt: download a video URL, transcode for AirPlay, and stream it to an Apple TV."""

__version__ = "0.1.0"


def main(argv: list[str] | None = None) -> None:
    "Entry point: download a URL, then AirPlay it (delegates to the CLI)."
    from airplay_yt.cli import main as _cli_main
    _cli_main(argv)
