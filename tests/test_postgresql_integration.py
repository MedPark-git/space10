import os
import pytest


pytestmark = pytest.mark.skipif(not all(os.getenv(k) for k in ["DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD"]), reason="PostgreSQL integration environment not configured")


def test_health_and_authentication_contract():
    from app import app
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SESSION_COOKIE_SECURE=False)
    client = app.test_client()
    assert client.get("/").status_code in (302, 401)
    api = client.get("/api/items")
    assert api.status_code == 401
    health = client.get("/health")
    assert health.status_code == 200
    assert health.get_json() == {"status":"ok", "database":True, "database_backend":"postgresql", "database_writable":True, "application_ready":True}

