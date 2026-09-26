# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

TREMOR is a GPS-synchronised grid-frequency monitoring network: five units
across Adelaide measure mains frequency and RoCoF (rate of change of
frequency) on the SA grid, GPS-timestamp disturbances, and use arrival-time
differences between units to locate disturbance sources — like a
seismometer network. The goal is estimating real SA grid inertia during
actual events.

Hardware per unit (not yet arrived — this repo is currently the offline
signal-processing core, developed against synthetic data): Raspberry Pi Pico
2WH running MicroPython, SparkFun MAX-M10S GNSS for PPS timing. Signal
chain: 9 V AC plugpack → 39k/2.2k divider (measured scale factor 18.80) → 1
µF film cap → mid-rail bias → 1 kΩ series + 1N4148 clamps → Pico ADC.

## Commands

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"   # first-time setup
.venv/bin/pytest -q                                          # run all tests
PYTHONPATH=src python scripts/replay_unit1.py --hours 2       # end-to-end: real Flask server + simulated Unit 1 traffic
python scripts/measure_db_latency.py --dir ~/tremor_data      # run ON PythonAnywhere: SQLite commit latency vs the device's 4 s deadline
.venv/bin/pytest tests/test_frequency.py -v                  # one file
.venv/bin/pytest tests/test_frequency.py::test_estimate_frequency_hand_built  # one test
.venv/bin/pytest -k "noise"                                  # by keyword
.venv/bin/tremor-dashboard                                   # single-unit live dashboard (synthetic feed)
.venv/bin/tremor-web-dashboard                                # multi-unit web dashboard (synthetic feed)
python3 freq_estimator.py                                     # standalone draft estimator's own validation harness
```

No lint/format tooling is configured yet.

## Architecture

Three modules under `src/tremor/`, split deliberately along a portability
boundary — the Pico will eventually run MicroPython, which has no `scipy`
and can't do non-causal (whole-buffer) filtering:

- **`signal.py`** (pure numpy) — `generate_mains_signal()` produces
  synthetic mains waveforms for testing. Frequency can be a constant offset
  or a callable `f(t) -> ndarray`, so ramps/steps can be modelled for future
  RoCoF work. Phase is obtained by integrating instantaneous frequency
  (trapezoidal cumulative sum), not `2*pi*f*t`, so the signal stays
  phase-continuous even when frequency varies over the buffer. Returns the
  ground-truth instantaneous frequency (`true_freq_hz`) alongside the
  samples, for validating estimators against.

- **`frequency.py`** (pure numpy, no scipy) — `estimate_frequency_zero_crossing()`
  is the batch estimator: rising zero-crossings are found and linearly
  interpolated, then frequency per cycle is `1 / diff(crossing_times)`,
  timestamped at the midpoint of each crossing pair. It takes samples
  exactly as given and has no notion of filtering or nominal frequency. It
  is sensitive to noise near a zero crossing (a single noisy sample can
  register a spurious crossing a fraction of a sample away from the real
  one, producing a wildly wrong instantaneous frequency for that one cycle)
  — offline this is handled by taking a median over a batch of estimates
  (see `tests/test_frequency.py`).

  `StreamingZeroCrossingDetector` is the causal, sample-at-a-time
  counterpart, used by live sources (`source.py`): it has no batch of future
  cycles to median over, so instead it rejects any rising crossing arriving
  less than `min_interval_s` (default 15 ms, well under a 50 Hz half-cycle)
  after the last *accepted* crossing. Both estimators are pure numpy/stdlib
  so they stay portable to MicroPython firmware later.

- **`filters.py`** — two conditioning options, chosen along the same
  portability line. `lowpass_filtfilt()` (scipy, imported lazily inside the
  function so importing the module never requires scipy) is a zero-phase
  Butterworth low-pass used offline before the batch estimator; it's
  explicitly non-causal (needs the whole buffer, forward+backward) and
  analysis-only. `SinglePoleLowPass` is the causal, sample-at-a-time on-device
  replacement — a first-order RC filter, pure Python arithmetic (`math`, no
  numpy/scipy) — used by live sources (`source.py`) before samples reach
  `StreamingZeroCrossingDetector`. It's weaker than the offline filter (6
  dB/octave vs. 4th-order) so don't expect it to match the offline filter's
  noise numbers exactly, just the same ballpark.

`scipy` is scoped as an optional dependency (`offline`/`dev` extras in
`pyproject.toml`), not a core dependency, precisely because `signal.py` and
`frequency.py` must stay importable without it.

### `freq_estimator.py` (repo root, not part of the `tremor` package)

A separate, self-contained draft: the same zero-crossing-with-hysteresis
idea as `frequency.py`/`filters.py`, but deliberately reimplemented with
zero dependencies (`math`/`random` only, no numpy) so it can be
copy-pasted directly into MicroPython firmware later with minimal changes.
It's a candidate for what actually ends up running on each Pico, whereas
`signal.py`/`frequency.py`/`filters.py` are the numpy-based *offline
analysis* toolkit. Not yet merged into `src/tremor/` — `units.py` (below)
imports it via a small `sys.path` shim rather than duplicating its logic;
if it's ever formalised into the package, that shim goes away.
`generate_synthetic_signal()` takes an optional `rng: random.Random`
instead of touching the global `random` module, specifically so multiple
instances can run concurrently (one per simulated unit) without racing on
shared global state.

### Live dashboard (`source.py`, `rocof.py`, `dashboard.py`)

`source.py` defines `FrequencySource`, the interface the dashboard programs
against (`stream() -> Iterator[FreqSample]`, plus optional `start()`/
`stop()`). `SyntheticFrequencySource` is the only implementation so far: a
background thread runs `signal.MainsSignalGenerator` (a stateful,
one-sample-at-a-time version of `generate_mains_signal` — needed because a
disturbance can be injected mid-stream, which a fixed-length batch buffer
can't represent) through `filters.SinglePoleLowPass` and then
`frequency.StreamingZeroCrossingDetector`, paced to real time via
`time.sleep`, pushing results onto a queue. `inject_step()` and
`inject_ramp()` mutate the frequency offset the generator is currently using
(a ramp is evaluated against wall-clock elapsed time each chunk, so it
progresses correctly regardless of how the dashboard is polling it). A real
serial feed from the Pico should implement `FrequencySource` the same way,
so `dashboard.py` doesn't change.

`rocof.py::rocof_from_window()` is a plain least-squares slope (Hz/s) over
a window of `(t, freq_hz)` points — used both for the dashboard's RoCoF
trace (a 500 ms trailing window, recomputed each time a new estimate
arrives) and intended as the starting point for offline inertia-estimation
work later.

`dashboard.py` runs a consumer thread that drains a `FrequencySource` into
rolling buffers (`_DashboardState`, lock-protected deques capped at the 60s
display window), and a matplotlib `FuncAnimation` on the main thread redraws
from a snapshot of those buffers every 150 ms. The frequency panel's y-axis
is intentionally a fixed `nominal ± 0.5 Hz` (not autoscaled) so small
deviations stay visually meaningful. The RoCoF panel discards the first
`ROCOF_STARTUP_DISCARD_S` (1 s) of estimates from its buffer — the
filter/detector haven't settled yet right after start, and that startup
transient would otherwise dominate the sliding-fit slope and poison the
panel's autoscale — and its y-axis (`_rocof_ylim()`) is clamped to
`ROCOF_Y_DEFAULT_HZ_S` (±2 Hz/s) by default, expanding only if the data
actually exceeds that. The big numeric readout shows a short rolling
median (`_readout_frequency()`, 1 s window) rather than the latest single
cycle, so it doesn't visibly jitter on per-cycle noise. Button widgets call
`inject_step`/`inject_ramp`/`reset` directly on the source, so they only
make sense for sources that expose them (they're hidden for a source that
isn't a `SyntheticFrequencySource`).

### Multi-unit web dashboard (`units.py`, `webapp.py`)

> **Superseded in part by "Server persistence" below:** real (ingested) units are no longer kept in
> `_UnitsState`'s receipt-time-stamped, 60 s in-memory buffers. They are stored permanently in SQLite under
> the device's own GPS UTC time, and `/api/units` reads its window from the database. `_UnitsState` and
> the paragraphs about receipt-time stamping / batch ids below still describe the *synthetic* feeds.

A second, separate live view from the single-unit matplotlib dashboard
above: a local Flask web app showing frequency + RoCoF for each of the 5
planned units (only 2-3 simulated today; the rest render as "no data yet"
placeholders — that's the exact swap-in point for real hardware later).

`units.py` defines `UnitFeed` (mirrors `source.FrequencySource`, but
tagged with a `unit_id`/`label`). `SyntheticUnitFeed` is the only
implementation: a background thread repeatedly generates a ~1s chunk via
`freq_estimator.generate_synthetic_signal` and runs it through
`freq_estimator.estimate_frequency` (hysteresis + moving-average, not
`tremor.frequency`'s pipeline — deliberately reusing the same draft that's
closer to what will actually run on hardware), pushing the per-cycle
readings onto a queue paced to real time. Every simulated unit measures
the same shared `true_grid_freq_hz(t)` (currently a flat 50 Hz) — the hook
for a future shared disturbance, since arrival-time comparison across
units is the whole point of the network — differing only in independent
noise and a small per-unit DC-offset (calibration error), not in the
underlying frequency they're measuring.

`webapp.py`'s `_UnitsState` is the same lock-protected rolling-buffer
pattern as `dashboard.py`'s `_DashboardState`, keyed per unit, computing
RoCoF via `rocof.rocof_from_window()` over a 2s trailing window (longer
than the single-unit dashboard's 500ms, since this view polls at a much
coarser ~1s cadence). The displayed frequency is a short rolling median
(`READOUT_WINDOW_S`), not the latest raw single-cycle reading, for the
same jitter reason as `dashboard.py`'s `_readout_frequency()`. The sparkline
history sent to the frontend is smoothed the same way (`_smoothed_history()`,
a per-point rolling median over `SPARKLINE_SMOOTHING_WINDOW_S`) -- the raw
per-cycle series genuinely does swing +/-0.15Hz cycle to cycle, which looks
far jumpier plotted at full resolution than the underlying frequency
actually is. The frontend
(`templates/index.html`) is a single self-contained page — inline CSS/JS,
canvas sparklines, no build step, no charting library — that polls
`GET /api/units` every second; `create_app()` takes `simulated_units`/
`unit_slots` so tests can spin up an app with zero or one feed instead of
all three.

### Server persistence (`ingest.py`, `store.py`, `timeline.py`, `retention.py`)

Every ingested reading is stored permanently under its **own GPS UTC time**, never receipt time. Receipt time
is metadata (`received_at`); the only place the server uses it is to pick which UTC day a legacy
seconds-of-day value belongs to. A reading with no usable GPS time is stored with `gps_utc_us` NULL and a flag
(`flags & 1` unlocked, `& 2` implausible time) and is never plotted or given a time.

- **`ingest.py`** parses two payload generations. **v1 (Unit 1 today):** `gps_utc_s` = UTC seconds-of-day as a
  float32 (~4-8 ms, no date). **v2 (client prepared, not yet flashed):** batch-level `boot_id`, per-reading `seq`,
  and integer `gps: [days_since_1970, second_of_day, microsecond]` built on the device with integer-only
  arithmetic (`pps_time_sync.PPSTimeSync.ticks_to_gps`), so it is exact and carries the device's own date. The
  old float `gps_utc_s` is **not** sent in v2 by default (`SEND_LEGACY_FLOAT = False` in `wifi_unit_client.py`;
  flip it to `True` at flash time only if a rolled-back pre-v2 server must keep reading times). Measured for a
  60-reading POST body: v1 5,855 B, v2 6,776 B (+16%), v2 with the float 8,396 B (+43%). A time more than 1 h old
  or 5 s in the future relative to receipt is flagged implausible, never trusted.
- **`store.py`** is a small `ReadingStore` interface with one implementation, `SqliteReadingStore` (stdlib
  sqlite3, default rollback journal -- *not* WAL, PythonAnywhere's disk is NFS; one short-lived connection per
  call). Dedupe is `INSERT OR IGNORE` against partial unique indexes: v2 on `(unit_id, boot_id, seq)`, legacy on
  `(unit_id, gps_utc_us)`. Legacy *unlocked* readings cannot be deduplicated (no key) -- stored and flagged on
  purpose, not matched by content. A storage failure is a `StoreError` -> HTTP 503, which is safe because the
  device keeps its readings and ingest is idempotent.
- **`timeline.py`** computes RoCoF over GPS-ordered points (one implementation shared by the live view, the
  aggregates and event detection); it never bridges a `boot_id` change or a gap over 1.5 s.
- **`retention.py`** keeps the disk bounded (free tier: 512 MB, ~12 MB/day/unit): raw rows older than
  `TREMOR_RAW_DAYS` (14) are pruned **only after** the day is exported to gzip CSV, aggregated to 1-minute rows
  (mean/min/max/std of freq, max |RoCoF|, locked/unlocked counts), and verified (export == database ==
  aggregates, checksum intact). Raw rows within +/-5 min of |RoCoF| > 0.1 Hz/s or freq outside 49.85-50.15 Hz
  are kept permanently (`events`). A day that fails verification is marked `attention` and is never pruned. It
  runs opportunistically from the ingest path in small resumable chunks (no scheduler needed) and via
  `python -m tremor.retention`.
- **API:** `/api/units` (window from the DB; adds `gps_utc`, `unlocked_count`, `duplicates_ignored`),
  `/api/history?unit=&from=&to=&limit=` (hard limit 10,000; `resolution=raw|1min|auto`), `/api/health` (DB size,
  quota use with a warning at 80%, days needing attention), `/api/export/<unit>/<YYYY-MM-DD>`.
- **Config (env):** `TREMOR_DB_PATH`, `TREMOR_QUOTA_MB` (512), `TREMOR_QUOTA_ROOT`, `TREMOR_RAW_DAYS`,
  `TREMOR_EXPORT_DIR`, `TREMOR_EVENT_ROCOF_HZ_S`, `TREMOR_EVENT_FREQ_LO/HI`, `TREMOR_SQLITE_SYNCHRONOUS` (FULL), plus the
  access-control settings below.
- **Running the tests:** the shared `.venv`'s editable install can point at another checkout; use
  `PYTHONPATH=src pytest` so the code under test is this tree. Tests that need "now" use `tests/helpers.py`'s
  `FakeClock` -- never the real clock (an earlier ingest test only passed near 11:23 UTC).

### Access control (`security.py`)

`/api/ingest` used to accept unauthenticated POSTs. It now supports a per-unit token in the `X-Tremor-Token`
header; tokens live only in the environment (the PythonAnywhere WSGI file) and, on the Pico, in the gitignored
`wifi_config.py` -- never in the repo (`tests/test_security.py` checks this).

- **`TREMOR_INGEST_AUTH`:** `off` (default when no tokens are set) / `optional` (a missing token is accepted, logged
  once per 10 min per unit and counted; a *wrong* token is rejected with 401) / `required`. Legacy Unit 1 cannot
  send a token, so the rollout is `optional` now and `required` after the reflash; `/api/health` →
  `ingest_auth` (`missing_accepted`, `units_seen_without_token`) shows when it is safe to flip.
- **`TREMOR_INGEST_TOKENS`:** `unit-1=<token>,unit-2=<token>` (each >= 16 chars). A 401 never says whether the
  token was missing or wrong; comparison is constant-time; misconfiguration raises at startup.
- **`/api/export`:** disabled unless `TREMOR_EXPORT_TOKEN` is set; header-only (never in the URL); rate limited
  (`TREMOR_EXPORT_RATE`, default 10/60 s per client). `/api/history` and `/api/health` stay public;
  `/api/history` is rate limited per client (`TREMOR_HISTORY_RATE`, default 30/60 s). Behind a proxy set
  `TREMOR_CLIENT_IP_HEADER` (e.g. `X-Real-IP`); `/api/health` shows `your_address_as_seen` so you can check.
- Request bodies over 512 KB are refused before parsing.
- **Client:** `INGEST_TOKEN` in `wifi_config.py` (optional) is sent by `wifi_unit_client.py` via
  `timeout_post(extra_headers=...)` and never printed.

### Standalone Unit 1 (flashed 2026-09-26; `main.py`, `boot_support.py`, `wdt_support.py`, `RECOVERY.md`)

The Pico now runs by itself at power-up (no Mac). Flash holds: `main.py`, `boot_support.py`, `wdt_support.py`,
`wifi_unit_client.py`, `http_client.py`, `pps_time_sync.py`, `nmea_parser.py`, `wifi_ingest.py`,
`chunk_summary.py`, `freq_estimator.py`, `wifi_config.py` (copied from the local gitignored file; holds the
Wi-Fi password and `INGEST_TOKEN`), and the leftover unused `http_keepalive.py`. Before that, the flash held
only the 8 files in `~/tremor-flash-backup-20260926/` (the rollback point; see `RECOVERY.md`).

* **Boot order (`main.py`).** (1) Escape hatch: GP22 (physical pin 29, internal pull-up, 8 low reads over ~40 ms)
  jumpered to GND (pin 28, next to it -- **never pin 30, which is RUN**) -> print a message, set the LED steady on,
  arm no watchdog, start no client, return to the REPL. This check uses only `machine`/`time`, before anything
  else is imported. (2) `wifi_config.py` is imported and its required fields validated BEFORE the watchdog is
  armed (`boot_support.REQUIRED_FIELDS`: `WIFI_SSID WIFI_PASSWORD UNIT_ID INGEST_URL INGEST_TOKEN`, non-empty
  text, `INGEST_URL` http(s)); a failure prints only the exception type, `file:line` and field NAMES, never a
  value or a message, then fast-blinks and returns to the REPL. (3) Otherwise `import wifi_unit_client` (arms
  the watchdog, runs forever); a crash or exit prints type + locations only and forces a watchdog reboot, so the
  unit keeps retrying. Any change to how `wifi_config.py` is checked must keep the "never print a value" rule
  (a traceback message can echo the file: on 2026-09-26 an `ast.parse` traceback leaked the token once).
* **LED (onboard):** steady on = maintenance (jumper); slow blink 1 Hz = running; fast blink 5 Hz = config
  error; off = not started. One timer, allocation-free callback; verified to keep blinking through an 8 s
  stalled TLS handshake. It says the timer is alive, not that the main loop is healthy (the watchdog does that).
* **Consecutive WDT resets** are counted in WATCHDOG SCRATCH3 (`wdt_support.ResetCounter`: magic | armed flag |
  count) and printed at boot as `# BOOT_COUNTER consecutive_wdt_resets=N` together with `# PREV_FREEZE`. The
  armed flag (set just before the client starts, cleared when `main.py` returns without arming) is what tells
  a real watchdog reset from a soft reset, because `machine.reset_cause()` keeps saying WDT_RESET across soft
  resets. The client zeroes the count after its first successful POST. It never stops the unit.
