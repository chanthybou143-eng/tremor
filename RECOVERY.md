# Recovering a TREMOR unit (Pico 2 W running standalone)

Once `main.py` is on the flash, the unit starts posting by itself at power-up and the hardware watchdog
(8 s) reboots it if anything hangs. That is what you want in the field and a nuisance at the bench: a
running unit cannot be talked to with `mpremote` for more than a few seconds. The **jumper** is the way in.

## What the LED tells you (onboard LED)

| LED | Meaning |
|---|---|
| **steady on** | Escape-hatch (maintenance) mode: the GP22 jumper was fitted at boot. Watchdog NOT armed, client NOT running, REPL free. |
| **slow blink** (1 Hz) | Running normally (client started, watchdog armed). |
| **fast blink** (5 Hz) | Configuration error: `wifi_config.py` is missing, unreadable, or lacks/has an empty required field. Watchdog NOT armed, client NOT running, REPL free. The USB console says which fields (names only, never values). |
| off | Not started yet (first second of boot), or the unit is stuck before `main.py` ran, or power is off. |

The blink runs from a timer, so it keeps going while the client is inside a slow network call. If the LED
is blinking slowly the *timer* is alive; the watchdog is what guards the main loop.

## Getting in: the jumper procedure

1. Unplug USB power.
2. Put a jumper wire between **pin 29 (GP22)** and **pin 28 (GND)**. They are next to each other on the
   same edge of the Pico: with the USB connector at the top, it is the right-hand edge, pins 21-40 counting
   up from the bottom-right corner, so pin 28 is the 8th and pin 29 the 9th.
3. Plug USB in. Within ~1 s the LED goes **steady on** and the console prints
   `# MAINTENANCE: GP22 (pin 29) is jumpered to GND -- watchdog NOT armed, client NOT started.`
   The check happens before the watchdog is armed and before any other file is loaded, so it works even
   if the rest of the flash is broken.
4. Connect from the Mac: `mpremote connect auto` (REPL), `mpremote fs ls`, `mpremote fs cp ... :`, and so on.
   There is no time limit; nothing will reset the board.
5. When done: unplug USB, **remove the jumper**, plug in again. The unit runs by itself.

If the LED is off, or blinking, with the jumper fitted, power-cycle again with the jumper fitted first
(the jumper must be in place *before* power is applied; it is read once, at boot).

## Killing `mpremote` does NOT stop the client

The client runs on the Pico, not on the Mac. Closing the terminal, killing `mpremote`, or unplugging the
cable's host end leaves it running (and posting) until power is removed.

* **Ctrl-C over the serial port** (what `mpremote` sends on connect) interrupts the client and gives you the
  REPL, **but the watchdog stays armed** (an armed RP2 watchdog cannot be stopped) and reboots the board
  about 8 s later, restarting the client and cutting your `mpremote` session. It also counts as one
  consecutive watchdog reset in the boot counter (below).
* So for anything longer than a quick look, **use the jumper**.
* `mpremote run some_script.py` with the client already running on the board is not useful for the same reason.

## Reading the boot log

Every boot prints (USB console only, so connect during or right after boot):

```
# BOOT_COUNTER consecutive_wdt_resets=N last_reset_was_wdt=True|False
# PREV_FREEZE last_stage=<stage> post_no=<n> stage_started_at_uptime_ms=<ms>     (after a watchdog reset only)
# BOOT reset_cause=... / # BOOT_ID boot_id=... / # AUTH ingest token configured
```

* `consecutive_wdt_resets` counts watchdog resets since the unit last demonstrably worked (its first
  successful POST after a boot sets it back to 0). A power cycle also resets it. It never stops the unit:
  it keeps rebooting and retrying, and the jumper still works.
* `last_stage` is the POST stage that was running when the board was reset: `dns`, `connect`,
  `tls_handshake`, `send`, `read_response`, or `idle` (between POSTs; so not a network stall, e.g. you
  interrupted it with Ctrl-C or the main loop hung).
* At run time, `# WDT_GUARD_EXTENDED stage=...` means a POST stage blocked longer than the bare 8 s
  watchdog tolerates and the guard kept the board alive; `# WDT_GUARD_EXPIRED` means it hit the 25 s cap and
  the watchdog is about to reset the board.

Fast blink and `# CONFIG_ERROR ...`: fix `wifi_config.py` (needs `WIFI_SSID`, `WIFI_PASSWORD`, `UNIT_ID`,
`INGEST_URL` starting with `http(s)://`, `INGEST_TOKEN`, all non-empty text). Copy the corrected file with
`mpremote fs cp wifi_config.py :` **from the jumpered (steady-on) state**, and never paste its contents into
a chat or a log.

## Restoring the pre-standalone flash (Step 0 backup)

The known-good state before `main.py` was flashed is in `~/tremor-flash-backup-20260926/` (outside the repo):
`chunk_summary.py freq_estimator.py http_client.py http_keepalive.py nmea_parser.py pps_time_sync.py
wifi_config.py wifi_ingest.py` plus `MANIFEST.txt` (sizes and SHA-256) and `PICO_INFO.txt` (MicroPython
v1.27.0, Pico 2 W). That state had **no `main.py`**: the board booted to the REPL and the client was started
from the Mac. Note that its `wifi_config.py` has no `INGEST_TOKEN`, and that the folder holds the Wi-Fi
password: keep it private.

1. Enter maintenance mode (jumper procedure above).
2. Remove what the standalone set added:
   `mpremote fs rm :main.py :boot_support.py :wdt_support.py :wifi_unit_client.py`
3. Put the backup files back (this overwrites the newer versions of the same names):
   `mpremote fs cp ~/tremor-flash-backup-20260926/*.py :`
4. Check it against the manifest: `mpremote fs ls` (sizes must match `MANIFEST.txt`); for a hash check
   run on the board:
   ```
   mpremote exec "import hashlib,binascii,os
   for f in sorted(os.listdir()):
       h=hashlib.sha256(open(f,'rb').read()).digest(); print(f, binascii.hexlify(h).decode())"
   ```
   and compare with the manifest.
5. Remove the jumper and power-cycle: the board now boots to a bare REPL, as before.

If the board will not even enter the REPL (or the filesystem is damaged): hold **BOOTSEL** while plugging in
USB, drag the MicroPython **v1.27.0 for Raspberry Pi Pico 2 W** UF2 onto the `RP2350` drive (this erases the
flash filesystem), then do steps 3-5 above.

## Quick reference

| Situation | Do |
|---|---|
| Need the REPL / to copy files | jumper (pin 29 to pin 28), power-cycle, `mpremote` |
| Unit rebooting every ~8-30 s | jumper, power-cycle, read the boot log |
| Fast blink | config error, fix `wifi_config.py` |
| Want the old bench setup back | restore the Step 0 backup |
| Ran `mpremote` and killed it | the client is still running on the board; to stop it use the jumper and power-cycle |
