# analysis/

Offline comparisons of Unit 1 against AEMO and the sampling-mechanism simulation behind
`docs/FREQUENCY_OFFSET.md`. Not part of the `tremor` package or the firmware; not run by pytest.

- `fetch_unit1_nem.py` -- Unit 1 raw readings per NEM day (UTC+10) from the public `/api/history`.
- `compare_week.py` -- Unit 1 vs AEMO 4 s frequency (`fpp` = NEMweb FPP SA1, `weekly` = Weekly Frequency
  Report `.xlsb`), old vs new firmware, lag scan, chart.
- `mechanism_sim.py` -- perfect sine through models of the old soft-timer and new hard-timer sampling, using
  the real `chunk_summary` / `adc_chunker` code.

Run them from `~/tremor-analysis/`, where the (large, uncommitted) data lives: `unit1_raw/`, `aemo/`,
`aemo_fpp/`; outputs go to `out/` and PNGs next to it. `compare_week.py weekly` needs `pyxlsb`.
