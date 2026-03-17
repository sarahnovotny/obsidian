# YouTube Transcript Clipper

Two ways to capture YouTube videos as Obsidian notes with transcripts: a browser extension template for one-at-a-time clipping, and a Python script for bulk downloading entire playlists.

## Option 1: Browser template (single videos)

Use the [Obsidian Web Clipper](https://obsidian.md/clipper) template to clip individual YouTube videos while you're watching them.

### Install

1. Open the Obsidian Web Clipper extension settings (gear icon)
2. Go to any template and click **Import** (top right)
3. Drag and drop `template.json`

### Usage

Navigate to any YouTube video, **open the transcript panel first** (click "Show transcript" under the video description), then click the Web Clipper extension. The template auto-triggers on `youtube.com/watch` URLs.

The transcript is captured directly from the open transcript panel in the browser, so it must be visible when you clip. This also means you get exactly what YouTube shows — including any manual or corrected captions.

### Note format

Filename: `YYYY-MM-DD VIDEO <author> - <title>`

| Frontmatter | Content |
|---|---|
| `url` | YouTube watch URL |
| `title` | Video title |
| `channel` | Channel name |
| `published` | Upload date |
| `thumbnailUrl` | Thumbnail image URL |
| `duration` | Video duration |
| `tags` | `video/youtube` |
| `created` | Clip timestamp |

The note body includes an embedded video player, description, and transcript.

## Option 2: Python script (bulk playlists)

Download metadata, transcripts, and optionally video files for every video in a YouTube playlist.

### Install

```bash
pip install yt-dlp youtube-transcript-api
```

### Usage

```bash
# Download everything (videos + notes)
python obsidian_capture_yt_transcript.py "https://www.youtube.com/playlist?list=..." ~/vault/Videos

# Notes only — no video file downloads
python obsidian_capture_yt_transcript.py --no-download "https://www.youtube.com/playlist?list=..." ~/vault/Videos
```

Video files are saved to an `_resources/` subdirectory and embedded in the notes with Obsidian's `![[...]]` syntax. If a video can't be downloaded, the note falls back to a YouTube link.

### Note format

Filename: `YYYY-MM-DD <channel> - <title>`

The script produces the same frontmatter fields as the browser template. Transcripts are fetched via the YouTube Transcript API (auto-generated captions).

## Differences between the two

| | Browser template | Python script |
|---|---|---|
| **Scope** | One video at a time | Entire playlists |
| **Transcript source** | Browser DOM (transcript panel) | YouTube Transcript API |
| **Video files** | No | Yes (optional) |
| **Requires** | Obsidian Web Clipper extension | Python + yt-dlp |
| **Best for** | Clipping as you browse | Archiving a playlist in bulk |
