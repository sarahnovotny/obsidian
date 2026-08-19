import json

import frontmatter
import pytest

import resolve_archive_urls as rau
from resolve_archive_urls import (
    RateLimited,
    TransientCheckError,
    clean_url,
    effective_archived,
    extract_original_url,
    synthesize_lookup_url,
    wayback_check,
)


def test_synthesize_builds_bare_lookup_url():
    assert synthesize_lookup_url("https://example.com/a") == \
        "https://web.archive.org/web/https://example.com/a"


def test_synthesize_rejects_empty_and_non_http():
    assert synthesize_lookup_url("") is None
    assert synthesize_lookup_url(None) is None
    assert synthesize_lookup_url("not-a-url") is None
    assert synthesize_lookup_url("ftp://example.com/x") is None


def test_synthesize_rejects_an_already_archived_url():
    assert synthesize_lookup_url("https://web.archive.org/web/20240101000000/https://x.com") is None


def test_synthesized_url_round_trips_through_extract():
    original = "https://example.com/a?utm_source=news&keep=1"
    round_tripped = extract_original_url(synthesize_lookup_url(original))
    assert round_tripped == clean_url(original)


def _post(**metadata):
    p = frontmatter.Post("body")
    p.metadata.update(metadata)
    return p


def test_effective_archived_returns_existing_value_untouched():
    post = _post(archived="https://web.archive.org/web/https://example.com/a",
                 source="https://example.com/other")
    assert effective_archived(post, from_source=True) == \
        "https://web.archive.org/web/https://example.com/a"


def test_effective_archived_is_inert_when_flag_is_off():
    post = _post(archived=None, source="https://example.com/a")
    assert effective_archived(post, from_source=False) == ""


def test_effective_archived_synthesizes_when_flag_is_on():
    post = _post(archived=None, source="https://example.com/a")
    assert effective_archived(post, from_source=True) == \
        "https://web.archive.org/web/https://example.com/a"


def test_effective_archived_handles_missing_keys():
    assert effective_archived(_post(), from_source=True) == ""
    assert effective_archived(_post(source="https://example.com/a"), from_source=True) == \
        "https://web.archive.org/web/https://example.com/a"


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise rau.requests.HTTPError(f"{self.status_code}")


def test_wayback_check_returns_snapshot_url_on_success(monkeypatch):
    payload = {"archived_snapshots": {"closest": {"available": True,
                                                  "url": "https://web.archive.org/web/20240101000000/https://x.com"}}}
    monkeypatch.setattr(rau.requests, "get", lambda *a, **k: _Resp(200, payload))
    assert wayback_check("https://x.com") == \
        "https://web.archive.org/web/20240101000000/https://x.com"


def test_wayback_check_retries_then_succeeds_on_429(monkeypatch):
    calls = {"n": 0}
    payload = {"archived_snapshots": {"closest": {"available": True, "url": "https://web.archive.org/web/1/x"}}}

    def fake_get(*a, **k):
        calls["n"] += 1
        return _Resp(429) if calls["n"] == 1 else _Resp(200, payload)

    monkeypatch.setattr(rau.requests, "get", fake_get)
    monkeypatch.setattr(rau.time, "sleep", lambda s: None)
    assert wayback_check("https://x.com") == "https://web.archive.org/web/1/x"
    assert calls["n"] == 2


def test_wayback_check_raises_ratelimited_when_throttling_persists(monkeypatch):
    monkeypatch.setattr(rau.requests, "get", lambda *a, **k: _Resp(429))
    monkeypatch.setattr(rau.time, "sleep", lambda s: None)
    with pytest.raises(RateLimited):
        wayback_check("https://x.com")


def test_wayback_check_treats_503_as_rate_limiting(monkeypatch):
    monkeypatch.setattr(rau.requests, "get", lambda *a, **k: _Resp(503))
    monkeypatch.setattr(rau.time, "sleep", lambda s: None)
    with pytest.raises(RateLimited):
        wayback_check("https://x.com")


