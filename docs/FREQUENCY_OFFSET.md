# Unit 1's frequency offset vs AEMO: cause and fix (closed 2026-10-14)

## Conclusion

Under the old firmware Unit 1 read **about 1.3 mHz low** relative to AEMO (−1.6 to −2.0 mHz overall).
The cause was **defects in how the ADC was sampled, not the crystal and not the estimator's maths**:

1. **Soft-timer sampling defects.** The ADC timer was a *soft* `machine.Timer`. Its callback is queued
   (`mp_sched_schedule`, scheduler depth 8 on rp2) and runs only between bytecodes. While the main
   thread sat in a long C call (TLS crypto in a POST, `gc.collect`, `json.dumps`), alarms queued up to 8
   deep and the rest were **dropped**. When the VM resumed, the queued callbacks ran **late and bunched**,
   each stamping and sampling at run time. Chunks spanning such holes and bunches read low.
2. **ADC ring overflow during long POSTs.** With a hard timer the samples keep coming during a POST. The
   old 4 s ring overflowed on every POST (plus its pre-POST work) longer than ~4 s, leaving gaps that bias
   chunks the same way.

**Fixed in firmware `2dc6afb`** (flashed 8 Oct 2026): a hard ADC timer (on-time stamps, no drops) and a
12 s ring. Afterwards Unit 1 reads −0.3 mHz (FPP SA1) / −0.56 mHz (weekly report). Coverage went from
~90 % to 100 %, and scatter improved slightly.

Not a cause: the batch-close chunking (chunks up to 1.124 s long) only made readings longer. It added
no bias, in simulation or in the field. The crystal had already been ruled out (3–5 ppm slow ≈ +0.2 mHz).

## Evidence (all four agree)

**1. Field comparison, two independent AEMO references** (`analysis/compare_week.py`). Unit 1 is
time-weighted onto each AEMO 4 s interval by GPS time; ≥ 90 % coverage. Lag, std and r are at the
best-fit lag.

| Reference | Period | Firmware | Offset (mHz) | Std (mHz) | r | Coverage |
|---|---|---|---|---|---|---|
| Weekly Frequency Report (mainland, NSW) | 28 Sep–5 Oct | old | −2.00 | 2.62 | 0.9925 | 97.6 % |
| Weekly Frequency Report | 5–8 Oct | old | −1.83 | 2.42 | 0.9934 | 90.1 % |
| Weekly Frequency Report | 8–12 Oct | **2dc6afb** | **−0.56** | 2.16 | 0.9951 | 100 % |
| FPP SA1 (NEMweb) | 28 Sep–5 Oct | old | −1.74 | 3.02 | 0.9901 | — |
| FPP SA1 | 5–8 Oct | old | −1.58 | 3.00 | 0.9900 | 90.1 % |
| FPP SA1 | 8–12 Oct | **2dc6afb** | **−0.29** | 2.84 | 0.9919 | 100 % |

Step at the flash: **+1.27 mHz** (weekly report) and **+1.29 mHz** (FPP). Every old-firmware day lies
between −1.3 and −2.2 mHz and every new-firmware day between −0.1 and −0.75 mHz, with no overlap. FPP's
lag is flat at 4.7–4.9 s throughout (the AEMO-side lag found in September), so the timing is unchanged
(+0.07 s, within noise). The weekly report's lag moved by ~0.6 s on 2 Oct with no Unit 1 change, so it
is not usable for sub-second timing.

**2. Mechanism simulation** (`analysis/mechanism_sim.py`). A perfect 50.000 Hz sine goes through the
real `chunk_summary.summarize_chunk` and the real `adc_chunker.ChunkBuilder`. The old path is modelled
from the v1.27 rp2 source (alarm every 971 µs, scheduler depth 8, drops and late back-to-back callbacks
during stalls, 128-sample batch close). 30 simulated minutes per scenario:
- new path: −0.15 ± 0.02 mHz (the estimator itself, see open item);
- old path with POST stalls only: −0.2 to −0.6 mHz;
- old path with GC-like pauses of 10–20 ms every ~1 s: **−1.2 to −1.3 mHz** (1.1–1.2 mHz below the new path);
- decomposition: old timer + new chunking −1.40; new timer + old chunking −0.16. The bias is all sampling.

**3. Bench runs on the real Pico, 8 Oct 2026, vs FPP SA1** (30 min each, ±0.3 mHz):

| Run (boot) | Firmware | Offset |
|---|---|---|
| A/B soft timer (`00eaaddc5a9d13dd`) | 1ec6045, soft | −1.34 mHz |
| A/B hard timer (`23eea7c98e340016`) | 1ec6045, hard, 4 s ring | −0.67 mHz |
| Outage run (`7e54dfef72b7ec2b`) | 1ec6045, hard, 4 s ring | −0.74 mHz |
| Confirmation run (`a5cd48446f7198f4`) | **2dc6afb** | −0.06 mHz |

The total of 1.28 mHz matches the field step. About half came from the hard timer, and about half from
the 12 s ring plus per-sample chunk close (with a hard timer and the 4 s ring, POSTs over ~4 s left
3.6–7 s of overflow gaps per 30 min).

## Reproducing

The data stays outside the repo, in `~/tremor-analysis/`: `unit1_raw/` (Unit 1 raw readings, fetched
before retention pruned them), `aemo/` (weekly report `.xlsb` files), `aemo_fpp/` (NEMweb FPPRUN zips).

```bash
cd ~/tremor-analysis
python3 ~/tremor-merge/analysis/fetch_unit1_nem.py 2026-10-05 2026-10-06 ...    # NEM days, public /api/history
python3 ~/tremor-merge/analysis/compare_week.py fpp    2026-10-05 2026-10-13   # needs aemo_fpp/
python3 ~/tremor-merge/analysis/compare_week.py weekly 2026-10-05 2026-10-12   # needs aemo/*.xlsb + pyxlsb
python3 ~/tremor-merge/analysis/mechanism_sim.py 30                             # ~20 min
```

## Open item (low priority)

- **The estimator reads −0.15 mHz on a perfect 50.000 Hz sine** (`mechanism_sim.py`, new path:
  −0.15 ± 0.02 mHz), so it accounts for part of the remaining −0.3 / −0.56 mHz against AEMO. Probably the
  boxcar pre-filter or crossing interpolation; worth a look before absolute-accuracy work, not urgent.
