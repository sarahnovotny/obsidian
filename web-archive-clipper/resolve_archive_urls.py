#!/usr/bin/env python3
"""
resolve_archive_urls.py

Scans an Obsidian clippings folder for notes whose `archived` frontmatter
field contains a bare Wayback Machine lookup URL and replaces it with a real
timestamped snapshot URL.

Resolution order:
  1. Wayback Machine availability check (existing snapshot)
  2. Wayback Machine Save Page Now v2 (authenticated POST, polls job status)

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
    --allow-save    Arm live Wayback Save-Now submissions (default: check-only,
                     only look for existing snapshots; never submit new ones)
    --check-only    No-op alias kept for compatibility; check-only is now default
    --all           Re-resolve notes that already have a resolved URL
    --retry-failed  Retry URLs that previously failed
    --retry-after N Retry failures older than N days (default: 7)
    --from-source   Derive a Wayback lookup URL from `source:` when `archived` is empty
    --max-files N   Stop after processing N files this run (default: 0, unlimited)

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
from urllib.parse import parse_qs, urldefrag, urlparse, urlencode, urlunparse

import requests
import frontmatter  # pip install python-frontmatter

# ── Constants ────────────────────────────────────────────────────────────────

WAYBACK_AVAIL_API    = "https://archive.org/wayback/available"
WAYBACK_SPN2_SAVE    = "https://web.archive.org/save"
WAYBACK_SPN2_STATUS  = "https://web.archive.org/save/status"

STATE_FILENAME = ".archive_resolver_state.json"
LOG_FILENAME   = ".archive_resolver.log"
LOG_MAX_RUNS   = 10
LOG_SEPARATOR  = "\n" + "═" * 60 + "\n"

BARE_LOOKUP_RE = re.compile(
    r"^https://web\.archive\.org/web/(?!(?:19|20)\d{12}/)(.+)$"
)
CONNECT_TIMEOUT     = 10   # seconds
READ_TIMEOUT        = 30   # seconds
REQUEST_DELAY       = 6    # seconds between files (archive.org rate-limits aggressively)
SPN2_POLL_TRIES     = 12   # polls after a queued save
SPN2_POLL_SLEEP     = 5    # seconds between SPN2 status polls

# Query parameters to strip from URLs before archiving
STRIP_PARAMS = {
    # Auth/session tokens
    "accessToken", "access_token", "id_token", "token", "auth",
    # UTM tracking
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    # Common referral/tracking
    "ref", "referer", "referrer", "fbclid", "gclid", "mc_cid", "mc_eid",
}

DEFAULT_RETRY_AFTER_DAYS = 7


# ── State file ───────────────────────────────────────────────────────────────

def setup_logging(clippings_dir: Path) -> None:
    """Redirect stdout and stderr to a log file, keeping only the last N runs."""
    log_path = clippings_dir / LOG_FILENAME

    # Read existing log and trim to last (N-1) runs so this run makes N.
    # Use errors='replace' so a previously-truncated UTF-8 character in the log
    # doesn't crash the next run (precedent: clippings-archive log mid-character
    # cut at byte 13887 caused 200+ hourly failures before being caught).
    existing = ""
    if log_path.exists():
        try:
            existing = log_path.read_text(encoding="utf-8", errors="replace")
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
    if re.match(r"^https?://web\.archive\.org/web/(?:19|20)\d{12}/", url):
        return True
    if re.match(r"^https?://archive\.(ph|today|is|fo)/[A-Za-z0-9]+$", url):
        return True
    return False


def clean_url(url: str) -> str:
    """Strip fragments (may contain auth tokens) and tracking/auth query params."""
    url, _ = urldefrag(url)
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    cleaned = {k: v for k, v in params.items() if k not in STRIP_PARAMS}
    new_query = urlencode(cleaned, doseq=True) if cleaned else ""
    return urlunparse(parsed._replace(query=new_query))


def extract_original_url(archived: str) -> str | None:
    m = BARE_LOOKUP_RE.match(archived)
    if not m:
        return None
    return clean_url(m.group(1))


def is_already_archived(url: str) -> bool:
    """Check if a URL is itself a Wayback or archive.ph snapshot."""
    if re.match(r"^https?://web\.archive\.org/web/", url):
        return True
    if re.match(r"^https?://archive\.(ph|today|is|fo)/", url):
        return True
    return False


def synthesize_lookup_url(source: str | None) -> str | None:
    """Build a bare Wayback lookup URL from a note's `source:` field.

    Returns None when there is nothing usable to build from, so callers can
    treat "no source" and "source is itself an archive link" identically.
    """
    if not source or not isinstance(source, str):
        return None
    source = source.strip()
    if not source.startswith(("http://", "https://")):
        return None
    if is_already_archived(source):
        return None
    return f"https://web.archive.org/web/{source}"


def effective_archived(post, from_source: bool) -> str:
    """The `archived` value to act on for this note.

    Normally the frontmatter value verbatim. With --from-source, an empty or
    absent `archived` falls back to a lookup URL synthesized from `source:`.
    An existing value is never overwritten.
    """
    archived = post.get("archived", "") or ""
    if archived or not from_source:
        return archived
    return synthesize_lookup_url(post.get("source", "")) or ""


def get_ia_auth() -> dict | None:
    access = os.environ.get("IA_ACCESS_KEY", "")
    secret = os.environ.get("IA_SECRET_KEY", "")
    if access and secret:
        return {"Authorization": f"LOW {access}:{secret}"}
    return None


# ── Wayback Machine ──────────────────────────────────────────────────────────

class RateLimited(Exception):
    """archive.org kept throttling us after the full backoff schedule."""


class TransientCheckError(Exception):
    """A per-URL failure that says nothing about whether a snapshot exists.

    Timeouts, connection failures and 403s must never be recorded as a
    definitive "no snapshot", because `failed` state rows are suppressed for
    DEFAULT_RETRY_AFTER_DAYS.
    """


RATE_LIMIT_STATUSES = frozenset({429, 503})
RATE_LIMIT_BACKOFF = (15, 45, 120)  # seconds to wait before each retry


def wayback_check(original_url: str) -> str | None:
    """Check the Wayback availability API for an existing snapshot.

    Returns the snapshot URL, or None when archive.org answers cleanly that it
    has nothing. Raises RateLimited when archive.org throttles us past the whole
    backoff schedule, so the caller can stop the run rather than record every
    remaining file as a permanent failure.
    """
    for attempt in range(len(RATE_LIMIT_BACKOFF) + 1):
        try:
            resp = requests.get(
                WAYBACK_AVAIL_API,
                params={"url": original_url},
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )
            if resp.status_code in RATE_LIMIT_STATUSES:
                if attempt < len(RATE_LIMIT_BACKOFF):
                    delay = RATE_LIMIT_BACKOFF[attempt]
                    print(f"    ⏳ rate-limited ({resp.status_code}); retrying in {delay}s")
                    time.sleep(delay)
                    continue
                raise RateLimited(
                    f"archive.org returned {resp.status_code} after "
                    f"{len(RATE_LIMIT_BACKOFF) + 1} attempts"
                )
            if resp.status_code != 200:
                raise TransientCheckError(
                    f"archive.org returned {resp.status_code}; treating as unknown"
                )
            try:
                closest = resp.json().get("archived_snapshots", {}).get("closest", {})
                if closest.get("available"):
                    return closest["url"]
                return None
            except (AttributeError, KeyError, TypeError) as exc:
                raise TransientCheckError(
                    f"unexpected availability response shape: {exc}"
                ) from exc
        except requests.RequestException as exc:
            raise TransientCheckError(f"{type(exc).__name__}: {exc}") from exc
    return None


def wayback_save(original_url: str) -> str | None:
    """
    Submit a save request via Wayback SPN2 (authenticated POST).
    Polls the job status endpoint until complete or timed out.

    Returns a snapshot URL, the "BLOCKED" sentinel for a definitive Wayback
    block, or None for the one case where SPN2 explicitly reports it could
    not archive the URL. Every other failure mode — missing credentials,
    connection/request errors, a non-200 response, a non-JSON body, or a
    poll timeout — says nothing about whether the URL is archivable, so it
    raises TransientCheckError instead. A wrongly recorded "failed" row is
    suppressed for DEFAULT_RETRY_AFTER_DAYS, so a wrong negative is expensive.
    """
    auth = get_ia_auth()
    if not auth:
        print("    ⚠  No IA credentials — set IA_ACCESS_KEY and IA_SECRET_KEY")
        raise TransientCheckError("no IA credentials configured")

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
        raise TransientCheckError(f"{type(exc).__name__}: {exc}") from exc

    if resp.status_code != 200:
        print(f"    ⚠  SPN2 returned HTTP {resp.status_code}: {resp.text[:200]}")
        raise TransientCheckError(f"SPN2 returned HTTP {resp.status_code}")

    # Check Content-Type — SPN2 sometimes returns HTML instead of JSON
    content_type = resp.headers.get("Content-Type", "")
    if "json" not in content_type:
        # Check for a redirect to a snapshot URL in the response
        if "/web/" in resp.url and resp.url != WAYBACK_SPN2_SAVE:
            return resp.url
        print(f"    ⚠  SPN2 returned non-JSON ({content_type}): {resp.text[:200]}")
        raise TransientCheckError(f"SPN2 returned non-JSON response ({content_type})")

    try:
        data = resp.json()
    except ValueError as exc:
        print(f"    ⚠  SPN2 response not parseable: {resp.text[:200]}")
        raise TransientCheckError(f"SPN2 response not parseable: {exc}") from exc

    # Check for blocked-url error. This — and the "error" verdict returned
    # from the status poll below — are the one case where SPN2 has
    # explicitly reported it could not (or would not) archive the URL: a
    # genuine, definitive verdict, not a transient failure.
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
        raise TransientCheckError(f"SPN2 returned no job_id: {data}")

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
    raise TransientCheckError("SPN2 save timed out waiting for completion")


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

    return None, ""


def process_file(path: Path, dry_run: bool, check_only: bool, reprocess_all: bool,
                 from_source: bool = False) -> str | None:
    """Returns the snapshot URL on success, or None on failure/skip."""
    try:
        post = frontmatter.load(str(path))
    except Exception as exc:
        print(f"  ⚠  Could not parse {path.name}: {exc}", file=sys.stderr)
        return None

    archived: str = effective_archived(post, from_source)
    if not archived:
        return None

    if is_resolved(archived) and not reprocess_all:
        return None

    original_url = extract_original_url(archived)
    if not original_url:
        return None

    # If the "original" URL is itself already an archive snapshot (double-wrapped),
    # use it directly as the resolved URL instead of trying to re-archive it
    if is_already_archived(original_url):
        print(f"  → {original_url}")
        print(f"    ✓ [already archived] {original_url}")
        if not dry_run:
            post["archived"] = original_url
            with open(path, "wb") as f:
                frontmatter.dump(post, f)
        return original_url

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
    from_source: bool,
    max_files: int = 0,
) -> None:
    md_files = sorted(clippings_dir.rglob("*.md"))
    if not md_files:
        print(f"No markdown files found in {clippings_dir}")
        return

    state = load_state(clippings_dir)
    updated = skipped = failed = blocked = 0
    processed = 0

    for path in md_files:
        # --max-files caps forward-progress processing (not state-skipped
        # files) so an hourly run doesn't spend its whole budget rechecking
        # the head of the alphabet and never reach the tail.
        if max_files and processed >= max_files:
            print(f"\n⏹  Reached --max-files {max_files}; stopping for this run.")
            break

        key = state_key(path)

        # Check state file — skip already-resolved or recently-failed
        skip_reason = should_skip(state, key, retry_failed, retry_after_days)
        if skip_reason and not reprocess_all:
            skipped += 1
            continue

        # Pre-check: if already resolved in frontmatter, record in state and skip
        try:
            post = frontmatter.load(str(path))
            archived = effective_archived(post, from_source)
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

        processed += 1
        print(f"\n📄 {path.name}")
        try:
            snapshot_url = process_file(path, dry_run, check_only, reprocess_all, from_source)
        except RateLimited as exc:
            print(f"\n⛔ {exc}")
            print("   Stopping here so the remaining files are not recorded as failures.")
            print("   Progress so far has been saved — re-run to continue where this left off.")
            if not dry_run:
                save_state(clippings_dir, state)
            return
        except TransientCheckError as exc:
            print(f"    ↻ {exc}")
            print("      Leaving unrecorded so it is retried next run.")
            time.sleep(REQUEST_DELAY)
            continue

        if snapshot_url == "BLOCKED":
            blocked += 1
            if not dry_run:
                state[key] = {
                    "status": "blocked",
                    "url": archived,
                    "blocked_at": datetime.now(timezone.utc).isoformat(),
                }
            time.sleep(REQUEST_DELAY)
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
                time.sleep(REQUEST_DELAY)  # rate-limit even after failures
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

def build_arg_parser() -> argparse.ArgumentParser:
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
                        help="No-op: check-only is now the default. Kept so existing "
                             "callers keep working unchanged; use --allow-save to "
                             "arm live Save-Now submissions.")
    parser.add_argument("--allow-save", action="store_true", dest="allow_save",
                        help="Allow submitting new Wayback Save-Now requests. "
                             "Without this flag, only existing snapshots are checked "
                             "(this is now the default behaviour).")
    parser.add_argument("--all", action="store_true", dest="reprocess_all",
                        help="Re-resolve all notes (ignores state file)")
    parser.add_argument("--retry-failed", action="store_true",
                        help="Retry URLs that previously failed")
    parser.add_argument("--retry-after", type=int, default=DEFAULT_RETRY_AFTER_DAYS,
                        metavar="DAYS",
                        help=f"Auto-retry failures older than N days (default: {DEFAULT_RETRY_AFTER_DAYS})")
    parser.add_argument("--from-source", action="store_true", dest="from_source",
                        help="When `archived` is empty, synthesize a Wayback lookup "
                             "URL from the note's `source:` field")
    parser.add_argument("--max-files", type=int, default=0, dest="max_files",
                        metavar="N",
                        help="Stop after processing N files this run (0 = unlimited). "
                             "State-skipped files don't count, so each run makes "
                             "forward progress through the vault instead of "
                             "rechecking the same head of the alphabet.")
    parser.add_argument("--log", action="store_true",
                        help="Write output to .archive_resolver.log in the clippings dir (keeps last 10 runs)")
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    # --allow-save arms live SPN2 submissions; its absence is check-only.
    # --check-only is accepted as a no-op alias for compatibility.
    check_only = not args.allow_save

    clippings_dir = args.clippings_dir.expanduser().resolve()
    if not clippings_dir.is_dir():
        print(f"Error: {clippings_dir} is not a directory.", file=sys.stderr)
        sys.exit(1)

    if args.log:
        setup_logging(clippings_dir)

    # Check for IA credentials
    if not get_ia_auth() and not check_only:
        print("⚠  IA_ACCESS_KEY / IA_SECRET_KEY not set — Wayback saves will be skipped.",
              file=sys.stderr)
        print("   Get keys at: https://archive.org/account/s3.php\n", file=sys.stderr)

    print(f"Scanning: {clippings_dir}")
    if args.dry_run:
        print("(dry-run mode — no files or state will be written)")
    if check_only:
        print("(check-only mode — no new archives will be submitted)")
    print()

    scan_vault(
        clippings_dir,
        args.dry_run,
        check_only,
        args.reprocess_all,
        args.retry_failed,
        args.retry_after,
        args.from_source,
        args.max_files,
    )


if __name__ == "__main__":
    main()
