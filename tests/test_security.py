"""Ingest tokens (off / optional / required), the export token, the /api/history rate limit,
the request-size cap, and the client side of the token header."""

from __future__ import annotations

import ast
import logging
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from helpers import FakeClock, T0, US, v1_batch, v1_reading, v2_series  # noqa: E402
from tremor.retention import RetentionConfig  # noqa: E402
from tremor.security import (  # noqa: E402
    MIN_TOKEN_LEN, TOKEN_HEADER, ConfigError, ExportAuth, IngestAuth, RateLimiter, parse_rate, parse_tokens)
from tremor.webapp import MAX_BODY_BYTES, create_app  # noqa: E402

BOOT = "9f3a51c07d2e4b18"
T1 = "unit1-token-0123456789-abcdef"
T2 = "unit2-token-9876543210-fedcba"
EXPORT = "export-token-0123456789abcdef"
TOK = lambda t: {TOKEN_HEADER: t}


def make_app(tmp_path, mode="optional", tokens=None, clock=None, **kw):
    clock = clock or FakeClock(T0)
    tokens = {"unit-1": T1, "unit-2": T2} if tokens is None else tokens
    auth = IngestAuth(tokens, mode, clock=clock, log=logging.getLogger("tremor.webapp"))
    app = create_app(simulated_units=[], db_path=str(tmp_path / "r.db"), clock=clock, ingest_auth=auth,
                     retention_config=RetentionConfig(export_dir=str(tmp_path / "ex")), **kw)
    return app, app.test_client(), clock


def batch(unit="unit-1", seq0=0):
    return v2_series(unit, BOOT, T0 - 5, 5, seq0=seq0)


def rows(app, unit="unit-1"):
    return len(app.config["TREMOR_STORE"].history(unit, 0, 2**62, 1000).rows)


# --- configuration is validated loudly --------------------------------------------------------------------

def test_parse_tokens_accepts_the_documented_format_and_splits_on_the_first_equals():
    assert parse_tokens(f"unit-1={T1}, unit-2={T2}") == {"unit-1": T1, "unit-2": T2}
    assert parse_tokens("unit-1=abcdefghijklmnop==") == {"unit-1": "abcdefghijklmnop=="}     # '=' inside a token
    assert parse_tokens("") == {} and parse_tokens(None) == {} and parse_tokens("  ,  ") == {}


@pytest.mark.parametrize("bad", ["unit-1", "unit-1=short", "unit 1=" + "x" * 20, f"unit-1={T1},unit-1={T2}", "=" + "x" * 20])
def test_bad_token_configuration_fails_at_startup_not_silently(bad):
    with pytest.raises(ConfigError):
        parse_tokens(bad)


def test_mode_configuration_errors():
    with pytest.raises(ConfigError):
        IngestAuth({"unit-1": T1}, "sometimes")
    with pytest.raises(ConfigError):
        IngestAuth({}, "optional")                    # needs tokens
    with pytest.raises(ConfigError):
        IngestAuth({}, "required")
    with pytest.raises(ConfigError):
        IngestAuth({"unit-1": "short"}, "required")
    assert IngestAuth({}).mode == "off" and IngestAuth({"unit-1": T1}).mode == "optional"     # sensible defaults
    assert IngestAuth.from_env({"TREMOR_INGEST_TOKENS": f"unit-1={T1}", "TREMOR_INGEST_AUTH": "required"}).mode == "required"
    with pytest.raises(ConfigError):
        ExportAuth("short")
    assert MIN_TOKEN_LEN == 16


# --- the three ingest modes over HTTP -------------------------------------------------------------------------

def test_off_accepts_everything_including_wrong_tokens(tmp_path):
    app, c, _ = make_app(tmp_path, mode="off", tokens={})
    try:
        assert c.post("/api/ingest", json=batch()).status_code == 202
        assert c.post("/api/ingest", json=batch(seq0=10), headers=TOK("anything-at-all-123456")).status_code == 202
        assert rows(app) == 10
    finally:
        app.config["TREMOR_SHUTDOWN"]()


