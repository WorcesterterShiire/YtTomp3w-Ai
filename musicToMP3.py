

"""
Version 1
Playlist to MP3 Downloader
---------------------------
Paste a playlist URL (YouTube, YouTube Music, SoundCloud, etc. — anything
yt-dlp supports) and this will download every track as an MP3.

Requirements (install once):
    pip install yt-dlp
    ffmpeg must be installed and on your PATH:
        - Windows:  choco install ffmpeg   (or download from ffmpeg.org and add to PATH)
        - Mac:      brew install ffmpeg
        - Linux:    sudo apt install ffmpeg

Where files go:
    By default, everything is saved to a "downloads" folder created next to
    this script. You can change DOWNLOAD_DIR below, or pass a custom path
    when running from the command line.

Usage:
    python playlist_to_mp3.py
    python playlist_to_mp3.py "https://youtube.com/playlist?list=..."
    python playlist_to_mp3.py "https://youtube.com/playlist?list=..." "D:/Music/MyPlaylist"
"""

import os
import sys
import logging
from pathlib import Path

try:
    import yt_dlp
except ImportError:
    print("yt-dlp is not installed. Run this first:\n\n    pip install yt-dlp\n")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Default folder where MP3s land if you don't specify one at runtime.
# This resolves to a "downloads" folder sitting right next to this script.
DOWNLOAD_DIR = Path(__file__).resolve().parent / "downloads"

# Audio quality for the MP3 (0 = best, 9 = worst; 0-2 is effectively transparent)
AUDIO_QUALITY = "0"

LOG_FILE = "download_log.txt"


# ---------------------------------------------------------------------------
# Logging setup — logs to both console and a file so you have a record of
# what succeeded/failed after a long playlist run.
# ---------------------------------------------------------------------------

def setup_logging(download_dir: Path) -> logging.Logger:
    download_dir.mkdir(parents=True, exist_ok=True)
    log_path = download_dir / LOG_FILE

    logger = logging.getLogger("playlist_downloader")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()  # avoid duplicate handlers if re-run in same session

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter("%(message)s"))

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


# ---------------------------------------------------------------------------
# Core downloader
# ---------------------------------------------------------------------------

