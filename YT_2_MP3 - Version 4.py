"""
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

    If you start seeing "HTTP Error 403: Forbidden" on tracks that used to
    work, update yt-dlp first — YouTube changes things often and the
    yt-dlp maintainers ship fixes for it constantly:
        pip install -U yt-dlp

Where files go:
    Everything lives under a "music" folder created next to this script.
    Every playlist gets its own subfolder inside "music", named after the
    playlist. A file called playlists_log.json sits at the top of "music"
    and remembers which folder belongs to which playlist link, so:
        - Paste a playlist link you've already downloaded before -> it's
          matched against the log and reuses that same folder (only tracks
          that are new since last time get downloaded).
        - Paste a playlist link that isn't in the log yet -> it's treated
          as new, gets its own fresh folder named after the playlist, and
          is added to the log for next time.
        - Paste a single video (not a playlist link) -> it goes in a
          shared "Stray" folder, not a one-off folder of its own.

    Duplicate songs across playlists: before downloading, the script looks
    at every MP3 already sitting anywhere under "music" (every playlist's
    folder, plus Stray) — not just the current one:
        - Same video already downloaded in another playlist -> skipped
          automatically, no prompt (it's unambiguously the same file).
        - A title that matches a different video/upload in another
          playlist -> you'll be asked whether to skip it, plus a one-time
          "use this answer for the rest of this run?" follow-up. Choosing
          to keep it downloads it fresh rather than copying the other copy.
    This is re-derived from what's actually on disk every run, so nothing
    needs migrating — music you already downloaded before this feature
    existed is picked up automatically the first time you run it.

    You can change MUSIC_DIR below, or pass a custom base folder when
    running from the command line — playlist subfolders are still created
    inside whatever base folder is used.

Usage:
    python playlist_to_mp3.py
    python playlist_to_mp3.py "https://youtube.com/playlist?list=..."
    python playlist_to_mp3.py "https://youtube.com/playlist?list=..." "D:/Music"
"""

import os
import re
import sys
import time
import json
import logging
import urllib.parse
from pathlib import Path

try:
    import yt_dlp
except ImportError:
    print("yt-dlp is not installed. Run this first:\n\n    pip install yt-dlp\n")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Main library folder. Every playlist gets its own subfolder created inside
# this one (see PlaylistLibrary below). This resolves to a "music" folder
# sitting right next to this script.
MUSIC_DIR = Path(__file__).resolve().parent / "music"

# Single (non-playlist) links all land in one shared folder instead of each
# getting a one-off folder of its own.
STRAY_FOLDER_NAME = "Stray"

# Audio quality for the MP3 (0 = best, 9 = worst; 0-2 is effectively transparent)
AUDIO_QUALITY = "0"

# Per-playlist download activity log (created inside each playlist's own folder).
LOG_FILE = "download_log.txt"

# Global playlist-link -> folder registry (lives at the top of MUSIC_DIR,
# shared across every playlist).
REGISTRY_FILE = "playlists_log.json"

# YouTube intermittently 403s a fraction of videos in any large batch —
# this is a known, ongoing server-side throttling/validation issue (see
# yt-dlp's issue tracker), not something specific to this script. The same
# track very often succeeds a short while later with no other change, so we
# retry a failed track a few times with a growing delay before giving up.
TRACK_RETRIES = 3            # total attempts per track (1 initial + 2 retries)
RETRY_WAIT_SECONDS = 20      # base wait before retry #1; grows each attempt

# Small randomized pause before each download so requests aren't fired back
# to back, which makes throttling more likely on long playlists.
SLEEP_INTERVAL = 2
MAX_SLEEP_INTERVAL = 5


# ---------------------------------------------------------------------------
# Logging setup — logs to console always, and to a file inside a playlist's
# folder once that folder has been resolved (see PlaylistLibrary below).
# ---------------------------------------------------------------------------