* **`mem32` reads are SIGNED on this build** (`0xC0DE8000` reads back as `-0x3F218000`): mask with
  `& 0xFFFFFFFF` before comparing (the counter never counted until this was found on the device).
* **Killing `mpremote` does not stop the client**, and Ctrl-C leaves the watchdog armed (the board reboots
  ~8 s later): use the jumper for any maintenance session. `mpremote run/mount` tests of the client from RAM
  work as before (`mpremote mount DIR run launcher.py`, with real copies, not symlinks, in DIR).
* **RP2350 caveat found while testing:** an input with the internal PULL-DOWN can stay latched high on this chip
  (erratum RP2350-E9), so pull-down readings are unreliable; the escape hatch uses the pull-up only.
* **Verification done (2026-09-26):** (a) power-cycle on Mac USB started and posted by itself; (b) jumper
  pin 29 -> pin 28 + power-cycle: LED steady on, REPL, uptime kept counting for 98 s with reset cause PWRON
  (no watchdog); (c) jumper removed + power-cycle: ran again; (d) on a USB phone charger with no Mac it posted,
  authenticated, with a new `boot_id` and `time_src` 2. One unexplained PWRON reset (~04:37 UTC, 90 s after the
  first power-up, on the Mac USB port) was seen; a cable/port glitch is the suspect. If unplanned PWRON
  boot_ids keep appearing, suspect the supply or cable, not the firmware (a watchdog reset reads WDT_RESET).

