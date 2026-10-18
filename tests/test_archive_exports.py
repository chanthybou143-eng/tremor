"""scripts/archive_exports.py end to end against the real server code (Flask test client as the HTTP layer):
download + verify + second copy + deletion list, then the server's delete-exports with that list."""

from __future__ import annotations

import gzip
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import archive_exports as ax  # noqa: E402
from helpers import FakeClock, utc, v2_series  # noqa: E402
from tremor.ingest import parse_payload  # noqa: E402
from tremor.retention import RetentionConfig, delete_exports  # noqa: E402
from tremor.webapp import create_app  # noqa: E402

TOKEN = "export-token-0123456789abcdef"
BOOT = "9f3a51c07d2e4b18"
NOW = utc(2026, 12, 1, 12)


@pytest.fixture
def world(tmp_path):
    clock = FakeClock(NOW)
    app = create_app(simulated_units=[], db_path=str(tmp_path / "srv" / "readings.db"), clock=clock, export_token=TOKEN,
                     retention_config=RetentionConfig(export_dir=str(tmp_path / "srv" / "exports"), raw_days=14,
                                                      settle_s=3600.0))
    store, eng = app.config["TREMOR_STORE"], app.config["TREMOR_RETENTION"]
    for i, (y, m, d) in enumerate(((2026, 10, 1), (2026, 10, 15), (2026, 11, 25))):
        t = utc(y, m, d, 3)
        store.ingest(parse_payload(v2_series("unit-1", BOOT, t, 200, seq0=i * 10_000)), t + 203)
    eng.run_until_idle()
    client = app.test_client()
    calls = []

    def fetch(path):
        calls.append(path)
        r = client.get(path, headers={"X-Tremor-Token": TOKEN})
        if r.status_code != 200:
            raise ax.FetchError(r.status_code)
        return r.data
    dest, second = tmp_path / "archive", tmp_path / "second"
    second.mkdir()
    yield dict(app=app, client=client, store=store, eng=eng, fetch=fetch, calls=calls, dest=dest, second=second,
               tmp=tmp_path)
    app.config["TREMOR_SHUTDOWN"]()


def run(w, **kw):
    out = []
    rc, conf = ax.archive(w["fetch"], w["dest"], w["second"], allow_same_disk=True, out=out.append,
                          today=date(2026, 12, 1), **kw)
    return rc, conf, out


@pytest.fixture
def w(world):
    return world


def test_first_run_archives_verifies_both_copies_and_lists_only_pruned_old_days(w):
    rc, conf, out = run(w)
    assert rc == 0
    for day in ("2026-10-01", "2026-10-15", "2026-11-25"):
        for base in (w["dest"], w["second"]):
            p = base / "unit-1" / f"readings_unit-1_{day}.csv.gz"
            assert p.exists() and gzip.decompress(p.read_bytes()).decode().count("\n") == 201
    listed = [ln.split()[1] for ln in conf.read_text().splitlines() if not ln.startswith("#")]
    assert listed == ["2026-10-01", "2026-10-15"]           # 25 Nov: not pruned, and younger than 30 days
    man = ax.load_local_manifest(w["dest"])
    assert set(man) == {("unit-1", "2026-10-01"), ("unit-1", "2026-10-15"), ("unit-1", "2026-11-25")}
    assert not list(w["dest"].rglob("*.partial")) and not list(w["second"].rglob("*.partial"))


def test_a_second_run_downloads_nothing_new_but_re_verifies_everything(w):
    run(w)
    n = len(w["calls"])
    rc, conf, out = run(w)
    assert rc == 0 and len(w["calls"]) == n + 1             # only the manifest
    assert "verified in both copies: 3" in out[-2]


def test_the_server_deletes_exactly_the_listed_days_and_the_archive_keeps_them(w):
    rc, conf, out = run(w)
    srv = []
    assert delete_exports(w["store"], w["eng"], str(conf), 30, True, NOW, srv.append) == 0
    assert srv[-1] == "deleted 2, skipped 0"
    assert w["client"].get("/api/export/unit-1/2026-10-01", headers={"X-Tremor-Token": TOKEN}).status_code == 410
    rc, conf2, out = run(w)                                 # next run: archived days stay verified, nothing to list
    assert rc == 0 and conf2 is None and "verified in both copies: 3" in out[-2]