def test_optional_accepts_missing_rejects_wrong_accepts_right(tmp_path):
    app, c, _ = make_app(tmp_path, mode="optional")
    try:
        assert c.post("/api/ingest", json=batch(seq0=0)).status_code == 202                                     # legacy unit: no token
        assert c.post("/api/ingest", json=batch(seq0=10), headers=TOK(T1)).status_code == 202                   # right token
        wrong = c.post("/api/ingest", json=batch(seq0=20), headers=TOK("x" * 30))
        assert wrong.status_code == 401 and wrong.get_json() == {"error": "unauthorized"}
        assert c.post("/api/ingest", json=batch(seq0=30), headers=TOK(T2)).status_code == 401                   # another unit's token
        assert c.post("/api/ingest", json=batch("unit-9", 0), headers=TOK(T1)).status_code == 401               # unit without a token
        assert c.post("/api/ingest", json=batch("unit-9", 0)).status_code == 202                                # ...but no token is fine here
        assert rows(app) == 10 and rows(app, "unit-9") == 5                                                     # rejected batches stored nothing
        s = app.config["TREMOR_INGEST_AUTH"].summary()
        assert s["mode"] == "optional" and s["authenticated"] == 1 and s["missing_accepted"] == 2 and s["rejected_wrong"] == 3
        assert s["units_seen_without_token"] == ["unit-1", "unit-9"]
    finally:
        app.config["TREMOR_SHUTDOWN"]()


def test_required_rejects_missing_and_wrong_and_accepts_only_the_right_token_for_that_unit(tmp_path):
    app, c, _ = make_app(tmp_path, mode="required")
    try:
        assert c.post("/api/ingest", json=batch()).status_code == 401
        assert c.post("/api/ingest", json=batch(), headers=TOK("nope" * 8)).status_code == 401
        assert c.post("/api/ingest", json=batch(), headers=TOK(T2)).status_code == 401
        assert c.post("/api/ingest", json=batch("unit-9"), headers=TOK(T1)).status_code == 401                  # unknown unit
        assert c.post("/api/ingest", json=batch(), headers=TOK("")).status_code == 401                          # empty header
        assert rows(app) == 0
        ok = c.post("/api/ingest", json=batch(), headers=TOK(T1))
        assert ok.status_code == 202 and ok.get_json()["inserted"] == 5
        assert c.post("/api/ingest", json=batch("unit-2"), headers=TOK(T2)).status_code == 202
        s = app.config["TREMOR_INGEST_AUTH"].summary()
        assert (s["authenticated"], s["rejected_missing"], s["rejected_wrong"]) == (2, 2, 3)
    finally:
        app.config["TREMOR_SHUTDOWN"]()


def test_legacy_v1_payloads_are_handled_the_same_way_as_v2(tmp_path):
    app, c, _ = make_app(tmp_path, mode="optional")
    try:
        v1 = v1_batch("unit-1", [v1_reading(50.0, T0 - 2), v1_reading(50.01, T0 - 1)])
        assert c.post("/api/ingest", json=v1).status_code == 202                 # the deployed Unit 1: no token, accepted
        assert c.post("/api/ingest", json=v1, headers=TOK("w" * 24)).status_code == 401
    finally:
        app.config["TREMOR_SHUTDOWN"]()


def test_a_401_does_not_reveal_whether_the_token_was_missing_or_wrong_and_never_echoes_it(tmp_path):
    app, c, _ = make_app(tmp_path, mode="required")
    try:
        missing = c.post("/api/ingest", json=batch())
        wrong = c.post("/api/ingest", json=batch(), headers=TOK("secret-wrong-token-12345"))
        assert missing.status_code == wrong.status_code == 401 and missing.get_json() == wrong.get_json()
        assert "secret-wrong-token-12345" not in wrong.get_data(as_text=True) and T1 not in wrong.get_data(as_text=True)
    finally:
        app.config["TREMOR_SHUTDOWN"]()


