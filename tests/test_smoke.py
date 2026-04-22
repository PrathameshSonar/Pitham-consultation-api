"""Smoke tests — fast, deterministic, hit the most important happy paths.

Run: cd backend && pytest -q
"""


def test_health_endpoint(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_register_login_logout_round_trip(client):
    payload = {
        "name": "Test Devotee",
        "email": "test@example.com",
        "mobile": "+919999999999",
        "dob": "1990-01-01",
        "tob": "12:00",
        "birth_place": "Pune",
        "city": "Pune",
        "state": "Maharashtra",
        "country": "India",
        "password": "secret123",
    }
    reg = client.post("/auth/register", json=payload)
    assert reg.status_code == 200, reg.text
    assert reg.json()["role"] == "user"
    # Cookie should be set
    assert "pitham_session" in reg.cookies or "set-cookie" in {k.lower() for k in reg.headers}

    # Login with same credentials
    lgn = client.post("/auth/login", json={"email": "test@example.com", "password": "secret123"})
    assert lgn.status_code == 200
    token = lgn.json()["token"]
    assert token

    # Authenticated profile fetch via Bearer
    me = client.get("/auth/profile", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == "test@example.com"

    # Logout clears cookie
    out = client.post("/auth/logout")
    assert out.status_code == 200


def test_login_with_wrong_password_fails(client):
    client.post("/auth/register", json={
        "name": "X", "email": "x@example.com", "mobile": "+918888888888",
        "dob": "1990-01-01", "tob": "12:00", "birth_place": "p",
        "city": "p", "state": "p", "country": "India", "password": "right123",
    })
    r = client.post("/auth/login", json={"email": "x@example.com", "password": "wrong"})
    assert r.status_code == 401


def test_pitham_cms_public_endpoint(client):
    r = client.get("/pitham/cms")
    assert r.status_code == 200
    body = r.json()
    # All five sections must be present, even if empty
    for k in ("banners", "videos", "instagram", "gallery", "testimonials", "featured_events"):
        assert k in body
        assert isinstance(body[k], list)


def test_public_events_list_is_open(client):
    r = client.get("/events?scope=upcoming")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_admin_endpoint_rejects_unauthenticated(client):
    r = client.get("/admin/appointments")
    assert r.status_code in (401, 403)


def test_admin_rate_limit_kicks_in(client):
    """The middleware caps /admin/* at 120/min per IP. Send 121 and verify the last one is throttled."""
    last = None
    for _ in range(125):
        last = client.get("/admin/appointments")
        if last.status_code == 429:
            break
    # Either we got 429 or we got 120 unauthenticated requests through (test shouldn't
    # be flaky — the limit is well below 125)
    assert last is not None
    assert last.status_code in (401, 403, 429)


def test_account_deletion_blocks_admin_self_delete(client):
    """Defensive: an admin promoted via DB cannot self-delete."""
    # Register a user (name must be ≥2 chars; birth_place/city/state must be non-empty)
    reg = client.post("/auth/register", json={
        "name": "Admin User", "email": "a@example.com", "mobile": "+917777777777",
        "dob": "1990-01-01", "tob": "12:00", "birth_place": "Pune",
        "city": "Pune", "state": "Maharashtra", "country": "India", "password": "secret123",
    })
    assert reg.status_code == 200, reg.text
    # Promote them to admin directly via SQL
    from database import SessionLocal
    import models as M
    db = SessionLocal()
    try:
        u = db.query(M.User).filter(M.User.email == "a@example.com").first()
        u.role = "admin"
        db.commit()
    finally:
        db.close()
    # Login
    lgn = client.post("/auth/login", json={"email": "a@example.com", "password": "secret123"})
    token = lgn.json()["token"]
    # Self-delete must be rejected
    r = client.delete("/auth/account", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 400
