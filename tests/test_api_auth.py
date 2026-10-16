"""Tests for server-side API token authentication."""

from app.config import get_settings


def test_health_remains_public_even_when_auth_configured(client, monkeypatch):
    """GET /health must never require authentication so platform health checks succeed."""
    monkeypatch.setenv("API_AUTH_TOKEN", "prod-secret-token-12345")
    get_settings.cache_clear()
    try:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
    finally:
        get_settings.cache_clear()


def test_leads_requires_auth_when_token_configured(client, monkeypatch):
    """Endpoints under /leads require valid Bearer token when API_AUTH_TOKEN is set."""
    token = "secret-token-for-tests-999"
    monkeypatch.setenv("API_AUTH_TOKEN", token)
    get_settings.cache_clear()
    try:
        # 1. Unauthenticated request -> 401
        res_no_auth = client.get("/leads")
        assert res_no_auth.status_code == 401
        assert res_no_auth.json() == {"detail": "Unauthorized"}
        assert res_no_auth.headers.get("www-authenticate") == "Bearer"

        # 2. Invalid token -> 401
        res_bad_auth = client.get("/leads", headers={"Authorization": "Bearer wrong-token"})
        assert res_bad_auth.status_code == 401
        assert res_bad_auth.json() == {"detail": "Unauthorized"}

        # 3. Valid token -> 200
        res_valid = client.get("/leads", headers={"Authorization": f"Bearer {token}"})
        assert res_valid.status_code == 200
        assert "items" in res_valid.json()

        # 4. POST /leads also protected
        post_bad = client.post("/leads", json={"name": "A", "email": "a@b.com", "company": "C", "message": "M"})
        assert post_bad.status_code == 401

        post_good = client.post(
            "/leads",
            json={"name": "Auth Test", "email": "authtest@example.com", "company": "Secure Inc", "message": "Testing auth", "source": "test-auth"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert post_good.status_code == 201
    finally:
        get_settings.cache_clear()


def test_production_mode_fails_closed_without_token(client, monkeypatch):
    """In production environment, unconfigured token fails closed and never allows unauthenticated access."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)
    get_settings.cache_clear()
    try:
        # /health remains accessible for platform health probes
        res_health = client.get("/health")
        assert res_health.status_code == 200

        # /leads rejects requests with 500 error instead of exposing data unauthenticated
        res_leads = client.get("/leads")
        assert res_leads.status_code == 500
        assert res_leads.json() == {"detail": "Authentication misconfigured in production"}
    finally:
        get_settings.cache_clear()