def test_tokens_never_appear_in_the_logs_and_missing_tokens_are_logged_rate_limited(tmp_path, caplog):
    app, c, clock = make_app(tmp_path, mode="optional")
    try:
        with caplog.at_level(logging.INFO):
            c.post("/api/ingest", json=batch(seq0=0), headers=TOK(T1))
            c.post("/api/ingest", json=batch(seq0=10), headers=TOK("wrong-token-abcdefghijk"))
            for i in range(5):                                                   # a legacy unit posting every 30 s
                clock.advance(30)
                c.post("/api/ingest", json=batch(seq0=100 + i * 10))
        text = caplog.text
        assert T1 not in text and T2 not in text and "wrong-token-abcdefghijk" not in text
        assert text.count("WITHOUT a token") == 1                                # once, not on every POST
        assert "rejected: token does not match" in text
        clock.advance(700)                                                       # a long time later: reminded once more
        with caplog.at_level(logging.INFO):
            c.post("/api/ingest", json=batch(seq0=500))
        assert caplog.text.count("WITHOUT a token") == 2
    finally:
        app.config["TREMOR_SHUTDOWN"]()


def test_tokens_are_compared_in_constant_time(tmp_path, monkeypatch):
    import tremor.security as sec
    seen = []
    real = sec.hmac.compare_digest
    monkeypatch.setattr(sec.hmac, "compare_digest", lambda a, b: seen.append((a, b)) or real(a, b))
    auth = IngestAuth({"unit-1": T1}, "required")
    assert auth.check("unit-1", T1).allowed and not auth.check("unit-1", "y" * 30).allowed
    assert len(seen) == 2 and all(isinstance(a, bytes) and isinstance(b, bytes) for a, b in seen)


def test_unauthorized_requests_never_reach_storage_even_if_storage_is_broken(tmp_path, monkeypatch):
    app, c, _ = make_app(tmp_path, mode="required")
    try:
        def boom(*_a, **_k):
            raise AssertionError("storage must not be touched for an unauthenticated request")
        monkeypatch.setattr(app.config["TREMOR_STORE"], "ingest", boom)
        assert c.post("/api/ingest", json=batch()).status_code == 401
    finally:
        app.config["TREMOR_SHUTDOWN"]()


def test_health_shows_the_auth_mode_and_the_counters_that_tell_you_when_it_is_safe_to_require_tokens(tmp_path):
    app, c, _ = make_app(tmp_path, mode="optional")
    try:
        c.post("/api/ingest", json=batch())
        h = c.get("/api/health").get_json()
        assert h["ingest_auth"]["mode"] == "optional" and h["ingest_auth"]["missing_accepted"] == 1
        assert h["ingest_auth"]["units_seen_without_token"] == ["unit-1"] and h["ingest_auth"]["units_with_tokens"] == 2
        assert T1 not in c.get("/api/health").get_data(as_text=True)               # never the tokens themselves
        assert h["export"]["enabled"] is False
    finally:
        app.config["TREMOR_SHUTDOWN"]()


def test_oversized_bodies_are_refused_before_parsing(tmp_path):
    app, c, _ = make_app(tmp_path, mode="required")
    try:
        r = c.post("/api/ingest", data=b"x" * (MAX_BODY_BYTES + 10), content_type="application/json")
        assert r.status_code == 413
        big_but_legal = v2_series("unit-1", BOOT, T0 - 500, 500)
        assert c.post("/api/ingest", json=big_but_legal, headers=TOK(T1)).status_code == 202
    finally:
        app.config["TREMOR_SHUTDOWN"]()


# --- /api/export ----------------------------------------------------------------------------------------------------