def setup_logging(playlist_dir: Path = None) -> logging.Logger:
    logger = logging.getLogger("playlist_downloader")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()  # avoid duplicate handlers if re-run in same session

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console_handler)

    # Only attach the file handler once we know which playlist folder this
    # run belongs to (see main()) — before that, this logs to console only.
    if playlist_dir is not None:
        playlist_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(playlist_dir / LOG_FILE, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(file_handler)

    return logger


# ---------------------------------------------------------------------------
# Playlist library — keeps every playlist in its own folder under MUSIC_DIR,
# and keeps a log (playlists_log.json) mapping each playlist link to its
# folder, so the same playlist link always lands in the same folder.
# ---------------------------------------------------------------------------

_INVALID_FS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _extract_playlist_key(url: str) -> str:
    """Best-effort canonical ID for a playlist link, taken from its 'list='
    query parameter when there is one (YouTube/YouTube Music). This way the
    same playlist pasted again later — maybe with an extra tracking param
    like ?si=... tacked on — still matches the same log entry instead of
    registering as a "new" playlist. Falls back to the raw link for sites
    that don't use a list= parameter."""
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        if query.get("list") and query["list"][0]:
            return query["list"][0]
    except Exception:
        pass
    return url.strip().rstrip("/")


def _sanitize_folder_name(name: str, fallback: str = "Untitled Playlist") -> str:
    """Strips characters that aren't allowed in Windows/Mac/Linux folder names."""
    cleaned = _INVALID_FS_CHARS.sub("", name).strip().rstrip(". ")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned or fallback)[:150]


# -- Song identity -----------------------------------------------------------
# Shared by both the per-folder duplicate check (PlaylistDownloader) and the
# whole-library scan below, so "is this song already downloaded" means the
# same thing whether we're looking at one folder or all of them.

# Matches a video ID embedded at the end of a filename, e.g.
# "01 - Song Name [aBc123XyZ9].mp3" -> "aBc123XyZ9"
_ID_PATTERN = re.compile(r"\[([A-Za-z0-9_-]+)\]\.mp3$", re.IGNORECASE)


def normalize_title(title: str) -> str:
    """Reduces a title to bare alphanumerics so minor differences in
    spacing/punctuation/casing don't cause a false 'new track' result."""
    t = re.sub(r"^\d+\s*-\s*", "", title)            # strip leading "01 - "
    t = re.sub(r"\s*\[[A-Za-z0-9_-]+\]\s*$", "", t)   # strip trailing " [ID]"
    t = t.lower()
    t = re.sub(r"[^a-z0-9]+", "", t)
    return t


def scan_music_library(music_dir: Path) -> dict:
    """Scans every folder under music_dir (every playlist, plus Stray) for
    MP3s already downloaded anywhere in the library — not just the current
    playlist's own folder — so the same song showing up in a different
    playlist gets recognized instead of re-downloaded. Re-derived fresh from
    disk on every run (nothing persisted), so it can never drift out of sync
    and there's nothing to migrate when this feature is first turned on —
    whatever's already on disk is picked up automatically.
    Returns {"by_id": {video_id: folder_name}, "by_title": {normalized_title:
    folder_name}}; first folder seen for a given id/title wins."""
    by_id, by_title = {}, {}
    if not music_dir.exists():
        return {"by_id": by_id, "by_title": by_title}

    for f in music_dir.rglob("*.mp3"):
        folder_name = f.parent.name
        match = _ID_PATTERN.search(f.name)
        if match and match.group(1) not in by_id:
            by_id[match.group(1)] = folder_name
        norm = normalize_title(f.stem)
        if norm and norm not in by_title:
            by_title[norm] = folder_name

    return {"by_id": by_id, "by_title": by_title}


def prompt_yes_no(question: str, default: bool = False) -> bool:
    """Simple y/n console prompt; blank input uses `default`."""
    suffix = " [Y/n]: " if default else " [y/N]: "
    while True:
        answer = input(question + suffix).strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("Please answer y or n.")


