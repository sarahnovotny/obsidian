#!/usr/bin/env python3
"""
resolve_archive_urls.py

Scans an Obsidian clippings folder for notes whose `archived` frontmatter
field contains a bare Wayback Machine lookup URL and replaces it with a real
timestamped snapshot URL.

Resolution order (always attempted by default):
  1. Wayback Machine availability check (existing snapshot)
  2. Wayback Machine Save Page Now v2 (authenticated POST, polls job status)
  3. archive.ph / archive.today (tries each mirror in turn)

Authentication:
    Set IA_ACCESS_KEY and IA_SECRET_KEY environment variables with your
    Internet Archive S3-style API keys.  Get them at:
    https://archive.org/account/s3.php

State file:
    A .archive_resolver_state.json file is written to the clippings directory
    to track resolved and failed URLs.  Subsequent runs skip files that have
    already been resolved or that failed recently (within --retry-after days).

Usage:
    python resolve_archive_urls.py /path/to/vault/Clippings

Options:
    --dry-run       Show what would change without writing files or state
    --check-only    Only look for existing snapshots; never submit new ones
    --all           Re-resolve notes that already have a resolved URL
    --retry-failed  Retry URLs that previously failed
    --retry-after N Retry failures older than N days (default: 7)

Requirements:
    pip install requests python-frontmatter
"""

import argparse
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import frontmatter  # pip install python-frontmatter

# ── Constants ────────────────────────────────────────────────────────────────

WAYBACK_AVAIL_API    = "https://archive.org/wayback/available"
WAYBACK_SPN2_SAVE    = "https://web.archive.org/save"
WAYBACK_SPN2_STATUS  = "https://web.archive.org/save/status"

ARCHIVE_PH_HOSTS = [
    "archive.ph",
    "archive.today",
]

STATE_FILENAME = ".archive_resolver_state.json"
LOG_FILENAME   = ".archive_resolver.log"
LOG_MAX_RUNS   = 10
LOG_SEPARATOR  = "\n" + "═" * 60 + "\n"

BARE_LOOKUP_RE = re.compile(
    r"^https://web\.archive\.org/web/(?!(?:19|20)\d{12}/)(.+)$"
)
ARCHIVE_PH_SNAP_RE = re.compile(
    r"^https://archive\.(ph|today|is|fo)/[A-Za-z0-9]+$"
)
ARCHIVE_PH_JOB_RE = re.compile(
    r"https://archive\.(ph|today|is|fo)/submit/[A-Za-z0-9]+"
)

CONNECT_TIMEOUT     = 10   # seconds
READ_TIMEOUT        = 30   # seconds
REQUEST_DELAY       = 1.5  # seconds between files
PH_POLL_INTERVAL    = 6    # seconds between archive.ph status checks
PH_POLL_MAX         = 10   # max polls (~60 s total)
SPN2_POLL_TRIES     = 12   # polls after a queued save
SPN2_POLL_SLEEP     = 5    # seconds between SPN2 status polls

DEFAULT_RETRY_AFTER_DAYS = 7


# ── State file ───────────────────────────────────────────────────────────────

def setup_logging(clippings_dir: Path) -> None:
    """Redirect stdout and stderr to a log file, keeping only the last N runs."""
    log_path = clippings_dir / LOG_FILENAME

    # Read existing log and trim to last (N-1) runs so this run makes N
    existing = ""
    if log_path.exists():
        try:
            existing = log_path.read_text()
        except OSError:
            pass

    if existing:
        runs = existing.split(LOG_SEPARATOR)
        runs = [r for r in runs if r.strip()]
        if len(runs) >= LOG_MAX_RUNS:
            runs = runs[-(LOG_MAX_RUNS - 1):]
        existing = LOG_SEPARATOR.join(runs) + LOG_SEPARATOR

    header = f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}]\n"

    # Write trimmed history + header, then append this run's output
    log_file = open(log_path, "w")
    log_file.write(existing + header)
    log_file.flush()

    sys.stdout = log_file
    sys.stderr = log_file