### Overnight freeze of 2026-09-25 15:04 UTC (root cause and fixes)

The soak log showed a WDT reset ~8 s into a POST with no STATUS line and 355 KB heap free. Reproduced on the
device from RAM:

* `ssl.wrap_socket`'s handshake honours `sock.settimeout(4)` **per socket operation**, not for the whole
  handshake: with every individual wait at 3.6 s it took 8.18 s, longer than the 8 s watchdog. A silent peer is
  cut at ~4.0 s (that path is fine).
* `socket.getaddrinfo` has **no timeout** (6.5-7 s against a dead DNS server; 27 ms healthy, 0 ms if lwIP has
  it cached). The DNS stage also had no watchdog feed before it.
* Timer callbacks (soft and hard) DO run while the main thread is inside such a C call.

Fixes (all in `http_client.py`, `wdt_support.py`, `wifi_unit_client.py`):

* **Stage log** at the START of every POST stage, before its blocking call:
  `# POST_STAGE stage=dns|connect|tls_handshake|send|read_response|done t_ms=... [cache|lookup|stale]` -- the last
  line before any silence names the frozen stage. The stage is also kept in WATCHDOG SCRATCH0-2
  (`Breadcrumb`), so the next boot prints `# PREV_FREEZE last_stage=... post_no=...` even with no USB host.
