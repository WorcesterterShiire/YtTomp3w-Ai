Playlist to MP3 Downloader (also works with single videos with a dedicated folder for it called stray)

A small Python command-line tool that takes a playlist link and saves each track as an MP3. It is a wrapper around yt-dlp and FFmpeg, built as a personal learning project in Python scripting, media processing, and file organization.

Features
  Downloads every track in a playlist as an MP3 (highest VBR quality by default)
  Gives each playlist its own folder, named after the playlist
  Remembers which link maps to which folder (playlists_log.json), so re-running a playlist only downloads new tracks
  Detects duplicates across playlists, by video ID (automatic skip) and by title (asks you first)
  Retries failed tracks with a growing delay, with randomized pauses between downloads
  Writes a per-playlist download_log.txt
  Loops back to the start screen after each run so you can process another link

Requirements
Python 3.8+
yt-dlp
FFmpeg installed and on your PATH

to install yt-dlp : pip install -U yt-dlp
ffmpeg link : https://www.gyan.dev/ffmpeg/builds/
deno link   : https://github.com/denoland/deno/releases


Works with a playlist link, it will download everything in the list. 
These is some media such as ffmpeg release esssentials and deno_msvc_zip ->  put Deno in the same spot as ffmpeg exe which is in bin
Put the path in environment variable under user variable and edit path and put it there.
example path: C:\ffmpeg-9.0.1-essentials_build\ffmpeg-9.0.1-essentials_build\bin

REMINDER!!! When downloading from a playlist make sure it is the playlist link itself
Run this on you cmd if you have an **outdated version** of deno or ffmpeg : pip install -U yt-dlp yt-dlp-ejs

RECOMENDED to run the script 2-3 times to make sure it has installed everything in the playlist
------------------------------------------------------------------------------------------------
DISCLAIMER

This project is for educational purposes: it demonstrates working with yt-dlp, FFmpeg, and Python file handling. It is not intended to encourage or enable copyright infringement.

Use it only on content you own, content in the public domain, content licensed for download (for example Creative Commons), or content you have the creator's permission to download.
Downloading from some platforms may violate their Terms of Service, and copyright law in your country may restrict copying protected works. You are solely responsible for how you use this software.
This project is not affiliated with, endorsed by, or sponsored by YouTube, Google, or any other platform.
The software is provided "as is", without warranty of any kind, and the author accepts no liability for misuse.
License

MIT. See LICENSE.
