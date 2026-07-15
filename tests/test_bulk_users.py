"""Unit tests for CSV bulk user import helpers."""
import os
import tempfile
from pathlib import Path

from utils.bulk_users import (
    parse_csv_users,
    import_users_from_csv_text,
    normalize_username,
    is_valid_email,
)
from utils.users_db import UsersDB


def test_parse_csv_without_header():
    text = "nkrishnan,nkrishnan@pixstone.com,Nkri@Sh12,user\nbob,bob@x.com,Password1!,power\n"
    rows = parse_csv_users(text)
    assert len(rows) == 2
    assert rows[0]["name"] == "nkrishnan"
    assert rows[0]["email"] == "nkrishnan@pixstone.com"
    assert rows[0]["password"] == "Nkri@Sh12"
    assert rows[0]["role"] == "user"
    assert rows[1]["role"] == "power"


def test_parse_csv_with_header():
    text = "name,email,password,role\nnkrishnan,nkrishnan@pixstone.com,Nkri@Sh12,user\n"
    rows = parse_csv_users(text)
    assert len(rows) == 1
    assert rows[0]["email"] == "nkrishnan@pixstone.com"


def test_normalize_and_email():
    assert normalize_username("nk rishnan") == "nk_rishnan"
    assert is_valid_email("nkrishnan@pixstone.com")
    assert not is_valid_email("not-an-email")


def test_import_creates_user_login_by_email():
    with tempfile.TemporaryDirectory() as td:
        db_path = os.path.join(td, "users.json")
        db = UsersDB(db_path)
        # seed admin so first-user force-admin does not apply
        db.add_user("admin-id", "admin", "AdminPass1!", True, email="admin@test.com")

        csv = "nkrishnan,nkrishnan@pixstone.com,Nkri@Sh12,user\n"
        result = import_users_from_csv_text(db, csv)
        assert result["created_count"] == 1
        assert result["error_count"] == 0

        # Login with email
        uid, rec = db.authenticate("nkrishnan@pixstone.com", "Nkri@Sh12")
        assert uid is not None
        assert rec["username"] == "nkrishnan"
        assert rec["email"] == "nkrishnan@pixstone.com"
        assert "user" in rec["groups"]

        # Login with username still works
        uid2, _ = db.authenticate("nkrishnan", "Nkri@Sh12")
        assert uid2 == uid

        # Duplicate skip
        result2 = import_users_from_csv_text(db, csv)
        assert result2["created_count"] == 0
        assert result2["skipped_count"] == 1
