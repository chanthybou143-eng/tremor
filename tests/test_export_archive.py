"""Export archiving (server side): /api/export/manifest, the gated `retention delete-exports`, the 410 for an
archived day, and the retention_days.export_deleted_at column added at startup without a version bump."""

from __future__ import annotations

import hashlib
import os
import sqlite3

import pytest

from helpers import FakeClock, utc, v2_series
from tremor.ingest import parse_payload
from tremor.retention import EXPORT_KEEP_DAYS, RetentionConfig, RetentionEngine, _cli, day_to_date, delete_exports
from tremor.store import SCHEMA_VERSION, open_store
from tremor.webapp import create_app

BOOT = "9f3a51c07d2e4b18"
TOKEN = "export-token-0123456789abcdef"
AUTH = {"X-Tremor-Token": TOKEN}
NOW = utc(2026, 12, 1, 12)                                   # "today"


def fill(store, day_iso, n=300, seq0=0):
    d = day_iso.split("-")
    t = utc(int(d[0]), int(d[1]), int(d[2]), 3)
    store.ingest(parse_payload(v2_series("unit-1", BOOT, t, n, seq0=seq0)), t + n + 3)


@pytest.fixture
def world(tmp_path):
    """Three days: 1 Oct and 15 Oct (old: exported, aggregated, pruned) and 25 Nov (exported, not yet pruned)."""
    clock = FakeClock(NOW)
    app = create_app(simulated_units=[], db_path=str(tmp_path / "readings.db"), clock=clock, export_token=TOKEN,
                     retention_config=RetentionConfig(export_dir=str(tmp_path / "exports"), raw_days=14,
                                                      settle_s=3600.0))
    store, eng = app.config["TREMOR_STORE"], app.config["TREMOR_RETENTION"]
    for i, day in enumerate(("2026-10-01", "2026-10-15", "2026-11-25")):
        fill(store, day, seq0=i * 10_000)
    eng.run_until_idle()
    yield app, app.test_client(), store, eng, clock, tmp_path
    app.config["TREMOR_SHUTDOWN"]()


def day_no(iso):
    from datetime import date
    return (date.fromisoformat(iso) - day_to_date(0)).days


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def confirm_file(tmp, lines):
    p = tmp / "delete_confirm.txt"
    p.write_text("".join(f"{u} {d} {h}\n" for u, d, h in lines))
    return str(p)


def lines_for(store, eng, days):
    out = []
    for d in days:
        st = store.day_states("unit-1")[day_no(d)]
        out.append(("unit-1", d, st.export_sha256))
    return out


# --- manifest -------------------------------------------------------------------------------------------

def test_the_manifest_needs_the_export_token_and_lists_every_exported_day(world):
    app, client, store, eng, clock, tmp = world
    assert client.get("/api/export/manifest").status_code == 401
    assert client.get("/api/export/manifest", headers={"X-Tremor-Token": "wrong-token-000000000"}).status_code == 401
    r = client.get("/api/export/manifest", headers=AUTH)
    assert r.status_code == 200
    days = {d["day"]: d for d in r.get_json()["units"]["unit-1"]}
    assert set(days) == {"2026-10-01", "2026-10-15", "2026-11-25"}       # empty days (no file) left out
    old, recent = days["2026-10-01"], days["2026-11-25"]
    path = eng.export_path("unit-1", day_no("2026-10-01"))
    assert old["sha256"] == sha(path) and old["size"] == os.path.getsize(path) and old["rows"] == 300
    assert old["pruned_done"] is True and old["file_present"] is True and old["export_deleted_at"] is None
    assert recent["pruned_done"] is False                    # inside raw_days: not pruned yet
    assert TOKEN not in r.get_data(as_text=True)


