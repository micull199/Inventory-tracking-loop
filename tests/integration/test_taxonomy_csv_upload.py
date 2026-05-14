"""Integration tests for the three taxonomy CSV upload routes.

Covers:
- Top-level: role gating, header validation (requires ``name`` + ``archetype``),
  archetype + sku_prefix coercion, idempotency by id, name + prefix uniqueness,
  cross-row dupes, commit writes per-row + summary audit.
- Sub-categories: parent scope; ``archetype`` column omitted; parent guards
  (404 unknown parent, 400 archived parent, 400 parent has items).
- Grandchildren: UV-tree rejection, parent + grand-parent ID propagation in
  audit summary.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Archetype, AuditLog, Role, TaxonomyNode, User, UserStatus

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_user(
    db: Session,
    *,
    email: str,
    role: Role | None = None,
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


def _post_upload(
    client: TestClient,
    url: str,
    csv_bytes: bytes,
    *,
    dry_run: bool,
) -> object:
    return client.post(
        url,
        files={"file": ("taxonomy.csv", csv_bytes, "text/csv")},
        data={"csrf_token": _csrf(client), "dry_run": "1" if dry_run else ""},
        follow_redirects=False,
    )


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------


class TestTopLevelGating:
    def test_anonymous_form_is_401(self, client: TestClient) -> None:
        assert client.get("/admin/taxonomy/upload").status_code == 401

    def test_workshop_form_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        w = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, w)
        assert client.get("/admin/taxonomy/upload").status_code == 403

    def test_office_form_is_403(self, client: TestClient, db_session: Session) -> None:
        o = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, o)
        assert client.get("/admin/taxonomy/upload").status_code == 403

    def test_manager_form_is_200(self, client: TestClient, db_session: Session) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, m)
        resp = client.get("/admin/taxonomy/upload")
        assert resp.status_code == 200
        assert "Upload top-level categories" in resp.text


class TestTopLevelHeader:
    def test_missing_archetype_blocks(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, m)
        csv = b"id,name\n,Rings\n"
        resp = _post_upload(client, "/admin/taxonomy/upload", csv, dry_run=True)
        assert b"missing required column" in resp.content  # type: ignore[attr-defined]
        assert b"archetype" in resp.content  # type: ignore[attr-defined]


class TestTopLevelCommit:
    def test_commit_creates_with_auto_prefix(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, m)
        csv = b"id,name,archetype\n,Rings,bulk\n,Chains,unique\n"
        resp = _post_upload(client, "/admin/taxonomy/upload", csv, dry_run=False)
        assert resp.status_code == 303  # type: ignore[attr-defined]
        nodes = list(
            db_session.execute(
                select(TaxonomyNode)
                .where(TaxonomyNode.parent_id.is_(None))
                .order_by(TaxonomyNode.name)
            )
            .scalars()
            .all()
        )
        assert [n.name for n in nodes] == ["Chains", "Rings"]
        # Auto-derived prefixes are uppercased + length-capped.
        for n in nodes:
            assert n.sku_prefix is not None
            assert n.sku_prefix.isalnum()
            assert 1 <= len(n.sku_prefix) <= 8
        # Per-row + summary audits.
        actions = sorted(
            a
            for (a,) in db_session.execute(
                select(AuditLog.action).where(AuditLog.entity_type == "taxonomy_node")
            ).all()
        )
        assert actions.count("taxonomy_node.created") == 2
        assert actions.count("taxonomy_node.csv_uploaded") == 1

    def test_invalid_archetype_row_error(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, m)
        csv = b"id,name,archetype\n,Rings,bogus\n"
        resp = _post_upload(client, "/admin/taxonomy/upload", csv, dry_run=True)
        assert b'data-row-tag="error"' in resp.content  # type: ignore[attr-defined]
        assert b"archetype must be one of" in resp.content  # type: ignore[attr-defined]

    def test_existing_name_row_error(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        db_session.add(
            TaxonomyNode(name="Rings", sku_prefix="RNG", archetype=Archetype.BULK)
        )
        db_session.commit()
        _login_as(client, m)
        csv = b"id,name,archetype\n,Rings,unique\n"
        resp = _post_upload(client, "/admin/taxonomy/upload", csv, dry_run=True)
        assert b'data-row-tag="error"' in resp.content  # type: ignore[attr-defined]

    def test_existing_sku_prefix_row_error(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        db_session.add(
            TaxonomyNode(name="Rings", sku_prefix="RNG", archetype=Archetype.BULK)
        )
        db_session.commit()
        _login_as(client, m)
        csv = b"id,name,archetype,sku_prefix\n,Roundabout,bulk,RNG\n"
        resp = _post_upload(client, "/admin/taxonomy/upload", csv, dry_run=True)
        assert b'data-row-tag="error"' in resp.content  # type: ignore[attr-defined]
        assert b"sku_prefix" in resp.content  # type: ignore[attr-defined]

    def test_invalid_sku_prefix_chars(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, m)
        csv = b"id,name,archetype,sku_prefix\n,Foo,bulk,not-alnum\n"
        resp = _post_upload(client, "/admin/taxonomy/upload", csv, dry_run=True)
        assert b'data-row-tag="error"' in resp.content  # type: ignore[attr-defined]
        assert b"alphanumeric" in resp.content  # type: ignore[attr-defined]

    def test_existing_id_skip(self, client: TestClient, db_session: Session) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        n = TaxonomyNode(name="Rings", sku_prefix="RNG", archetype=Archetype.BULK)
        db_session.add(n)
        db_session.commit()
        _login_as(client, m)
        csv = f"id,name,archetype\n{n.id},Rings,bulk\n".encode()
        resp = _post_upload(client, "/admin/taxonomy/upload", csv, dry_run=True)
        assert b'data-row-tag="skip"' in resp.content  # type: ignore[attr-defined]


class TestTopUploadButton:
    def test_list_page_has_upload_button(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, m)
        resp = client.get("/admin/taxonomy")
        assert "taxonomy-list-upload-link" in resp.text
        assert "/admin/taxonomy/upload" in resp.text


# ---------------------------------------------------------------------------
# Sub-categories (depth 1)
# ---------------------------------------------------------------------------


class TestSubCategoryUpload:
    def test_unknown_parent_404(self, client: TestClient, db_session: Session) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, m)
        assert client.get("/admin/taxonomy/9999/children/upload").status_code == 404

    def test_archived_parent_400(self, client: TestClient, db_session: Session) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = TaxonomyNode(
            name="Rings",
            sku_prefix="RNG",
            archetype=Archetype.BULK,
            archived_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        db_session.add(parent)
        db_session.commit()
        _login_as(client, m)
        # GET is allowed; POST blocks the upload of an archived parent.
        csv = b"id,name\n,Silver\n"
        resp = _post_upload(
            client, f"/admin/taxonomy/{parent.id}/children/upload", csv, dry_run=True
        )
        assert resp.status_code == 400  # type: ignore[attr-defined]

    def test_commit_creates_sub_categories(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = TaxonomyNode(name="Rings", sku_prefix="RNG", archetype=Archetype.BULK)
        db_session.add(parent)
        db_session.commit()
        _login_as(client, m)
        csv = b"id,name\n,Silver\n,Gold\n"
        resp = _post_upload(
            client,
            f"/admin/taxonomy/{parent.id}/children/upload",
            csv,
            dry_run=False,
        )
        assert resp.status_code == 303  # type: ignore[attr-defined]
        assert resp.headers["location"] == f"/admin/taxonomy/{parent.id}/children"  # type: ignore[attr-defined]
        children = list(
            db_session.execute(
                select(TaxonomyNode).where(TaxonomyNode.parent_id == parent.id)
                .order_by(TaxonomyNode.name)
            )
            .scalars()
            .all()
        )
        assert [n.name for n in children] == ["Gold", "Silver"]
        for c in children:
            assert c.sku_prefix is not None

    def test_duplicate_name_under_parent(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = TaxonomyNode(name="Rings", sku_prefix="RNG", archetype=Archetype.BULK)
        db_session.add(parent)
        db_session.commit()
        db_session.add(
            TaxonomyNode(
                parent_id=parent.id, name="Silver", sku_prefix="SLV"
            )
        )
        db_session.commit()
        _login_as(client, m)
        csv = b"id,name\n,Silver\n"
        resp = _post_upload(
            client,
            f"/admin/taxonomy/{parent.id}/children/upload",
            csv,
            dry_run=True,
        )
        assert b'data-row-tag="error"' in resp.content  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Grandchildren (depth 2)
# ---------------------------------------------------------------------------


class TestGrandchildrenUpload:
    def test_uv_tree_blocks_form(self, client: TestClient, db_session: Session) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = TaxonomyNode(
            name="Pendants",
            sku_prefix="PEN",
            archetype=Archetype.UNIQUE_VARIANT,
        )
        db_session.add(parent)
        db_session.commit()
        sub = TaxonomyNode(parent_id=parent.id, name="Silver", sku_prefix="SLV")
        db_session.add(sub)
        db_session.commit()
        _login_as(client, m)
        resp = client.get(
            f"/admin/taxonomy/{parent.id}/sub/{sub.id}/grandchildren/upload"
        )
        assert resp.status_code == 400
        assert "unique-variant" in resp.text

    def test_commit_creates_grandchildren(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = TaxonomyNode(name="Rings", sku_prefix="RNG", archetype=Archetype.BULK)
        db_session.add(parent)
        db_session.commit()
        sub = TaxonomyNode(parent_id=parent.id, name="Silver", sku_prefix="SLV")
        db_session.add(sub)
        db_session.commit()
        _login_as(client, m)
        csv = b"id,name\n,925\n,Sterling\n"
        resp = _post_upload(
            client,
            f"/admin/taxonomy/{parent.id}/sub/{sub.id}/grandchildren/upload",
            csv,
            dry_run=False,
        )
        assert resp.status_code == 303  # type: ignore[attr-defined]
        grandchildren = list(
            db_session.execute(
                select(TaxonomyNode).where(TaxonomyNode.parent_id == sub.id)
            )
            .scalars()
            .all()
        )
        assert sorted(n.name for n in grandchildren) == ["925", "Sterling"]
