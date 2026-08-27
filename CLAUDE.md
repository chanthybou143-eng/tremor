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
.venv/bin/pytest tests/test_frequency.py -v                  # one file
.venv/bin/pytest tests/test_frequency.py::test_estimate_frequency_hand_built  # one test
.venv/bin/pytest -k "noise"                                  # by keyword
.venv/bin/tremor-dashboard                                   # live dashboard (synthetic feed)
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
