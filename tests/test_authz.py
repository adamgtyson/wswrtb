"""require_membership authorization dependency."""
from .conftest import register


def test_member_can_access_own_group(client, seed):
    """A member gets 200 on their own group's protected route."""
    info = seed(code="CLUBA", seats=8, email="ownera@example.com", group_name="Club A")
    register(client, code="CLUBA", email="member@example.com")
    res = client.get(f"/api/groups/{info['group_id']}")
    assert res.status_code == 200
    assert res.json()["name"] == "Club A"


def test_non_member_gets_403(client, seed):
    """A user with no membership in a group is refused with 403."""
    seed(code="CLUBA", seats=8, email="ownera@example.com", group_name="Club A")
    other = seed(code="CLUBB", seats=8, email="ownerb@example.com", group_name="Club B")

    # Register into Club A only, then try to reach Club B.
    register(client, code="CLUBA", email="member@example.com")
    res = client.get(f"/api/groups/{other['group_id']}")
    assert res.status_code == 403


def test_protected_route_requires_auth(client, seed):
    """Without a session, the protected group route returns 401."""
    info = seed(code="CLUBA", seats=8)
    res = client.get(f"/api/groups/{info['group_id']}")
    assert res.status_code == 401
