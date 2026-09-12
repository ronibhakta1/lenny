#!/usr/bin/env python
"""
Tests for LennyAPI.rename_item and LennyAPI.reupload — mocked at the same
module boundary test_item_delete.py uses, no real DB/S3 writes.
"""

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from lenny.core.api import LennyAPI
from lenny.core.exceptions import (
    DatabaseUpdateError,
    ItemExistsError,
    ItemNotFoundError,
    S3UploadError,
)


def _client_error(op="op"):
    return ClientError({"Error": {"Code": "500", "Message": "boom"}}, op)


# --- rename_item -------------------------------------------------------------


def test_rename_same_olid_is_a_noop_and_returns_existing_item():
    fake_item = MagicMock()
    with patch("lenny.core.api.Item") as mock_item_cls:
        mock_item_cls.exists.return_value = fake_item
        result = LennyAPI.rename_item(123, 123)
    assert result is fake_item
    mock_item_cls.exists.assert_called_once_with(123)


def test_rename_raises_not_found_when_source_missing():
    with patch("lenny.core.api.Item") as mock_item_cls:
        mock_item_cls.exists.return_value = None
        with pytest.raises(ItemNotFoundError):
            LennyAPI.rename_item(123, 456)


def test_rename_raises_exists_error_on_target_collision():
    fake_source = MagicMock()
    fake_target = MagicMock()

    def fake_exists(olid):
        return fake_source if olid == 123 else fake_target

    with patch("lenny.core.api.Item") as mock_item_cls:
        mock_item_cls.exists.side_effect = fake_exists
        with pytest.raises(ItemExistsError):
            LennyAPI.rename_item(123, 456)


def test_rename_moves_s3_keys_and_updates_db():
    fake_item = MagicMock(openlibrary_edition=123)

    def fake_exists(olid):
        return fake_item if olid == 123 else None

    with patch("lenny.core.api.Item") as mock_item_cls, \
         patch("lenny.core.api.s3") as mock_s3, \
         patch("lenny.core.api.db"):
        mock_item_cls.exists.side_effect = fake_exists
        mock_s3.get_keys.return_value = ["123.epub", "123_encrypted.epub"]

        LennyAPI.rename_item(123, 456)

        copy_keys = {c.kwargs["Key"] for c in mock_s3.copy_object.call_args_list}
        assert copy_keys == {"456.epub", "456_encrypted.epub"}
        delete_keys = {c.kwargs["Key"] for c in mock_s3.delete_object.call_args_list}
        assert delete_keys == {"123.epub", "123_encrypted.epub"}
        assert fake_item.openlibrary_edition == 456


def test_rename_rolls_back_copies_on_copy_failure():
    fake_item = MagicMock(openlibrary_edition=123)

    def fake_exists(olid):
        return fake_item if olid == 123 else None

    with patch("lenny.core.api.Item") as mock_item_cls, \
         patch("lenny.core.api.s3") as mock_s3, \
         patch("lenny.core.api.db"):
        mock_item_cls.exists.side_effect = fake_exists
        mock_s3.get_keys.return_value = ["123.epub", "123_encrypted.epub"]
        # First copy succeeds, second raises.
        mock_s3.copy_object.side_effect = [None, _client_error()]

        with pytest.raises(S3UploadError):
            LennyAPI.rename_item(123, 456)

        # The one successful copy must be cleaned up.
        deleted = {c.kwargs["Key"] for c in mock_s3.delete_object.call_args_list}
        assert "456.epub" in deleted
        # OLID must not have been changed since the operation failed.
        assert fake_item.openlibrary_edition == 123


def test_rename_rolls_back_copies_on_db_failure():
    fake_item = MagicMock(openlibrary_edition=123)

    def fake_exists(olid):
        return fake_item if olid == 123 else None

    with patch("lenny.core.api.Item") as mock_item_cls, \
         patch("lenny.core.api.s3") as mock_s3, \
         patch("lenny.core.api.db") as mock_db:
        mock_item_cls.exists.side_effect = fake_exists
        mock_s3.get_keys.return_value = ["123.epub"]
        mock_db.commit.side_effect = Exception("db exploded")

        with pytest.raises(DatabaseUpdateError):
            LennyAPI.rename_item(123, 456)

        deleted = {c.kwargs["Key"] for c in mock_s3.delete_object.call_args_list}
        assert "456.epub" in deleted
        mock_db.rollback.assert_called_once()


# --- reupload ------------------------------------------------------------


def test_reupload_raises_not_found_when_missing():
    with patch("lenny.core.api.Item") as mock_item_cls:
        mock_item_cls.exists.return_value = None
        with pytest.raises(ItemNotFoundError):
            LennyAPI.reupload(123, files=[MagicMock()])


def test_reupload_deletes_old_keys_then_uploads_and_updates_flags():
    fake_item = MagicMock(openlibrary_edition=123)

    with patch("lenny.core.api.Item") as mock_item_cls, \
         patch("lenny.core.api.s3") as mock_s3, \
         patch("lenny.core.api.db"), \
         patch.object(LennyAPI, "_item_s3_keys", return_value=["123.epub"]), \
         patch.object(LennyAPI, "upload_files", return_value=1) as mock_upload:
        mock_item_cls.exists.return_value = fake_item

        result = LennyAPI.reupload(123, files=["fake-file"], encrypt=True)

        mock_s3.delete_object.assert_called_once()
        assert mock_s3.delete_object.call_args.kwargs["Key"] == "123.epub"
        mock_upload.assert_called_once_with(["fake-file"], 123, encrypt=True)
        assert fake_item.encrypted is True
        assert result is fake_item


def test_reupload_db_failure_raises_database_update_error():
    fake_item = MagicMock(openlibrary_edition=123)

    with patch("lenny.core.api.Item") as mock_item_cls, \
         patch("lenny.core.api.s3"), \
         patch("lenny.core.api.db") as mock_db, \
         patch.object(LennyAPI, "_item_s3_keys", return_value=[]), \
         patch.object(LennyAPI, "upload_files", return_value=1):
        mock_item_cls.exists.return_value = fake_item
        mock_db.commit.side_effect = Exception("db exploded")

        with pytest.raises(DatabaseUpdateError):
            LennyAPI.reupload(123, files=["fake-file"], encrypt=False)

        mock_db.rollback.assert_called_once()
