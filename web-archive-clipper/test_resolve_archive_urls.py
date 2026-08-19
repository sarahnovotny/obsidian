import frontmatter
import pytest

from resolve_archive_urls import (
    clean_url,
    effective_archived,
    extract_original_url,
    synthesize_lookup_url,
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


import resolve_archive_urls as rau
from resolve_archive_urls import RateLimited, wayback_check


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}

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


def test_wayback_check_returns_none_when_no_snapshot(monkeypatch):
    monkeypatch.setattr(rau.requests, "get", lambda *a, **k: _Resp(200, {"archived_snapshots": {}}))
    assert wayback_check("https://x.com") is None


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


def test_ordinary_http_error_is_transient_not_definitive(monkeypatch):
    monkeypatch.setattr(rau.requests, "get", lambda *a, **k: _Resp(404))
    monkeypatch.setattr(rau.time, "sleep", lambda s: None)
    with pytest.raises(TransientCheckError):
        wayback_check("https://x.com")


from resolve_archive_urls import TransientCheckError


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


def test_rate_limit_still_raises_ratelimited_not_transient(monkeypatch):
    monkeypatch.setattr(rau.requests, "get", lambda *a, **k: _Resp(429))
    monkeypatch.setattr(rau.time, "sleep", lambda s: None)
    with pytest.raises(RateLimited):
        wayback_check("https://x.com")
