#!/usr/bin/env python3
"""Apply archive_url_status: token to every wiki/sources/*.md per classification.

Rules:
  pinned             - leave archive_url as-is, set status=pinned
  wayback-unpinned   - leave archive_url as-is (resolver needs the malformed URL), set status=wayback-unpinned
  needs-save         - clear archive_url to empty, set status=needs-save
  print-canonical    - clear archive_url to empty, set status=print-canonical
  video-no-wayback   - clear archive_url to empty, set status=video-no-wayback
  source-unknown     - clear archive_url to empty, set status=source-unknown

If archive_url_status: line already exists, overwrite its value with the token.
If it doesn't exist, insert it directly after the archive_url: line.
"""

import re
import sys
from pathlib import Path

SOURCES = Path.home() / "Obsidian" / "brains-claude" / "llm-wiki" / "wiki" / "sources"

RE_PINNED = re.compile(r"https?://web\.archive\.org/web/\d{14}/")
RE_UNPINNED_TIMESTAMPLESS = re.compile(r"https?://web\.archive\.org/web/https?://")
RE_UNPINNED_WILDCARD_YEAR = re.compile(r"https?://web\.archive\.org/web/\d{4}\*/")
RE_UNPINNED_WILDCARD_TIMESTAMP = re.compile(r"https?://web\.archive\.org/web/\d{14}\*/")
RE_UNPINNED_YEAR_ONLY = re.compile(r"https?://web\.archive\.org/web/\d{4}/https?://")

VIDEO_HINTS = ("youtube", "youtu.be", "transcript", "podcast")

# Manual classifications for ambiguous PDFs (pre-eyeballed)
MANUAL_OVERRIDES = {
    # print-canonical: born-print books / canonical reprints where PDF in raw/ is the durable record
    "summary-clark-natural-born-cyborgs.md": "print-canonical",
    "summary-haraway-cyborg-manifesto.md": "print-canonical",
    "summary-hayles-posthuman.md": "print-canonical",
    "summary-jaynes-bicameral-mind.md": "print-canonical",
    "summary-licklider-man-computer-symbiosis.md": "print-canonical",
    "summary-suchman-human-machine-reconfigurations.md": "print-canonical",
    "summary-turkle-alone-together.md": "print-canonical",
    "summary-turkle-second-self.md": "print-canonical",
    "summary-wiener-cybernetics-1948.md": "print-canonical",
    "summary-wiener-human-use-human-beings.md": "print-canonical",
    "summary-lessig-code-1999.md": "print-canonical",
    # needs-save: web-hosted PDFs with known canonical source URL, no Wayback yet
    "summary-abelson-report-mit-swartz.md": "needs-save",
    "summary-bencsath-territorial-dispute-2018.md": "needs-save",
    "summary-boulanin-sipri-strategic-stability-2019.md": "needs-save",
    "summary-goldman-cfaa-interpretation-2012.md": "needs-save",
    "summary-rivera-escalation-risks-2024.md": "needs-save",
    "summary-shaw-nave-tri-system-cognition.md": "needs-save",
    "summary-vanishing-culture-ia-2026.md": "needs-save",
    "summary-widder-west-whittaker-open-for-business-2023.md": "needs-save",
    "summary-us-v-swartz-superseding.md": "needs-save",
    # wayback-unpinned: Wayback URL with year-only or 14-digit-plus-wildcard form (regex misses)
    "summary-edwards-closed-world.md": "wayback-unpinned",
    "summary-khlaaf-myers-west-whittaker-mind-the-gap-2024.md": "wayback-unpinned",
    # source-unknown: source_url is descriptive text or unknown (added 2026-05-01 to keep script idempotent)
    "summary-morozov-socialize-data-centres.md": "source-unknown",  # source_url is NLR 91 print citation, not URL
    "summary-openai-industrial-policy-2026.md": "source-unknown",  # source_url literally "UNVERIFIED — PDF acquired via external download..."
    "summary-swartz-ss-boston-foia-reports.md": "source-unknown",  # source_url is internal USSS Outlook Web Access path, not public
    "summary-lerchner-abstraction-fallacy-2026.md": "source-unknown",  # UUID-named PDF; canonical source_url path needs reconstruction
}


def parse_frontmatter_fields(text):
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not m:
        return {}, None
    fm = m.group(1)
    fields = {}
    current = None
    buffer = []
    for line in fm.split("\n"):
        if re.match(r"^[a-z_]+:", line):
            if current is not None:
                fields[current] = "\n".join(buffer).strip()
            key, _, val = line.partition(":")
            current = key.strip()
            buffer = [val.strip()]
        else:
            buffer.append(line)
    if current is not None:
        fields[current] = "\n".join(buffer).strip()
    return fields, m.end()


