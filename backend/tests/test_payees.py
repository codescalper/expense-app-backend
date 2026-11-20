from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest

from app.core.errors import BadRequestError
from app.core.security import utcnow
from app.db.session import get_database
from app.models.enums import RecordScope, TransactionStatus, TransactionType
from app.services.payees import PayeeService, keyword_category
from tests.helpers_catalog import (
    API,
    BUILTIN,
    create_family,
    household,
    insert_tx,
    user,
)

FOOD = BUILTIN["Food & Dining"]
GROCERIES = BUILTIN["Groceries"]
SHOPPING = BUILTIN["Shopping"]
FUEL = BUILTIN["Fuel"]


async def make(client, who, **body: Any) -> dict[str, Any]:
    r = await client.post(f"{API}/payees", headers=who.h, json=body)
    assert r.status_code == 201, r.text
    return dict(r.json())


async def suggest(client, who, **params: Any) -> dict[str, Any]:
    r = await client.get(f"{API}/payees/suggest-category", headers=who.h, params=params)
    assert r.status_code == 200, r.text
    return dict(r.json())


async def touch(owner_id: str, upi_id: str, name: str | None = None, **kw: Any) -> uuid.UUID:
    family_id = kw.pop("family_id", None)
    async with get_database().sessionmaker() as s:
        payee = await PayeeService(s).touch(
            uuid.UUID(owner_id),
            RecordScope.FAMILY if family_id else RecordScope.PERSONAL,
            uuid.UUID(family_id) if family_id else None,
            upi_id,
            name,
            **kw,
        )
        await s.commit()
        return payee.id


# --- CRUD & validation -----------------------------------------------------------------


async def test_payee_crud(client):
    asha = await user(client, "asha")
    p = await make(
        client,
        asha,
        name="Ravi Kirana",
        upi_id="  Ravi.Kirana-01@OKAXIS ",
        mobile="+91 98765-43210",
        merchant_code="5411",
        default_category_id=GROCERIES,
    )
    assert p["upi_id"] == "ravi.kirana-01@okaxis" and p["mobile"] == "9876543210"
    assert p["scope"] == "personal" and p["family_id"] is None and p["owner_user_id"] == asha.id
    assert p["use_count"] == 0 and p["last_used_at"] is None
    await make(client, asha, name="Milkman", mobile="9123456789")
    await make(client, asha, name="Amma", upi_id="amma@ybl")

    got = await client.get(f"{API}/payees/{p['id']}", headers=asha.h)
    assert got.status_code == 200 and got.json()["name"] == "Ravi Kirana"

    page = (await client.get(f"{API}/payees", headers=asha.h)).json()
    assert [x["name"] for x in page["items"]] == ["Amma", "Milkman", "Ravi Kirana"]
    assert page["total"] == 3 and page["has_more"] is False
    for q, expected in (("kirana", ["Ravi Kirana"]), ("YBL", ["Amma"]), ("91234", ["Milkman"])):
        r = await client.get(f"{API}/payees", headers=asha.h, params={"q": q})
        assert [x["name"] for x in r.json()["items"]] == expected, q
    r = await client.get(f"{API}/payees", headers=asha.h, params={"limit": 2})
    assert len(r.json()["items"]) == 2 and r.json()["has_more"] is True

    r = await client.patch(
        f"{API}/payees/{p['id']}",
        headers=asha.h,
        json={"name": "Ravi Stores", "mobile": None, "default_category_id": None},
    )
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Ravi Stores" and r.json()["mobile"] is None
    assert r.json()["default_category_id"] is None and r.json()["upi_id"] == p["upi_id"]

    r = await client.delete(f"{API}/payees/{p['id']}", headers=asha.h)
    assert r.status_code == 204
    assert (await client.get(f"{API}/payees/{p['id']}", headers=asha.h)).status_code == 404
    # the VPA can be saved again once the old payee is deleted
    await make(client, asha, name="Ravi again", upi_id="ravi.kirana-01@okaxis")


@pytest.mark.parametrize(
    "body",
    [
        {"name": "X", "upi_id": "plainname"},
        {"name": "X", "upi_id": "x@bank"},  # local part too short
        {"name": "X", "upi_id": "abc@b"},  # handle too short
        {"name": "X", "upi_id": "abc@1bank"},  # handle must start with a letter
        {"name": "X", "upi_id": "a b@bank"},
        {"name": "X", "upi_id": "abc@bank@x"},
        {"name": "X", "upi_id": "a" * 250 + "@okaxis"},  # longer than the column
        {"name": "X", "mobile": "12345"},
        {"name": "X", "mobile": "5876543210"},
        {"name": "X", "mobile": "+1 9876543210"},
        {"name": "X", "merchant_code": "54a1"},
        {"name": ""},
        {"name": "X", "scope": "family"},
        {"name": "X", "use_count": 5},
    ],
)
async def test_payee_validation(client, body):
    asha = await user(client, "asha")
    r = await client.post(f"{API}/payees", headers=asha.h, json=body)
    assert r.status_code == 422, r.text