def test_ratelimited_backoff_schedule_is_15_45_120_over_4_attempts(monkeypatch):
    sleeps = []
    calls = {"n": 0}

    def fake_get(*a, **k):
        calls["n"] += 1
        return _Resp(429)

    monkeypatch.setattr(rau.requests, "get", fake_get)
    monkeypatch.setattr(rau.time, "sleep", lambda s: sleeps.append(s))

    with pytest.raises(RateLimited):
        wayback_check("https://x.com")

    assert sleeps == [15, 45, 120]
    assert calls["n"] == 4


def test_ordinary_http_error_is_transient_not_definitive(monkeypatch):
    monkeypatch.setattr(rau.requests, "get", lambda *a, **k: _Resp(404))
    monkeypatch.setattr(rau.time, "sleep", lambda s: None)
    with pytest.raises(TransientCheckError):
        wayback_check("https://x.com")


def test_3xx_status_is_transient_not_a_definitive_none(monkeypatch):
    # A 3xx with a JSON-ish body used to slip past `>= 400` and reach the
    # extraction, producing a definitive None. It must not.
    monkeypatch.setattr(rau.requests, "get",
                        lambda *a, **k: _Resp(301, {"archived_snapshots": {}}))
    monkeypatch.setattr(rau.time, "sleep", lambda s: None)
    with pytest.raises(TransientCheckError):
        wayback_check("https://x.com")


def test_timeout_is_transient_not_a_definitive_negative(monkeypatch):
    def boom(*a, **k):
        raise rau.requests.ReadTimeout("too slow")
    monkeypatch.setattr(rau.requests, "get", boom)
    monkeypatch.setattr(rau.time, "sleep", lambda s: None)
    with pytest.raises(TransientCheckError):
        wayback_check("https://x.com")


def test_connection_error_is_transient(monkeypatch):
    def boom(*a, **k):
        raise rau.requests.ConnectionError("no route")
    monkeypatch.setattr(rau.requests, "get", boom)
    monkeypatch.setattr(rau.time, "sleep", lambda s: None)
    with pytest.raises(TransientCheckError):
        wayback_check("https://x.com")


def test_403_is_transient(monkeypatch):
    monkeypatch.setattr(rau.requests, "get", lambda *a, **k: _Resp(403))
    monkeypatch.setattr(rau.time, "sleep", lambda s: None)
    with pytest.raises(TransientCheckError):
        wayback_check("https://x.com")


def test_clean_no_snapshot_is_still_a_definitive_none(monkeypatch):
    monkeypatch.setattr(rau.requests, "get",
                        lambda *a, **k: _Resp(200, {"archived_snapshots": {}}))
    assert wayback_check("https://x.com") is None


# ── BLOCKING 1: well-formed 200 with unexpected JSON shape ─────────────────

def test_wayback_check_raises_transient_when_archived_snapshots_is_null(monkeypatch):
    monkeypatch.setattr(rau.requests, "get",
                        lambda *a, **k: _Resp(200, {"archived_snapshots": None}))
    monkeypatch.setattr(rau.time, "sleep", lambda s: None)
    with pytest.raises(TransientCheckError):
        wayback_check("https://x.com")


def test_wayback_check_raises_transient_when_closest_has_no_url(monkeypatch):
    payload = {"archived_snapshots": {"closest": {"available": True}}}
    monkeypatch.setattr(rau.requests, "get", lambda *a, **k: _Resp(200, payload))
    monkeypatch.setattr(rau.time, "sleep", lambda s: None)
    with pytest.raises(TransientCheckError):
        wayback_check("https://x.com")


def test_wayback_check_raises_transient_on_list_body(monkeypatch):
    monkeypatch.setattr(rau.requests, "get", lambda *a, **k: _Resp(200, [1, 2, 3]))
    monkeypatch.setattr(rau.time, "sleep", lambda s: None)
    with pytest.raises(TransientCheckError):
        wayback_check("https://x.com")


# ── BLOCKING 2: wayback_save transient failures ─────────────────────────────

