"""Integration tests for the Manager-owned metals admin.

Two route surfaces:
- ``/admin/metals`` — CRUD for ``metal_master``.
- ``/admin/metal-prices`` — daily entries spec §2.2 v1 path.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AlloyFamily,
    AuditLog,
    Metal,
    MetalColour,
    MetalSpotPrice,
    Role,
    User,
    UserStatus,
)


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


def _make_metal(db: Session, code: str = "18KYG", *, alloy: AlloyFamily = AlloyFamily.GOLD) -> Metal:
    metal = Metal(
        metal_code=code,
        name=f"Test {code}",
        alloy_family=alloy,
        karat=18 if alloy is AlloyFamily.GOLD else None,
        purity_pct=Decimal("75.000"),
        colour=MetalColour.YELLOW if alloy is AlloyFamily.GOLD else MetalColour.PLATINUM,
    )
    db.add(metal)
    db.commit()
    db.refresh(metal)
    return metal


def _audit_rows(db: Session, *, entity_type: str, action: str | None = None) -> list[AuditLog]:
    stmt = select(AuditLog).where(AuditLog.entity_type == entity_type).order_by(AuditLog.id)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    return list(db.execute(stmt).scalars().all())


# ---------------------------------------------------------------------------
# Role enforcement
# ---------------------------------------------------------------------------


class TestRoleEnforcement:
    def test_anonymous_metals_is_401(self, client: TestClient) -> None:
        assert client.get("/admin/metals").status_code == 401

    def test_workshop_metals_is_403(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        assert client.get("/admin/metals").status_code == 403

    def test_office_metals_is_403(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, u)
        assert client.get("/admin/metals").status_code == 403

    def test_manager_metals_is_200(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        assert client.get("/admin/metals").status_code == 200

    def test_anonymous_prices_is_401(self, client: TestClient) -> None:
        assert client.get("/admin/metal-prices").status_code == 401

    def test_workshop_prices_is_403(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        assert client.get("/admin/metal-prices").status_code == 403


# ---------------------------------------------------------------------------
# Metals CRUD
# ---------------------------------------------------------------------------


class TestMetalCreate:
    def test_happy_path(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.post(
            "/admin/metals",
            data={
                "metal_code": "9krg",
                "name": "9ct Rose Gold",
                "alloy_family": "gold",
                "karat": "9",
                "purity_pct": "37.500",
                "colour": "rose",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text
        row = db_session.execute(
            select(Metal).where(Metal.metal_code == "9KRG")
        ).scalar_one()
        # Code is auto-uppercased on input.
        assert row.metal_code == "9KRG"
        assert row.alloy_family is AlloyFamily.GOLD
        assert row.purity_pct == Decimal("37.500")
        audit = _audit_rows(db_session, entity_type="metal", action="metal.created")
        assert len(audit) == 1

    def test_karat_on_non_gold_rejected(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.post(
            "/admin/metals",
            data={
                "metal_code": "PT-CUSTOM",
                "name": "Custom Platinum",
                "alloy_family": "platinum",
                "karat": "18",  # nonsense for platinum
                "purity_pct": "95.000",
                "colour": "platinum",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert "karat applies only to gold" in resp.text

    def test_duplicate_code_rejected(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _make_metal(db_session, code="18KYG")
        _login_as(client, u)
        resp = client.post(
            "/admin/metals",
            data={
                "metal_code": "18KYG",  # already in seed-derived test data
                "name": "Conflict",
                "alloy_family": "gold",
                "karat": "18",
                "purity_pct": "75.000",
                "colour": "yellow",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_purity_out_of_range_rejected(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.post(
            "/admin/metals",
            data={
                "metal_code": "BAD-PURE",
                "name": "Bad purity",
                "alloy_family": "gold",
                "karat": "18",
                "purity_pct": "150.000",
                "colour": "yellow",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400


class TestMetalEdit:
    def test_happy_update(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        metal = _make_metal(db_session)
        _login_as(client, u)
        resp = client.post(
            f"/admin/metals/{metal.id}",
            data={
                "metal_code": metal.metal_code,
                "name": "Renamed yellow gold",
                "alloy_family": "gold",
                "karat": "18",
                "purity_pct": "75.000",
                "colour": "yellow",
                "hallmark_stamp": "750",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(metal)
        assert metal.name == "Renamed yellow gold"
        assert metal.hallmark_stamp == "750"
        audit = _audit_rows(db_session, entity_type="metal", action="metal.updated")
        assert len(audit) == 1
        # Diff records only changed fields.
        assert "name" in audit[0].after_json
        assert "hallmark_stamp" in audit[0].after_json


class TestMetalArchive:
    def test_archive_unarchive(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        metal = _make_metal(db_session)
        _login_as(client, u)
        # Archive.
        resp = client.post(
            f"/admin/metals/{metal.id}/archive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(metal)
        assert metal.archived_at is not None
        # Unarchive.
        resp = client.post(
            f"/admin/metals/{metal.id}/unarchive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(metal)
        assert metal.archived_at is None


# ---------------------------------------------------------------------------
# Metal prices
# ---------------------------------------------------------------------------


class TestMetalPrices:
    def test_create_price(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        metal = _make_metal(db_session)
        _login_as(client, u)
        resp = client.post(
            "/admin/metal-prices",
            data={
                "metal_id": str(metal.id),
                "as_of_date": "2026-05-15",
                "price_per_gram": "125.500000",
                "source": "manual",
                "notes": "Friday close",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text
        row = db_session.execute(select(MetalSpotPrice)).scalar_one()
        assert row.metal_id == metal.id
        assert row.as_of_date == date(2026, 5, 15)
        assert row.price_per_gram == Decimal("125.500000")
        assert row.source == "manual"
        audit = _audit_rows(
            db_session, entity_type="metal_price", action="metal_price.created"
        )
        assert len(audit) == 1

    def test_duplicate_date_rejected(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        metal = _make_metal(db_session)
        db_session.add(
            MetalSpotPrice(
                metal_id=metal.id,
                as_of_date=date(2026, 5, 15),
                price_per_gram=Decimal("100.0"),
                source="manual",
            )
        )
        db_session.commit()
        _login_as(client, u)
        resp = client.post(
            "/admin/metal-prices",
            data={
                "metal_id": str(metal.id),
                "as_of_date": "2026-05-15",
                "price_per_gram": "105.0",
                "source": "manual",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert "already exists" in resp.text

    def test_zero_price_rejected(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        metal = _make_metal(db_session)
        _login_as(client, u)
        resp = client.post(
            "/admin/metal-prices",
            data={
                "metal_id": str(metal.id),
                "as_of_date": "2026-05-15",
                "price_per_gram": "0",
                "source": "manual",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_archived_metal_rejected(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        metal = _make_metal(db_session)
        metal.archived_at = datetime(2026, 1, 1, tzinfo=UTC)
        db_session.commit()
        _login_as(client, u)
        resp = client.post(
            "/admin/metal-prices",
            data={
                "metal_id": str(metal.id),
                "as_of_date": "2026-05-15",
                "price_per_gram": "125.0",
                "source": "manual",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_filter_by_metal(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        metal_a = _make_metal(db_session, code="18KYG")
        metal_b = _make_metal(
            db_session, code="PLAT950", alloy=AlloyFamily.PLATINUM
        )
        db_session.add_all(
            [
                MetalSpotPrice(
                    metal_id=metal_a.id,
                    as_of_date=date(2026, 5, 15),
                    price_per_gram=Decimal("125.0"),
                    source="manual",
                ),
                MetalSpotPrice(
                    metal_id=metal_b.id,
                    as_of_date=date(2026, 5, 15),
                    price_per_gram=Decimal("48.5"),
                    source="manual",
                ),
            ]
        )
        db_session.commit()
        _login_as(client, u)
        # Filter narrows to one metal's history.
        resp = client.get(f"/admin/metal-prices?metal_id={metal_a.id}")
        assert resp.status_code == 200
        assert "125.0" in resp.text
        assert "48.5" not in resp.text

    def test_update_price(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        metal = _make_metal(db_session)
        price = MetalSpotPrice(
            metal_id=metal.id,
            as_of_date=date(2026, 5, 15),
            price_per_gram=Decimal("100.0"),
            source="manual",
        )
        db_session.add(price)
        db_session.commit()
        db_session.refresh(price)
        _login_as(client, u)
        resp = client.post(
            f"/admin/metal-prices/{price.id}",
            data={
                "metal_id": str(metal.id),
                "as_of_date": "2026-05-15",
                "price_per_gram": "125.0",  # correction
                "source": "manual",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(price)
        assert price.price_per_gram == Decimal("125.0")
        audit = _audit_rows(
            db_session, entity_type="metal_price", action="metal_price.updated"
        )
        assert len(audit) == 1
