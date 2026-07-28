"""Preference-profile persistence and round-tripping."""
from .conftest import register

_SAMPLE = {
    "favorite_genres": ["sci-fi", "mystery"],
    "favorite_authors": ["Ursula K. Le Guin"],
    "examples": ["The Left Hand of Darkness"],
    "dislikes": ["gratuitous gore"],
    "content_preferences": {
        "max_violence": "mild",
        "max_language": "moderate",
        "romance_ok": False,
        "explicit_ok": False,
    },
    "reading_pace": "slow",
    "preferred_length": "long",
}


def test_profile_defaults_after_registration(client, seed):
    """A freshly registered user has empty lists and default content prefs."""
    seed(code="CLUB", seats=8)
    register(client, code="CLUB", email="m@example.com")
    res = client.get("/api/profile")
    assert res.status_code == 200
    body = res.json()
    assert body["favorite_genres"] == []
    assert body["content_preferences"]["romance_ok"] is True
    assert body["display_name"] == "Member"


def test_profile_round_trips(client, seed):
    """A saved profile comes back identically on the next read."""
    seed(code="CLUB", seats=8)
    register(client, code="CLUB", email="m@example.com")

    put = client.put("/api/profile", json=_SAMPLE)
    assert put.status_code == 200

    got = client.get("/api/profile").json()
    for key, value in _SAMPLE.items():
        assert got[key] == value


def test_profile_persists_across_logout_login(client, seed):
    """Profile data survives logging out and back in."""
    seed(code="CLUB", seats=8)
    register(client, code="CLUB", email="m@example.com", password="password1")
    client.put("/api/profile", json=_SAMPLE)

    client.post("/api/logout")
    # After logout the session cookie is gone -> profile is unauthenticated.
    assert client.get("/api/profile").status_code == 401

    login = client.post(
        "/api/login", json={"email": "m@example.com", "password": "password1"}
    )
    assert login.status_code == 200

    got = client.get("/api/profile").json()
    assert got["favorite_genres"] == _SAMPLE["favorite_genres"]
    assert got["reading_pace"] == "slow"


def test_profile_requires_auth(client):
    """Unauthenticated profile access returns 401."""
    assert client.get("/api/profile").status_code == 401


def test_profile_rejects_bad_enum(client, seed):
    """An invalid content level is rejected by server-side validation."""
    seed(code="CLUB", seats=8)
    register(client, code="CLUB", email="m@example.com")
    bad = dict(_SAMPLE)
    bad["content_preferences"] = {**_SAMPLE["content_preferences"], "max_violence": "extreme"}
    res = client.put("/api/profile", json=bad)
    assert res.status_code == 422
