"""Owner/admin group management: roster visibility, member removal, invite-code
lifecycle, and cross-tenant isolation.

The TestClient keeps one cookie jar, so these tests switch identity explicitly by
logging in as the actor a given request should come from. `seed` creates an owner but
does not log them in; `register` creates a member AND logs in as that new member.
"""
from .conftest import OWNER_PASSWORD, register


def _login(client, email, password):
    """Log in, replacing the client's session cookie with this user's."""
    res = client.post("/api/login", json={"email": email, "password": password})
    assert res.status_code == 200, f"login failed for {email}"
    return res


def _login_owner(client, email="owner@example.com"):
    """Log in as a seeded owner (all seeds use OWNER_PASSWORD)."""
    return _login(client, email, OWNER_PASSWORD)


def _code_state(fetchone, code):
    """Read a code's redemption_count/active directly from the DB."""
    row = fetchone(
        "SELECT redemption_count, active FROM invite_codes WHERE code = ?", (code,)
    )
    return {"redemption_count": row[0], "active": row[1]}


# ----- Access control -----
def test_unauthenticated_and_non_member_are_refused(client, seed):
    """No session -> 401; a member of a different group -> 403."""
    info = seed(code="SOLOCLUB", seats=8)
    # Unauthenticated.
    assert client.get(f"/api/groups/{info['group_id']}/members").status_code == 401

    # A member of a *different* group must not see this group.
    seed(code="OTHERCLUB", seats=8, email="owner2@example.com", group_name="Other")
    register(client, code="OTHERCLUB", email="stranger@example.com")
    assert client.get(f"/api/groups/{info['group_id']}/members").status_code == 403
    assert client.get(f"/api/groups/{info['group_id']}/invite-codes").status_code == 403


def test_member_can_view_roster_but_not_manage(client, seed):
    """A non-owner member reads the roster (no emails) but is 403 on owner endpoints."""
    info = seed(code="VIEWCLUB", seats=8)
    register(client, code="VIEWCLUB", email="viewer@example.com")  # now logged in as member

    roster = client.get(f"/api/groups/{info['group_id']}/members")
    assert roster.status_code == 200
    body = roster.json()
    assert body["is_owner"] is False
    # Non-owners never see email addresses.
    assert all("email" not in m for m in body["members"])
    assert {m["role"] for m in body["members"]} == {"owner", "member"}

    gid = info["group_id"]
    assert client.get(f"/api/groups/{gid}/invite-codes").status_code == 403
    assert client.post(f"/api/groups/{gid}/invite-codes", json={"max_redemptions": 5}).status_code == 403
    assert client.patch(
        f"/api/groups/{gid}/invite-codes/{info['invite_code_id']}/deactivate"
    ).status_code == 403
    assert client.delete(f"/api/groups/{gid}/members/{info['owner_user_id']}").status_code == 403


def test_owner_of_other_group_cannot_manage_this_one(client, seed):
    """Cross-tenant: an owner of group B cannot touch group A (the highest-risk bug class)."""
    a = seed(code="CLUBAAAA", seats=8, email="ownera@example.com", group_name="A")
    seed(code="CLUBBBBB", seats=8, email="ownerb@example.com", group_name="B")

    _login(client, "ownerb@example.com", OWNER_PASSWORD)
    gid = a["group_id"]
    assert client.get(f"/api/groups/{gid}/invite-codes").status_code == 403
    assert client.post(f"/api/groups/{gid}/invite-codes", json={"max_redemptions": 5}).status_code == 403
    assert client.delete(f"/api/groups/{gid}/members/{a['owner_user_id']}").status_code == 403


# ----- Member removal -----
def test_owner_removes_member_who_then_loses_access(client, seed, fetchone):
    """Owner removes a member; the member's account/profile survive but group access is gone."""
    info = seed(code="REMOVECLUB", seats=8)
    register(client, code="REMOVECLUB", email="bob@example.com", password="password1")
    bob = fetchone("SELECT id FROM users WHERE email = ?", ("bob@example.com",))
    bob_id = bob[0]

    _login_owner(client)
    res = client.delete(f"/api/groups/{info['group_id']}/members/{bob_id}")
    assert res.status_code == 200

    # The membership row is gone...
    assert fetchone(
        "SELECT 1 FROM memberships WHERE user_id = ? AND group_id = ?",
        (bob_id, info["group_id"]),
    ) is None
    # ...but Bob's account and profile remain intact.
    assert fetchone("SELECT id FROM users WHERE email = ?", ("bob@example.com",)) is not None

    # Bob logs back in: profile still works, group access is refused.
    _login(client, "bob@example.com", "password1")
    assert client.get("/api/profile").status_code == 200
    assert client.get(f"/api/groups/{info['group_id']}").status_code == 403


def test_cannot_remove_the_owner(client, seed):
    """Removing the group's owner via the endpoint is refused (400), even by the owner."""
    info = seed(code="OWNERCLUB", seats=8)
    _login_owner(client)
    res = client.delete(f"/api/groups/{info['group_id']}/members/{info['owner_user_id']}")
    assert res.status_code == 400


