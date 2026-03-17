import yt_dlp
import re
from pathlib import Path
from datetime import datetime
from youtube_transcript_api import YouTubeTranscriptApi

OUTPUT_DIR    = Path("/Users/sarahnovotny/brains-claude/Videos")
RESOURCES_DIR = OUTPUT_DIR / "_resources"
PLAYLIST_URL  = "https://www.youtube.com/playlist?list=PLTMG0vH9I0QBZ0uJHn12j5CCO4oHwJFJ9"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
RESOURCES_DIR.mkdir(parents=True, exist_ok=True)

# Instantiate once, reuse for all videos
_ytt = YouTubeTranscriptApi()

def safe_filename(s):
    return re.sub(r'[\\/*?:"<>|]', "", s).strip()[:100]

def get_transcript(video_id):
    try:
        fetched = _ytt.fetch(video_id)
        return " ".join(s.text for s in fetched.snippets)
    except Exception as e:
        return f"_Transcript error: {e}_"

def format_duration(seconds):
    if not seconds:
        return ""
    h, remainder = divmod(int(seconds), 3600)
    m, s = divmod(remainder, 60)
    return f"{h}H{m:02d}M" if h else f"{m}M{s:02d}S"

def download_video(video_id, filename_stem):
    """Download video to _resources, return the relative Obsidian path or None on failure."""
    out_template = str(RESOURCES_DIR / f"{filename_stem}.%(ext)s")
    ydl_opts = {
        "quiet": True,
        "format": "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl": out_template,
        "remote_components": ["ejs:github"],
        "cookiesfrombrowser": ("chrome",),
        "merge_output_format": "mp4",
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
            ext = info.get("ext", "mp4")
            local_path = RESOURCES_DIR / f"{filename_stem}.{ext}"
            # Return path relative to the Videos/ note directory for Obsidian embeds
            return f"_resources/{filename_stem}.{ext}"
    except Exception as e:
        print(f"    Video download failed: {e}", flush=True)
        return None

COMMON_OPTS = {
    "quiet": True,
    "skip_download": True,
    "remote_components": ["ejs:github"],
    "cookiesfrombrowser": ("chrome",),
}

# ── Pass 1: flat fetch — just get IDs, nothing can crash ─────────────────────
print("Fetching playlist video IDs...", flush=True)
with yt_dlp.YoutubeDL({**COMMON_OPTS, "extract_flat": True}) as ydl:
    playlist_info = ydl.extract_info(PLAYLIST_URL, download=False)

entries   = playlist_info.get("entries", [])
video_ids = [e["id"] for e in entries if e and e.get("id")]
print(f"Found {len(video_ids)} videos. Processing...\n", flush=True)

skipped = 0
written = 0

# ── Pass 2: one ydl instance for metadata, separate download per video ───────
with yt_dlp.YoutubeDL({**COMMON_OPTS, "extract_flat": False}) as ydl:
    for i, video_id in enumerate(video_ids, 1):
        url = f"https://www.youtube.com/watch?v={video_id}"
        print(f"[{i}/{len(video_ids)}] Fetching metadata for {video_id}...", flush=True)

        try:
            video = ydl.extract_info(url, download=False)
        except Exception as e:
            print(f"[{i}/{len(video_ids)}] SKIPPING {video_id}: {e}\n", flush=True)
            skipped += 1
            continue

        if not video:
            skipped += 1
            continue

        title       = video.get("title", "Untitled")
        author      = video.get("uploader", video.get("channel", "Unknown"))
        duration    = format_duration(video.get("duration"))
        thumbnail   = video.get("thumbnail", "")
        description = video.get("description", "")
        upload_date = video.get("upload_date", "")
        published   = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}" if upload_date else ""

        note_name = safe_filename(f"{published} {author} - {title}")
        created   = datetime.now().strftime("%Y-%m-%dT%H:%M:%S+00:00")

        print(f"[{i}/{len(video_ids)}] {title}", flush=True)

        print(f"[{i}/{len(video_ids)}]   → Downloading video...", flush=True)
        video_rel_path = download_video(video_id, note_name)
        if video_rel_path:
            video_embed = f"![[{video_rel_path}]]"
            print(f"[{i}/{len(video_ids)}]   → Downloaded: {video_rel_path}", flush=True)
        else:
            video_embed = f"> Video unavailable locally. [Watch on YouTube]({url})"

        print(f"[{i}/{len(video_ids)}]   → Fetching transcript...", flush=True)
        transcript = get_transcript(video_id)

        content = f"""---
created: {created}
url: {url}
title: {title}
channel: {author}
published: {published}
thumbnailUrl: {thumbnail}
duration: {duration}
reviewed: 
tags:
  - video/youtube
related:
  - "[[Videos]]"
watched: 
status: 
---

## About

type:: #type/video/youtube

{video_embed}

## Description

{description}

## Notes



## Transcript

{transcript}
"""
        output_path = OUTPUT_DIR / f"{note_name}.md"
        output_path.write_text(content, encoding="utf-8")
        print(f"[{i}/{len(video_ids)}]   → Written: {note_name}.md\n", flush=True)
        written += 1

print(f"Done! {written} notes written, {skipped} skipped.")
