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
  arithmetic (`pps_time_sync.PPSTimeSync.ticks_to_gps`), so it is exact and carries the device's own date. v2
  still includes the float `gps_utc_s` so an older server keeps working. A time more than 1 h old or 5 s in the
  future relative to receipt is flagged implausible, never trusted.
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
  `TREMOR_EXPORT_DIR`, `TREMOR_EVENT_ROCOF_HZ_S`, `TREMOR_EVENT_FREQ_LO/HI`, `TREMOR_SQLITE_SYNCHRONOUS` (FULL).
- **Running the tests:** the shared `.venv`'s editable install can point at another checkout; use
  `PYTHONPATH=src pytest` so the code under test is this tree. Tests that need "now" use `tests/helpers.py`'s
  `FakeClock` -- never the real clock (an earlier ingest test only passed near 11:23 UTC).

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
