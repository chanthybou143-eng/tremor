from machine import ADC
import time

adc = ADC(26)
t0 = time.ticks_us()

while True:
    raw = adc.read_u16()
    voltage = raw * 3.3 / 65535
    t_us = time.ticks_diff(time.ticks_us(), t0)
    print(str(t_us) + "," + str(voltage))
    time.sleep_us(500)
