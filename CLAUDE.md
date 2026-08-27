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
  is the estimator: rising zero-crossings are found and linearly
  interpolated, then frequency per cycle is `1 / diff(crossing_times)`,
  timestamped at the midpoint of each crossing pair. This module takes
  samples exactly as given and has no notion of filtering or nominal
  frequency — it's intentionally kept small enough to eventually port to
  MicroPython firmware. It is sensitive to noise near a zero crossing (a
  single noisy sample can register a spurious crossing a fraction of a
  sample away from the real one, producing a wildly wrong instantaneous
  frequency for that one cycle) — that's why filtering lives elsewhere.

- **`filters.py`** (scipy, offline-only) — `lowpass_filtfilt()` is a
  zero-phase Butterworth low-pass (`scipy.signal.butter` + `filtfilt`) used
  to condition a buffer before handing it to the estimator. It's explicitly
  non-causal (needs the whole buffer, forward+backward) and documented as
  analysis-only. When firmware work starts, the on-device replacement will
  be a causal single-pole IIR — that substitution should not require
  touching `frequency.py`.

`scipy` is scoped as an optional dependency (`offline`/`dev` extras in
`pyproject.toml`), not a core dependency, precisely because `signal.py` and
`frequency.py` must stay importable without it.

### On-device crossing detection (future work)

When `frequency.py`'s crossing detection is ported to run live on the Pico,
it needs a minimum-interval guard: reject any rising crossing that arrives
less than ~15 ms after the previous accepted one (15 ms is well under a 50
Hz half-cycle, so it only rejects spurious crossings, not real ones). This
is the causal, streaming counterpart to the median-based noise robustness
used offline — on-device there's no buffer of future cycles to take a
median over, so a noise-induced spurious crossing has to be rejected right
at detection time instead of filtered out afterward.

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