def test_empty_days_without_an_export_file_are_not_listed(world):
    app, client, store, eng, clock, tmp = world
    states = store.day_states("unit-1")
    assert any(s.export_done and s.export_sha256 is None for s in states.values())   # e.g. 26-30 Nov, no data
    days = [d["day"] for d in client.get("/api/export/manifest", headers=AUTH).get_json()["units"]["unit-1"]]
    assert days == ["2026-10-01", "2026-10-15", "2026-11-25"]


def test_the_manifest_is_off_when_exports_are_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("TREMOR_EXPORT_TOKEN", raising=False)
    app = create_app(simulated_units=[], db_path=str(tmp_path / "r.db"), clock=FakeClock(NOW), export_token=None,
                     retention_config=RetentionConfig(export_dir=str(tmp_path / "ex")))
    assert app.test_client().get("/api/export/manifest", headers=AUTH).status_code == 403
    app.config["TREMOR_SHUTDOWN"]()


# --- delete-exports: every gate -------------------------------------------------------------------------

def test_a_dry_run_deletes_nothing(world):
    app, client, store, eng, clock, tmp = world
    out = []
    rc = delete_exports(store, eng, confirm_file(tmp, lines_for(store, eng, ["2026-10-01"])), 30, False, NOW, out.append)
    assert rc == 0 and out[0].startswith("WOULD DELETE unit-1 2026-10-01")
    assert os.path.isfile(eng.export_path("unit-1", day_no("2026-10-01")))


def test_a_verified_pruned_old_export_is_deleted_recorded_and_then_answers_410(world):
    app, client, store, eng, clock, tmp = world
    path = eng.export_path("unit-1", day_no("2026-10-01"))
    out = []
    rc = delete_exports(store, eng, confirm_file(tmp, lines_for(store, eng, ["2026-10-01"])), 30, True, NOW, out.append)
    assert rc == 0 and out[0].startswith("DELETED") and not os.path.exists(path)
    st = store.day_states("unit-1")[day_no("2026-10-01")]
    assert st.export_deleted_at == NOW and st.pruned_done
    r = client.get("/api/export/unit-1/2026-10-01", headers=AUTH)
    assert r.status_code == 410 and "archived off-server on 2026-12-01" in r.get_json()["error"]
    assert client.get("/api/export/unit-1/2026-10-15", headers=AUTH).status_code == 200      # others untouched
    man = {d["day"]: d for d in client.get("/api/export/manifest", headers=AUTH).get_json()["units"]["unit-1"]}
    assert man["2026-10-01"]["file_present"] is False and man["2026-10-01"]["export_deleted_at"] == NOW
    assert client.get("/api/health").get_json()["store"]["days_export_archived_off_server"] == 1
    out.clear()                                              # idempotent: a second run skips it
    assert delete_exports(store, eng, confirm_file(tmp, lines_for(store, eng, ["2026-10-01"])), 30, True, NOW, out.append) == 1
    assert "already deleted" in out[0]


def test_a_day_not_yet_pruned_is_refused_because_it_would_block_its_prune(world):
    app, client, store, eng, clock, tmp = world
    out = []
    rc = delete_exports(store, eng, confirm_file(tmp, lines_for(store, eng, ["2026-11-25"])), 0, True, NOW, out.append)
    assert rc == 1 and "not pruned yet" in out[0]
    assert os.path.isfile(eng.export_path("unit-1", day_no("2026-11-25")))


def test_a_day_younger_than_keep_days_is_refused(world):
    app, client, store, eng, clock, tmp = world
    out = []
    rc = delete_exports(store, eng, confirm_file(tmp, lines_for(store, eng, ["2026-10-15"])), 60, True, NOW, out.append)
    assert rc == 1 and "younger than 60 days" in out[0]


def test_a_hash_that_does_not_match_the_stored_one_is_refused(world):
    app, client, store, eng, clock, tmp = world
    out = []
    rc = delete_exports(store, eng, confirm_file(tmp, [("unit-1", "2026-10-01", "ab" * 32)]), 30, True, NOW, out.append)
    assert rc == 1 and "differs from the hash stored" in out[0]