def test_a_changed_local_copy_is_reported_and_never_listed(w):
    run(w)
    p = w["second"] / "unit-1" / "readings_unit-1_2026-10-01.csv.gz"
    p.write_bytes(p.read_bytes() + b"x")
    rc, conf, out = run(w)
    assert rc == 1 and any("second copy no longer matches" in o for o in out)
    listed = [ln.split()[1] for ln in conf.read_text().splitlines() if not ln.startswith("#")]
    assert listed == ["2026-10-15"]


def test_a_corrupt_download_is_rejected_and_not_kept(w):
    real = w["fetch"]

    def bad(path):
        data = real(path)
        return data[:-5] + b"xxxxx" if "/2026-10-15" in path else data
    out = []
    rc, conf = ax.archive(bad, w["dest"], w["second"], allow_same_disk=True, out=out.append, today=date(2026, 12, 1))
    assert rc == 1 and any("FAIL    unit-1 2026-10-15: SHA-256 differs" in o for o in out)
    assert not (w["dest"] / "unit-1" / "readings_unit-1_2026-10-15.csv.gz").exists()
    assert "2026-10-15" not in conf.read_text()


def test_row_count_and_header_are_checked_not_only_the_hash():
    data = gzip.compress(b"unit_id,boot_id\n1,2\n")
    import hashlib
    want = dict(sha256=hashlib.sha256(data).hexdigest(), size=len(data), rows=1)
    assert ax.check_export_bytes(data, want) == ["CSV header is not the export header"]
    good = gzip.compress((",".join(ax.EXPORT_HEADER) + "\n" + ",".join("x" * len(ax.EXPORT_HEADER)) + "\n").encode())
    want = dict(sha256=hashlib.sha256(good).hexdigest(), size=len(good), rows=2)
    assert ax.check_export_bytes(good, want) == ["1 rows != 2"]


def test_a_second_copy_is_required_and_must_be_a_separate_location(w):
    out = []
    assert ax.archive(w["fetch"], w["dest"], w["tmp"] / "nope", out=out.append)[0] == 2 and "not an existing" in out[0]
    w["dest"].mkdir(exist_ok=True)
    out.clear()
    assert ax.archive(w["fetch"], w["dest"], w["dest"], allow_same_disk=True, out=out.append)[0] == 2
    assert "separate location" in out[0]
    out.clear()                                             # tmp dirs share a disk: refused without the override
    assert ax.archive(w["fetch"], w["dest"], w["second"], out=out.append)[0] == 2 and "same disk" in out[0]


def test_a_wrong_token_stops_with_the_status_only(w):
    def fetch401(path):
        raise ax.FetchError(401)
    out = []
    assert ax.archive(fetch401, w["dest"], w["second"], allow_same_disk=True, out=out.append)[0] == 2
    assert out == ["manifest: HTTP 401 (401 = wrong token, 403 = exports disabled on the server)"]


def test_the_token_never_appears_in_any_output_or_file(w, capsys):
    rc, conf, out = run(w)
    delete_exports(w["store"], w["eng"], str(conf), 30, True, NOW, out.append)
    run(w)
    blob = "\n".join(out) + capsys.readouterr().out
    for p in list(w["dest"].rglob("*")) + list(w["second"].rglob("*")):
        if p.is_file():
            blob += p.read_bytes().decode("latin-1")
    assert TOKEN not in blob


def test_the_https_fetcher_reports_http_errors_by_status_only(monkeypatch):
    import urllib.error
    import urllib.request

    def boom(req, timeout=None):
        assert req.get_header("X-tremor-token") == TOKEN and TOKEN not in req.full_url
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(ax.FetchError) as e:
        ax.https_fetcher("https://example.invalid", TOKEN, pace_s=0)("/api/export/manifest")
    assert str(e.value) == "HTTP 401" and TOKEN not in str(e.value) and e.value.__cause__ is None


# --- iCloud Drive placeholders in the second copy -----------------------------------------------------------

