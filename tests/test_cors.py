"""CORS whitelist: allowed origins get permissive headers, others do not.

conftest sets ALLOWED_ORIGINS=http://localhost:8000 before the app is imported, so the
middleware is configured with that single origin.
"""


def test_allowed_origin_gets_cors_headers(client):
    """A whitelisted Origin receives an echoing Access-Control-Allow-Origin header."""
    res = client.get("/health", headers={"Origin": "http://localhost:8000"})
    assert res.status_code == 200
    assert res.headers.get("access-control-allow-origin") == "http://localhost:8000"
    # Credentialed CORS must echo the specific origin (never "*") and allow credentials.
    assert res.headers.get("access-control-allow-credentials") == "true"


def test_unlisted_origin_gets_no_cors_headers(client):
    """A non-whitelisted Origin is not granted CORS headers (browser would block it)."""
    res = client.get("/health", headers={"Origin": "http://evil.example"})
    # The request itself still processes; it's the missing header the browser enforces on.
    assert "access-control-allow-origin" not in res.headers


def test_preflight_from_unlisted_origin_is_refused(client):
    """A CORS preflight from a non-whitelisted origin is not granted an allow-origin."""
    res = client.options(
        "/api/login",
        headers={
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert "access-control-allow-origin" not in res.headers
