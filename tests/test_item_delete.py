#!/usr/bin/env python
"""
Tests for item deletion: the S3 key-boundary fix (OLID prefix collisions)
and delete_many's partial-failure reporting.

Mocks db/s3 at the same module boundary test_loan_logic.py uses for Loan
tests, rather than hitting the real DB — this repo's TESTING flag doesn't
default on in every environment, so tests must not assume an isolated DB.
"""

from unittest.mock import MagicMock, patch

from lenny.core.api import LennyAPI
from lenny.core.exceptions import ItemNotFoundError


def test_item_s3_keys_avoids_prefix_collision():
    """OLID 123 must not match S3 keys belonging to OLID 1234."""
    with patch("lenny.core.api.s3") as mock_s3:
        mock_s3.get_keys.return_value = ["123.epub", "123_encrypted.epub", "1234.epub"]
        keys = LennyAPI._item_s3_keys(123)
    assert sorted(keys) == ["123.epub", "123_encrypted.epub"]


def test_item_s3_keys_bare_match():
    """A key with no extension at all (== the OLID exactly) still matches."""
    with patch("lenny.core.api.s3") as mock_s3:
        mock_s3.get_keys.return_value = ["123", "1234.epub"]
        keys = LennyAPI._item_s3_keys(123)
    assert keys == ["123"]


def test_delete_many_reports_each_outcome_separately():
    fake_items = {1: MagicMock(), 2: MagicMock()}

    def fake_exists(olid):
        return fake_items.get(olid)

    with patch("lenny.core.api.Item") as mock_item_cls, \
         patch("lenny.core.api.s3") as mock_s3, \
         patch("lenny.core.api.db"):
        mock_item_cls.exists.side_effect = fake_exists
        mock_s3.get_keys.return_value = []

        result = LennyAPI.delete_many([1, 2, 999])

    assert result["deleted"] == [1, 2]
    assert result["not_found"] == [999]
    assert result["failed"] == {}


def test_delete_many_one_failure_does_not_block_the_rest():
    fake_items = {1: MagicMock(), 2: MagicMock()}

    def fake_exists(olid):
        return fake_items.get(olid)

    with patch("lenny.core.api.Item") as mock_item_cls, \
         patch("lenny.core.api.s3") as mock_s3, \
         patch("lenny.core.api.db") as mock_db:
        mock_item_cls.exists.side_effect = fake_exists
        mock_s3.get_keys.return_value = []
        # First commit (for olid=1) raises, second (olid=2) succeeds.
        mock_db.commit.side_effect = [Exception("boom"), None]

        result = LennyAPI.delete_many([1, 2])

    assert result["deleted"] == [2]
    assert 1 in result["failed"]


def test_delete_many_respects_max_cap():
    with patch("lenny.core.api.Item") as mock_item_cls, \
         patch("lenny.core.api.s3") as mock_s3, \
         patch("lenny.core.api.db"):
        mock_item_cls.exists.return_value = None
        mock_s3.get_keys.return_value = []

        oversized = list(range(LennyAPI.MAX_BULK_DELETE + 50))
        result = LennyAPI.delete_many(oversized)

    total_seen = len(result["deleted"]) + len(result["not_found"]) + len(result["failed"])
    assert total_seen == LennyAPI.MAX_BULK_DELETE