def test_an_evicted_icloud_file_is_warned_about_by_name_not_hashed_and_not_listed(w, monkeypatch):
    run(w)
    evicted = w["second"] / "unit-1" / "readings_unit-1_2026-10-01.csv.gz"
    real = ax.icloud_placeholder
    monkeypatch.setattr(ax, "icloud_placeholder",
                        lambda p: "iCloud placeholder (evicted): not downloaded on this Mac" if Path(p) == evicted else real(p))
    hashed = []
    real_sha = ax.sha256_of
    monkeypatch.setattr(ax, "sha256_of", lambda p: hashed.append(Path(p)) or real_sha(p))
    rc, conf, out = run(w)
    assert rc == 1 and evicted not in hashed
    assert any(o.startswith("WARNING unit-1 2026-10-01: second copy is an iCloud placeholder (evicted)") and
               "Keep Downloaded" in o for o in out)
    assert [ln.split()[1] for ln in conf.read_text().splitlines() if not ln.startswith("#")] == ["2026-10-15"]


def test_an_old_style_icloud_stub_is_recognised_and_not_re_downloaded_over(w):
    run(w)
    p = w["second"] / "unit-1" / "readings_unit-1_2026-10-15.csv.gz"
    stub = p.with_name("." + p.name + ".icloud")
    p.rename(stub)                                         # what older macOS leaves behind for an evicted file
    assert ax.icloud_placeholder(p).startswith("iCloud placeholder (.icloud stub)")
    rc, conf, out = run(w)
    assert rc == 1 and any("2026-10-15: second copy is an iCloud placeholder (.icloud stub)" in o for o in out)
    assert not p.exists()                                  # not silently re-written beside the stub
    assert "2026-10-15" not in conf.read_text()


def test_a_dataless_flag_is_detected(tmp_path, monkeypatch):
    f = tmp_path / "x.csv.gz"
    f.write_bytes(b"1")
    assert ax.icloud_placeholder(f) is None
    import os as _os
    real = _os.stat

    class S:
        def __init__(self, st):
            self._st = st
        def __getattr__(self, k):
            return getattr(self._st, k)
        st_flags = ax.SF_DATALESS
    monkeypatch.setattr(ax.os, "stat", lambda p, *a, **k: S(real(p, *a, **k)))
    assert "evicted" in ax.icloud_placeholder(f)


# --- unattended runs: the token from the macOS Keychain (--token-keychain) ----------------------------------

def fake_security(calls, rc=0, stdout=TOKEN + "\n"):
    import subprocess

    def run(args, **kw):
        calls.append((list(args), kw))
        return subprocess.CompletedProcess(args, rc, stdout=stdout if rc == 0 else "", stderr="")
    return run


def test_keychain_token_is_read_from_security_stdout_into_memory_only():
    import subprocess
    calls = []
    assert ax.keychain_token("tremor-export-token", run=fake_security(calls)) == TOKEN
    (args, kw), = calls
    assert args == ["/usr/bin/security", "find-generic-password", "-s", "tremor-export-token", "-w"]
    assert kw["capture_output"] is True and kw["stdin"] is subprocess.DEVNULL      # captured, never printed
    assert "env" not in kw and TOKEN not in " ".join(args)


@pytest.mark.parametrize("rc,stdout,expect", [(44, "", "exit 44; 44 = no such item"),
                                              (36, "", "exit 36"), (0, "  \n", "is empty")])
def test_keychain_failures_raise_without_the_token(rc, stdout, expect):
    with pytest.raises(ax.TokenError) as e:
        ax.keychain_token("svc", run=fake_security([], rc=rc, stdout=stdout))
    assert expect in str(e.value) and TOKEN not in str(e.value)


def test_keychain_cannot_run_security_is_a_token_error():
    def run(args, **kw):
        raise FileNotFoundError(args[0])
    with pytest.raises(ax.TokenError, match="could not run /usr/bin/security"):
        ax.keychain_token("svc", run=run)