def load_state(clippings_dir: Path) -> dict:
    state_path = clippings_dir / STATE_FILENAME
    if state_path.exists():
        try:
            return json.loads(state_path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            print(f"⚠  Could not read state file: {exc}", file=sys.stderr)
    return {}


def save_state(clippings_dir: Path, state: dict) -> None:
    state_path = clippings_dir / STATE_FILENAME
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def state_key(path: Path) -> str:
    return path.name


def should_skip(state: dict, key: str, retry_failed: bool, retry_after_days: int) -> str | None:
    """Return a skip reason string, or None if the file should be processed."""
    entry = state.get(key)
    if not entry:
        return None
    status = entry.get("status")
    if status == "resolved":
        return "resolved"
    if status == "blocked":
        return "blocked"
    if status == "failed" and not retry_failed:
        failed_at = entry.get("failed_at", "")
        if failed_at:
            try:
                failed_dt = datetime.fromisoformat(failed_at)
                age_days = (datetime.now(timezone.utc) - failed_dt).days
                if age_days < retry_after_days:
                    return f"failed {age_days}d ago (retry after {retry_after_days}d)"
            except ValueError:
                pass
        else:
            return "failed (no timestamp)"
    return None


# ── Helpers ──────────────────────────────────────────────────────────────────

def is_resolved(url: str) -> bool:
    if ARCHIVE_PH_SNAP_RE.match(url):
        return True
    if re.match(r"^https?://web\.archive\.org/web/(?:19|20)\d{12}/", url):
        return True
    return False


def extract_original_url(archived: str) -> str | None:
    m = BARE_LOOKUP_RE.match(archived)
    return m.group(1) if m else None


def get_ia_auth() -> dict | None:
    access = os.environ.get("IA_ACCESS_KEY", "")
    secret = os.environ.get("IA_SECRET_KEY", "")
    if access and secret:
        return {"Authorization": f"LOW {access}:{secret}"}
    return None


# ── Wayback Machine ──────────────────────────────────────────────────────────

def wayback_check(original_url: str) -> str | None:
    """Check Wayback availability API for an existing snapshot."""
    try:
        resp = requests.get(
            WAYBACK_AVAIL_API,
            params={"url": original_url},
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        )
        resp.raise_for_status()
        closest = resp.json().get("archived_snapshots", {}).get("closest", {})
        if closest.get("available"):
            return closest["url"]
    except requests.RequestException as exc:
        print(f"    ⚠  Wayback check error: {exc}", file=sys.stderr)
    return None


def wayback_save(original_url: str) -> str | None:
    """
    Submit a save request via Wayback SPN2 (authenticated POST).
    Polls the job status endpoint until complete or timed out.
    """
    auth = get_ia_auth()
    if not auth:
        print("    ⚠  No IA credentials — set IA_ACCESS_KEY and IA_SECRET_KEY")
        return None

    # Submit the save request
    try:
        resp = requests.post(
            WAYBACK_SPN2_SAVE,
            data={"url": original_url, "capture_all": "1"},
            headers={**auth, "Accept": "application/json"},
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        )
    except requests.RequestException as exc:
        print(f"    ⚠  SPN2 save request error: {exc}", file=sys.stderr)
        return None

    if resp.status_code != 200:
        print(f"    ⚠  SPN2 returned HTTP {resp.status_code}: {resp.text[:200]}")
        return None

    # Check Content-Type — SPN2 sometimes returns HTML instead of JSON
    content_type = resp.headers.get("Content-Type", "")
    if "json" not in content_type:
        # Check for a redirect to a snapshot URL in the response
        if "/web/" in resp.url and resp.url != WAYBACK_SPN2_SAVE:
            return resp.url
        print(f"    ⚠  SPN2 returned non-JSON ({content_type}): {resp.text[:200]}")
        return None

    try:
        data = resp.json()
    except ValueError:
        print(f"    ⚠  SPN2 response not parseable: {resp.text[:200]}")
        return None

    # Check for blocked-url error
    if data.get("status") == "error":
        status_ext = data.get("status_ext", "")
        msg = data.get("message", "unknown error")
        if "blocked" in status_ext:
            print(f"    ⚠  URL is blocked by Wayback: {msg}")
            return "BLOCKED"
        print(f"    ⚠  SPN2 error: {msg}")
        return None

    job_id = data.get("job_id")
    if not job_id:
        # Sometimes the response contains the URL directly
        url = data.get("url")
        if url:
            return f"https://web.archive.org/web/{data.get('timestamp', '')}/{url}"
        print(f"    ⚠  SPN2 returned no job_id: {data}", file=sys.stderr)
        return None

    print(f"    Save job queued: {job_id}")

    # Poll the status endpoint
    for i in range(SPN2_POLL_TRIES):
        time.sleep(SPN2_POLL_SLEEP)
        print(f"    ⏳ SPN2 polling… ({i + 1}/{SPN2_POLL_TRIES})")
        try:
            status_resp = requests.get(
                WAYBACK_SPN2_STATUS + f"/{job_id}",
                headers=auth,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )
            status_resp.raise_for_status()
            status_data = status_resp.json()
        except requests.RequestException as exc:
            print(f"    ⚠  SPN2 status poll error: {exc}", file=sys.stderr)
            continue

        status = status_data.get("status")
        if status == "success":
            ts = status_data.get("timestamp", "")
            orig = status_data.get("original_url", original_url)
            return f"https://web.archive.org/web/{ts}/{orig}"
        elif status == "error":
            msg = status_data.get("message", "unknown error")
            print(f"    ⚠  SPN2 save failed: {msg}")
            return None
        # else "pending" — keep polling

    print("    ⚠  SPN2 save timed out waiting for completion.")
    return None


# ── archive.ph (with mirror fallback) ────────────────────────────────────────

_PH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def _parse_ph_response(resp: requests.Response) -> str | None:
    """Extract a snapshot or job URL from an archive.ph response."""
    if ARCHIVE_PH_SNAP_RE.match(resp.url):
        return resp.url
    for header in ("Refresh", "Location"):
        val = resp.headers.get(header, "")
        m = re.search(r"(https://archive\.(?:ph|today|is|fo)/[A-Za-z0-9]+)$", val)
        if m:
            return m.group(1)
    m = re.search(r"https://archive\.(?:ph|today|is|fo)/(?:submit/)?[A-Za-z0-9]+",
                  resp.text[:4000])
    return m.group(0) if m else None


def _poll_ph_job(job_url: str) -> str | None:
    for i in range(PH_POLL_MAX):
        time.sleep(PH_POLL_INTERVAL)
        print(f"    ⏳ archive.ph processing… ({i + 1}/{PH_POLL_MAX})")
        try:
            resp = requests.get(
                job_url, headers=_PH_HEADERS,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                allow_redirects=True,
            )
            result = _parse_ph_response(resp)
            if result and not ARCHIVE_PH_JOB_RE.match(result):
                return result
        except requests.RequestException:
            pass
    return None


def archive_ph_save(original_url: str) -> str | None:
    """Try each archive.ph mirror in turn until one succeeds."""
    for host in ARCHIVE_PH_HOSTS:
        submit_url = f"https://{host}/submit/"
        print(f"    Trying {host} …")
        try:
            resp = requests.post(
                submit_url,
                data={"url": original_url, "anyway": "1"},
                headers=_PH_HEADERS,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                allow_redirects=True,
            )
        except requests.Timeout:
            print(f"    ⚠  {host} timed out — trying next mirror.", file=sys.stderr)
            continue
        except requests.RequestException as exc:
            print(f"    ⚠  {host} error: {exc}", file=sys.stderr)
            continue

        result = _parse_ph_response(resp)
        if not result:
            print(f"    ⚠  {host} returned unexpected response (HTTP {resp.status_code}).",
                  file=sys.stderr)
            continue

        if ARCHIVE_PH_JOB_RE.match(result):
            return _poll_ph_job(result)
        return result

    return None


# ── Core logic ───────────────────────────────────────────────────────────────

def resolve(original_url: str, check_only: bool) -> tuple[str | None, str]:
    url = wayback_check(original_url)
    if url:
        return url, "Wayback (existing)"

    if check_only:
        return None, ""

    print("    No existing snapshot — trying Wayback Save-Now …")
    result = wayback_save(original_url)
    if result == "BLOCKED":
        return None, "blocked"
    if result:
        return result, "Wayback (saved)"

    print("    Wayback save failed — trying archive.ph mirrors …")
    url = archive_ph_save(original_url)
    if url:
        return url, "archive.ph"

    return None, ""


def process_file(path: Path, dry_run: bool, check_only: bool, reprocess_all: bool) -> str | None:
    """Returns the snapshot URL on success, or None on failure/skip."""
    try:
        post = frontmatter.load(str(path))
    except Exception as exc:
        print(f"  ⚠  Could not parse {path.name}: {exc}", file=sys.stderr)
        return None

    archived: str = post.get("archived", "")
    if not archived:
        return None

    if is_resolved(archived) and not reprocess_all:
        return None

    original_url = extract_original_url(archived)
    if not original_url:
        return None

    print(f"  → {original_url}")

    snapshot_url, source = resolve(original_url, check_only)

    if source == "blocked":
        print("    ✗ URL is permanently blocked from archiving.")
        return "BLOCKED"

    if not snapshot_url:
        print("    ✗ All archiving methods failed for this URL.")
        return None

    print(f"    ✓ [{source}] {snapshot_url}")

    if not dry_run:
        post["archived"] = snapshot_url
        with open(path, "wb") as f:
            frontmatter.dump(post, f)

    return snapshot_url


def scan_vault(
    clippings_dir: Path,
    dry_run: bool,
    check_only: bool,
    reprocess_all: bool,
    retry_failed: bool,
    retry_after_days: int,
) -> None:
    md_files = sorted(clippings_dir.rglob("*.md"))
    if not md_files:
        print(f"No markdown files found in {clippings_dir}")
        return

    state = load_state(clippings_dir)
    updated = skipped = failed = blocked = 0

    for path in md_files:
        key = state_key(path)

        # Check state file — skip already-resolved or recently-failed
        skip_reason = should_skip(state, key, retry_failed, retry_after_days)
        if skip_reason and not reprocess_all:
            skipped += 1
            continue

        # Pre-check: if already resolved in frontmatter, record in state and skip
        try:
            post = frontmatter.load(str(path))
            archived = post.get("archived", "")
        except Exception:
            archived = ""

        if archived and is_resolved(archived) and not reprocess_all:
            if key not in state and not dry_run:
                state[key] = {
                    "status": "resolved",
                    "url": archived,
                    "resolved_at": datetime.now(timezone.utc).isoformat(),
                }
            skipped += 1
            continue

        print(f"\n📄 {path.name}")
        snapshot_url = process_file(path, dry_run, check_only, reprocess_all)

        if snapshot_url == "BLOCKED":
            blocked += 1
            if not dry_run:
                state[key] = {
                    "status": "blocked",
                    "url": archived,
                    "blocked_at": datetime.now(timezone.utc).isoformat(),
                }
        elif snapshot_url:
            updated += 1
            if not dry_run:
                state[key] = {
                    "status": "resolved",
                    "url": snapshot_url,
                    "resolved_at": datetime.now(timezone.utc).isoformat(),
                }
            time.sleep(REQUEST_DELAY)
        else:
            if archived and not is_resolved(archived):
                failed += 1
                if not dry_run:
                    state[key] = {
                        "status": "failed",
                        "url": archived,
                        "failed_at": datetime.now(timezone.utc).isoformat(),
                    }
            else:
                skipped += 1

    if not dry_run:
        save_state(clippings_dir, state)

    mode = " (dry run)" if dry_run else ""
    print(f"\n{'─' * 50}")
    parts = [f"{updated} resolved", f"{skipped} skipped"]
    if blocked:
        parts.append(f"{blocked} blocked")
    parts.append(f"{failed} failed")
    print(f"Done{mode}: {', '.join(parts)}.")


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Resolve bare Wayback URLs in Obsidian clippings. "
            "Uses authenticated SPN2 API for saves. "
            "Tracks state to skip already-processed files."
        )
    )
    parser.add_argument("clippings_dir", type=Path,
                        help="Path to your Obsidian Clippings folder")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without writing files or state")
    parser.add_argument("--check-only", action="store_true",
                        help="Only check for existing snapshots; never submit new ones")
    parser.add_argument("--all", action="store_true", dest="reprocess_all",
                        help="Re-resolve all notes (ignores state file)")
    parser.add_argument("--retry-failed", action="store_true",
                        help="Retry URLs that previously failed")
    parser.add_argument("--retry-after", type=int, default=DEFAULT_RETRY_AFTER_DAYS,
                        metavar="DAYS",
                        help=f"Auto-retry failures older than N days (default: {DEFAULT_RETRY_AFTER_DAYS})")
    parser.add_argument("--log", action="store_true",
                        help="Write output to .archive_resolver.log in the clippings dir (keeps last 10 runs)")
    args = parser.parse_args()

    clippings_dir = args.clippings_dir.expanduser().resolve()
    if not clippings_dir.is_dir():
        print(f"Error: {clippings_dir} is not a directory.", file=sys.stderr)
        sys.exit(1)

    if args.log:
        setup_logging(clippings_dir)

    # Check for IA credentials
    if not get_ia_auth() and not args.check_only:
        print("⚠  IA_ACCESS_KEY / IA_SECRET_KEY not set — Wayback saves will be skipped.",
              file=sys.stderr)
        print("   Get keys at: https://archive.org/account/s3.php\n", file=sys.stderr)

    print(f"Scanning: {clippings_dir}")
    if args.dry_run:
        print("(dry-run mode — no files or state will be written)")
    if args.check_only:
        print("(check-only mode — no new archives will be submitted)")
    print()

    scan_vault(
        clippings_dir,
        args.dry_run,
        args.check_only,
        args.reprocess_all,
        args.retry_failed,
        args.retry_after,
    )


if __name__ == "__main__":
    main()