def _app_with_an_export(tmp_path, export_token=EXPORT, **kw):
    app, c, clock = make_app(tmp_path, mode="off", tokens={}, export_token=export_token, **kw)
    from helpers import utc
    from tremor.ingest import parse_payload
    old = utc(2026, 9, 1, 3)
    app.config["TREMOR_STORE"].ingest(parse_payload(v2_series("unit-1", BOOT, old, 50)), old + 53)
    app.config["TREMOR_RETENTION"].run_until_idle()
    return app, c, clock


def test_export_is_disabled_when_no_token_is_configured(tmp_path):
    app, c, _ = _app_with_an_export(tmp_path, export_token=None)
    try:
        r = c.get("/api/export/unit-1/2026-09-01", headers=TOK(EXPORT))
        assert r.status_code == 403 and "disabled" in r.get_json()["error"]
        assert c.get("/api/health").get_json()["export"]["enabled"] is False
    finally:
        app.config["TREMOR_SHUTDOWN"]()


def test_export_needs_the_right_token_in_the_header(tmp_path):
    app, c, _ = _app_with_an_export(tmp_path)
    try:
        url = "/api/export/unit-1/2026-09-01"
        assert c.get(url).status_code == 401
        assert c.get(url, headers=TOK("wrong-token-0123456789")).status_code == 401
        assert c.get(url + f"?token={EXPORT}").status_code == 401                # URLs end up in access logs: never accepted
        ok = c.get(url, headers=TOK(EXPORT))
        assert ok.status_code == 200 and ok.mimetype == "application/gzip"
        assert c.get("/api/export/unit-1/2026-09-05", headers=TOK(EXPORT)).status_code == 404
        assert c.get("/api/health").get_json()["export"]["enabled"] is True
    finally:
        app.config["TREMOR_SHUTDOWN"]()


def test_an_ingest_token_does_not_unlock_export_and_vice_versa(tmp_path):
    app, c, _ = make_app(tmp_path, mode="required", export_token=EXPORT)
    try:
        assert c.get("/api/export/unit-1/2026-09-01", headers=TOK(T1)).status_code == 401
        assert c.post("/api/ingest", json=batch(), headers=TOK(EXPORT)).status_code == 401
    finally:
        app.config["TREMOR_SHUTDOWN"]()


def test_export_is_rate_limited_and_rejects_before_doing_any_file_work(tmp_path):
    app, c, clock = _app_with_an_export(tmp_path, export_rate=(3, 60.0))
    try:
        codes = [c.get("/api/export/unit-1/2026-09-01", headers=TOK(EXPORT)).status_code for _ in range(5)]
        assert codes == [200, 200, 200, 429, 429]
        clock.advance(61)
        assert c.get("/api/export/unit-1/2026-09-01", headers=TOK(EXPORT)).status_code == 200
    finally:
        app.config["TREMOR_SHUTDOWN"]()


# --- /api/history and /api/health stay public; history is rate limited ---------------------------------------------------

def test_history_and_health_need_no_token_even_when_ingest_is_required(tmp_path):
    app, c, _ = make_app(tmp_path, mode="required")
    try:
        c.post("/api/ingest", json=batch(), headers=TOK(T1))
        assert c.get("/api/history?unit=unit-1").status_code == 200
        assert c.get("/api/health").status_code == 200 and c.get("/api/units").status_code == 200
    finally:
        app.config["TREMOR_SHUTDOWN"]()


def test_history_is_rate_limited_per_client_and_refills_with_time(tmp_path):
    app, c, clock = make_app(tmp_path, mode="off", tokens={}, history_rate=(5, 60.0))
    try:
        c.post("/api/ingest", json=batch())
        codes = [c.get("/api/history?unit=unit-1").status_code for _ in range(8)]
        assert codes == [200] * 5 + [429] * 3
        limited = c.get("/api/history?unit=unit-1")
        assert limited.status_code == 429 and limited.get_json()["error"] == "rate limit exceeded"
        assert 1 <= int(limited.headers["Retry-After"]) <= 13
        clock.advance(13)                                                        # 5 per 60 s = one token per 12 s
        assert c.get("/api/history?unit=unit-1").status_code == 200
        assert c.get("/api/history?unit=unit-1").status_code == 429
    finally:
        app.config["TREMOR_SHUTDOWN"]()