def classify_auto(fields):
    archive_url = fields.get("archive_url", "").strip().strip('"').strip("'")
    source_file = fields.get("source_file", "").strip()
    existing_status = fields.get("archive_url_status", "").strip().lower()
    haystack = f"{source_file} {fields.get('source_url', '')} {existing_status}".lower()

    is_empty = (
        archive_url == ""
        or archive_url.lower() == "null"
        or archive_url.startswith("FLAG")
        or archive_url.startswith("MISSING")
        or archive_url.startswith("(not in raw")
    )

    if not is_empty and RE_PINNED.search(archive_url):
        return "pinned"
    if not is_empty and (
        RE_UNPINNED_TIMESTAMPLESS.search(archive_url)
        or RE_UNPINNED_WILDCARD_YEAR.search(archive_url)
        or RE_UNPINNED_WILDCARD_TIMESTAMP.search(archive_url)
        or RE_UNPINNED_YEAR_ONLY.search(archive_url)
    ):
        return "wayback-unpinned"
    if "malformed" in existing_status or "latest snapshot" in existing_status:
        return "wayback-unpinned"
    if any(h in haystack for h in VIDEO_HINTS) or "youtube" in existing_status:
        return "video-no-wayback"
    if is_empty:
        return "needs-save"  # default; PDFs are overridden manually above
    return None  # unmatched


def rewrite_frontmatter_fields(text, token):
    """Rewrite archive_url + archive_url_status in the frontmatter block.

    For non-URL tokens (needs-save, print-canonical, video-no-wayback, source-unknown):
      archive_url:  (empty value)
    For URL tokens (pinned, wayback-unpinned):
      archive_url:  (preserved as-is)

    archive_url_status: <token> goes immediately after archive_url:.
    """
    m = re.match(r"^(---\n)(.*?)(\n---\n)", text, re.DOTALL)
    if not m:
        return text, "no-frontmatter"
    head, fm, tail = m.group(1), m.group(2), m.group(3)
    rest = text[m.end():]

    lines = fm.split("\n")
    out = []
    archive_url_idx = None
    status_existing_idx = None
    # Multi-line values: we want to find the START of archive_url: line
    for i, line in enumerate(lines):
        if re.match(r"^archive_url:", line):
            archive_url_idx = i
        elif re.match(r"^archive_url_status:", line):
            status_existing_idx = i

    if archive_url_idx is None:
        # Insert archive_url: + archive_url_status: after source_url: (or at end)
        insert_at = len(lines)
        for i, line in enumerate(lines):
            if re.match(r"^source_url:", line):
                # find end of source_url multi-line
                k = i
                for j in range(i + 1, len(lines)):
                    if re.match(r"^[a-z_]+:", lines[j]):
                        break
                    k = j
                insert_at = k + 1
                break
        if token in ("pinned", "wayback-unpinned"):
            url_line = "archive_url:"
        else:
            url_line = "archive_url:"
        new_lines = lines[:insert_at] + [url_line, f"archive_url_status: {token}"] + lines[insert_at:]
        new_fm = "\n".join(new_lines)
        return head + new_fm + tail + rest, "ok"

    # Determine end-line of archive_url: value (handle multi-line continuation)
    end = archive_url_idx
    for j in range(archive_url_idx + 1, len(lines)):
        if re.match(r"^[a-z_]+:", lines[j]):
            break
        end = j

    # Build new archive_url line(s)
    if token in ("pinned", "wayback-unpinned"):
        # Preserve existing value verbatim — collapse multi-line if any
        new_archive_lines = lines[archive_url_idx:end + 1]
    else:
        # Clear to empty
        new_archive_lines = ["archive_url:"]

    new_status_line = f"archive_url_status: {token}"

    # Splice
    before = lines[:archive_url_idx]
    after = lines[end + 1:]

    # Remove existing archive_url_status line if present elsewhere
    if status_existing_idx is not None:
        # Adjust index in 'after' if it was after archive_url
        if status_existing_idx > end:
            adj = status_existing_idx - (end + 1)
            after = after[:adj] + after[adj + 1:]
        elif status_existing_idx < archive_url_idx:
            # before
            before = before[:status_existing_idx] + before[status_existing_idx + 1:]
        # else: it was inside the archive_url range — already consumed

    new_lines = before + new_archive_lines + [new_status_line] + after
    new_fm = "\n".join(new_lines)
    return head + new_fm + tail + rest, "ok"


def main():
    dry_run = "--apply" not in sys.argv
    counts = {}
    changes = []
    errors = []

    for path in sorted(SOURCES.glob("*.md")):
        if path.name == "index.md":
            continue
        text = path.read_text()
        fields, _ = parse_frontmatter_fields(text)
        if fields.get("type", "").strip() != "source":
            continue
        token = MANUAL_OVERRIDES.get(path.name) or classify_auto(fields)
        if token is None:
            errors.append((path.name, "unclassified"))
            continue
        counts[token] = counts.get(token, 0) + 1
        new_text, status = rewrite_frontmatter_fields(text, token)
        if status != "ok":
            errors.append((path.name, status))
            continue
        if new_text != text:
            changes.append((path.name, token))
            if not dry_run:
                path.write_text(new_text)

    print(f"\n=== {'DRY RUN' if dry_run else 'APPLIED'} ===")
    for token in sorted(counts, key=lambda k: -counts[k]):
        print(f"  {counts[token]:4d}  {token}")
    print(f"  {sum(counts.values()):4d}  TOTAL classified")
    print(f"  {len(changes):4d}  files changed")
    if errors:
        print(f"\n=== ERRORS ({len(errors)}) ===")
        for name, err in errors:
            print(f"  {name}: {err}")


if __name__ == "__main__":
    main()