* **DNS cache** (`DnsCache`): resolve once, reuse; max age 1 h, stale-if-error up to 24 h; dropped on a
  connect/TLS failure, a non-2xx reply, or every 3rd consecutive failure. If the server IP changes, the old
  address stops answering, so 1-2 POSTs fail (readings stay buffered), the cache is dropped and the next POST
  re-resolves and succeeds. SNI always uses the real hostname.
* **Bounded watchdog guard** (`WatchdogGuard`, `POST_WDT_GUARD_MS = 25000`, 0 disables): a 1 s Timer feeds the
  watchdog only inside a POST window, so a slow stall costs some overflowed ADC samples instead of a reboot that
  discards the RAM buffer (up to ~10 min of readings). The 25 s cap is enforced by the timer itself, not by
  the POST code: with no `stop()` and no main-thread feeds it expired at 25.0 s and the watchdog then reset
  the board (verified on the device). Every stall the bare 8 s watchdog would not have survived is logged as
  `# WDT_GUARD_EXTENDED stage=... stalled_ms=...`; the cap expiring logs `# WDT_GUARD_EXPIRED`. STATUS carries
  `dns_*`, `guard_windows/feeds/expired/ext/longest_stall_ms`.

### PPS interval filter and anchor recovery (`pps_time_sync.py`)

