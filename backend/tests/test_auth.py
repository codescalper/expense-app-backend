from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import httpx
import jwt
from sqlalchemy import event, func, select, text

from app.core import rate_limit
from app.core.config import get_settings
from app.core.rate_limit import hashed_key, ip_limit_key
from app.db.session import get_database
from app.models.user import User
from tests.conftest import PASSWORD, bearer, emails_to, latest_code, login, register_verified

API = "/api/v1"


async def _user_count() -> int:
    async with get_database().sessionmaker() as s:
        return int((await s.execute(select(func.count()).select_from(User))).scalar_one())


# --- registration & verification -------------------------------------------------------


async def test_register_queues_code_and_requires_verification(client):
    r = await client.post(
        f"{API}/auth/register",
        json={"email": "Asha@Example.com", "password": PASSWORD, "full_name": "Asha"},
    )
    assert r.status_code == 202
    mails = await emails_to("asha@example.com")  # email normalised to lowercase
    assert len(mails) == 1 and "verification code" in mails[0]["subject"]

    r = await login(client)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "email_not_verified"


async def test_register_existing_email_is_indistinguishable(client):
    await register_verified(client)
    r = await client.post(
        f"{API}/auth/register",
        json={"email": "asha@example.com", "password": "another long passphrase", "full_name": "X"},
    )
    assert r.status_code == 202
    assert r.json()["message"].startswith("If the address can be used")
    assert await _user_count() == 1
    assert "already" not in r.text.lower()
    mails = await emails_to("asha@example.com")
    assert "tried to register" in mails[0]["subject"]
    # The original password still works — registration can't hijack an account.
    assert (await login(client)).status_code == 200


async def test_weak_password_rejected(client):
    for pw in ("short", "password123", "aaaaaaaaaaaa", "asha.rao-secret!"):
        r = await client.post(
            f"{API}/auth/register",
            json={"email": "asha.rao@example.com", "password": pw, "full_name": "A"},
        )
        assert r.status_code == 422, pw
    assert await _user_count() == 0


async def test_validation_errors_never_echo_input(client):
    r = await client.post(
        f"{API}/auth/login", json={"email": "not-an-email", "password": "s3cr3t-value-xyz"}
    )
    assert r.status_code == 422
    assert "s3cr3t-value-xyz" not in r.text
    assert r.json()["error"]["code"] == "validation_error"


async def test_unknown_fields_rejected(client):
    r = await client.post(
        f"{API}/auth/register",
        json={"email": "a@example.com", "password": PASSWORD, "full_name": "A", "is_admin": True},
    )
    assert r.status_code == 422


async def test_wrong_code_attempts_burn_the_code(client, settings):
    await client.post(
        f"{API}/auth/register",
        json={"email": "asha@example.com", "password": PASSWORD, "full_name": "Asha"},
    )
    good = await latest_code("asha@example.com")
    bad = "000000" if good != "000000" else "111111"
    for _ in range(settings.email_code_max_attempts):
        r = await client.post(
            f"{API}/auth/verify-email", json={"email": "asha@example.com", "code": bad}
        )
        assert r.status_code == 400
    # After max attempts even the correct code no longer works.
    r = await client.post(
        f"{API}/auth/verify-email", json={"email": "asha@example.com", "code": good}
    )
    assert r.status_code == 400


