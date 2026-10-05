"""GET /health tests: application availability + PostgreSQL reachability.

The endpoint must distinguish "process is running" (200) from "the process
can actually reach PostgreSQL" (503), without leaking driver messages.
"""

from app import db


def test_health_ok_when_database_reachable(client) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_reports_unavailable_when_database_unreachable(
    client, schema_ready: str
) -> None:
    """A broken pool (database gone) yields 503, never a 500 or a leak.

    The pool is closed for real rather than mocked so the endpoint's whole
    failure path is exercised, then restored immediately: no extra
    infrastructure, no state left behind for the rest of the session.
    """
    db.close_pool()
    try:
        response = client.get("/health")
    finally:
        db.configure_pool(schema_ready)

    assert response.status_code == 503
    assert response.json() == {"detail": "Service unavailable"}
    # Database internals must never reach the client.
    assert "postgresql://" not in response.text
    assert "Database pool" not in response.text
    assert "Traceback" not in response.text

    # The failure path must not leave the service unhealthy: with the pool
    # restored, the same endpoint reports 200 again.
    recovered = client.get("/health")
    assert recovered.status_code == 200
    assert recovered.json() == {"status": "ok"}
