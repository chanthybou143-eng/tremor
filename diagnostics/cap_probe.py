"""Temporary diagnostic: measure the isolated coupling cap's real
capacitance via repeated RC charge transients through a known series
resistor, since the multimeter has no capacitance range. Not part of the
tremor signal-processing pipeline -- this file and its temporary wiring go
away once the cap's real value is confirmed.

Wiring (cap has one leg lifted out of the main circuit for this test,
same isolation a multimeter capacitance test would have needed anyway):

    GPIO14 --[100kOhm resistor]--+-- one leg of the lifted cap
                                  |
                                 GP28 (ADC2)
                                  |
                            other leg of the cap -- GND

GPIO14 drives the charge/discharge; GP28 reads the R/C junction, i.e.
directly across the capacitor under test. GP27 (used for the earlier
bias-node probe) and GP26 (the main signal path) are untouched.

Why R=100kOhm: tau=R*C lands at ~100ms for a true 1uF cap and ~1ms if it's
actually behaving like 10nF -- both far longer than a single buffered ADC
read (a few us), so the same resistor resolves either case without
retuning. DISCHARGE_MS and CHARGE_CAPTURE_MS give >=5x margin against the
slower (1uF) case; if a run comes back with the buffer still visibly
rising at the end of the window, tau is longer than expected and both
constants need increasing before trusting the fit.

Each cycle's samples are captured into a preallocated RAM buffer with no
print() inside the timing loop -- same reasoning as adc_stream_gps.py's
ring buffer: print()'s per-call overhead would otherwise cap the
achievable sample rate well below what's needed to resolve a transient if
the cap turns out much smaller than the documented 1uF. Each cycle's
buffer is printed in one batched call afterward, outside the timed
section, so it can't distort the measurement.

Host side fits V(t) = V_inf*(1 - exp(-t/tau)) to each cycle's curve (see
the analysis run alongside this capture) and averages tau across cycles
for noise immunity -- more robust than timing a single
63%-of-final-value threshold crossing, which (like this repo's
zero-crossing frequency estimator) is sensitive to noise right at the
threshold.

Usage (mpremote, from the repo root):
    python3 -m mpremote connect /dev/cu.usbmodem101 run diagnostics/cap_probe.py
"""

from machine import Pin, ADC
import array
import time

DRIVE = Pin(14, Pin.OUT)
adc = ADC(28)  # ADC2 / GP28 -- taps the R/C junction, across the cap under test

N_CYCLES = 20
DISCHARGE_MS = 800       # >=5x the ~100ms tau expected for a true 1uF cap at R=100k
CHARGE_CAPTURE_MS = 600  # >=5x that same tau, so a true 1uF cap fully settles in-window
BUF_LEN = 20000          # a first run showed the bare polling loop (no print()
                         # inside it) comfortably exceeds 10kHz -- sized so the
                         # 600ms time budget, not this buffer, is what ends a
                         # cycle, regardless of how fast the loop actually runs

ticks_buf = array.array("L", [0] * BUF_LEN)
raw_buf = array.array("H", [0] * BUF_LEN)

DRIVE.value(0)
for cycle in range(N_CYCLES):
    DRIVE.value(0)
    time.sleep_ms(DISCHARGE_MS)  # actively pulls the cap toward 0V through the 100k

    n = 0
    t0 = time.ticks_us()
    DRIVE.value(1)  # charge starts now
    while n < BUF_LEN and time.ticks_diff(time.ticks_us(), t0) < CHARGE_CAPTURE_MS * 1000:
        ticks_buf[n] = time.ticks_diff(time.ticks_us(), t0)
        raw_buf[n] = adc.read_u16()
        n += 1

    print("# CYCLE " + str(cycle) + " n=" + str(n))
    PRINT_BATCH = 128  # see adc_stream_gps.py: batching print() calls avoids both
                        # per-call print() overhead and (learned the hard way here)
                        # a MemoryError from joining thousands of lines into one string
    for start in range(0, n, PRINT_BATCH):
        lines = [str(ticks_buf[i]) + "," + str(raw_buf[i])
                  for i in range(start, min(start + PRINT_BATCH, n))]
        print("\n".join(lines))

DRIVE.value(0)
print("# DONE")