def test_removing_non_member_returns_404(client, seed):
    """Removing a user who isn't a member is a 404, not a 500."""
    info = seed(code="GHOSTCLUB", seats=8)
    _login_owner(client)
    res = client.delete(f"/api/groups/{info['group_id']}/members/999999")
    assert res.status_code == 404


# ----- Invite-code creation -----
def test_owner_creates_custom_code_and_member_registers(client, seed, fetchone):
    """Owner creates a code (normalized like seed_group.py); a new member redeems it."""
    info = seed(code="FIRSTCODE", seats=8)
    _login_owner(client)
    res = client.post(
        f"/api/groups/{info['group_id']}/invite-codes",
        json={"code": "wave2-code", "max_redemptions": 3},
    )
    assert res.status_code == 201
    assert res.json()["code"] == "WAVE2-CODE"  # normalization applied identically

    # Someone registers with the brand-new code (register logs in as them).
    reg = register(client, code="wave2-code", email="new@example.com")
    assert reg.status_code == 201
    # ...and lands in the same group.
    user = fetchone("SELECT id FROM users WHERE email = ?", ("new@example.com",))
    membership = fetchone(
        "SELECT role FROM memberships WHERE user_id = ? AND group_id = ?",
        (user[0], info["group_id"]),
    )
    assert membership is not None and membership[0] == "member"


def test_create_code_validation(client, seed):
    """Bad codes and out-of-range seat counts are rejected by server-side validation."""
    info = seed(code="VALIDCLUB", seats=8)
    _login_owner(client)
    gid = info["group_id"]
    # Too short (shared normalization rule: 4-32 chars).
    assert client.post(f"/api/groups/{gid}/invite-codes", json={"code": "ab", "max_redemptions": 3}).status_code == 422
    # Seat count out of range.
    assert client.post(f"/api/groups/{gid}/invite-codes", json={"code": "GOODCODE", "max_redemptions": 0}).status_code == 422
    assert client.post(f"/api/groups/{gid}/invite-codes", json={"code": "GOODCODE", "max_redemptions": 999}).status_code == 422
    # Duplicate of the existing seeded code -> conflict.
    assert client.post(f"/api/groups/{gid}/invite-codes", json={"code": "VALIDCLUB", "max_redemptions": 3}).status_code == 409


# ----- Deactivation -----
def test_deactivate_blocks_registration_and_is_idempotent(client, seed, fetchone):
    """Deactivating a code stops further registration; deactivating twice is a no-op success."""
    info = seed(code="KILLCLUB", seats=8)
    _login_owner(client)
    gid, cid = info["group_id"], info["invite_code_id"]

    assert client.patch(f"/api/groups/{gid}/invite-codes/{cid}/deactivate").status_code == 200
    # Idempotent: deactivating an already-inactive code still succeeds.
    assert client.patch(f"/api/groups/{gid}/invite-codes/{cid}/deactivate").status_code == 200
    assert _code_state(fetchone, "KILLCLUB")["active"] == 0

    # A new registrant can no longer redeem it.
    reg = register(client, code="KILLCLUB", email="late@example.com")
    assert reg.status_code == 400
    assert _code_state(fetchone, "KILLCLUB")["redemption_count"] == 0


def test_deactivate_unknown_code_returns_404(client, seed):
    """Deactivating a code that doesn't belong to the group is a 404."""
    info = seed(code="NOSUCHCLUB", seats=8)
    _login_owner(client)
    assert client.patch(
        f"/api/groups/{info['group_id']}/invite-codes/999999/deactivate"
    ).status_code == 404


# ----- Multiple active codes -----
def test_multiple_active_codes_are_independent(client, seed, fetchone):
    """Two active codes for one group track seats independently."""
    info = seed(code="CODEONE", seats=5)
    _login_owner(client)
    assert client.post(
        f"/api/groups/{info['group_id']}/invite-codes",
        json={"code": "CODETWO", "max_redemptions": 5},
    ).status_code == 201

    # Redeem CODEONE once (register logs in as the new member).
    assert register(client, code="CODEONE", email="p1@example.com").status_code == 201

    assert _code_state(fetchone, "CODEONE")["redemption_count"] == 1
    assert _code_state(fetchone, "CODETWO")["redemption_count"] == 0  # untouched


# ----- Extended group route -----
def test_group_route_includes_role(client, seed):
    """GET /api/groups/{id} reports name, type, and the caller's role."""
    info = seed(code="ROLECLUB", seats=8)
    _login_owner(client)
    body = client.get(f"/api/groups/{info['group_id']}").json()
    assert body["role"] == "owner"
    assert body["name"] and body["type"]

    register(client, code="ROLECLUB", email="mem@example.com")  # now logged in as member
    assert client.get(f"/api/groups/{info['group_id']}").json()["role"] == "member"
