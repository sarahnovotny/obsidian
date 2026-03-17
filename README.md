# Obsidian Tooling

Automation tools for [Obsidian](https://obsidian.md), built around the [Obsidian Web Clipper](https://obsidian.md/clipper) browser extension.

## [Web Archive Clipper](web-archive-clipper/)

Clip web articles into Obsidian with automatic archiving. A browser extension template captures pages with a Wayback Machine URL, and a Python script resolves those into stable timestamped snapshots using the Internet Archive's Save Page Now API. Includes state tracking for incremental runs and a launchd plist for daily automation.

## [YouTube Transcript Clipper](youtube-transcript-clipper/)

Capture YouTube videos as Obsidian notes with transcripts. Two options: a browser extension template for clipping individual videos as you watch them, and a Python script for bulk downloading entire playlists with metadata, transcripts, and optionally the video files themselves.

## License

[MIT](LICENSE)
