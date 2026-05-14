"""Integration tests for Apple Numbers (.numbers) uploads.

CSV uploads spec extension: each list view that accepts a CSV upload also
accepts a Numbers file. The server converts the Numbers file's first
sheet's first table to CSV in-memory and feeds it to the same per-domain
validator the CSV path uses.

Tests use ``numbers_parser`` itself to *create* a small Numbers file on
disk, then upload it via the route — round-trip parity with the library
used by the parser.
"""

from __future__ import annotations

import io
import pathlib
import tempfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, Role, Supplier, User, UserStatus


def _make_user(
    db: Session,
    *,
    email: str,
    role: Role,
    status: UserStatus = UserStatus.ACTIVE,
) -> User:
    user = User(
        google_sub=f"sub-{email}",
        email=email,
        name=email.split("@")[0].title(),
        role=role,
        status=status,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _login_as(client: TestClient, user: User) -> None:
    resp = client.post(
        "/auth/_dev-login",
        data={"email": user.email, "sub": user.google_sub},
        follow_redirects=False,
    )
    assert resp.status_code == 303


def _csrf(client: TestClient) -> str:
    if "csrftoken" not in client.cookies:
        client.get("/")
    return client.cookies["csrftoken"]


def _build_suppliers_numbers_bytes(rows: list[dict[str, str]]) -> bytes:
    """Synthesise a Numbers file with a suppliers-shaped table.

    Header row: ``id, name, email, phone, notes``. ``rows`` is a list of
    dicts; missing keys → blank cell. Uses ``numbers_parser`` directly so
    the file is byte-for-byte what real Numbers would produce.
    """
    numbers_parser = pytest.importorskip("numbers_parser")
    headers = ["id", "name", "email", "phone", "notes"]
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "out.numbers"
        doc = numbers_parser.Document()
        table = doc.sheets[0].tables[0]
        for col, h in enumerate(headers):
            table.write(0, col, h)
        for r_idx, row in enumerate(rows, start=1):
            for col, h in enumerate(headers):
                table.write(r_idx, col, row.get(h, ""))
        doc.save(str(path))
        return path.read_bytes()


def _post_upload(
    client: TestClient,
    csv_bytes: bytes,
    *,
    filename: str,
    content_type: str,
    dry_run: bool,
) -> object:
    return client.post(
        "/admin/suppliers/upload",
        files={"file": (filename, io.BytesIO(csv_bytes), content_type)},
        data={"csrf_token": _csrf(client), "dry_run": "1" if dry_run else ""},
        follow_redirects=False,
    )


class TestNumbersUpload:
    def test_numbers_dry_run_tags_new(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        data = _build_suppliers_numbers_bytes(
            [{"name": "Acme Wax", "email": "orders@acme.test"}]
        )
        # Sanity: bytes start with the zip magic Numbers uses.
        assert data.startswith(b"PK\x03\x04")
        resp = _post_upload(
            client,
            data,
            filename="suppliers.numbers",
            content_type="application/zip",
            dry_run=True,
        )
        assert resp.status_code == 200  # type: ignore[attr-defined]
        body = resp.content  # type: ignore[attr-defined]
        assert b"csv-upload-top-error" not in body
        assert b'data-row-tag="new"' in body
        # Dry-run: nothing landed.
        assert list(db_session.execute(select(Supplier)).scalars().all()) == []

    def test_numbers_commit_creates_rows(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        data = _build_suppliers_numbers_bytes(
            [
                {"name": "Acme Wax", "email": "orders@acme.test"},
                {"name": "Beta Metal"},
            ]
        )
        resp = _post_upload(
            client,
            data,
            filename="suppliers.numbers",
            content_type="application/zip",
            dry_run=False,
        )
        assert resp.status_code == 303  # type: ignore[attr-defined]
        names = sorted(
            n for (n,) in db_session.execute(select(Supplier.name)).all()
        )
        assert names == ["Acme Wax", "Beta Metal"]
        # The summary audit row's file_sha256 hashes the raw .numbers bytes,
        # not the converted CSV — confirms by length only (full sha = 64 chars).
        summary = db_session.execute(
            select(AuditLog).where(AuditLog.action == "supplier.csv_uploaded")
        ).scalar_one()
        assert summary.after_json is not None
        assert summary.after_json["count"] == 2
        assert len(summary.after_json["file_sha256"]) == 64

    def test_renamed_zip_falls_through_as_csv_error(
        self, client: TestClient, db_session: Session
    ) -> None:
        """A non-Numbers zip uploaded as ``.numbers`` should surface a
        ``CsvUploadError`` from the parser, not 500."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        # Build a minimal zip that isn't a Numbers file.
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("hello.txt", "not a numbers file")
        resp = _post_upload(
            client,
            buf.getvalue(),
            filename="weird.numbers",
            content_type="application/zip",
            dry_run=True,
        )
        # Top-level error rendered in the preview, not a 500.
        assert resp.status_code == 200  # type: ignore[attr-defined]
        assert b"csv-upload-top-error" in resp.content  # type: ignore[attr-defined]
        assert b"could not read Numbers file" in resp.content  # type: ignore[attr-defined]

    def test_numbers_int_cells_round_trip_as_ints(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Numbers stores integer cells as Python floats (``1`` → ``1.0``).
        Naive ``str(1.0) == "1.0"`` then fails ``int()`` parsing on the
        id column. Regression test for the round-trip a user hit re-uploading
        the items export through Numbers.
        """
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        existing = Supplier(name="Existing")
        db_session.add(existing)
        db_session.commit()
        db_session.refresh(existing)
        _login_as(client, mgr)
        # Write the id as a Python int — numbers-parser returns it as a float.
        # The bug: the id-cell on re-upload was "1.0", failing int() in the
        # id-skip check.
        numbers_parser = pytest.importorskip("numbers_parser")
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "out.numbers"
            doc = numbers_parser.Document()
            table = doc.sheets[0].tables[0]
            for col, h in enumerate(["id", "name", "email", "phone", "notes"]):
                table.write(0, col, h)
            table.write(1, 0, existing.id)  # int → Numbers float
            table.write(1, 1, "Existing")
            doc.save(str(path))
            data = path.read_bytes()
        resp = _post_upload(
            client,
            data,
            filename="suppliers.numbers",
            content_type="application/zip",
            dry_run=True,
        )
        body = resp.content  # type: ignore[attr-defined]
        assert b"id must be a whole number" not in body, body[:500]
        # The id matches → row tagged ``skip``, not ``error``.
        assert b'data-row-tag="skip"' in body
        assert b'data-row-tag="error"' not in body

    def test_zip_without_extension_treated_as_csv(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Numbers detection requires *both* magic bytes AND the
        ``.numbers`` extension. A zip uploaded as ``.csv`` falls through
        to the UTF-8 CSV path and 400s on non-UTF-8.
        """
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        data = _build_suppliers_numbers_bytes(
            [{"name": "Acme Wax"}]
        )
        resp = _post_upload(
            client,
            data,
            filename="suppliers.csv",  # wrong extension
            content_type="text/csv",
            dry_run=True,
        )
        # Top-level error: not UTF-8 (raw zip bytes can't decode).
        assert b"csv-upload-top-error" in resp.content  # type: ignore[attr-defined]
        assert b"UTF-8" in resp.content  # type: ignore[attr-defined]
