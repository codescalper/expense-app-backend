from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from app.db.session import get_database
from app.models.enums import TransactionType
from app.models.finance import Payee
from app.models.system import AuditLog
from tests.conftest import BUILTIN_CATEGORIES
from tests.helpers_catalog import (
    API,
    BUILTIN,
    create_family,
    household,
    insert_tx,
    tx_category,
    user,
)

FOOD = BUILTIN["Food & Dining"]
GROCERIES = BUILTIN["Groceries"]
SALARY = BUILTIN["Salary"]


async def make(client, who, **body: Any) -> dict[str, Any]:
    r = await client.post(f"{API}/categories", headers=who.h, json=body)
    assert r.status_code == 201, r.text
    return dict(r.json())


def ids(tree: list[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for node in tree:
        out.add(node["id"])
        out |= {c["id"] for c in node["children"]}
    return out


async def tree(client, who, **params: Any) -> list[dict[str, Any]]:
    r = await client.get(f"{API}/categories", headers=who.h, params=params)
    assert r.status_code == 200, r.text
    return list(r.json())


# --- listing & tree --------------------------------------------------------------------


async def test_builtins_listed_as_tree_with_kind_filter(client):
    asha = await user(client, "asha")
    everything = await tree(client, asha)
    assert {c["id"] for c in everything} == {c["id"] for c in BUILTIN_CATEGORIES}
    assert all(c["ownership"] == "builtin" and c["is_builtin"] for c in everything)
    expense = await tree(client, asha, kind="expense")
    assert [c["name"] for c in expense][:3] == ["Food & Dining", "Groceries", "Rent"]
    assert len(expense) == 20 and {c["kind"] for c in expense} == {"expense"}
    income = await tree(client, asha, kind="income")
    assert len(income) == 6
    assert (await client.get(f"{API}/categories")).status_code == 401

    icons = (await client.get(f"{API}/categories/icons", headers=asha.h)).json()
    assert {c["icon"] for c in BUILTIN_CATEGORIES} <= set(icons)
    assert len(icons) >= 60


async def test_personal_categories_and_subcategories(client):
    asha = await user(client, "asha")
    coffee = await make(client, asha, name="Coffee", parent_id=FOOD, icon="coffee", color="#a0b1c2")
    assert coffee["ownership"] == "personal" and coffee["parent_id"] == FOOD
    assert coffee["color"] == "#A0B1C2" and coffee["icon"] == "coffee"
    pets = await make(client, asha, name="Pets", icon="paw-print", sort_order=50)

    listed = await tree(client, asha, kind="expense")
    food = next(c for c in listed if c["id"] == FOOD)
    assert [c["name"] for c in food["children"]] == ["Coffee"]
    assert any(c["id"] == pets["id"] and c["children"] == [] for c in listed)
    got = (await client.get(f"{API}/categories/{FOOD}", headers=asha.h)).json()
    assert [c["id"] for c in got["children"]] == [coffee["id"]]

    # depth is limited to 2, parent must be the same kind and visible
    for body in (
        {"name": "Espresso", "parent_id": coffee["id"]},
        {"name": "Bonus", "parent_id": FOOD, "kind": "income"},
        {"name": "Tips", "parent_id": SALARY},
        {"name": "Ghost", "parent_id": str(uuid.uuid4())},
    ):
        r = await client.post(f"{API}/categories", headers=asha.h, json=body)
        assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_parent", body

    # validation
    for bad in (
        {"name": "X", "icon": "not-an-icon"},
        {"name": "X", "color": "red"},
        {"name": "X", "color": "#12345"},
        {"name": ""},
        {"name": "x" * 61},
        {"name": "X", "scope": "family"},
        {"name": "X", "family_id": str(uuid.uuid4())},
        {"name": "X", "owner_user_id": asha.id},
    ):
        r = await client.post(f"{API}/categories", headers=asha.h, json=bad)
        assert r.status_code == 422, bad


async def test_names_unique_case_insensitively(client):
    asha = await user(client, "asha")
    await make(client, asha, name="Pets")
    for body in (
        {"name": "pets"},
        {"name": " PETS "},
        {"name": "groceries"},  # clashes with a built-in sibling
    ):
        r = await client.post(f"{API}/categories", headers=asha.h, json=body)
        assert r.status_code == 409 and r.json()["error"]["code"] == "category_name_taken", body
    # same name elsewhere is fine: another parent, another kind, another user
    await make(client, asha, name="Pets", parent_id=FOOD)
    await make(client, asha, name="Pets", kind="income")
    bob = await user(client, "bob")
    await make(client, bob, name="Pets")
    # renaming onto an existing sibling name conflicts too
    vet = await make(client, asha, name="Vet")
    r = await client.patch(f"{API}/categories/{vet['id']}", headers=asha.h, json={"name": "PETS"})
    assert r.status_code == 409


async def test_update_category(client):
    asha = await user(client, "asha")
    pets = await make(client, asha, name="Pets", icon="dog")
    food_sub = await make(client, asha, name="Snacks", parent_id=FOOD)

    r = await client.patch(
        f"{API}/categories/{pets['id']}",
        headers=asha.h,
        json={"name": "Pet care", "icon": None, "color": "#00ff00", "sort_order": 3},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["name"], body["icon"], body["color"], body["sort_order"]) == (
        "Pet care",
        None,
        "#00FF00",
        3,
    )
    # move a top-level category under a built-in, then back to top level
    r = await client.patch(
        f"{API}/categories/{pets['id']}", headers=asha.h, json={"parent_id": GROCERIES}
    )
    assert r.status_code == 200 and r.json()["parent_id"] == GROCERIES
    r = await client.patch(
        f"{API}/categories/{pets['id']}", headers=asha.h, json={"parent_id": None}
    )
    assert r.status_code == 200 and r.json()["parent_id"] is None

    # a category with children can't become a child; can't parent itself
    await make(client, asha, name="Food", parent_id=pets["id"])
    for parent in (GROCERIES, pets["id"]):
        r = await client.patch(
            f"{API}/categories/{pets['id']}", headers=asha.h, json={"parent_id": parent}
        )
        assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_parent"
    # a child can't be moved under another child
    r = await client.patch(
        f"{API}/categories/{food_sub['id']}", headers=asha.h, json={"parent_id": food_sub["id"]}
    )
    assert r.status_code == 422
    for bad in ({"name": None}, {"sort_order": None}, {"kind": "income"}, {"icon": "nope"}):
        r = await client.patch(f"{API}/categories/{pets['id']}", headers=asha.h, json=bad)
        assert r.status_code == 422, bad


async def test_builtin_categories_are_read_only(client):
    asha = await user(client, "asha")
    for method, path, kwargs in (
        ("PATCH", f"/categories/{FOOD}", {"json": {"name": "Eating"}}),
        ("POST", f"/categories/{FOOD}/archive", {}),
        ("POST", f"/categories/{FOOD}/unarchive", {}),
        ("DELETE", f"/categories/{FOOD}", {}),
    ):
        r = await client.request(method, f"{API}{path}", headers=asha.h, **kwargs)
        assert r.status_code == 403, (method, path)
        assert r.json()["error"]["code"] == "builtin_category"


async def test_archive_and_unarchive(client):
    asha = await user(client, "asha")
    pets = await make(client, asha, name="Pets")
    vet = await make(client, asha, name="Vet", parent_id=pets["id"])
    coffee = await make(client, asha, name="Coffee", parent_id=FOOD)

    r = await client.post(f"{API}/categories/{pets['id']}/archive", headers=asha.h)
    assert r.status_code == 200 and r.json()["is_archived"] is True
    await client.post(f"{API}/categories/{coffee['id']}/archive", headers=asha.h)
    visible = ids(await tree(client, asha))
    assert pets["id"] not in visible and vet["id"] not in visible and coffee["id"] not in visible
    everything = ids(await tree(client, asha, include_archived="true"))
    assert {pets["id"], vet["id"], coffee["id"]} <= everything
    # archived categories can't be used as a parent
    r = await client.post(
        f"{API}/categories", headers=asha.h, json={"name": "Toys", "parent_id": pets["id"]}
    )
    assert r.status_code == 422

    r = await client.post(f"{API}/categories/{pets['id']}/unarchive", headers=asha.h)
    assert r.status_code == 200 and r.json()["is_archived"] is False
    assert {pets["id"], vet["id"]} <= ids(await tree(client, asha))


# --- isolation & family permissions ----------------------------------------------------


async def test_isolation_between_users_and_families(client):
    asha, bob = await user(client, "asha"), await user(client, "bob")
    fam_b = await create_family(client, bob, "Bob Family")
    mine = await make(client, asha, name="Asha Only")
    bobs = await make(client, bob, name="Bob Only")
    fam_cat = await make(client, bob, name="Bob Family Cat", scope="family", family_id=fam_b)
    fam_sub = await make(
        client, bob, name="Family Snacks", scope="family", family_id=fam_b, parent_id=FOOD
    )

    asha_view = ids(await tree(client, asha))
    assert mine["id"] in asha_view
    assert not {bobs["id"], fam_cat["id"], fam_sub["id"]} & asha_view
    bob_personal = ids(await tree(client, bob))
    assert bobs["id"] in bob_personal and fam_cat["id"] not in bob_personal
    bob_family = await tree(client, bob, family_id=fam_b)
    assert {fam_cat["id"], fam_sub["id"]} <= ids(bob_family) and bobs["id"] not in ids(bob_family)
    food = next(c for c in bob_family if c["id"] == FOOD)
    assert [c["id"] for c in food["children"]] == [fam_sub["id"]]

    r = await client.get(f"{API}/categories", headers=asha.h, params={"family_id": fam_b})
    assert r.status_code == 404
    r = await client.get(
        f"{API}/categories", headers=asha.h, params={"family_id": str(uuid.uuid4())}
    )
    assert r.status_code == 404
    for target in (bobs["id"], fam_cat["id"]):
        assert (await client.get(f"{API}/categories/{target}", headers=asha.h)).status_code == 404
        for method, suffix in (("PATCH", ""), ("POST", "/archive"), ("DELETE", "")):
            r = await client.request(
                method,
                f"{API}/categories/{target}{suffix}",
                headers=asha.h,
                json={"name": "hijack"} if method == "PATCH" else None,
            )
            assert r.status_code == 404, (method, suffix)
    # Asha can't create in Bob's family or hang a subcategory under Bob's categories
    r = await client.post(
        f"{API}/categories",
        headers=asha.h,
        json={"name": "X", "scope": "family", "family_id": fam_b},
    )
    assert r.status_code == 404
    r = await client.post(
        f"{API}/categories", headers=asha.h, json={"name": "X", "parent_id": bobs["id"]}
    )
    assert r.status_code == 422
    # a personal category can't hang under a family category (different context)
    r = await client.post(
        f"{API}/categories", headers=bob.h, json={"name": "X", "parent_id": fam_cat["id"]}
    )
    assert r.status_code == 422


async def test_family_category_roles(client):
    fam, p = await household(client)
    for role in ("owner", "admin"):
        await make(client, p[role], name=f"By {role}", scope="family", family_id=fam)
    for role in ("member", "viewer"):
        r = await client.post(
            f"{API}/categories",
            headers=p[role].h,
            json={"name": "Nope", "scope": "family", "family_id": fam},
        )
        assert r.status_code == 403 and r.json()["error"]["code"] == "insufficient_family_role"

    cat = await make(client, p["admin"], name="Shared", scope="family", family_id=fam)
    for role in ("member", "viewer"):
        listed = ids(await tree(client, p[role], family_id=fam))
        assert cat["id"] in listed
        got = await client.get(f"{API}/categories/{cat['id']}", headers=p[role].h)
        assert got.status_code == 200 and got.json()["ownership"] == "family"
        r = await client.patch(
            f"{API}/categories/{cat['id']}", headers=p[role].h, json={"name": "Changed"}
        )
        assert r.status_code == 403
        r = await client.delete(f"{API}/categories/{cat['id']}", headers=p[role].h)
        assert r.status_code == 403
    r = await client.patch(
        f"{API}/categories/{cat['id']}", headers=p["owner"].h, json={"name": "Changed"}
    )
    assert r.status_code == 200


# --- deletion --------------------------------------------------------------------------


async def test_delete_unused_category(client):
    asha = await user(client, "asha")
    pets = await make(client, asha, name="Pets")
    r = await client.delete(f"{API}/categories/{pets['id']}", headers=asha.h)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "deleted_ids": [pets["id"]],
        "reassigned_to": None,
        "moved_transactions": 0,
        "moved_recurring": 0,
        "moved_budget_limits": 0,
        "removed_budget_limits": 0,
    }
    assert (await client.get(f"{API}/categories/{pets['id']}", headers=asha.h)).status_code == 404
    assert pets["id"] not in ids(await tree(client, asha, include_archived="true"))
    # the name is free again
    await make(client, asha, name="Pets")
    r = await client.delete(f"{API}/categories/{pets['id']}", headers=asha.h)
    assert r.status_code == 404


async def test_delete_in_use_requires_reassign_and_moves_transactions(client):
    asha, bob = await user(client, "asha"), await user(client, "bob")
    pets = await make(client, asha, name="Pets")
    vet = await make(client, asha, name="Vet", parent_id=pets["id"])
    other = await make(client, asha, name="Animals")
    salary_sub = await make(client, asha, name="Freelance", kind="income")
    bobs = await make(client, bob, name="Bob Pets")
    tx1 = await insert_tx(asha, category_id=pets["id"])
    tx2 = await insert_tx(asha, category_id=vet["id"])
    deleted_tx = await insert_tx(asha, category_id=pets["id"], deleted=True)
    untouched = await insert_tx(asha, category_id=FOOD)
    async with get_database().sessionmaker() as s:
        payee = Payee(
            owner_user_id=uuid.UUID(asha.id),
            name="Vet clinic",
            default_category_id=uuid.UUID(vet["id"]),
        )
        s.add(payee)
        await s.commit()
        payee_id = payee.id

    r = await client.delete(f"{API}/categories/{pets['id']}", headers=asha.h)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "category_in_use"
    assert r.json()["error"]["details"] == {"transactions": 2, "recurring": 0}

    for target in (salary_sub["id"], bobs["id"], vet["id"], pets["id"], str(uuid.uuid4())):
        r = await client.delete(
            f"{API}/categories/{pets['id']}", headers=asha.h, params={"reassign_to": target}
        )
        assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_reassign_target"
    assert await tx_category(tx1) == pets["id"]  # nothing changed so far

    r = await client.delete(
        f"{API}/categories/{pets['id']}", headers=asha.h, params={"reassign_to": other["id"]}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["deleted_ids"]) == {pets["id"], vet["id"]}
    assert body["moved_transactions"] == 2 and body["reassigned_to"] == other["id"]
    for tx in (tx1, tx2, deleted_tx):
        assert await tx_category(tx) == other["id"]
    assert await tx_category(untouched) == FOOD
    async with get_database().sessionmaker() as s:
        reloaded = await s.get(Payee, payee_id)
        assert reloaded is not None and str(reloaded.default_category_id) == other["id"]
        actions = list(
            await s.scalars(select(AuditLog.action).where(AuditLog.entity_type == "category"))
        )
    assert actions.count("category.deleted") == 1
    listing = ids(await tree(client, asha, include_archived="true"))
    assert pets["id"] not in listing and vet["id"] not in listing

    # reassigning to a built-in works as well
    groceries_like = await make(client, asha, name="Veg")
    tx3 = await insert_tx(asha, category_id=groceries_like["id"], type_=TransactionType.REFUND)
    r = await client.delete(
        f"{API}/categories/{groceries_like['id']}",
        headers=asha.h,
        params={"reassign_to": GROCERIES},
    )
    assert r.status_code == 200 and await tx_category(tx3) == GROCERIES


async def test_delete_family_category_with_reassign(client):
    fam, p = await household(client)
    cat = await make(client, p["admin"], name="Shared", scope="family", family_id=fam)
    other_family = await create_family(client, p["owner"], "Second")
    elsewhere = await make(
        client, p["owner"], name="Elsewhere", scope="family", family_id=other_family
    )
    personal = await make(client, p["owner"], name="Mine")
    tx = await insert_tx(p["member"], family_id=fam, category_id=cat["id"])

    for target in (elsewhere["id"], personal["id"]):  # other contexts are not allowed
        r = await client.delete(
            f"{API}/categories/{cat['id']}", headers=p["owner"].h, params={"reassign_to": target}
        )
        assert r.status_code == 422
    r = await client.delete(
        f"{API}/categories/{cat['id']}", headers=p["member"].h, params={"reassign_to": FOOD}
    )
    assert r.status_code == 403
    r = await client.delete(
        f"{API}/categories/{cat['id']}", headers=p["owner"].h, params={"reassign_to": FOOD}
    )
    assert r.status_code == 200 and r.json()["moved_transactions"] == 1
    assert await tx_category(tx) == FOOD


# --- tags ------------------------------------------------------------------------------


async def test_tag_autocomplete(client):
    fam, p = await household(client)
    owner, member = p["owner"], p["member"]
    stranger = await user(client, "stranger")
    await insert_tx(owner, tags=("groceries", "weekly"))
    await insert_tx(owner, tags=("groceries",))
    await insert_tx(owner, tags=("secret",), deleted=True)
    await insert_tx(member, tags=("member-personal",))
    await insert_tx(member, family_id=fam, tags=("trip", "groceries"))
    await insert_tx(owner, family_id=fam, tags=("trip",))
    await insert_tx(stranger, tags=("stranger-tag",))

    r = await client.get(f"{API}/tags", headers=owner.h)
    assert r.status_code == 200
    assert r.json() == [{"name": "groceries", "count": 2}, {"name": "weekly", "count": 1}]
    r = await client.get(f"{API}/tags", headers=owner.h, params={"q": "WEE"})
    assert r.json() == [{"name": "weekly", "count": 1}]
    r = await client.get(f"{API}/tags", headers=p["viewer"].h, params={"family_id": fam})
    assert r.json() == [{"name": "trip", "count": 2}, {"name": "groceries", "count": 1}]
    r = await client.get(f"{API}/tags", headers=owner.h, params={"q": "%"})
    assert r.json() == []
    r = await client.get(f"{API}/tags", headers=stranger.h, params={"family_id": fam})
    assert r.status_code == 404
    r = await client.get(f"{API}/tags", headers=stranger.h)
    assert r.json() == [{"name": "stranger-tag", "count": 1}]
