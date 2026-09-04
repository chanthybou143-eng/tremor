from machine import ADC
import time

adc = ADC(26)

while True:
    raw = adc.read_u16()
    voltage = raw * 3.3 / 65535
    print(voltage)
    time.sleep_us(500)
