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
