"""Integration tests for the slim ``POST /admin/taxonomy/{node_id}/fields/pick``.

Picks are now a visibility selector (node_id + catalog key + required +
sort_order). Every catalog entry is column-backed so the picker just
records which fields show on the items form for that category.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, Role, TaxonomyFieldDef, TaxonomyNode, User, UserStatus


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


def _make_node(db: Session, name: str = "Rings") -> TaxonomyNode:
    n = TaxonomyNode(name=name)
    db.add(n)
    db.commit()
    db.refresh(n)
    return n


def _pick(client: TestClient, node_id: int, catalog_key: str) -> object:
    return client.post(
        f"/admin/taxonomy/{node_id}/fields/pick",
        data={"catalog_key": catalog_key, "csrf_token": _csrf(client)},
        follow_redirects=False,
    )


class TestPickHappyPath:
    def test_pick_creates_slim_row(self, client: TestClient, db_session: Session) -> None:
        node = _make_node(db_session)
        _login_as(client, _make_user(db_session, email="m@x.test", role=Role.MANAGER))
        resp = _pick(client, node.id, "ring_size")
        assert resp.status_code == 303  # type: ignore[attr-defined]
        fd = db_session.execute(select(TaxonomyFieldDef)).scalar_one()
        assert fd.key == "ring_size"
        assert fd.required is False

    def test_pick_writes_audit_row(self, client: TestClient, db_session: Session) -> None:
        node = _make_node(db_session)
        _login_as(client, _make_user(db_session, email="m@x.test", role=Role.MANAGER))
        _pick(client, node.id, "ring_size")
        audit = db_session.execute(
            select(AuditLog).where(AuditLog.action == "taxonomy_field_def.picked_from_catalog")
        ).scalar_one()
        assert audit.after_json is not None
        assert audit.after_json["key"] == "ring_size"


class TestPickValidation:
    def test_unknown_key_400(self, client: TestClient, db_session: Session) -> None:
        node = _make_node(db_session)
        _login_as(client, _make_user(db_session, email="m@x.test", role=Role.MANAGER))
        resp = _pick(client, node.id, "not-a-real-key")
        assert resp.status_code == 400  # type: ignore[attr-defined]
        assert db_session.execute(select(TaxonomyFieldDef)).first() is None

    def test_same_node_pick_twice_400(self, client: TestClient, db_session: Session) -> None:
        node = _make_node(db_session)
        _login_as(client, _make_user(db_session, email="m@x.test", role=Role.MANAGER))
        assert _pick(client, node.id, "ring_size").status_code == 303  # type: ignore[attr-defined]
        resp = _pick(client, node.id, "ring_size")
        assert resp.status_code == 400  # type: ignore[attr-defined]


class TestTreeUniqueness:
    def test_parent_already_picks_blocks_child(
        self, client: TestClient, db_session: Session
    ) -> None:
        parent = _make_node(db_session, "Rings")
        child = TaxonomyNode(name="Silver", parent_id=parent.id)
        db_session.add(child)
        db_session.commit()
        _login_as(client, _make_user(db_session, email="m@x.test", role=Role.MANAGER))
        _pick(client, parent.id, "ring_size")
        resp = _pick(client, child.id, "ring_size")
        assert resp.status_code == 400  # type: ignore[attr-defined]

    def test_siblings_can_share(self, client: TestClient, db_session: Session) -> None:
        parent = _make_node(db_session, "Rings")
        a = TaxonomyNode(name="Silver", parent_id=parent.id)
        b = TaxonomyNode(name="Gold", parent_id=parent.id)
        db_session.add_all([a, b])
        db_session.commit()
        db_session.refresh(a)
        db_session.refresh(b)
        _login_as(client, _make_user(db_session, email="m@x.test", role=Role.MANAGER))
        assert _pick(client, a.id, "ring_size").status_code == 303  # type: ignore[attr-defined]
        assert _pick(client, b.id, "ring_size").status_code == 303  # type: ignore[attr-defined]