class PlaylistLibrary:
    """Owns playlists_log.json inside the music folder: a playlist-link ->
    folder lookup so every playlist gets exactly one folder, made once and
    reused on every future run."""

    def __init__(self, music_dir: Path):
        self.music_dir = music_dir
        self.registry_path = music_dir / REGISTRY_FILE
        self._data = self._load()

    def _load(self) -> dict:
        if self.registry_path.exists():
            try:
                with open(self.registry_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save(self):
        self.music_dir.mkdir(parents=True, exist_ok=True)
        with open(self.registry_path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)

    def _unique_folder_name(self, name: str) -> str:
        taken = {entry.get("folder") for entry in self._data.values()} | {STRAY_FOLDER_NAME}
        candidate = name
        counter = 2
        while candidate in taken or (self.music_dir / candidate).exists():
            candidate = f"{name} ({counter})"
            counter += 1
        return candidate

    def resolve(self, url: str, playlist_title: str):
        """Compares `url` against every playlist link logged so far.
        Returns (folder_path, matched): matched is True if this playlist
        link was already in the log (its existing folder is reused), or
        False if it's new (a fresh folder was just created and logged)."""
        key = _extract_playlist_key(url)

        if key in self._data:
            entry = self._data[key]
            folder = self.music_dir / entry["folder"]
            folder.mkdir(parents=True, exist_ok=True)  # in case it was moved/deleted
            if entry.get("title") != playlist_title:
                entry["title"] = playlist_title  # keep the stored title current
                self._save()
            return folder, True

        base_name = _sanitize_folder_name(playlist_title)
        claimed = {entry.get("folder") for entry in self._data.values()}

        # A folder with this exact name may already exist on disk with no
        # registry entry — most likely this playlist was downloaded before
        # this log existed. As long as it isn't already claimed by a
        # *different* registered playlist, adopt it instead of creating a
        # "(2)" sibling, so it settles into the log in place.
        if base_name not in claimed and (self.music_dir / base_name).exists():
            folder_name = base_name
        else:
            folder_name = self._unique_folder_name(base_name)

        folder = self.music_dir / folder_name
        folder.mkdir(parents=True, exist_ok=True)

        self._data[key] = {
            "url": url,
            "title": playlist_title,
            "folder": folder_name,
            "added": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._save()
        return folder, False


# ---------------------------------------------------------------------------
# Core downloader
# ---------------------------------------------------------------------------

class PlaylistDownloader:
    def __init__(self, download_dir: Path, logger: logging.Logger, library_index: dict = None):
        self.download_dir = download_dir  # this playlist's own folder inside MUSIC_DIR
        self.logger = logger
        # {"by_id": {video_id: folder_name}, "by_title": {normalized_title: folder_name}}
        # covering every folder in the music library, not just this one — see
        # scan_music_library(). Used to catch the same song showing up in a
        # different playlist.
        self.library_index = library_index or {"by_id": {}, "by_title": {}}
        # None = ask about each ambiguous cross-playlist title match; once the
        # user picks "apply to all", this locks in True (skip) or False (keep)
        # for the rest of this run.
        self._cross_title_policy = None
        self.succeeded = []
        self.failed = []
        self.skipped = []
        self.duplicates = []

    # -- Duplicate detection (within this playlist's own folder) ------------
    def _scan_existing(self):
        """Looks at MP3s already sitting in this playlist's folder and builds
        two lookup sets: video IDs (exact match, from files this script
        downloaded before) and normalized titles (fallback match, so older
        files without an embedded ID still get recognized)."""
        existing_ids = set()
        existing_titles = set()

        if not self.download_dir.exists():
            return existing_ids, existing_titles

        for f in self.download_dir.glob("*.mp3"):
            match = _ID_PATTERN.search(f.name)
            if match:
                existing_ids.add(match.group(1))
            existing_titles.add(normalize_title(f.stem))

        return existing_ids, existing_titles

    def _progress_hook(self, d):
        """yt-dlp calls this during download; we use it just to log cleanly."""
        if d["status"] == "finished":
            filename = os.path.basename(d.get("filename", "unknown"))
            self.logger.info(f"  -> Converting: {filename}")
        elif d["status"] == "error":
            self.logger.error(f"  -> Error during download: {d}")

    def _ydl_opts(self, idx: int):
        return {
            "format": "bestaudio/best",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": AUDIO_QUALITY,
                }
            ],
            # Tracks are downloaded one at a time below (not through yt-dlp's
            # own playlist loop), so yt-dlp never has playlist context for a
            # given download and %(playlist_index)s resolves to "NA". We embed
            # the real position (idx, from our own loop) directly instead.
            # %(id)s is still embedded so future runs can detect this exact
            # track already exists and skip re-downloading it.
            "outtmpl": str(self.download_dir / f"{idx:02d} - %(title)s [%(id)s].%(ext)s"),
            "ignoreerrors": True,       # keep going if one track fails
            "no_warnings": False,
            "logger": self._YdlLoggerAdapter(self.logger),
            "progress_hooks": [self._progress_hook],
            "noplaylist": True,          # never let a single per-track call silently
                                          # expand into re-downloading a whole playlist
            "continuedl": True,         # resume partial downloads if re-run
            "retries": 3,
            "fragment_retries": 3,
            "sleep_interval": SLEEP_INTERVAL,          # pause before each
            "max_sleep_interval": MAX_SLEEP_INTERVAL,  # download (see above)
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

    @staticmethod
    def get_playlist_info(url: str, logger: logging.Logger):
        """Fetch playlist metadata without downloading, so we can show a
        preview, resolve the right folder, and catch bad URLs early."""
        opts = {"quiet": True, "extract_flat": True, "ignoreerrors": True}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                return info
        except yt_dlp.utils.DownloadError as e:
            logger.error(f"Could not read playlist info: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error reading playlist: {e}")
            return None

    def download(self, url: str, info: dict = None):
        self.download_dir.mkdir(parents=True, exist_ok=True)

        if info is None:
            self.logger.info("\nFetching playlist details...")
            info = self.get_playlist_info(url, self.logger)

        if info is None:
            self.logger.error(
                "Failed to fetch playlist. Check that the URL is correct, "
                "public, and not region/age-restricted."
            )
            return

        entries = info.get("entries", None)
        if entries is None:
            # It's a single track, not a playlist — handle it gracefully.
            # But if the URL clearly looks like a playlist link, this is a red
            # flag: yt-dlp likely failed to enumerate it fully, and blindly
            # proceeding here is exactly what caused mass re-downloads before.
            title = info.get("title", "this track")
            if "list=" in url or "playlist" in url.lower():
                self.logger.warning(
                    f"\nWarning: this looks like a playlist link, but only ONE item "
                    f"came back ('{title}') instead of the full track list. "
                    f"Downloading just this one item rather than guessing further — "
                    f"re-check the link (make sure it's not Private/restricted, and "
                    f"try copying it fresh from the playlist page itself rather than "
                    f"from a specific song within it)."
                )
            else:
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

        existing_ids, existing_titles = self._scan_existing()
        self.logger.info(f"Found {len(existing_ids | existing_titles)} track(s) already in the folder — these will be skipped.\n")

        for idx, entry in enumerate(entries, start=1):
            # Some playlist entries are None if a video was deleted/private
            if entry is None:
                self.skipped.append(f"Track {idx}: unavailable (deleted or private)")
                self.logger.warning(f"[{idx}/{total}] Skipped — video unavailable")
                continue

            video_url = entry.get("url") or entry.get("webpage_url") or entry.get("id")
            video_id = entry.get("id")
            title = entry.get("title") or f"Track {idx}"
            norm_title = normalize_title(title)

            # 1. Already in THIS playlist's folder — unchanged, original behavior.
            if (video_id and video_id in existing_ids) or (norm_title in existing_titles):
                self.logger.info(f"[{idx}/{total}] {title} — already downloaded here, skipping")
                self.duplicates.append(title)
                continue

            # 2. Exact same video already downloaded in a DIFFERENT playlist's
            #    folder — unambiguous (same video ID), always skip without asking.
            other_by_id = self.library_index["by_id"].get(video_id) if video_id else None
            if other_by_id and other_by_id != self.download_dir.name:
                self.logger.info(f"[{idx}/{total}] {title} — already in '{other_by_id}', skipping")
                self.duplicates.append(title)
                continue

            self.logger.info(f"[{idx}/{total}] {title}")

            # 3. Title matches something in a DIFFERENT folder, but it's a
            #    different video (different upload/version) — ambiguous, ask
            #    (unless a locked-in "apply to all" answer already covers it).
            other_by_title = self.library_index["by_title"].get(norm_title)
            if other_by_title and other_by_title != self.download_dir.name:
                if self._cross_title_policy is None:
                    skip_it = prompt_yes_no(
                        f"  -> '{title}' looks like it might already be downloaded "
                        f"(different video/upload) in '{other_by_title}'. Skip it here too?"
                    )
                    if prompt_yes_no("     Use that same answer for every other match like this in this run?"):
                        self._cross_title_policy = skip_it
                else:
                    skip_it = self._cross_title_policy

                if skip_it:
                    self.logger.info(f"  -> matches a title already in '{other_by_title}', skipping")
                    self.duplicates.append(title)
                    continue
                else:
                    self.logger.info(f"  -> matches a title in '{other_by_title}', downloading fresh anyway")

            last_error = None
            for attempt in range(1, TRACK_RETRIES + 1):
                try:
                    # A fresh YoutubeDL instance per track so its outtmpl
                    # carries this track's correct index (see _ydl_opts).
                    with yt_dlp.YoutubeDL(self._ydl_opts(idx)) as ydl:
                        ydl.download([video_url])
                    self.succeeded.append(title)
                    if video_id:
                        existing_ids.add(video_id)
                    existing_titles.add(norm_title)
                    last_error = None
                    break

                except yt_dlp.utils.DownloadError as e:
                    last_error = e
                    if attempt < TRACK_RETRIES:
                        wait = RETRY_WAIT_SECONDS * attempt
                        self.logger.warning(
                            f"  -> Attempt {attempt}/{TRACK_RETRIES} failed ({e}); "
                            f"this is often a temporary YouTube-side block — "
                            f"retrying in {wait}s..."
                        )
                        time.sleep(wait)
                    continue

                except KeyboardInterrupt:
                    self.logger.warning("\nInterrupted by user. Stopping early.")
                    raise

                except Exception as e:
                    last_error = e
                    break

            if last_error is not None:
                self.logger.error(f"  -> Failed after {TRACK_RETRIES} attempt(s): {last_error}")
                self.failed.append((title, str(last_error)))

        self._print_summary(total)

    def _print_summary(self, total: int):
        self.logger.info("\n" + "=" * 50)
        self.logger.info("SUMMARY")
        self.logger.info("=" * 50)
        self.logger.info(f"Succeeded:  {len(self.succeeded)}/{total}")
        self.logger.info(f"Duplicates: {len(self.duplicates)}/{total} (already had these)")
        self.logger.info(f"Failed:     {len(self.failed)}/{total}")
        self.logger.info(f"Skipped:    {len(self.skipped)}/{total}")

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
    print(f"yt-dlp version: {yt_dlp.version.__version__}")
    print("=" * 50)

    # URL: from command line arg, or prompt for it
    if len(sys.argv) >= 2:
        url = sys.argv[1].strip()
    else:
        url = input("\nPaste the playlist URL: ").strip()

    if not url:
        print("No URL provided. Exiting.")
        sys.exit(1)

    # Base "music" folder: from command line arg, or use the default.
    # Playlist subfolders are always created inside this base folder.
    if len(sys.argv) >= 3:
        music_dir = Path(sys.argv[2]).expanduser().resolve()
    else:
        music_dir = MUSIC_DIR

    # Console-only logger for now — we don't know which playlist folder (and
    # therefore which file log) this run belongs to until after we've fetched
    # the playlist's title and checked it against the log below.
    logger = setup_logging()
    logger.info("Fetching playlist details...")
    info = PlaylistDownloader.get_playlist_info(url, logger)

    if info is None:
        logger.error(
            "Failed to fetch playlist. Check that the URL is correct, "
            "public, and not region/age-restricted."
        )
        sys.exit(1)

    if info.get("entries") is None:
        # Single video, not a playlist — these all share one folder rather
        # than each getting a one-off folder of their own.
        playlist_dir = music_dir / STRAY_FOLDER_NAME
        playlist_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Single track (not a playlist) -> using '{STRAY_FOLDER_NAME}' folder")
    else:
        playlist_title = info.get("title") or "Untitled Playlist"
        library = PlaylistLibrary(music_dir)
        playlist_dir, matched = library.resolve(url, playlist_title)
        if matched:
            logger.info(f"Matched this link against the playlist log -> reusing folder '{playlist_dir.name}'")
        else:
            logger.info(f"New playlist link (no match in the log) -> created folder '{playlist_dir.name}'")

    # Now that the folder is settled, attach the per-playlist file log.
    logger = setup_logging(playlist_dir)

    # Look at every song already downloaded anywhere in the music library —
    # not just this folder — so a track that's already sitting in a
    # different playlist gets recognized instead of downloaded again.
    library_index = scan_music_library(music_dir)

    downloader = PlaylistDownloader(playlist_dir, logger, library_index)

    try:
        downloader.download(url, info=info)
    except KeyboardInterrupt:
        downloader._print_summary(len(downloader.succeeded) + len(downloader.failed) + len(downloader.skipped))
        sys.exit(0)
    except Exception as e:
        logger.error(f"\nFatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