Plugpack switching puts glitch edges on the PPS line (~1-1.7 extra edges per second while the plugpack is
unplugged, measured exactly on the device).

* **Hard IRQ** (`Pin.irq(..., hard=True)`): a soft handler does not run while the main thread is inside a
  blocking network call, so every ~2.3 s POST used to cost about one late/lost edge (40 resyncs and 58 rejected
  anchors in 30 min). With hard=True: 0 lost edges. `_on_pps` is integer-only and allocation-free (an AST test
  enforces it).
* **Edge filter:** an edge is accepted only if it arrives within +/-50 ms (`PPS_TOLERANCE_US`) of k x 1 s,
  k = 1..5, after the last ACCEPTED edge (k > 1 = a missed edge, counted as a resync); after 6 s with nothing
  accepted the next edge re-anchors unconditionally. Only accepted edges become the pending edge that pairs
  with an RMC sentence. Known limitation (tested, documented): a glitch landing within 50 ms BEFORE a true edge
  is accepted and the true edge rejected; the timing error is bounded by the tolerance and self-corrects at
  the next edge. Plugpack test (3 unplug/replug cycles, 303 s): 348 edges, 304 accepted (one per second),
  44 glitches rejected, 0 resyncs, 0 anchors lost to glitches.
* **Anchor recovery:** the 250 ms sanity check compares each candidate anchor with the CURRENT anchor, so a
  wrong first anchor (an RMC read after the NEXT edge, e.g. after the boot-time Wi-Fi connect) made every later
  correct candidate look wrong and the unit ran ~1 s off until reboot (seen on the device). Now 5 rejected
  candidates in a row (`PPS_REANCHOR_STREAK`) that agree with each other replace the anchor, logged as
  `# REANCHOR old_utc=... new_utc=... correction_us=...` (both UTC at the new edge; < 0 = old anchor was fast).