class PlaylistDownloader:
    def __init__(self, download_dir: Path, logger: logging.Logger):
        self.download_dir = download_dir
        self.logger = logger
        self.succeeded = []
        self.failed = []
        self.skipped = []

    def _progress_hook(self, d):
        """yt-dlp calls this during download; we use it just to log cleanly."""
        if d["status"] == "finished":
            filename = os.path.basename(d.get("filename", "unknown"))
            self.logger.info(f"  -> Converting: {filename}")
        elif d["status"] == "error":
            self.logger.error(f"  -> Error during download: {d}")

    def _ydl_opts(self):
        return {
            "format": "bestaudio/best",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": AUDIO_QUALITY,
                }
            ],
            # %(playlist_index)s keeps track order visible in the filename
            "outtmpl": str(self.download_dir / "%(playlist_index)02d - %(title)s.%(ext)s"),
            "ignoreerrors": True,       # keep going if one track fails
            "no_warnings": False,
            "logger": self._YdlLoggerAdapter(self.logger),
            "progress_hooks": [self._progress_hook],
            "noplaylist": False,
            "continuedl": True,         # resume partial downloads if re-run
            "retries": 3,
            "fragment_retries": 3,
        }

    class _YdlLoggerAdapter:
        """Routes yt-dlp's internal logging into our logger instead of stdout."""
        def __init__(self, logger):
            self.logger = logger

        def debug(self, msg):
            if msg.startswith("[debug] "):
                return
            self.logger.debug(msg)

        def info(self, msg):
            self.logger.debug(msg)

        def warning(self, msg):
            self.logger.warning(msg)

        def error(self, msg):
            self.logger.error(msg)

    def get_playlist_info(self, url: str):
        """Fetch playlist metadata without downloading, so we can show a
        preview and catch bad URLs early."""
        opts = {"quiet": True, "extract_flat": True, "ignoreerrors": True}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                return info
        except yt_dlp.utils.DownloadError as e:
            self.logger.error(f"Could not read playlist info: {e}")
            return None
        except Exception as e:
            self.logger.error(f"Unexpected error reading playlist: {e}")
            return None

    def download(self, url: str):
        self.download_dir.mkdir(parents=True, exist_ok=True)

        self.logger.info(f"\nFetching playlist details...")
        info = self.get_playlist_info(url)

        if info is None:
            self.logger.error(
                "Failed to fetch playlist. Check that the URL is correct, "
                "public, and not region/age-restricted."
            )
            return

        entries = info.get("entries", None)
        if entries is None:
            # It's a single track, not a playlist — handle it gracefully
            title = info.get("title", "this track")
            self.logger.info(f"This looks like a single track ('{title}'), not a playlist. Downloading it anyway.")
            entries = [info]
        else:
            entries = [e for e in entries if e is not None]

        total = len(entries)
        playlist_title = info.get("title", "Unknown playlist")
        self.logger.info(f"Playlist: {playlist_title}")
        self.logger.info(f"Tracks found: {total}")
        self.logger.info(f"Saving to: {self.download_dir}\n")

        if total == 0:
            self.logger.warning("No downloadable tracks found in this playlist (all entries may be private/deleted).")
            return

        with yt_dlp.YoutubeDL(self._ydl_opts()) as ydl:
            for idx, entry in enumerate(entries, start=1):
                # Some playlist entries are None if a video was deleted/private
                if entry is None:
                    self.skipped.append(f"Track {idx}: unavailable (deleted or private)")
                    self.logger.warning(f"[{idx}/{total}] Skipped — video unavailable")
                    continue

                video_url = entry.get("url") or entry.get("webpage_url") or entry.get("id")
                title = entry.get("title", f"Track {idx}")
                self.logger.info(f"[{idx}/{total}] {title}")

                try:
                    ydl.download([video_url])
                    self.succeeded.append(title)

                except yt_dlp.utils.DownloadError as e:
                    self.logger.error(f"  -> Failed: {e}")
                    self.failed.append((title, str(e)))
                    continue

                except KeyboardInterrupt:
                    self.logger.warning("\nInterrupted by user. Stopping early.")
                    raise

                except Exception as e:
                    self.logger.error(f"  -> Unexpected error: {e}")
                    self.failed.append((title, str(e)))
                    continue

        self._print_summary(total)

    def _print_summary(self, total: int):
        self.logger.info("\n" + "=" * 50)
        self.logger.info("SUMMARY")
        self.logger.info("=" * 50)
        self.logger.info(f"Succeeded: {len(self.succeeded)}/{total}")
        self.logger.info(f"Failed:    {len(self.failed)}/{total}")
        self.logger.info(f"Skipped:   {len(self.skipped)}/{total}")

        if self.failed:
            self.logger.info("\nFailed tracks:")
            for title, reason in self.failed:
                self.logger.info(f"  - {title}: {reason[:100]}")

        if self.skipped:
            self.logger.info("\nSkipped tracks:")
            for reason in self.skipped:
                self.logger.info(f"  - {reason}")

        self.logger.info(f"\nFiles saved to: {self.download_dir}")
        self.logger.info(f"Full log saved to: {self.download_dir / LOG_FILE}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    print("=" * 50)
    print("Playlist -> MP3 Downloader")
    print("=" * 50)

    # URL: from command line arg, or prompt for it
    if len(sys.argv) >= 2:
        url = sys.argv[1].strip()
    else:
        url = input("\nPaste the playlist URL: ").strip()

    if not url:
        print("No URL provided. Exiting.")
        sys.exit(1)

    # Download directory: from command line arg, or use default
    if len(sys.argv) >= 3:
        download_dir = Path(sys.argv[2]).expanduser().resolve()
    else:
        download_dir = DOWNLOAD_DIR

    logger = setup_logging(download_dir)
    downloader = PlaylistDownloader(download_dir, logger)

    try:
        downloader.download(url)
    except KeyboardInterrupt:
        downloader._print_summary(len(downloader.succeeded) + len(downloader.failed) + len(downloader.skipped))
        sys.exit(0)
    except Exception as e:
        logger.error(f"\nFatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