class _SaveResp:
    """Stand-in for a requests.Response from SPN2 endpoints."""

    def __init__(self, status_code, json_payload=None, content_type="application/json",
                 url=None, text=None):
        self.status_code = status_code
        self._json = json_payload
        self.headers = {"Content-Type": content_type}
        self.url = url if url is not None else rau.WAYBACK_SPN2_SAVE
        if text is not None:
            self.text = text
        elif json_payload is not None:
            self.text = json.dumps(json_payload)
        else:
            self.text = ""

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise rau.requests.HTTPError(f"{self.status_code}")


def test_wayback_save_raises_transient_when_no_credentials(monkeypatch):
    monkeypatch.delenv("IA_ACCESS_KEY", raising=False)
    monkeypatch.delenv("IA_SECRET_KEY", raising=False)
    with pytest.raises(TransientCheckError):
        rau.wayback_save("https://x.com")


def test_wayback_save_raises_transient_on_connection_error(monkeypatch):
    monkeypatch.setenv("IA_ACCESS_KEY", "k")
    monkeypatch.setenv("IA_SECRET_KEY", "s")

    def boom(*a, **k):
        raise rau.requests.ConnectionError("no route")

    monkeypatch.setattr(rau.requests, "post", boom)
    with pytest.raises(TransientCheckError):
        rau.wayback_save("https://x.com")


def test_wayback_save_raises_transient_on_non_200(monkeypatch):
    monkeypatch.setenv("IA_ACCESS_KEY", "k")
    monkeypatch.setenv("IA_SECRET_KEY", "s")
    monkeypatch.setattr(rau.requests, "post",
                        lambda *a, **k: _SaveResp(500, text="server error"))
    with pytest.raises(TransientCheckError):
        rau.wayback_save("https://x.com")


def test_wayback_save_raises_transient_on_non_json_no_redirect(monkeypatch):
    monkeypatch.setenv("IA_ACCESS_KEY", "k")
    monkeypatch.setenv("IA_SECRET_KEY", "s")
    monkeypatch.setattr(
        rau.requests, "post",
        lambda *a, **k: _SaveResp(200, content_type="text/html", text="<html>",
                                  url=rau.WAYBACK_SPN2_SAVE),
    )
    with pytest.raises(TransientCheckError):
        rau.wayback_save("https://x.com")


def test_wayback_save_raises_transient_on_unparseable_json(monkeypatch):
    monkeypatch.setenv("IA_ACCESS_KEY", "k")
    monkeypatch.setenv("IA_SECRET_KEY", "s")
    monkeypatch.setattr(rau.requests, "post",
                        lambda *a, **k: _SaveResp(200, json_payload=None, text="not json"))
    with pytest.raises(TransientCheckError):
        rau.wayback_save("https://x.com")


def test_wayback_save_raises_transient_on_poll_timeout(monkeypatch):
    monkeypatch.setenv("IA_ACCESS_KEY", "k")
    monkeypatch.setenv("IA_SECRET_KEY", "s")
    monkeypatch.setattr(
        rau.requests, "post",
        lambda *a, **k: _SaveResp(200, json_payload={"job_id": "abc123"}),
    )
    monkeypatch.setattr(
        rau.requests, "get",
        lambda *a, **k: _SaveResp(200, json_payload={"status": "pending"}),
    )
    monkeypatch.setattr(rau.time, "sleep", lambda s: None)
    with pytest.raises(TransientCheckError):
        rau.wayback_save("https://x.com")


def test_wayback_save_returns_none_on_explicit_spn2_error_verdict(monkeypatch):
    # This is the one case that should remain a bare `None`: SPN2 itself
    # reported it could not archive the URL.
    monkeypatch.setenv("IA_ACCESS_KEY", "k")
    monkeypatch.setenv("IA_SECRET_KEY", "s")
    monkeypatch.setattr(
        rau.requests, "post",
        lambda *a, **k: _SaveResp(200, json_payload={"status": "error", "message": "nope"}),
    )
    assert rau.wayback_save("https://x.com") is None


def test_wayback_save_returns_blocked_sentinel_unchanged(monkeypatch):
    monkeypatch.setenv("IA_ACCESS_KEY", "k")
    monkeypatch.setenv("IA_SECRET_KEY", "s")
    monkeypatch.setattr(
        rau.requests, "post",
        lambda *a, **k: _SaveResp(200, json_payload={
            "status": "error", "status_ext": "error:blocked-url", "message": "blocked",
        }),
    )
    assert rau.wayback_save("https://x.com") == "BLOCKED"


