"""Dashboard HTTP Basic Auth tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(name="auth_env")
def _auth_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("DASHBOARD_USER", "trader")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "s3cret-x9q!")
    yield


def test_no_credentials_returns_401(auth_env: None) -> None:
    with TestClient(app) as client:
        r = client.get("/")
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers
    assert "Basic" in r.headers["WWW-Authenticate"]


def test_wrong_password_returns_401(auth_env: None) -> None:
    with TestClient(app) as client:
        r = client.get("/", auth=("trader", "wrong"))
    assert r.status_code == 401


def test_wrong_user_returns_401(auth_env: None) -> None:
    with TestClient(app) as client:
        r = client.get("/", auth=("notmyname", "s3cret-x9q!"))
    assert r.status_code == 401


def test_correct_credentials_returns_200(auth_env: None) -> None:
    with TestClient(app) as client:
        r = client.get("/", auth=("trader", "s3cret-x9q!"))
    assert r.status_code == 200
    assert "trading-app" in r.text


def test_partial_routes_also_gated(auth_env: None) -> None:
    with TestClient(app) as client:
        r_no_auth = client.get("/partials/pnl")
        r_with_auth = client.get("/partials/pnl", auth=("trader", "s3cret-x9q!"))
    assert r_no_auth.status_code == 401
    assert r_with_auth.status_code == 200


def test_health_endpoint_is_NOT_gated(auth_env: None) -> None:
    # /health is on the app directly, not the dashboard router — should always work.
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200


def test_auth_disabled_when_creds_blank() -> None:
    # No env vars set — dashboard returns 200 with no auth header.
    with TestClient(app) as client:
        r = client.get("/")
    assert r.status_code == 200
