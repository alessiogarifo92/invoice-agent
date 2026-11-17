"""If-Match parsing for the decision endpoint (RFC 9110 strong comparison): pure."""

from uuid import UUID

import pytest

from app.api.proposals import parse_if_match
from app.review.store import proposal_etag

ETAG = '"5f0c7e9a-0000-4000-8000-000000000001.3"'


@pytest.mark.parametrize("header", [None, "", "  ", " , ", "*", f"{ETAG}, *"])
def test_no_entity_tag_means_none(header: str | None) -> None:
    assert parse_if_match(header) is None


def test_a_list_of_tags_is_split_and_stripped() -> None:
    assert parse_if_match(f' "a" ,{ETAG}') == frozenset({'"a"', ETAG})


def test_weak_tags_never_match() -> None:
    assert parse_if_match(f"W/{ETAG}") == frozenset()
    assert parse_if_match(f"W/{ETAG}, {ETAG}") == frozenset({ETAG})


def test_the_etag_names_the_proposal_and_the_invoice_version() -> None:
    proposal = UUID("5f0c7e9a-0000-4000-8000-000000000001")
    assert proposal_etag(proposal, 3) == ETAG
    assert proposal_etag(proposal, 4) != ETAG
