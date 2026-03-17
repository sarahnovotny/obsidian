# Web Archive Clipper

Clip web pages into Obsidian with automatic archiving. An [Obsidian Web Clipper](https://obsidian.md/clipper) template captures articles with a Wayback Machine URL, and a Python script resolves those into stable timestamped snapshots.

## How it works

1. You clip an article using the browser extension. The template saves the page content as a markdown note with an `archived` frontmatter field pointing to `https://web.archive.org/web/<url>` — a bare redirect that works immediately but isn't a permanent link.

2. `resolve_archive_urls.py` scans your clippings folder and upgrades those bare URLs into real timestamped snapshot URLs (e.g. `https://web.archive.org/web/20260317154348/https://example.com`). If no snapshot exists, it submits one via the Wayback Machine's Save Page Now API.

## Setup

### 1. Install the template

1. Open the Obsidian Web Clipper extension settings (gear icon)
2. Go to any template and click **Import** (top right)
3. Drag and drop `template.json`

### 2. Install the resolver script

```bash
pip install requests python-frontmatter
```

You'll need an Internet Archive account with API keys. Get them at https://archive.org/account/s3.php, then export them:

```bash
export IA_ACCESS_KEY="your-access-key"
export IA_SECRET_KEY="your-secret-key"
```

### 3. Run

```bash
# Standard run
python resolve_archive_urls.py ~/path/to/vault/Clippings

# Dry run — see what would change without writing files
python resolve_archive_urls.py --dry-run ~/path/to/vault/Clippings

# Only check for existing snapshots, never submit new ones
python resolve_archive_urls.py --check-only ~/path/to/vault/Clippings

# Write output to a rolling log file (keeps last 10 runs)
python resolve_archive_urls.py --log ~/path/to/vault/Clippings

# Retry URLs that previously failed
python resolve_archive_urls.py --retry-failed ~/path/to/vault/Clippings

# Re-resolve everything (ignores state file)
python resolve_archive_urls.py --all ~/path/to/vault/Clippings
```

### 4. Automate (macOS)

A template launchd plist is included at [`com.example.archive-resolver.plist`](com.example.archive-resolver.plist). To use it:

1. Create a wrapper script that exports your API keys and runs the resolver:
   ```bash
   #!/bin/bash
   export IA_ACCESS_KEY="your-access-key"
   export IA_SECRET_KEY="your-secret-key"
   /path/to/python /path/to/resolve_archive_urls.py --log /path/to/vault/Clippings
   ```
2. Update the paths in the plist to match your setup
3. Copy the plist to `~/Library/LaunchAgents/`
4. Load it: `launchctl load ~/Library/LaunchAgents/com.example.archive-resolver.plist`

## State tracking

The script writes a `.archive_resolver_state.json` file to your clippings directory. This tracks each note as:

- **resolved** — has a timestamped snapshot URL, skipped on future runs
- **failed** — archiving didn't work, auto-retried after 7 days (configurable with `--retry-after`)
- **blocked** — the URL is permanently blocked by archive services (e.g. X/Twitter), never retried

This means the script stays fast as your archive grows — it only processes new or unresolved clippings.

## Frontmatter

The template captures these fields per note:

| Field | Content |
|---|---|
| `source` | Original page URL |
| `archived` | Wayback Machine snapshot URL (resolved by the script) |
| `clipped` | Date you clipped the article |
| `published` | Page publication date (when available) |
| `author` | Page author |
| `site` | Site/publisher name |
| `description` | Page description/excerpt |
| `tags` | `clipping` (default) |

## Known limitations

- **X/Twitter URLs** are permanently blocked by both Wayback Machine and archive.ph
- **archive.ph** rate-limits automated submissions aggressively — it's used as a fallback but is unreliable
- The Wayback availability API returns `http://` URLs; the script handles both `http` and `https` schemes
