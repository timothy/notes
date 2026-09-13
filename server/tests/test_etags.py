"""Strong ETags: opaque versions, quoted on the wire, and strict If-Match parsing."""

import pytest

from notes_api.etags import STRONG_ETAG, new_version, parse_if_match, quote
from notes_api.http.problems import MalformedRequest, PreconditionRequired


def test_new_versions_are_opaque_unique_strong_tags() -> None:
    first, second = quote(new_version()), quote(new_version())
    assert first != second
    assert STRONG_ETAG.match(first) and STRONG_ETAG.match(second)


def test_missing_if_match_is_precondition_required() -> None:
    with pytest.raises(PreconditionRequired) as info:
        parse_if_match(None)
    assert info.value.status == 428


@pytest.mark.parametrize("value", ['W/"note-v1"', "*", '"a", "b"', "note-v1", '""', '"a b"'])
def test_weak_wildcard_list_and_unquoted_values_are_malformed(value: str) -> None:
    with pytest.raises(MalformedRequest) as info:
        parse_if_match(value)
    assert info.value.status == 400
    assert [(e.location, e.pointer) for e in info.value.errors or []] == [("header", "If-Match")]


def test_one_strong_tag_is_returned_unchanged() -> None:
    assert parse_if_match('"note-v1"') == '"note-v1"'
