# PDF Clipper

CLI counterpart to the Obsidian Web Clipper for PDFs. Downloads a PDF + writes a markdown sidecar with the **same frontmatter shape** as the HTML clipper template, so the existing ingest workflow doesn't need a new code path.

## Why this exists

The Obsidian Web Clipper handles HTML pages but doesn't clip PDFs. Without a parallel tool, PDFs land in `raw/papers/` with no `source` URL, no `archived` Wayback URL, no `published` date — provenance is lost at acquisition time.

`pdf-clip` fills that gap. One command per PDF; output is symmetric with HTML clippings.

## Install

Already done if your machine has the sibling `web-archive-clipper` working. The script depends on:

- **Python 3.12 venv** at `~/Github/python3.12-venv/` with `requests` + `python-frontmatter`
- **poppler** (`pdfinfo`, `pdftotext`) on PATH — `brew install poppler`
- **macOS Keychain** items `ia-spn2-access-key` and `ia-spn2-secret-key` (same items the resolver uses; see `../run_resolve_archive.sh`)

Symlink onto PATH for convenience:

```bash
ln -s ~/Github/obsidian/pdf-clipper/pdf-clip ~/bin/pdf-clip
```

## Usage

```bash
pdf-clip <pdf-url> [options]
```

Options:

- `--slug <slug>` — override auto-generated filename slug (omit `.pdf` extension). Required when the PDF has no `Title` metadata.
- `--dest <dir>` — destination directory (default: `~/Obsidian/brains-claude/llm-wiki/raw/papers`).
- `--no-spn2` — skip the Wayback Save-Page-Now submission. Sidecar carries a bare `https://web.archive.org/web/<url>` redirect URL; the daily resolver picks it up later.
- `--editor` — open the sidecar in `$EDITOR` after writing for human verification.

## What it does, in order

1. Fetch IA SPN2 keys from Keychain into the environment (so the sibling resolver's `wayback_save()` helper can authenticate).
2. Download the PDF to `<dest>/<slug>.pdf`.
3. Run `pdfinfo` to extract Title / Author / Subject / Pages / CreationDate; `pdftotext -l 1` for a description fallback if the PDF carries no Subject.
4. Determine slug: `slugify(Title)` by default, or `--slug` override. Collision check refuses overwrite — re-run with a different slug instead.
5. Submit `clean_url(source_url)` to Wayback via the resolver's `wayback_save()` helper:
   - First try `wayback_check()` — if a snapshot already exists, use it.
   - Otherwise authenticated POST to SPN2 + poll until terminal.
   - If save fails (paywall / Cloudflare / origin unreachable / rate-limit), write a bare `web.archive.org/web/<url>` URL into the sidecar — the daily resolver retries on its own schedule.
6. Write `<dest>/<slug>.md` sidecar with frontmatter matching `web-archive-clipper/template.json` exactly.
7. Optionally open in `$EDITOR` for verification.

## Frontmatter shape

Identical to the HTML clipper output (`web-archive-clipper/template.json`):

```yaml
---
source: <pdf-url>                  # URL the user provided, verbatim
archived: <wayback-url>            # pinned timestamped URL on success; bare redirect on fallback
clipped: YYYY-MM-DD                # today, UTC
published: YYYY-MM-DD              # extracted from PDF CreationDate (may be empty)
author: <pdf-Author>               # from pdfinfo (may be empty)
site: <netloc-of-source-url>       # e.g. "arxiv.org"
description: <pdf-Subject>         # falls back to first-page-text if Subject empty
tags:
  - clipping
---

# <pdf-Title>

PDF: [[<slug>.pdf]]
```

The `source_file:` field in the wiki summary points at the sidecar's stem (or the `.pdf`); ingest reads frontmatter from the sidecar same as it would for a `raw/articles/*.md` HTML clipping.

## Failure modes (deliberate)

- **Slug collision** → fail loudly. Never silently overwrite anything in `raw/papers/`.
- **No Title metadata + no `--slug`** → fail with instruction to re-run with `--slug`. Prevents UUID-named PDFs from landing as `311CAEC6-3CD1-...md` (the case that motivated `archive_url_status: source-unknown`).
- **SPN2 fails** (paywall / Cloudflare / 523 / 429) → write sidecar with bare Wayback redirect URL. Daily `resolve_archive_urls.py` will retry it on its `--retry-after 7` schedule. No new code path; reuses the existing failure-recovery loop.
- **`Content-Type` is not PDF** → warn but proceed (some servers misreport).
- **Keychain miss + not `--no-spn2`** → fail at startup with rotation instructions.

## Relation to sibling tools

| Tool | Input | Output | Wayback |
|---|---|---|---|
| Obsidian Web Clipper extension | HTML page in browser | `raw/articles/*.md` with frontmatter | bare redirect URL |
| `web-archive-clipper/resolve_archive_urls.py` | scans `*.md` for bare URLs | upgrades `archived:` to timestamped | authenticated SPN2 |
| `pdf-clipper/pdf-clip` (this) | PDF URL via CLI | `raw/papers/<slug>.pdf` + `<slug>.md` sidecar | authenticated SPN2 (or falls back to bare for resolver) |

The three together provide symmetric provenance for HTML, PDF, and previously-clipped material.

## Smoke test

```bash
pdf-clip https://nicholas.carlini.com/writing/2025/are-llms-worth-it.html  # not a PDF — should warn
pdf-clip https://arxiv.org/pdf/2401.03408v1                                # arXiv PDF — should succeed
```

After: `ls ~/Obsidian/brains-claude/llm-wiki/raw/papers/` should show the new pair, and the sidecar's `archived:` field should be a timestamped Wayback URL.
