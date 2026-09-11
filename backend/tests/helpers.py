"""Small helpers for API-level tests (as opposed to factories.py's
DB-object builders)."""

from fastapi.testclient import TestClient


def login(client: TestClient, username: str, password: str) -> str:
    """Logs in through the real HTTP endpoint and returns the access
    token — exercises actual password verification, not a shortcut."""
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def auth_headers(client: TestClient, username: str, password: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {login(client, username, password)}"}