async def test_verify_signs_in_and_code_is_single_use(client):
    await client.post(
        f"{API}/auth/register",
        json={"email": "asha@example.com", "password": PASSWORD, "full_name": "Asha"},
    )
    code = await latest_code("asha@example.com")
    r = await client.post(
        f"{API}/auth/verify-email", json={"email": "asha@example.com", "code": code}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["email_verified"] is True
    assert set(body["tokens"]) >= {"access_token", "refresh_token"}
    again = await client.post(
        f"{API}/auth/verify-email", json={"email": "asha@example.com", "code": code}
    )
    assert again.status_code == 400


async def test_resend_verification_invalidates_previous_code(client):
    await client.post(
        f"{API}/auth/register",
        json={"email": "asha@example.com", "password": PASSWORD, "full_name": "Asha"},
    )
    first = await latest_code("asha@example.com")
    r = await client.post(f"{API}/auth/resend-verification", json={"email": "asha@example.com"})
    assert r.status_code == 202
    second = await latest_code("asha@example.com")
    if first != second:
        r = await client.post(
            f"{API}/auth/verify-email", json={"email": "asha@example.com", "code": first}
        )
        assert r.status_code == 400
    r = await client.post(
        f"{API}/auth/verify-email", json={"email": "asha@example.com", "code": second}
    )
    assert r.status_code == 200


# --- login -----------------------------------------------------------------------------


async def test_login_success_and_response_has_no_secrets(client):
    await register_verified(client)
    r = await login(client)
    assert r.status_code == 200
    raw = r.text
    for forbidden in (
        "password_hash",
        "failed_login_count",
        "locked_until",
        "token_hash",
        "$argon2",
    ):
        assert forbidden not in raw


async def test_login_failures_are_generic(client):
    await register_verified(client)
    wrong_pw = await login(client, password="definitely not it!")
    unknown = await login(client, email="nobody@example.com")
    assert wrong_pw.status_code == unknown.status_code == 401
    assert wrong_pw.json() == unknown.json()


async def test_login_throttled_after_repeated_failures(client, settings):
    await register_verified(client)
    for _ in range(settings.login_max_failures):
        assert (await login(client, password="wrong password here")).status_code == 401
    r = await login(client)  # even the right password is refused while throttled
    assert r.status_code == 429
    assert "Retry-After" in r.headers


async def test_unknown_email_throttled_the_same_way(client, settings):
    for _ in range(settings.login_max_failures):
        assert (await login(client, email="ghost@example.com")).status_code == 401
    assert (await login(client, email="ghost@example.com")).status_code == 429


class _Clock:
    """Drives ``app.core.rate_limit._now`` so tests can cross window boundaries."""

    def __init__(self, monkeypatch, start: datetime) -> None:
        self.now = start
        monkeypatch.setattr(rate_limit, "_now", lambda: self.now)

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _window_start(seconds: int) -> datetime:
    epoch = int(datetime.now(UTC).timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, UTC)


async def test_lockout_identical_for_real_and_unknown_email_across_window_boundary(
    client, settings, monkeypatch
):
    """Regression: failures spread over a window boundary used to lock a *real* account
    (non-decaying DB counter -> 429) while an unknown email kept getting 401."""
    await register_verified(client)
    w = settings.login_failure_window_seconds
    clock = _Clock(monkeypatch, _window_start(w) + timedelta(seconds=w - 10))
    start = clock.now
    observed: dict[str, list[int]] = {}
    for email in ("asha@example.com", "ghost@example.com"):
        clock.now = start
        statuses = [(await login(client, email, "wrong password here")).status_code]
        statuses += [(await login(client, email, "wrong password here")).status_code]
        statuses += [(await login(client, email, "wrong password here")).status_code]
        clock.advance(20)  # crosses into the next fixed window
        for _ in range(3):
            statuses.append((await login(client, email, "wrong password here")).status_code)
        statuses.append((await login(client, email)).status_code)  # right password for asha
        observed[email] = statuses
        clock.advance(settings.login_lockout_seconds + 1)  # lock lifts
        assert (await login(client, email)).status_code == (
            200 if email == "asha@example.com" else 401
        )
    # Failures keep counting across the boundary (decayed, not reset) ...
    assert observed["asha@example.com"] == [401] * 6 + [429]
    # ... and a registered address is indistinguishable from an unknown one.
    assert observed["asha@example.com"] == observed["ghost@example.com"]


async def test_login_failure_count_decays(client, settings, monkeypatch):
    """Old failures fade out instead of accumulating forever on the account."""
    await register_verified(client)
    w = settings.login_failure_window_seconds
    clock = _Clock(monkeypatch, _window_start(w) + timedelta(seconds=5))
    for _ in range(settings.login_max_failures - 1):
        assert (await login(client, password="wrong password here")).status_code == 401
    clock.advance(2 * w)  # all of those have decayed away
    for _ in range(settings.login_max_failures - 1):
        assert (await login(client, password="wrong password here")).status_code == 401
    assert (await login(client)).status_code == 200


async def test_password_reset_lifts_login_lockout(client, settings):
    await register_verified(client)
    for _ in range(settings.login_max_failures):
        await login(client, password="wrong password here")
    assert (await login(client)).status_code == 429
    await client.post(f"{API}/auth/forgot-password", json={"email": "asha@example.com"})
    code = await latest_code("asha@example.com")
    r = await client.post(
        f"{API}/auth/reset-password",
        json={"email": "asha@example.com", "code": code, "new_password": "another fine passphrase"},
    )
    assert r.status_code == 204
    assert (await login(client, password="another fine passphrase")).status_code == 200


async def test_rate_limit_keys_are_hmac_not_plain_hashes(client):
    await login(client, email="asha@example.com", password="wrong password here")
    async with get_database().sessionmaker() as s:
        keys = [r[0] for r in await s.execute(text("SELECT key FROM rate_limit_counters"))]
    plain = hashlib.sha256(b"asha@example.com").hexdigest()[:40]
    assert any(k.startswith("login_fail:") for k in keys)
    assert not any(plain in k or "asha" in k for k in keys)
    assert hashed_key("login_fail", "asha@example.com") == hashed_key(
        "login_fail", "ASHA@example.com"
    )


async def test_per_ip_rate_limit(client, settings):
    settings.auth_ip_rate_limit = 3
    for _ in range(3):
        r = await client.post(f"{API}/auth/forgot-password", json={"email": "x@example.com"})
        assert r.status_code == 202
    r = await client.post(f"{API}/auth/forgot-password", json={"email": "x@example.com"})
    assert r.status_code == 429


# --- access tokens -----------------------------------------------------------------------


async def test_protected_endpoint_requires_valid_token(client):
    assert (await client.get(f"{API}/users/me")).status_code == 401
    r = await client.get(f"{API}/users/me", headers={"Authorization": "Bearer garbage"})
    assert r.status_code == 401


async def test_forged_and_expired_tokens_rejected(client):
    auth = await register_verified(client)
    claims = jwt.decode(auth["tokens"]["access_token"], options={"verify_signature": False})

    forged = jwt.encode(claims, "attacker-key-" + "x" * 40, algorithm="HS256")
    r = await client.get(f"{API}/users/me", headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code == 401

    none_alg = jwt.encode(claims, key=None, algorithm="none")  # type: ignore[arg-type]
    r = await client.get(f"{API}/users/me", headers={"Authorization": f"Bearer {none_alg}"})
    assert r.status_code == 401

    secret = get_settings().jwt_secret_key.get_secret_value()
    expired = jwt.encode(
        {**claims, "exp": int((datetime.now(UTC) - timedelta(minutes=1)).timestamp())},
        secret,
        algorithm="HS256",
    )
    r = await client.get(f"{API}/users/me", headers={"Authorization": f"Bearer {expired}"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "token_expired"


# --- refresh rotation ----------------------------------------------------------------------


async def test_refresh_rotates_tokens(client):
    auth = await register_verified(client)
    r = await client.post(
        f"{API}/auth/refresh", json={"refresh_token": auth["tokens"]["refresh_token"]}
    )
    assert r.status_code == 200
    new = r.json()
    assert new["refresh_token"] != auth["tokens"]["refresh_token"]
    assert (await client.get(f"{API}/users/me", headers=bearer(new))).status_code == 200
    # The new refresh token works once more.
    r = await client.post(f"{API}/auth/refresh", json={"refresh_token": new["refresh_token"]})
    assert r.status_code == 200


async def test_refresh_token_reuse_revokes_session(client):
    auth = await register_verified(client)
    old = auth["tokens"]["refresh_token"]
    rotated = (await client.post(f"{API}/auth/refresh", json={"refresh_token": old})).json()

    r = await client.post(f"{API}/auth/refresh", json={"refresh_token": old})  # replay
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "refresh_token_reused"
    # The whole session is dead: both the legit new refresh token and access token fail.
    r = await client.post(f"{API}/auth/refresh", json={"refresh_token": rotated["refresh_token"]})
    assert r.status_code == 401
    assert (await client.get(f"{API}/users/me", headers=bearer(rotated))).status_code == 401


async def test_refresh_with_unknown_token(client):
    r = await client.post(f"{API}/auth/refresh", json={"refresh_token": "x" * 64})
    assert r.status_code == 401


# --- logout ---------------------------------------------------------------------------------


async def test_logout_invalidates_access_token_immediately(client):
    auth = await register_verified(client)
    headers = bearer(auth["tokens"])
    assert (await client.post(f"{API}/auth/logout", headers=headers)).status_code == 204
    assert (await client.get(f"{API}/users/me", headers=headers)).status_code == 401
    r = await client.post(
        f"{API}/auth/refresh", json={"refresh_token": auth["tokens"]["refresh_token"]}
    )
    assert r.status_code == 401


async def test_logout_all_devices(client):
    first = await register_verified(client)
    second = (await login(client)).json()
    r = await client.post(f"{API}/auth/logout-all", headers=bearer(second["tokens"]))
    assert r.status_code == 204
    for auth in (first, second):
        assert (
            await client.get(f"{API}/users/me", headers=bearer(auth["tokens"]))
        ).status_code == 401


# --- password reset -------------------------------------------------------------------------


async def test_password_reset_flow_revokes_sessions(client):
    auth = await register_verified(client)
    r = await client.post(f"{API}/auth/forgot-password", json={"email": "asha@example.com"})
    assert r.status_code == 202
    code = await latest_code("asha@example.com")
    new_pw = "a brand new passphrase"
    r = await client.post(
        f"{API}/auth/reset-password",
        json={"email": "asha@example.com", "code": code, "new_password": new_pw},
    )
    assert r.status_code == 204
    assert (await client.get(f"{API}/users/me", headers=bearer(auth["tokens"]))).status_code == 401
    assert (await login(client)).status_code == 401
    assert (await login(client, password=new_pw)).status_code == 200
    # Code is single-use.
    r = await client.post(
        f"{API}/auth/reset-password",
        json={"email": "asha@example.com", "code": code, "new_password": "yet another passphrase"},
    )
    assert r.status_code == 400


async def test_forgot_password_unknown_email_looks_identical(client):
    await register_verified(client)
    known = await client.post(f"{API}/auth/forgot-password", json={"email": "asha@example.com"})
    unknown = await client.post(f"{API}/auth/forgot-password", json={"email": "who@example.com"})
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()
    assert await emails_to("who@example.com") == []


async def test_email_send_quota(client, settings):
    await register_verified(client)
    for _ in range(settings.email_send_limit + 2):
        await client.post(f"{API}/auth/forgot-password", json={"email": "asha@example.com"})
    resets = [m for m in await emails_to("asha@example.com") if "reset" in m["subject"]]
    assert len(resets) == settings.email_send_limit


# --- regression tests: security review -------------------------------------------------------

ATTACKER_PASSWORD = "attacker chosen passphrase"


async def _register(client, email: str, password: str, name: str) -> None:
    r = await client.post(
        f"{API}/auth/register", json={"email": email, "password": password, "full_name": name}
    )
    assert r.status_code == 202, r.text


async def test_preregistration_takeover_prevented(client):
    """Attacker registers the victim's address first (unverified, attacker's password); the
    victim registers later and verifies. The attacker must not end up with access."""
    victim = "victim@example.com"
    await _register(client, victim, ATTACKER_PASSWORD, "Attacker")
    stale_code = await latest_code(victim)  # lands in the victim's inbox, never used

    await _register(client, victim, PASSWORD, "Victim")
    code = await latest_code(victim)
    if stale_code != code:  # the earlier registration's code is dead
        r = await client.post(
            f"{API}/auth/verify-email", json={"email": victim, "code": stale_code}
        )
        assert r.status_code == 400
    r = await client.post(f"{API}/auth/verify-email", json={"email": victim, "code": code})
    assert r.status_code == 200
    assert r.json()["user"]["full_name"] == "Victim"

    assert (await login(client, victim, ATTACKER_PASSWORD)).status_code == 401
    assert (await login(client, victim, PASSWORD)).status_code == 200
    assert await _user_count() == 1


async def test_verify_with_password_rejects_a_later_hostile_registration(client):
    """Reverse race: the attacker re-registers *after* the victim. A client that sends the
    password it registered with can never verify the attacker's registration."""
    victim = "victim@example.com"
    await _register(client, victim, PASSWORD, "Victim")
    await _register(client, victim, ATTACKER_PASSWORD, "Attacker")
    latest = await latest_code(victim)
    r = await client.post(
        f"{API}/auth/verify-email", json={"email": victim, "code": latest, "password": PASSWORD}
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_code"

    await _register(client, victim, PASSWORD, "Victim")  # victim simply registers again
    r = await client.post(
        f"{API}/auth/verify-email",
        json={"email": victim, "code": await latest_code(victim), "password": PASSWORD},
    )
    assert r.status_code == 200
    assert (await login(client, victim, ATTACKER_PASSWORD)).status_code == 401
    assert (await login(client, victim, PASSWORD)).status_code == 200


async def test_code_failures_capped_per_day_across_reissued_codes(client, settings):
    settings.email_code_daily_max_failures = 6
    await _register(client, "asha@example.com", PASSWORD, "Asha")
    first = await latest_code("asha@example.com")
    bad = "000000" if first != "000000" else "111111"
    for _ in range(settings.email_code_max_attempts):  # burns the first code
        r = await client.post(
            f"{API}/auth/verify-email", json={"email": "asha@example.com", "code": bad}
        )
        assert r.status_code == 400
    await client.post(f"{API}/auth/resend-verification", json={"email": "asha@example.com"})
    good = await latest_code("asha@example.com")
    bad = "000000" if good != "000000" else "111111"
    r = await client.post(
        f"{API}/auth/verify-email", json={"email": "asha@example.com", "code": bad}
    )
    assert r.status_code == 400  # 6th failure in 24 h
    r = await client.post(
        f"{API}/auth/verify-email", json={"email": "asha@example.com", "code": good}
    )
    assert r.status_code == 400  # refused even with the right code
    assert r.json()["error"]["code"] == "invalid_code"  # generic


def test_ip_limit_key_groups_ipv6_by_64():
    assert ip_limit_key("2001:db8:1:2::1") == ip_limit_key("2001:db8:1:2:ffff:ffff:ffff:ffff")
    assert ip_limit_key("2001:db8:1:2::1") != ip_limit_key("2001:db8:1:3::1")
    assert ip_limit_key("203.0.113.7") == "203.0.113.7"
    assert ip_limit_key("::ffff:203.0.113.7") == "203.0.113.7"


async def test_per_ip_rate_limit_applies_per_ipv6_64(settings):
    from app.main import app

    settings.auth_ip_rate_limit = 2

    async def forgot(ip: str) -> int:
        transport = httpx.ASGITransport(app=app, client=(ip, 50000))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(f"{API}/auth/forgot-password", json={"email": "x@example.com"})
            return r.status_code

    assert await forgot("2001:db8:1:2::1") == 202
    assert await forgot("2001:db8:1:2::2") == 202
    assert await forgot("2001:db8:1:2:abcd::9") == 429  # same /64, new address: still limited
    assert await forgot("2001:db8:1:3::1") == 202  # a different /64


async def _job_kinds() -> list[str]:
    async with get_database().sessionmaker() as s:
        rows = await s.execute(text("SELECT kind FROM background_jobs ORDER BY created_at"))
        return [r[0] for r in rows]


async def test_forgot_and_resend_do_equivalent_work_for_unknown_addresses(client):
    await register_verified(client)
    async with get_database().engine.begin() as conn:
        await conn.execute(text("TRUNCATE background_jobs, rate_limit_counters"))

    await client.post(f"{API}/auth/forgot-password", json={"email": "asha@example.com"})
    await client.post(f"{API}/auth/forgot-password", json={"email": "who@example.com"})
    assert await _job_kinds() == ["send_email", "noop_email"]
    # resend-verification for a verified (ineligible) and an unknown address
    await client.post(f"{API}/auth/resend-verification", json={"email": "asha@example.com"})
    await client.post(f"{API}/auth/resend-verification", json={"email": "nobody@example.com"})
    assert (await _job_kinds())[2:] == ["noop_email", "noop_email"]
    async with get_database().sessionmaker() as s:
        quota_rows = (
            await s.execute(
                text("SELECT count(*) FROM rate_limit_counters WHERE key LIKE 'email:%'")
            )
        ).scalar_one()
    assert quota_rows == 4  # every address's quota was bumped, known or not


async def test_emails_to_unverified_addresses_omit_name_and_expire(client):
    await _register(client, "asha@example.com", PASSWORD, "Click http://evil.example now")
    [mail] = await emails_to("asha@example.com")
    assert "evil.example" not in mail["body"] and "evil.example" not in mail["subject"]
    expires = datetime.fromisoformat(mail["expires_at"])
    assert datetime.now(UTC) < expires <= datetime.now(UTC) + timedelta(minutes=31)

    await client.post(f"{API}/auth/forgot-password", json={"email": "asha@example.com"})
    reset = (await emails_to("asha@example.com"))[0]
    assert "reset" in reset["subject"] and "evil.example" not in reset["body"]


async def test_register_strips_control_characters_from_name(client):
    auth = await register_verified(client, full_name="Asha\r\nBcc: x@evil.example\u202e\x00 Rao")
    assert auth["user"]["full_name"] == "Asha Bcc: x@evil.example Rao"
    r = await client.post(
        f"{API}/auth/register",
        json={"email": "b@example.com", "password": PASSWORD, "full_name": "\n\t\u2028"},
    )
    assert r.status_code == 422


async def test_absolute_session_lifetime(client, settings):
    auth = await register_verified(client)
    days = settings.session_absolute_lifetime_days
    async with get_database().engine.begin() as conn:
        await conn.execute(
            text("UPDATE user_sessions SET created_at = now() - make_interval(days => :d)"),
            {"d": days - 2},
        )
    r = await client.post(
        f"{API}/auth/refresh", json={"refresh_token": auth["tokens"]["refresh_token"]}
    )
    assert r.status_code == 200
    capped = datetime.fromisoformat(r.json()["refresh_token_expires_at"])
    assert capped <= datetime.now(UTC) + timedelta(days=2, minutes=1)  # not now + 7 days

    async with get_database().engine.begin() as conn:
        await conn.execute(
            text("UPDATE user_sessions SET created_at = now() - make_interval(days => :d)"),
            {"d": days + 1},
        )
    tokens = r.json()
    assert (await client.get(f"{API}/users/me", headers=bearer(tokens))).status_code == 401
    r = await client.post(f"{API}/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 401 and r.json()["error"]["code"] == "session_expired"


async def test_requests_never_hold_two_pooled_connections(client):
    """Rate-limit writes share the request's connection instead of checking out a second one
    while the request's session still holds the first (pool starvation under load)."""
    auth = await register_verified(client)
    pool = get_database().engine.sync_engine.pool
    state = {"out": 0, "max": 0}

    def checkout(*_a):
        state["out"] += 1
        state["max"] = max(state["max"], state["out"])

    def checkin(*_a):
        state["out"] -= 1

    event.listen(pool, "checkout", checkout)
    event.listen(pool, "checkin", checkin)
    try:
        await login(client, password="wrong password here")
        await login(client, email="ghost@example.com")
        await login(client)
        await client.post(f"{API}/auth/forgot-password", json={"email": "asha@example.com"})
        await client.post(f"{API}/auth/forgot-password", json={"email": "who@example.com"})
        await _register(client, "asha@example.com", PASSWORD, "Asha")
        await _register(client, "new@example.com", PASSWORD, "New")
        await client.post(
            f"{API}/auth/verify-email", json={"email": "new@example.com", "code": "000000"}
        )
        await client.post(
            f"{API}/users/me/change-password",
            headers=bearer(auth["tokens"]),
            json={"current_password": "wrong one entirely", "new_password": "x" * 12},
        )
    finally:
        event.remove(pool, "checkout", checkout)
        event.remove(pool, "checkin", checkin)
    assert state["max"] == 1
