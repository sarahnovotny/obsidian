# Obsidian Web Clipper + Archive Resolver

Two tools that work together: a browser extension template clips web pages into Obsidian, and a Python script resolves the archive URLs to stable timestamped snapshots.

## Files

- `templates/web-archive-clipper.json` — Obsidian Web Clipper template. Captures articles with frontmatter (`source`, `archived`, `clipped`, `published`, `author`, `site`, `description`, `tags`). The `archived` field starts as a bare Wayback redirect URL.
- `resolve_archive_urls.py` — Resolves bare `archived:` URLs into real timestamped Wayback snapshots. Uses authenticated SPN2 API (POST), falls back to archive.ph/archive.today.
- `run_resolve_archive.sh` — Wrapper script for launchd (contains IA API keys, gitignored).

## Key paths

- Python venv: `~/Github/python3.12-venv/`
- Vault clippings: `~/brains-claude/Clippings-archive/`
- State file: `~/brains-claude/Clippings-archive/.archive_resolver_state.json`
- Log file: `~/brains-claude/Clippings-archive/.archive_resolver.log`
- Launch agent: `~/Library/LaunchAgents/com.sarahnovotny.archive-resolver.plist`

## resolve_archive_urls.py

Dependencies: `requests`, `python-frontmatter` (not `frontmatter` — different package).

Flags: `--dry-run`, `--check-only`, `--all`, `--retry-failed`, `--retry-after DAYS`, `--log`

State file tracks each note as `resolved`, `failed` (auto-retries after 7 days), or `blocked` (permanent, never retried). The `--log` flag appends to a rolling log file keeping the last 10 runs.

Runs daily at 10:00 AM via launchd.

## Known limitations

- X/Twitter URLs are permanently blocked by Wayback (`error:blocked-url`) and archive.ph
- archive.ph rate-limits aggressively (HTTP 429); archive.is and archive.fo have SSL issues — only archive.ph and archive.today are attempted
- Wayback availability API returns `http://` URLs (not `https://`) — `is_resolved()` handles both

## Potential next steps

- Surface `archived` as a clickable link in Obsidian via CSS snippet or Dataview query
- Dataview dashboard showing unresolved clips
- Store both `archived_wayback` and `archived_ph` as separate fields for redundancy