* **Blocked-window shadow:** while the main loop is blocked (a POST, the boot-time connect) the GPS UART goes
  unread and the first sentence read afterwards is stale, yet gets paired with the newest edge; same-length
  slow POSTs (a long outage) make such mispairs agree with each other. `blocking_started()/blocking_ended()`
  (called by the client around every POST and once before the main loop) mark that; a candidate read within 2 s
  (`PPS_BLOCK_SHADOW_US`) of the end of a blocking window is ignored (neither extends nor resets the streak),
  and cannot be a FIRST anchor either. Tested with a worst-case outage (same-size stale mispairs, no good
  anchor in between) with and without the guard.
* STATUS carries `pps_edges/accepted/rejected/resync`, `sync_count`, `sync_rejected`, `no_edge`, `reanchors`,
  `sync_shadow`.

### Open items

* **TLS certificate verification is off** (no CA on the Pico), so a man-in-the-middle could read the ingest
  token. Accepted for now. To do: estimate the heap cost (and handshake time) of verifying the server
  certificate with the CA root on this MicroPython build (v1.27.0, `ssl` with `cadata`/`CERT_REQUIRED`) before
  deciding.
* **NMEA pairing losses during POSTs** (left as is): while a POST blocks the loop the UART goes unread, so
  ~1 anchor per POST is lost (the 250 ms sanity check rejects it; none is accepted wrongly). Possible fix: ignore
  sentences whose paired edge is > ~0.9 s old (note: this does NOT catch a sentence that is merely late, which is
  what the blocked-window shadow is for).