async def test_duplicate_vpa_and_default_category_checks(client):
    fam, p = await household(client)
    owner, admin = p["owner"], p["admin"]
    first = await make(client, owner, name="Shop", upi_id="shop@okicici")
    r = await client.post(
        f"{API}/payees", headers=owner.h, json={"name": "Shop 2", "upi_id": "SHOP@okicici"}
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "payee_exists"
    assert r.json()["error"]["details"] == {"payee_id": first["id"]}
    # same VPA: fine for another user, and once per family (whoever created it)
    await make(client, admin, name="Shop", upi_id="shop@okicici")
    await make(client, owner, name="Shop", upi_id="shop@okicici", scope="family", family_id=fam)
    r = await client.post(
        f"{API}/payees",
        headers=admin.h,
        json={"name": "Shop", "upi_id": "shop@okicici", "scope": "family", "family_id": fam},
    )
    assert r.status_code == 409
    second = await make(client, owner, name="Other", upi_id="other@okicici")
    r = await client.patch(
        f"{API}/payees/{second['id']}", headers=owner.h, json={"upi_id": "shop@okicici"}
    )
    assert r.status_code == 409

    # default category must be usable in the payee's scope
    mine = (
        await client.post(f"{API}/categories", headers=owner.h, json={"name": "Owner Only"})
    ).json()
    admins = (
        await client.post(f"{API}/categories", headers=admin.h, json={"name": "Admin Only"})
    ).json()
    fam_cat = (
        await client.post(
            f"{API}/categories",
            headers=owner.h,
            json={"name": "Fam Cat", "scope": "family", "family_id": fam},
        )
    ).json()
    await make(client, owner, name="A", default_category_id=mine["id"])
    await make(client, owner, name="B", scope="family", family_id=fam, default_category_id=FOOD)
    await make(
        client, owner, name="C", scope="family", family_id=fam, default_category_id=fam_cat["id"]
    )
    for body in (
        {"name": "D", "default_category_id": admins["id"]},
        {"name": "D", "default_category_id": fam_cat["id"]},
        {"name": "D", "default_category_id": str(uuid.uuid4())},
        {"name": "D", "scope": "family", "family_id": fam, "default_category_id": mine["id"]},
    ):
        r = await client.post(f"{API}/payees", headers=owner.h, json=body)
        assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_category", body


async def test_family_payee_roles_and_isolation(client):
    fam, p = await household(client)
    stranger = await user(client, "stranger")
    fam_s = await create_family(client, stranger, "Stranger Family")
    shared = await make(client, p["admin"], name="Society", scope="family", family_id=fam)
    personal = await make(client, p["member"], name="Member Only")
    strangers = await make(client, stranger, name="Stranger Payee")
    strangers_fam = await make(
        client, stranger, name="Stranger Fam Payee", scope="family", family_id=fam_s
    )

    for role in ("member", "viewer"):
        r = await client.post(
            f"{API}/payees",
            headers=p[role].h,
            json={"name": "Nope", "scope": "family", "family_id": fam},
        )
        assert r.status_code == 403
        listed = await client.get(f"{API}/payees", headers=p[role].h, params={"family_id": fam})
        assert [x["id"] for x in listed.json()["items"]] == [shared["id"]]
        assert (
            await client.get(f"{API}/payees/{shared['id']}", headers=p[role].h)
        ).status_code == 200
        r = await client.patch(
            f"{API}/payees/{shared['id']}", headers=p[role].h, json={"name": "Changed"}
        )
        assert r.status_code == 403
        r = await client.delete(f"{API}/payees/{shared['id']}", headers=p[role].h)
        assert r.status_code == 403
    r = await client.patch(
        f"{API}/payees/{shared['id']}", headers=p["owner"].h, json={"name": "Society Maint."}
    )
    assert r.status_code == 200

    # personal list never includes family payees and vice versa
    own = await client.get(f"{API}/payees", headers=p["member"].h)
    assert [x["id"] for x in own.json()["items"]] == [personal["id"]]

    # stranger sees none of it, and nobody sees the stranger's payees
    r = await client.get(f"{API}/payees", headers=stranger.h, params={"family_id": fam})
    assert r.status_code == 404
    for path in ("", "/recent", "/frequent", "/suggest-category"):
        r = await client.get(f"{API}/payees{path}", headers=stranger.h, params={"family_id": fam})
        assert r.status_code == 404, path
    for target in (shared["id"], personal["id"]):
        assert (await client.get(f"{API}/payees/{target}", headers=stranger.h)).status_code == 404
        r = await client.patch(f"{API}/payees/{target}", headers=stranger.h, json={"name": "x"})
        assert r.status_code == 404
        assert (
            await client.delete(f"{API}/payees/{target}", headers=stranger.h)
        ).status_code == 404
    for target in (strangers["id"], strangers_fam["id"]):
        r = await client.get(f"{API}/payees/{target}", headers=p["owner"].h)
        assert r.status_code == 404


# --- touch / recent / frequent ---------------------------------------------------------


async def test_touch_upserts_and_feeds_recent_and_frequent(client):
    fam, p = await household(client)
    owner, member = p["owner"], p["member"]
    named = await make(client, owner, name="My Grocer", upi_id="grocer@okaxis")
    await make(client, owner, name="Never Used", upi_id="never@okaxis")

    t0 = utcnow() - timedelta(hours=3)
    grocer_id = await touch(owner.id, "GROCER@okaxis", "Some Other Name", used_at=t0)
    assert str(grocer_id) == named["id"]
    await touch(owner.id, "grocer@okaxis", used_at=t0 + timedelta(minutes=1))
    new_id = await touch(
        owner.id,
        "chai.wala@ybl",
        None,
        mobile="+91 9988776655",
        merchant_code="bad",
        default_category_id=FOOD,
        used_at=t0 + timedelta(hours=1),
    )
    async with get_database().sessionmaker() as s:
        with pytest.raises(BadRequestError):
            await PayeeService(s).touch(
                uuid.UUID(owner.id), RecordScope.PERSONAL, None, "not-a-vpa", "X"
            )

    r = await client.get(f"{API}/payees/{new_id}", headers=owner.h)
    created = r.json()
    assert created["name"] == "chai.wala" and created["mobile"] == "9988776655"
    assert created["merchant_code"] is None and created["default_category_id"] == FOOD
    assert created["use_count"] == 1
    grocer = (await client.get(f"{API}/payees/{named['id']}", headers=owner.h)).json()
    assert grocer["name"] == "My Grocer" and grocer["use_count"] == 2

    recent = (await client.get(f"{API}/payees/recent", headers=owner.h)).json()
    assert [x["name"] for x in recent] == ["chai.wala", "My Grocer"]
    frequent = (await client.get(f"{API}/payees/frequent", headers=owner.h)).json()
    assert [x["name"] for x in frequent] == ["My Grocer", "chai.wala"]
    r = await client.get(f"{API}/payees/recent", headers=owner.h, params={"limit": 1})
    assert len(r.json()) == 1
    assert (await client.get(f"{API}/payees/recent", headers=member.h)).json() == []

    # family scope: one payee per VPA per family, whoever pays
    fam_id = await touch(member.id, "society@okhdfc", "Society", family_id=fam)
    again = await touch(owner.id, "society@okhdfc", "Society", family_id=fam)
    assert fam_id == again
    fam_recent = await client.get(
        f"{API}/payees/recent", headers=p["viewer"].h, params={"family_id": fam}
    )
    assert [(x["name"], x["use_count"]) for x in fam_recent.json()] == [("Society", 2)]
    # the family touch did not create personal payees
    personal = (await client.get(f"{API}/payees", headers=member.h)).json()
    assert personal["items"] == []


# --- category suggestions --------------------------------------------------------------


def test_keyword_matching_rules():
    def kw(*texts: str | None) -> str | None:
        found = keyword_category(*texts)
        return str(found) if found else None

    assert kw("swiggy.instamart@icici") == FOOD
    assert kw("BIGBASKET@okaxis") == GROCERIES
    assert kw(None, "HPCL petrol pump") == FUEL
    assert kw("ajio@hdfcbank") == SHOPPING  # not "jio"
    assert kw("jiomart@okaxis") == GROCERIES  # not "jio"
    assert kw("jio.recharge@paytm") == BUILTIN["Mobile Recharge"]
    assert kw("vivek@okaxis") is None  # not "vi"
    assert kw("coca-cola") is None  # not "ola"
    assert kw("amazonprime@apl") == BUILTIN["Subscriptions"]
    assert kw("amazon@apl") == SHOPPING
    assert kw("Tata Power DDL") == BUILTIN["Electricity"]
    assert kw("", None) is None


async def test_suggest_category_sources(client):
    asha, bob = await user(client, "asha"), await user(client, "bob")
    # (c) keywords
    r = await suggest(client, asha, upi_id="swiggy.stores@axb")
    assert (r["category_id"], r["source"], r["confidence"]) == (FOOD, "keyword", 0.35)
    assert r["category"]["name"] == "Food & Dining"
    r = await suggest(client, asha, merchant="Blinkit Commerce")
    assert (r["category_id"], r["source"]) == (GROCERIES, "keyword")
    r = await suggest(client, asha, q="petrol at highway")
    assert (r["category_id"], r["source"]) == (FUEL, "keyword")
    assert await suggest(client, asha, upi_id="vivek@okaxis") == {
        "category_id": None,
        "source": "none",
        "confidence": 0.0,
        "category": None,
    }
    assert (await suggest(client, asha))["source"] == "none"

    # (b) the payee's default category beats keywords
    pets = (await client.post(f"{API}/categories", headers=asha.h, json={"name": "Pets"})).json()
    payee = await make(
        client,
        asha,
        name="Zomato pet food",
        upi_id="petfood@okaxis",
        default_category_id=pets["id"],
    )
    for params in ({"payee_id": payee["id"]}, {"upi_id": "PETFOOD@okaxis"}):
        r = await suggest(client, asha, **params)
        assert (r["category_id"], r["source"], r["confidence"]) == (
            pets["id"],
            "payee_default",
            0.6,
        )

    # (a) history beats both: most-used category on completed expenses for the payee
    await insert_tx(asha, payee_id=payee["id"], category_id=SHOPPING)
    await insert_tx(asha, payee_id=payee["id"], category_id=SHOPPING)
    await insert_tx(asha, payee_id=payee["id"], category_id=GROCERIES)
    # ignored: pending, deleted, income, other users' transactions
    for kw in (
        {"status": TransactionStatus.PENDING},
        {"deleted": True},
        {"type_": TransactionType.INCOME, "category_id": BUILTIN["Salary"]},
    ):
        await insert_tx(asha, payee_id=payee["id"], **{"category_id": FOOD, **kw})
    for _ in range(3):
        await insert_tx(bob, payee_id=payee["id"], category_id=FOOD)
    for params in ({"payee_id": payee["id"]}, {"upi_id": "petfood@okaxis"}):
        r = await suggest(client, asha, **params)
        assert (r["category_id"], r["source"], r["confidence"]) == (SHOPPING, "history", 0.5)

    # history by merchant name (case-insensitive)
    await insert_tx(asha, merchant="Corner Bakery", category_id=FOOD)
    r = await suggest(client, asha, merchant="corner bakery")
    assert (r["category_id"], r["source"]) == (FOOD, "history")

    # someone else's payee → 404; bad VPA → 422
    bobs = await make(client, bob, name="Bob's", upi_id="bobs@okaxis")
    r = await client.get(
        f"{API}/payees/suggest-category", headers=asha.h, params={"payee_id": bobs["id"]}
    )
    assert r.status_code == 404
    r = await client.get(
        f"{API}/payees/suggest-category", headers=asha.h, params={"upi_id": "no-at-sign"}
    )
    assert r.status_code == 422


async def test_suggestion_respects_family_context(client):
    fam, p = await household(client)
    owner, member = p["owner"], p["member"]
    mine = (await client.post(f"{API}/categories", headers=owner.h, json={"name": "Mine"})).json()
    fam_cat = (
        await client.post(
            f"{API}/categories",
            headers=owner.h,
            json={"name": "Household", "scope": "family", "family_id": fam},
        )
    ).json()
    # owner's personal history uses a personal category; family history uses a family one
    for _ in range(3):
        await insert_tx(owner, merchant="Local Store", category_id=mine["id"])
    await insert_tx(member, family_id=fam, merchant="Local Store", category_id=fam_cat["id"])

    r = await suggest(client, owner, merchant="Local Store")
    assert (r["category_id"], r["source"]) == (mine["id"], "history")
    # in the family context a personal category can't be suggested
    r = await suggest(client, owner, merchant="Local Store", family_id=fam)
    assert (r["category_id"], r["source"]) == (fam_cat["id"], "history")
    # the viewer sees the family transaction, so gets the same family suggestion
    r = await suggest(client, p["viewer"], merchant="Local Store", family_id=fam)
    assert r["category_id"] == fam_cat["id"]
    # the viewer's personal context can't use the family category → falls through
    r = await suggest(client, p["viewer"], merchant="Local Store")
    assert r["source"] == "none"
