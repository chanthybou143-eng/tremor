"""scripts/delete_boot_rows.py: the guarded one-off delete of one boot's raw rows."""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
spec = importlib.util.spec_from_file_location("delete_boot_rows", ROOT / "scripts" / "delete_boot_rows.py")
dbr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dbr)

from tremor.store import DAY_US, SqliteReadingStore  # noqa: E402

BAD = "398474c3bef237a1"
GOOD = "83cb5bd5d963c1e4"
DAY = 20722                                   # 2026-09-26


@pytest.fixture
def dbfile(tmp_path):
    path = tmp_path / "data" / "readings.db"
    SqliteReadingStore(str(path))              # creates the real schema
    db = sqlite3.connect(path)
    n = 0

    def add(unit, boot, seq, gps, ra=1790397000.0):
        nonlocal n
        n += 1
        db.execute("INSERT INTO readings(unit_id,boot_id,seq,gps_utc_us,gps_raw,time_src,freq_hz,amplitude_v,gps_locked,flags,received_at,ingest_id)"
                   " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (unit, boot, seq, gps, None, 2, 50.0 + (seq or 0) * 1e-4, 0.8, 1, 0, ra, 1))
    base = DAY * DAY_US + 5_000_000
    for i in range(289):
        add("unit-1", BAD, i, base + i * 1_000_000)
    for i in range(50):
        add("unit-1", GOOD, i, base + 400_000_000 + i * 1_000_000)
    for i in range(5):
        add("unit-2", BAD, i, base + i * 1_000_000)             # same boot id string, OTHER unit
    for i in range(7):
        add("unit-1", None, None, base + 900_000_000 + i * 1_000_000)   # legacy rows (no boot id)
    db.commit()
    db.close()
    return path


def run(dbfile, *extra, unit="unit-1", boot=BAD, expect="289"):
    args = ["--db", str(dbfile), "--unit", unit, "--boot-id", boot, "--expect-rows", expect, *extra]
    return dbr.main(args)


def counts(dbfile):
    db = sqlite3.connect(dbfile)
    out = dict(db.execute("SELECT unit_id || '/' || COALESCE(boot_id,'NULL'), COUNT(*) FROM readings GROUP BY 1").fetchall())
    db.close()
    return out


def test_dry_run_changes_nothing_and_says_so(dbfile, capsys):
    before = counts(dbfile)
    assert run(dbfile) == 0
    out = capsys.readouterr().out
    assert "found 289 rows" in out and "DRY RUN" in out and "--confirm-boot-id " + BAD in out
    assert counts(dbfile) == before
    assert not (dbfile.parent / "quarantine").exists()


def test_confirmed_delete_removes_exactly_that_boot_of_that_unit_and_saves_a_copy(dbfile, capsys):
    assert run(dbfile, "--confirm-boot-id", BAD) == 0
    out = capsys.readouterr().out
    assert "DELETED 289 rows" in out
    c = counts(dbfile)
    assert BAD not in " ".join(k for k in c if k.startswith("unit-1/"))
    assert c["unit-1/" + GOOD] == 50 and c["unit-2/" + BAD] == 5 and c["unit-1/NULL"] == 7
    files = list((dbfile.parent / "quarantine").glob(f"unit-1_{BAD}_*.jsonl"))
    assert len(files) == 1 and stat.S_IMODE(os.stat(files[0]).st_mode) == 0o600
    rows = [json.loads(line) for line in files[0].read_text().splitlines()]
    assert len(rows) == 289 and {r["boot_id"] for r in rows} == {BAD} and {r["unit_id"] for r in rows} == {"unit-1"}
    assert sorted(r["seq"] for r in rows) == list(range(289))
    assert stat.S_IMODE(os.stat(files[0].parent).st_mode) == 0o700


def test_a_second_run_finds_nothing_and_refuses(dbfile, capsys):
    assert run(dbfile, "--confirm-boot-id", BAD) == 0
    capsys.readouterr()
    assert run(dbfile, "--confirm-boot-id", BAD) == 2
    assert "expected 289 rows for unit-1 / " + BAD + ", found 0" in capsys.readouterr().err


@pytest.mark.parametrize("expect", ["288", "290", "0", "-1"])
def test_wrong_expected_count_refuses(dbfile, capsys, expect):
    before = counts(dbfile)
    assert run(dbfile, "--confirm-boot-id", BAD, expect=expect) == 2
    assert "Nothing was changed" in capsys.readouterr().err
    assert counts(dbfile) == before


@pytest.mark.parametrize("boot", ["", "%", "398474c3bef237a%", "398474C3BEF237A1", "398474c3bef237a", "398474c3bef237a1x",
                                  "398474c3bef237a1' OR '1'='1", "NULL", "None"])
def test_a_malformed_or_wildcard_boot_id_refuses(dbfile, capsys, boot):
    before = counts(dbfile)
    assert run(dbfile, "--confirm-boot-id", boot, boot=boot) == 2
    assert counts(dbfile) == before


def test_the_confirmation_must_repeat_the_boot_id_exactly(dbfile, capsys):
    before = counts(dbfile)
    assert run(dbfile, "--confirm-boot-id", GOOD) == 2
    assert "does not match" in capsys.readouterr().err and counts(dbfile) == before
    assert not (dbfile.parent / "quarantine").exists()


def test_a_missing_expect_rows_argument_is_an_argparse_error(dbfile):
    with pytest.raises(SystemExit) as e:
        dbr.main(["--db", str(dbfile), "--unit", "unit-1", "--boot-id", BAD])
    assert e.value.code == 2


def test_it_refuses_when_retention_has_already_exported_or_aggregated_the_day(dbfile, capsys):
    db = sqlite3.connect(dbfile)
    db.execute("INSERT INTO retention_days(unit_id, day, export_done) VALUES('unit-1', ?, 1)", (DAY,))
    db.commit(); db.close()
    before = counts(dbfile)
    assert run(dbfile, "--confirm-boot-id", BAD) == 2
    assert "already exported" in capsys.readouterr().err and counts(dbfile) == before
    db = sqlite3.connect(dbfile)
    db.execute("UPDATE retention_days SET export_done = 0, agg_next_hour = 3")
    db.commit(); db.close()
    assert run(dbfile, "--confirm-boot-id", BAD) == 2 and counts(dbfile) == before


def test_an_untouched_retention_row_does_not_block(dbfile):
    db = sqlite3.connect(dbfile)
    db.execute("INSERT INTO retention_days(unit_id, day) VALUES('unit-1', ?)", (DAY,))
    db.commit(); db.close()
    assert run(dbfile, "--confirm-boot-id", BAD) == 0


def test_it_refuses_when_an_event_overlaps_the_rows(dbfile, capsys):
    db = sqlite3.connect(dbfile)
    base = DAY * DAY_US + 5_000_000
    db.execute("INSERT INTO events(unit_id,start_us,end_us,reason) VALUES('unit-1',?,?, 'rocof')", (base + 100_000_000, base + 110_000_000))
    db.commit(); db.close()
    before = counts(dbfile)
    assert run(dbfile, "--confirm-boot-id", BAD) == 2
    assert "event interval" in capsys.readouterr().err and counts(dbfile) == before


def test_an_event_on_another_unit_does_not_block(dbfile):
    db = sqlite3.connect(dbfile)
    base = DAY * DAY_US + 5_000_000
    db.execute("INSERT INTO events(unit_id,start_us,end_us,reason) VALUES('unit-2',?,?, 'rocof')", (base, base + 10_000_000))
    db.commit(); db.close()
    assert run(dbfile, "--confirm-boot-id", BAD) == 0


def test_a_delete_that_would_touch_a_different_count_is_rolled_back(dbfile, monkeypatch):
    """Simulate rows changing between the guard check and the delete: the transaction re-checks and rolls back."""
    db = dbr._connect(str(dbfile))
    db2 = sqlite3.connect(dbfile)
    db2.execute("DELETE FROM readings WHERE unit_id='unit-1' AND boot_id=? AND seq < 3", (BAD,))
    db2.commit(); db2.close()
    before = counts(dbfile)
    with pytest.raises(dbr.Refused):
        dbr.delete(db, "unit-1", BAD, 289)
    db.close()
    assert counts(dbfile) == before


def test_a_missing_database_refuses_without_creating_one(tmp_path, capsys):
    p = tmp_path / "nope.db"
    assert dbr.main(["--db", str(p), "--unit", "unit-1", "--boot-id", BAD, "--expect-rows", "289"]) == 2
    assert not p.exists()


def test_only_the_readings_table_is_touched(dbfile):
    db = sqlite3.connect(dbfile)
    db.execute("INSERT INTO units(unit_id, first_seen, last_received_at, readings_total) VALUES('unit-1', 1, 2, 47188)")
    db.commit(); db.close()
    assert run(dbfile, "--confirm-boot-id", BAD) == 0
    db = sqlite3.connect(dbfile)
    assert db.execute("SELECT readings_total FROM units").fetchone()[0] == 47188
    db.close()