* Optional: precompile the client with `mpy-cross` (boot compiles ~52 KB of source each time).
* `http_keepalive.py` is on the flash but unused; leave it or remove it in a maintenance session.

### Switching the server to `TREMOR_INGEST_AUTH=required`

Do it only when all hold after >= 24 h of standalone running: `ingest_auth.missing_accepted` has not risen
since the standalone unit's first POST, `rejected_wrong` is 0, `units_seen_without_token` is empty, and
`authenticated` keeps rising at ~2 per minute. Roll back by setting `optional` again and reloading.

### Known-bad data (exclude from analysis)

* **boot_id `398474c3bef237a1`, unit-1, 289 rows** (RAM test run of 2026-09-26 ~04:00 UTC, the first plugpack
  toggle test): every timestamp is ~1 s EARLY. Cause: the anchor-lockout bug (the first PPS anchor was paired
  with an RMC sentence a whole second off because the main loop had not been reading the GPS UART, and every
  later correct candidate was then rejected against it; fixed in 4361f64, hardened by the blocked-window
  shadow after it). Evidence: the server's receive lag for that boot is 3.83 s median vs 2.96 / 2.88 s for the
  two good boots of the same session. Exclude it when analysing (`WHERE boot_id != '398474c3bef237a1'`). The
  rows are still on the server; delete them with the guarded one-off `scripts/delete_boot_rows.py` (dry run by
  default; needs `--expect-rows` to match exactly and the boot id typed again; saves the rows to a quarantine
  file first; see its docstring). Its dry run reports the exact row count (289 is what `/api/history` showed).
* Other 2026-09-26 RAM-run boots on unit-1 (and the earlier legacy soak) are test data too. Boots
  `87e9675a612ac091`, `c7181effaa38c678`, `5887ce55578f0a22` and `7bc83d47bbd0d653` had clean sync counters
  (anchors good; the lag check above agrees for the first two). Short throw-away boots from the reset-counter
  experiments (e.g. `b6b28fd1b272156d`) were not checked individually.

### Test tolerances are tied to real hardware constraints

Tests in `tests/test_frequency.py` and `tests/test_filters.py` are
parametrized over `sample_rate_hz` in `{4000, 10000}` — realistic rates for
the Pico's ADC under MicroPython — rather than arbitrary high offline-DSP
rates, so passing tolerances actually mean something for the target
hardware. `test_frequency.py::test_recovers_offset_with_noise` asserts on
`np.median(result.freq_hz)`, not the mean, on the unfiltered signal —
because rare zero-crossing glitches from noise blow up the mean but not the
median; the filtered pipeline (`test_filters.py`) uses the mean against a
tighter tolerance, since that's the intended production path (filter, then
estimate).
