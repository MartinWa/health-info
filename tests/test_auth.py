"""Session auth: lockout (app and admin), CSRF, cookie flags, password change."""

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse

from records import lockout


@pytest.fixture(autouse=True)
def empty_lockout(monkeypatch):
    monkeypatch.setattr(lockout, "FAILURES", {})


def test_five_failures_lock_the_username_and_ip(client, user):
    for _ in range(lockout.LOCKOUT_LIMIT):
        response = client.post(reverse("login"), {"username": "alice", "password": "wrong"})
        assert response.status_code == 200
    response = client.post(reverse("login"), {"username": "alice", "password": "alice-password-1"})
    assert response.status_code == 429


def test_another_ip_is_unaffected(client, user):
    for _ in range(lockout.LOCKOUT_LIMIT):
        client.post(
            reverse("login"),
            {"username": "alice", "password": "wrong"},
            REMOTE_ADDR="10.1.1.1",
        )
    response = client.post(
        reverse("login"),
        {"username": "alice", "password": "alice-password-1"},
        REMOTE_ADDR="10.2.2.2",
    )
    assert response.status_code == 302


def test_admin_login_uses_the_same_lockout(client, user):
    user.is_staff = True
    user.is_superuser = True
    user.save()
    for _ in range(lockout.LOCKOUT_LIMIT):
        response = client.post(
            "/admin/login/",
            {"username": "alice", "password": "wrong", "next": "/admin/"},
        )
        assert response.status_code == 200
    response = client.post(
        "/admin/login/",
        {"username": "alice", "password": "alice-password-1", "next": "/admin/"},
    )
    assert response.status_code == 429


def test_login_sets_an_httponly_lax_session_cookie(client, user):
    response = client.post(reverse("login"), {"username": "alice", "password": "alice-password-1"})
    assert response.status_code == 302
    cookie = client.cookies[settings.SESSION_COOKIE_NAME]
    assert settings.SESSION_COOKIE_NAME == "__Host-health_info_session"
    assert cookie["httponly"]
    assert cookie["samesite"] == "Lax"


def test_post_without_a_csrf_token_is_forbidden(user):
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    response = client.post(reverse("records:quick_note"), {"body": "x", "at": "2026-11-02T08:00"})
    assert response.status_code == 403


def test_password_change_ends_other_sessions(user, logged_in):
    other = Client()
    other.force_login(user)
    assert other.get(reverse("records:dashboard")).status_code == 200
    response = logged_in.post(
        reverse("password"),
        {
            "old_password": "alice-password-1",
            "new_password1": "alice-password-2",
            "new_password2": "alice-password-2",
        },
    )
    assert response.status_code == 302
    assert other.get(reverse("records:dashboard")).status_code == 302


def test_healthz_is_anonymous_and_carries_no_account_data(client, user):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert "alice" not in response.content.decode()


def test_sign_out_requires_post(client, user):
    client.force_login(user)
    assert client.get(reverse("logout")).status_code == 405
    assert client.post(reverse("logout")).status_code == 302