def test_main_with_token_keychain_never_prompts_and_never_leaks_the_token(w, monkeypatch, capsys):
    import os
    calls, seen = [], []
    monkeypatch.setattr(ax.subprocess, "run", fake_security(calls))
    monkeypatch.setattr(ax.getpass, "getpass", lambda *a, **k: pytest.fail("prompted in unattended mode"))
    monkeypatch.setattr(ax, "https_fetcher", lambda base, token: seen.append(token) or w["fetch"])
    argv = ["--token-keychain", "tremor-export-token", "--dest", str(w["dest"]), "--second-copy", str(w["second"]),
            "--allow-same-disk"]
    assert ax.main(argv) == 0
    assert seen == [TOKEN] and len(calls) == 1
    blob = capsys.readouterr().out + " ".join(argv) + " ".join(os.environ.values())
    for p in list(w["dest"].rglob("*")) + list(w["second"].rglob("*")):
        if p.is_file():
            blob += p.read_bytes().decode("latin-1")
    assert "archived this run: 3" in blob and TOKEN not in blob


def test_main_with_an_unreadable_keychain_item_stops_before_touching_anything(w, monkeypatch, capsys):
    monkeypatch.setattr(ax.subprocess, "run", fake_security([], rc=44))
    monkeypatch.setattr(ax.getpass, "getpass", lambda *a, **k: pytest.fail("prompted in unattended mode"))
    monkeypatch.setattr(ax, "https_fetcher", lambda *a: pytest.fail("fetched without a token"))
    rc = ax.main(["--token-keychain", "svc", "--dest", str(w["dest"]), "--second-copy", str(w["second"])])
    assert rc == 2 and not w["dest"].exists()
    assert "Keychain: no readable item for service 'svc' (security exit 44" in capsys.readouterr().out


# --- an unreadable / unwritable copy is a reported problem, never a crash -----------------------------------

def test_an_unreadable_second_copy_is_a_problem_line_not_a_crash_and_never_listed(w, monkeypatch):
    run(w)
    blocked = w["second"] / "unit-1" / "readings_unit-1_2026-10-01.csv.gz"
    real_sha = ax.sha256_of

    def sha(p):
        if Path(p) == blocked:                 # what macOS's network-volume privacy block looks like
            raise PermissionError(1, "Operation not permitted", str(p))
        return real_sha(p)
    monkeypatch.setattr(ax, "sha256_of", sha)
    calls_before = len(w["calls"])
    rc, conf, out = run(w)
    assert rc == 1
    assert f"PROBLEM unit-1 2026-10-01: second copy unreadable (Operation not permitted) ({blocked})" in out
    assert out[-2] == "archived this run: 0; verified in both copies: 2; problems: 1"     # the run completed
    assert [ln.split()[1] for ln in conf.read_text().splitlines() if not ln.startswith("#")] == ["2026-10-15"]
    assert w["calls"][calls_before:] == ["/api/export/manifest"]                        # nothing re-downloaded


def test_a_copy_that_cannot_even_be_looked_at_is_not_re_downloaded_over(w, monkeypatch):
    run(w)
    real = ax.icloud_placeholder

    def placeholder(p):
        if Path(p).is_relative_to(w["second"]):
            raise PermissionError(1, "Operation not permitted", str(p))
        return real(p)
    real_exists = Path.exists

    def exists(self):
        if self.is_relative_to(w["second"]):
            raise PermissionError(1, "Operation not permitted", str(self))
        return real_exists(self)
    monkeypatch.setattr(ax, "icloud_placeholder", placeholder)
    monkeypatch.setattr(Path, "exists", exists)
    calls_before = len(w["calls"])
    rc, conf, out = run(w)
    assert rc == 1 and conf is None and w["calls"][calls_before:] == ["/api/export/manifest"]
    assert sum("second copy unreadable (Operation not permitted)" in o for o in out) == 3


def test_a_failing_write_is_a_fail_line_and_the_day_is_not_recorded(w, monkeypatch):
    real = ax.write_verified

    def write(data, target, sha):
        if Path(target).is_relative_to(w["second"]) and "2026-10-15" in Path(target).name:
            raise PermissionError(1, "Operation not permitted", str(target))
        return real(data, target, sha)
    monkeypatch.setattr(ax, "write_verified", write)
    rc, conf, out = run(w)
    assert rc == 1 and "FAIL    unit-1 2026-10-15: local write failed (Operation not permitted)" in out
    assert ("unit-1", "2026-10-15") not in ax.load_local_manifest(w["dest"])
    assert "2026-10-15" not in conf.read_text()
