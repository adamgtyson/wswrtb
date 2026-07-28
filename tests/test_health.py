"""Smoke tests: the runner works and the app is alive."""


def test_project_scaffold():
    """Confirms the test suite is operational."""
    assert True


def test_health_endpoint(client):
    """/health returns 200 with an ok status."""
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}
