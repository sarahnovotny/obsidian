# Obsidian Tooling

Obsidian browser extension templates and supporting scripts, organized by tool.

## Structure

```
web-archive-clipper/
  template.json          — Web Clipper template (captures articles with archive URL)
  resolve_archive_urls.py — Resolves bare Wayback URLs to timestamped snapshots
youtube-transcript-clipper/
  template.json          — Web Clipper template (captures YouTube videos with transcript)
  obsidian_capture_yt_transcript.py — Fetches and formats YouTube transcripts
run_resolve_archive.sh   — Wrapper script for launchd (contains IA API keys, gitignored)
```

## Key paths

- Python venv: `~/Github/python3.12-venv/`
- Vault clippings: `~/brains-claude/Clippings-archive/`
- State file: `~/brains-claude/Clippings-archive/.archive_resolver_state.json`
- Log file: `~/brains-claude/Clippings-archive/.archive_resolver.log`
- Launch agent: `~/Library/LaunchAgents/com.sarahnovotny.archive-resolver.plist`

## web-archive-clipper

The template captures articles with frontmatter (`source`, `archived`, `clipped`, `published`, `author`, `site`, `description`, `tags`). The `archived` field starts as a bare Wayback redirect URL.

`resolve_archive_urls.py` resolves those into real timestamped Wayback snapshots using the authenticated SPN2 API (POST). URLs are cleaned before archiving (fragments, auth tokens, and tracking params like `utm_*` are stripped).

Dependencies: `requests`, `python-frontmatter` (not `frontmatter` — different package).

Flags: `--dry-run`, `--check-only`, `--all`, `--retry-failed`, `--retry-after DAYS`, `--log`

State file tracks each note as `resolved`, `failed` (auto-retries after 7 days), or `blocked` (permanent, never retried). The `--log` flag appends to a rolling log file keeping the last 10 runs.

Runs daily at 10:00 AM via launchd.

### Known limitations

- X/Twitter URLs are permanently blocked by Wayback (`error:blocked-url`)
- Wayback's SPN2 browser sometimes crashes on certain sites — these fail and auto-retry after 7 days
- Wayback availability API returns `http://` URLs (not `https://`) — `is_resolved()` handles both
- Double-wrapped Wayback URLs (clipping a page that's already archived) are detected and resolved directly

## Potential next steps

- Surface `archived` as a clickable link in Obsidian via CSS snippet or Dataview query
- Dataview dashboard showing unresolved clips