def test_history_rate_limit_is_per_client_address_and_can_use_a_proxy_header(tmp_path):
    app, c, _ = make_app(tmp_path, mode="off", tokens={}, history_rate=(2, 60.0), client_ip_header="X-Real-IP")
    try:
        c.post("/api/ingest", json=batch())
        hit = lambda ip: c.get("/api/history?unit=unit-1", headers={"X-Real-IP": ip}).status_code
        assert [hit("1.1.1.1"), hit("1.1.1.1"), hit("1.1.1.1")] == [200, 200, 429]
        assert hit("2.2.2.2") == 200                                             # a different client is unaffected
        assert hit("1.1.1.1, 10.0.0.1") == 429                                   # first hop of a list is the client
        h = c.get("/api/health", headers={"X-Real-IP": "9.9.9.9"}).get_json()
        assert h["history_rate_limit"]["your_address_as_seen"] == "9.9.9.9"
        assert h["history_rate_limit"]["client_ip_header"] == "X-Real-IP" and h["history_rate_limit"]["requests"] == 2
    finally:
        app.config["TREMOR_SHUTDOWN"]()


def test_a_rate_limited_history_request_does_not_touch_the_database(tmp_path, monkeypatch):
    app, c, _ = make_app(tmp_path, mode="off", tokens={}, history_rate=(1, 60.0))
    try:
        c.post("/api/ingest", json=batch())
        assert c.get("/api/history?unit=unit-1").status_code == 200

        def boom(*_a, **_k):
            raise AssertionError("the database must not be queried for a rate-limited request")
        monkeypatch.setattr(app.config["TREMOR_STORE"], "unit_states", boom)
        monkeypatch.setattr(app.config["TREMOR_STORE"], "history", boom)
        assert c.get("/api/history?unit=unit-1").status_code == 429
    finally:
        app.config["TREMOR_SHUTDOWN"]()


def test_rate_limiter_is_bounded_and_deterministic():
    clock = FakeClock(0.0)
    rl = RateLimiter(2, 10.0, clock, max_keys=3)
    assert [rl.allow("a")[0] for _ in range(3)] == [True, True, False]
    for k in "bcdef":
        rl.allow(k)
    assert len(rl._buckets) <= 3                                                 # memory is bounded (LRU)
    clock.advance(5)
    assert rl.allow("a")[0] is True                                              # evicted key starts fresh, refilled key allowed
    assert parse_rate("30/60", (1, 1.0)) == (30, 60.0) and parse_rate("", (7, 9.0)) == (7, 9.0)
    for bad in ("30", "0/60", "30/0", "x/y"):
        with pytest.raises(ConfigError):
            parse_rate(bad, (1, 1.0))


# --- environment wiring ---------------------------------------------------------------------------------------------------------

def test_create_app_reads_its_security_settings_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("TREMOR_INGEST_TOKENS", f"unit-1={T1}")
    monkeypatch.setenv("TREMOR_INGEST_AUTH", "required")
    monkeypatch.setenv("TREMOR_EXPORT_TOKEN", EXPORT)
    monkeypatch.setenv("TREMOR_HISTORY_RATE", "7/30")
    monkeypatch.setenv("TREMOR_CLIENT_IP_HEADER", "X-Real-IP")
    app = create_app(simulated_units=[], db_path=str(tmp_path / "r.db"), clock=FakeClock(T0))
    try:
        c = app.test_client()
        assert c.post("/api/ingest", json=batch()).status_code == 401
        assert c.post("/api/ingest", json=batch(), headers=TOK(T1)).status_code == 202
        h = c.get("/api/health").get_json()
        assert h["ingest_auth"]["mode"] == "required" and h["export"]["enabled"] and h["history_rate_limit"]["requests"] == 7
    finally:
        app.config["TREMOR_SHUTDOWN"]()