def test_a_server_file_changed_since_export_is_refused(world):
    app, client, store, eng, clock, tmp = world
    path = eng.export_path("unit-1", day_no("2026-10-01"))
    with open(path, "ab") as fh:
        fh.write(b"x")
    out = []
    rc = delete_exports(store, eng, confirm_file(tmp, lines_for(store, eng, ["2026-10-01"])), 30, True, NOW, out.append)
    assert rc == 1 and "no longer matches" in out[0] and os.path.exists(path)


def test_a_day_needing_attention_and_malformed_lines_are_refused(world):
    app, client, store, eng, clock, tmp = world
    st = store.day_states("unit-1")[day_no("2026-10-15")]
    st.attention = "something"
    store.save_day_state(st)
    p = tmp / "c.txt"
    p.write_text("# comment\nunit-1 2026-10-15 " + st.export_sha256 + "\nunit-1 2026-13-40 " + "a" * 64
                 + "\nunit-1 2026-10-01\nunit-1 2026-10-02 " + "a" * 64 + "\n")
    out = []
    assert delete_exports(store, eng, str(p), 30, True, NOW, out.append) == 1
    assert "needs attention" in out[0] and "bad date" in out[1] and "not 'unit" in out[2]
    assert "no export recorded" in out[3] and out[-1] == "deleted 0, skipped 4"


def test_the_cli_is_a_dry_run_by_default_and_deletes_with_yes(world, capsys, monkeypatch):
    app, client, store, eng, clock, tmp = world
    import tremor.retention
    monkeypatch.setattr(tremor.retention.time, "time", lambda: NOW)     # the CLI reads the real clock
    c = confirm_file(tmp, lines_for(store, eng, ["2026-10-01", "2026-10-15"]))
    args = ["--db", store.path, "--export-dir", eng.cfg.export_dir, "delete-exports", "--confirm", c]
    assert _cli(args) == 0 and "would delete 2" in capsys.readouterr().out
    assert _cli(args + ["--yes"]) == 0 and "deleted 2, skipped 0" in capsys.readouterr().out
    assert EXPORT_KEEP_DAYS == 30


def test_retention_carries_on_normally_after_exports_are_deleted(world):
    app, client, store, eng, clock, tmp = world
    delete_exports(store, eng, confirm_file(tmp, lines_for(store, eng, ["2026-10-01", "2026-10-15"])), 30, True, NOW,
                   lambda *_: None)
    clock.t = NOW + 30 * 86400                               # the 25 Nov day now ages out and is pruned as usual
    eng.run_until_idle()
    states = store.day_states("unit-1")
    assert states[day_no("2026-11-25")].pruned_done and all(s.attention is None for s in states.values())
    assert client.get("/api/health").get_json()["store"]["days_needing_attention"] == []
    ov = client.get("/api/history", query_string={"unit": "unit-1", "resolution": "1min",
                                                  "from": utc(2026, 10, 1), "to": utc(2026, 10, 2)}).get_json()
    assert sum(a["n"] for a in ov["aggregates"]) == 300      # the aggregates of an archived day are still served


# --- schema ----------------------------------------------------------------------------------------------

def test_export_deleted_at_is_added_to_an_existing_database_without_a_version_bump(tmp_path):
    path = str(tmp_path / "r.db")
    open_store(path)
    with sqlite3.connect(path) as db:                        # a retention_days table from before the column
        db.execute("ALTER TABLE retention_days DROP COLUMN export_deleted_at")
        db.execute("INSERT INTO retention_days(unit_id, day, export_done, pruned_done) VALUES ('unit-1', 20000, 1, 1)")
    store = open_store(path)
    with sqlite3.connect(path) as db:
        assert "export_deleted_at" in {r[1] for r in db.execute("PRAGMA table_info(retention_days)")}
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1 == SCHEMA_VERSION
    st = store.day_states("unit-1")[20000]
    assert st.pruned_done and st.export_deleted_at is None