# ── 3: --allow-save gate ─────────────────────────────────────────────────

def test_check_only_is_the_default():
    parser = rau.build_arg_parser()
    args = parser.parse_args(["/tmp/fake-clippings"])
    assert args.allow_save is False


def test_check_only_flag_is_still_accepted_as_a_noop():
    parser = rau.build_arg_parser()
    args = parser.parse_args(["/tmp/fake-clippings", "--check-only"])
    assert args.allow_save is False


def test_allow_save_flag_disables_check_only():
    parser = rau.build_arg_parser()
    args = parser.parse_args(["/tmp/fake-clippings", "--allow-save"])
    assert args.allow_save is True


def test_default_args_never_call_wayback_save(monkeypatch):
    """With default args (no --allow-save), resolve() must never reach
    wayback_save — this is the only thing standing between an unattended
    run and a live SPN2 submission."""
    parser = rau.build_arg_parser()
    args = parser.parse_args(["/tmp/fake-clippings"])
    check_only = not args.allow_save
    assert check_only is True

    calls = {"n": 0}

    def fake_save(url):
        calls["n"] += 1
        return "https://web.archive.org/web/1/x"

    monkeypatch.setattr(rau, "wayback_save", fake_save)
    monkeypatch.setattr(rau, "wayback_check", lambda url: None)

    result = rau.resolve("https://example.com/a", check_only)

    assert calls["n"] == 0
    assert result == (None, "")


# ── 4: --max-files caps forward progress ────────────────────────────────

def _write_note(path, archived_value):
    path.write_text(f"---\narchived: {archived_value}\n---\nbody\n")


def test_max_files_caps_processing(tmp_path, monkeypatch):
    for i in range(5):
        _write_note(tmp_path / f"note{i}.md",
                    "https://web.archive.org/web/https://example.com/x")

    calls = {"n": 0}

    def fake_check(url):
        calls["n"] += 1
        return "https://web.archive.org/web/20240101000000/https://example.com/x"

    monkeypatch.setattr(rau, "wayback_check", fake_check)
    monkeypatch.setattr(rau.time, "sleep", lambda s: None)

    rau.scan_vault(
        tmp_path,
        dry_run=True,
        check_only=True,
        reprocess_all=False,
        retry_failed=False,
        retry_after_days=7,
        from_source=False,
        max_files=2,
    )

    assert calls["n"] == 2


def test_max_files_zero_means_unlimited(tmp_path, monkeypatch):
    for i in range(3):
        _write_note(tmp_path / f"note{i}.md",
                    "https://web.archive.org/web/https://example.com/x")

    calls = {"n": 0}

    def fake_check(url):
        calls["n"] += 1
        return "https://web.archive.org/web/20240101000000/https://example.com/x"

    monkeypatch.setattr(rau, "wayback_check", fake_check)
    monkeypatch.setattr(rau.time, "sleep", lambda s: None)

    rau.scan_vault(
        tmp_path,
        dry_run=True,
        check_only=True,
        reprocess_all=False,
        retry_failed=False,
        retry_after_days=7,
        from_source=False,
        max_files=0,
    )

    assert calls["n"] == 3


# ── 5: BLOCKED branch respects the rate-limit delay ─────────────────────

def test_blocked_branch_sleeps_for_request_delay(tmp_path, monkeypatch):
    _write_note(tmp_path / "note.md",
               "https://web.archive.org/web/https://example.com/x")

    monkeypatch.setattr(rau, "wayback_check", lambda url: None)
    monkeypatch.setattr(rau, "wayback_save", lambda url: "BLOCKED")
    sleeps = []
    monkeypatch.setattr(rau.time, "sleep", lambda s: sleeps.append(s))

    rau.scan_vault(
        tmp_path,
        dry_run=True,
        check_only=False,
        reprocess_all=False,
        retry_failed=False,
        retry_after_days=7,
        from_source=False,
        max_files=0,
    )

    assert rau.REQUEST_DELAY in sleeps
