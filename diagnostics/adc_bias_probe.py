"""Temporary diagnostic: sample the mid-rail bias node through a second,
otherwise-unused ADC channel, to substitute for a multimeter whose lowest
AC range (20V) can't resolve a signal in the tens-to-hundreds-of-mV range.

Not part of the tremor signal-processing pipeline and not meant to be kept
long-term -- once the bias-node loss question (raise the mid-rail bias
resistors vs. chase the coupling cap) is settled, this file and its jumper
wire can both go away.

Wiring (temporary, does not disturb the existing GP26/ADC0 signal path):
    Run a jumper from the bias node -- the junction after the 1uF coupling
    cap, before the 1kOhm series resistor and 1N4148 clamps -- to GP27
    (ADC1, physical pin 32 on the standard 40-pin Pico/Pico 2 pinout,
    directly next to GP26/ADC0 at physical pin 31, separated by a GND pin
    at physical pin 33). GP28 (ADC2, physical pin 34) works the same way if
    GP27 isn't convenient. Pico GND is already common with the rest of the
    circuit, so no separate ground jumper is needed.

Caveat carried over from the RP2350 datasheet's RIN_ADC >= 100kOhm spec:
wiring a second ADC pin onto this node adds another ~100kOhm-class load on
top of whatever's already loading it, so this reading will itself pull the
node down somewhat if the node is genuinely high-impedance -- it's a
"dead vs. small-but-real" test, not a clean unloaded-voltage measurement.

No Timer/ISR, no GPS/UART -- this only needs enough samples over a few
seconds to see the AC swing, not the full production sampling pipeline.

Usage (mpremote, from the repo root):
    python3 -m mpremote connect /dev/cu.usbmodem101 run diagnostics/adc_bias_probe.py
"""

from machine import ADC
import time

adc = ADC(27)  # GP27 / ADC1 -- jumpered to the bias node under test
DURATION_S = 8

t0 = time.ticks_us()
while time.ticks_diff(time.ticks_us(), t0) < DURATION_S * 1_000_000:
    raw = adc.read_u16()
    t_us = time.ticks_diff(time.ticks_us(), t0)
    print(str(t_us) + "," + str(raw))
    time.sleep_us(1000)

print("# DONE")