def test_a_misconfigured_environment_stops_the_app_from_starting(tmp_path, monkeypatch):
    monkeypatch.setenv("TREMOR_INGEST_TOKENS", "unit-1=tooshort")
    with pytest.raises(ConfigError):
        create_app(simulated_units=[], db_path=str(tmp_path / "r.db"))
    monkeypatch.setenv("TREMOR_INGEST_TOKENS", f"unit-1={T1}")
    monkeypatch.setenv("TREMOR_INGEST_AUTH", "sometimes")
    with pytest.raises(ConfigError):
        create_app(simulated_units=[], db_path=str(tmp_path / "r.db"))


def test_with_nothing_configured_the_server_behaves_exactly_as_before(tmp_path, monkeypatch):
    for k in ("TREMOR_INGEST_TOKENS", "TREMOR_INGEST_AUTH", "TREMOR_EXPORT_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    app = create_app(simulated_units=[], db_path=str(tmp_path / "r.db"), clock=FakeClock(T0))
    try:
        c = app.test_client()
        assert c.post("/api/ingest", json=batch()).status_code == 202              # no auth by default: nothing breaks on upgrade
        assert c.get("/api/health").get_json()["ingest_auth"]["mode"] == "off"
    finally:
        app.config["TREMOR_SHUTDOWN"]()


# --- the client side: the token stays on the Pico, never in the repo --------------------------------------------------------------

def _src(name):
    return (ROOT / name).read_text()


def test_client_sends_the_token_header_only_when_configured_and_never_prints_it():
    src = _src("wifi_unit_client.py")
    tree = ast.parse(src)
    assert "import wifi_config" in src and 'getattr(wifi_config, "INGEST_TOKEN", None)' in src
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "timeout_post"]
    assert len(calls) == 1 and any(k.arg == "extra_headers" for k in calls[0].keywords)
    assert '"X-Tremor-Token": INGEST_TOKEN' in src and "if INGEST_TOKEN else None" in src
    for n in ast.walk(tree):                                                            # no print() may mention the token
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "print":
            assert "INGEST_TOKEN" not in ast.dump(n)


def test_http_client_puts_the_token_in_a_header_line_of_the_request():
    sys.path.insert(0, str(ROOT))
    from http_client import _build_request
    req = _build_request("POST", "example.com", "/api/ingest", b"{}", {"X-Tremor-Token": T1})
    head = (req if isinstance(req, bytes) else req.encode()).split(b"\r\n\r\n")[0].decode()
    assert f"X-Tremor-Token: {T1}" in head
    assert "X-Tremor-Token" not in (_build_request("POST", "example.com", "/x", b"{}", None)).decode()


def test_no_token_is_committed_and_wifi_config_stays_untracked():
    example = _src("wifi_config.py.example")
    assert "INGEST_TOKEN" in example and "paste-the-token-here" in example                  # placeholder only
    ignored = subprocess.run(["git", "check-ignore", "wifi_config.py"], cwd=ROOT, capture_output=True, text=True)
    assert ignored.returncode == 0                                                          # gitignored
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    assert "wifi_config.py" not in tracked
    for f in tracked:
        if f.endswith((".py", ".md", ".example", ".html")) and (ROOT / f).is_file():
            text = (ROOT / f).read_text(errors="ignore")
            assert T1 not in text or f.startswith("tests/")                                # test fixtures aside, no real-looking secrets
    wsgi = _src("deploy/pythonanywhere_wsgi.py")
    assert "TREMOR_INGEST_TOKENS" in wsgi and "<" in wsgi.split("TREMOR_INGEST_TOKENS", 1)[1].split("\n", 1)[0]   # a <placeholder>, not a token
