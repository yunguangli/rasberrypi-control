# Hardware test plan — GUI-driven tests on the Raspberry Pi

Every test here is **driven from the GUI** and verified **physically on the
Pi and its connected components** (LED lights, relay clicks, motor spins…).
Ordered from the simplest case — one LED switch — outward.

Control the Pi from the GUI on your PC/phone; watch/touch the hardware.

---

## 0. Setup & prerequisites

**On the Pi** (live mode, real GPIO):

```bash
pip install "gpiozero==1.6.2" "setuptools<81"   # Pi target: Python 3.11 + gpiozero 1.6.2
python3 pi.py --port 8642              # sections 1–11, 13 (read-only agent tests)
python3 pi.py --port 8642 --allow-code # + section 12 and the script escape hatch
python3 pi.py --help                   # list every flag
```
→ banner `picontrol bridge listening on 0.0.0.0:8642 [live GPIO via RPiGPIOFactory...]`

All flags (`--host`, `--port`, `--dev`, `--allow-code`) are documented in
`README.md`. Script tests need `--allow-code`; everything else works without it.
Agent tests (§13) need a configured model (OpenRouter key or local Ollama) and a
reachable bridge.

**On the PC:** `uv run flet run` → enter `<pi-ip>:8642` → **Connect**
(green dot, `connected · N ms`).

### Wiring under test

| Component | BCM (phys) | Notes |
|---|---|---|
| Status LED (+ 330 Ω) | 17 (11) | LED anode → GPIO17 via resistor, cathode → GND |
| RGB LED (common cathode) | 19/16/26 (35/36/37) | R→19, G→16, B→26, one ~220 Ω per leg, common → GND |
| Relay module 1 | 22 (15) | **active-low**; load on COM/NO; module VCC → 5 V |
| Relay module 2 | 27 (13) | active-low |
| Buzzer (passive) | 23 (16) | 300 ms pulse + note buttons (tones) |
| Push button | 24 (18) | other leg → GND (internal pull-up)
| PIR (HC-SR501) | 25 (22) | OUT → GPIO25, VCC 5 V, GND |
| DHT22 | 4 (7) | data → GPIO4 (+ 10 kΩ pull-up to 3V3) |
| HC-SR04 | trig 5 (29), echo 6 (31) | **echo via level divider** (5 V → 3.3 V) |
| PWM LED / dimmer | 18 (12) | hardware PWM pin |
| Motor driver (L298N etc.) | fwd 13 (33), back 12 (32) | never wire a motor directly to a GPIO |
| MCP3008 + LDR (if fitted) | SPI0: 8/9/10/11 | light sensor — SPI0 must be enabled, else the card shows a readable fault |

**Handy verdict tools:** multimeter (voltages, relay contacts), phone camera
(PWM flicker, LED brightness differences), stopwatch/`pi.py` console
(transaction timestamps), a ruler or tape (ultrasonic distance).

> **Safety:** relay and motor loads use their own supply; share GND with the
> Pi. Never power a motor from the Pi's 3V3/5V rail for anything bigger than a
> vibration motor.

## 0b. Layout & navigation (do this first)

| ID | GUI action | Verification |
|----|-----------|--------------|
| HW-01 | Wide window (≥ 600 px) | Header (target + status) stays fixed while the page scrolls; the 2×20 board strip is pinned above the **Board / Control / Script / Agent** tabs; Script and Agent are one click away |
| HW-02 | Phone portrait (< 600 px) | Bottom navigation **Board · Control · Script · Agent**; Control starts directly with the device cards (no full board above them); Script shows the editor and console stacked; Agent is one column with the composer at the bottom |
| HW-03 | Open the **Board** page | 40 detail cards: physical #, GPIO name, BCM, alt function, function family, wired device, live state chip; legend plus a **Wired only** filter |
| HW-04 | Tap a wired output on the Board page (e.g. #11 Status LED) | Same toggle as its Control card; the chip follows within one poll |
| HW-05 | Resize/rotate across 600 px | Tabs ↔ bottom nav switch, the current page is kept, no duplicated controls, nothing needs a restart |
| HW-06 | Activity section: toggle and **Clear** | Body collapses/expands; Clear empties the list; new ops reappear as you use the GUI |
| HW-07 | Read any label at arm's length | Pin names, subtitles and the log are legible (12–15 px), headings 20 px, sensor values 28 px |

---

## 1. LED switches — the simple end-to-end case

| ID | GUI action | Physical verification on the Pi |
|----|-----------|--------------------------------|
| HW-10 | Toggle **Status LED** ON | LED lights ≤ ~1.6 s after the click (within one poll); stays lit |
| HW-11 | Toggle OFF | LED goes dark within one poll |
| HW-12 | Flip ON, watch the Pi console | One line `→ digital_write 17 = 1 <- ok` — exactly one per click (no duplicate ops) |
| HW-13 | LED wired to a multimeter instead | 3.3 V ON / 0 V OFF |
| HW-14 | Toggle ON, then refresh/reconnect the GUI | Switch still shows ON — state is server-side, not just optimistic UI |
| HW-15 | Toggle rapidly 10× | LED ends in the state shown; Pi console shows no tracebacks; GPIO17 not stuck |
| HW-16 | **RGB LED**: click the red swatch, then green, then blue | LED changes colour within one poll; one `rgb_write pin 19 = <n> <- ok` per tap |
| HW-17 | Click white, then the off swatch | All three dies lit together → fully dark |
| HW-18 | Toggle the RGB card's switch | ON → white, OFF → dark; switch position survives a GUI refresh |
| HW-19 | **ALL OFF** with a colour showing | RGB LED goes dark together with everything else |

## 2. Relays

| ID | GUI action | Physical verification |
|----|-----------|------------------------|
| HW-20 | Toggle **Relay 1** ON | Audible **click**; multimeter across COM–NO shows continuity (CLOSED); LED on the module lights (active-low: energised = logical ON) |
| HW-21 | Toggle OFF | Second click; COM–NO opens |
| HW-22 | Relay 1 ON + Relay 2 ON together | Both click independently; a load on relay 2 doesn't disturb relay 1 |
| HW-23 | Live low-voltage load (e.g. 12 V lamp) on NO | Lamp follows the switch within one poll; off = fully off (no flicker) |
| HW-24 | **ALL OFF** with both relays energised | Both drop out (click-click), load off |

## 3. Buzzer (pulse + tones)

| ID | GUI action | Physical verification |
|----|-----------|------------------------|
| HW-30 | Click **Buzz 300 ms** | One chirp of ~0.3 s per click (measure by ear/stopwatch vs Pi console timestamp) |
| HW-31 | Click 5× rapidly | At most one pulse in flight; no machine-gun overlapping beeps |
| HW-32 | Click the note buttons **C4 D4 E4 G4 A4 C5** | Six distinct rising pitches (262→523 Hz), each ~0.4 s |

## 4. Inputs — component drives the GUI

(These run physical-first: your hand is the stimulus, the GUI is the display.)

| ID | Physical action | GUI verification |
|----|----------------|------------------|
| HW-40 | Press the push button on GPIO24 | **Door button** dot → amber `active` ≤ 1.6 s; board cell #18 badge `ACT` |
| HW-41 | Release | Back to grey `idle` within one poll |
| HW-42 | Hold 5 s, release | No stale `active` — exactly one state change each way |
| HW-43 | Walk in front of the PIR | **PIR sensor** activates after its delay jumper setting; deactivates when still |
| HW-44 | Cross-check wiring | Button not working → check GND leg (uses internal pull-up); PIR not working → check OUT↔GPIO25 and 5 V supply |

## 5. DHT22 room temperature

| ID | GUI action / condition | Physical verification |
|----|-----------|------------------------|
| HW-50 | Watch **Room temp** card for 1–2 min | Value refreshes every poll, plausible room temp (~15–30 °C), no `ERR` |
| HW-51 | Breathe on / cup the sensor | Reading climbs within ~10–20 s (DHT22 max sample rate ≈ 0.5 Hz — don't expect per-second changes) |
| HW-52 | Room temp card with the Adafruit driver **not** installed | Red fault `no DHT22 driver: … adafruit-circuitpython-dht is unavailable …` — actionable text, no crash; install it (`pip install adafruit-circuitpython-dht`, module `adafruit_dht`), wait ~30 s and the card recovers on its own (failed constructions are retried, no `pi.py` restart) |

## 6. HC-SR04 distance sensor

| ID | GUI action | Physical verification |
|----|-----------|------------------------|
| HW-60 | Hold a flat object 30 cm in front, then 1 m | **Distance** card reads ≈ 0.30 m / ≈ 1.0 m (± a few cm); update with your hand movement |
| HW-61 | Move the object slowly 1 m → 10 cm | Value tracks smoothly; **the rest of the GUI keeps polling** (switches still respond while the sonar runs — the `partial=True` guarantee) |
| HW-62 | Remove all objects / point at open space | Card shows the max range or last echo — GUI never hangs, dot stays green |
| HW-63 | Tape over the echo pin (no return) | Card may fault or hold; **poll loop survives**, other cards keep updating |

## 7. Light sensor via MCP3008 (only if fitted)

| ID | GUI action | Physical verification |
|----|-----------|------------------------|
| HW-70 | Live mode, MCP3008 not wired (or SPI0 not enabled) | The card either shows one readable red fault naming the missing piece (e.g. an SPI-open/`/dev/spidev0.0` error) or a stable near-0 % reading via the software-SPI fallback — no traceback, other cards keep polling; after wiring/enabling SPI0 the card recovers within ~30 s (failure retry). Without the `spidev` package the bridge prints one startup hint (`pip install spidev`) instead of a warning per rebuild |
| HW-71 | ADC fitted, cover the LDR with your hand, then shine a torch | Value in % drops toward 0 when covered, rises when lit |

## 8. PWM dimmer

| ID | GUI action | Physical verification |
|----|-----------|------------------------|
| HW-80 | Drag **Desk dimmer** to 0 / 50 / 100 %, release | LED darkness/brightness follows **on release**; at 100 % full brightness, 0 % off |
| HW-81 | Sweep 0 → 100 in steps of 25 % | Monotonic brightness steps, no flicker/jumping at each stop |
| HW-82 | Camera at ~10 % | Phone camera shows PWM flicker/stripe pattern (proof it's duty-cycling, not linear dimming) |
| HW-83 | Set 70 %, click **ALL OFF** | LED off; slider resets to 0 % and stays consistent on the next poll |

## 9. Motor (via driver — L298N/DRV8833)

| ID | GUI action | Physical verification |
|----|-----------|------------------------|
| HW-90 | Motor slider → +50 % | Motor spins forward at half speed |
| HW-91 | Slider → −50 % | Motor spins **reverse** (this is what distinguishes the Motor card from a plain dimmer) |
| HW-92 | Slider → 0 | Motor stops (both driver inputs low) |
| HW-93 | +100 % vs +25 % | Clearly different speeds; motor supply doesn't sag the Pi (Pi stays connected) |
| HW-94 | **ALL OFF** while spinning | Motor stops within one poll + op round trip |

## 10. Pi-internal cards (no wiring)

| ID | GUI action | Physical verification |
|----|-----------|------------------------|
| HW-100 | Watch **CPU temp** card on the Pi | Plausible die temp (~40–60 °C idle); rises if you load the Pi (`stress`/`yes > /dev/null`) |
| HW-101 | **System load** card while running a CPU loop | Load climbs within ~1 min (1-min average), falls after you kill it |
| HW-102 | Cross-check | `cat /sys/class/thermal/thermal_zone0/temp` and `cat /proc/loadavg` on the Pi ≈ card values |

## 11. System-level & safety

| ID | GUI action | Physical verification |
|----|-----------|------------------------|
| HW-110 | **ALL OFF** with everything energised (LED, relays, motor, dimmer) | Everything physically quiets down at once; card badges agree |
| HW-111 | Unplug the Pi's network 30 s, plug back, **Connect** | GUI shows red → green; hardware state matches what the GUI shows (server-authoritative) |
| HW-112 | Run the GUI from a phone (portrait) against the same Pi | All controls usable; toggling from the phone affects the same hardware; both GUIs agree within a poll |
| HW-113 | Leave everything on for 10 min | No thermal shutdown, no relay chatter, no drift; CPU temp card tracks the Pi's actual temp |
| HW-114 | Kill `pi.py` (Ctrl-C **or** SIGTERM/`systemctl stop`) on the Pi | Both paths print `shutting down`; a running script is stopped first (`stopped the running script`), then outputs are switched off and GPIO released — restarting `pi.py` restores control without replugging components |
| HW-115 | Button + LED combo: press the button while toggling the LED from the GUI | Both directions independent — input state and output state never corrupt each other |

## 12. Script tests (needs `pi.py --allow-code`)

Restart the bridge with the flag for this section. Open the **Script** page
(its own tab on wide screens, bottom-nav item in portrait). The editor is
always editable (compose while offline or mid-job); only
**Run on Pi** / **Stop** are gated. Add `--console` to the bridge command if
you also want the job's output on the Pi's terminal while testing.

| ID | GUI action | Physical verification |
|----|-----------|------------------------|
| HW-120 | Bridge started **without** `--allow-code`: open the Script page | **Run on Pi** disabled, status red `requires pi.py --allow-code`; editor still editable |
| HW-121 | With the flag: paste a GPIO17 blink loop, click **Run on Pi** | Status → `running · job 1`; **Status LED physically blinks at 1 Hz**; every card shows the `halted` chip with `script running (GPIO released)`; the console streams the script's prints, then green `job 1 finished · exit 0` |
| HW-122 | **Stop** mid-blink | Blinking stops ≤ 2 s; cards go live again; log line `script job N exited with …` |
| HW-123 | While it runs: toggle a card switch, and click **Run on Pi** again | Both refused in the log: `script is running — stop it first` / `job 1 is still running — stop it first` — no crash |
| HW-124 | Relay-blink script on GPIO22, then **ALL OFF** | Script stops and the relay drops out |
| HW-125 | Script prints DHT22 readings while the Room temp card is halted | Output pane shows the script's values; GUI keeps polling (cards resume when the job ends); no bridge crash |
| HW-126 | Run a script that raises (`1/0`) | Output pane shows the traceback tail; status red `job N finished · exit 1`; bridge still serves the next test |
| HW-127 | Script starts a helper process (`subprocess.Popen`), then **Stop** | The helper dies too (process-group kill): status returns to idle within ~2 s, no stray CPU load on the Pi |
| HW-128 | Script prints thousands of lines | Output pane keeps only the tail (≤ 200 lines / 16 KiB); GUI stays responsive |
| HW-129 | With a job running, `Ctrl-C` / SIGTERM the bridge | Console prints `stopped the running script` before `switched off N output(s)`; no orphaned Python process on the Pi (`ps -ef | grep python`) |

## 13. Agent — typed natural language

Requires a **tool-calling** model: OpenRouter (cloud) or Ollama (local). Configure
it with the gear on the Agent page or the `PICONTROL_LLM_*` variables
(`README.md` → *Agent*). The bridge can stay in normal mode; the script escape
hatch additionally needs `--allow-code`.

The expected reply is an **action chip** (e.g. `status_led.on → on`) plus one or
two sentences; every chip is mirrored in the activity log and the Pi console
shows the real op.

| ID | GUI action | Verification |
|----|-----------|--------------|
| HW-130 | Open **Agent** with no model configured | Status `no model configured`; Send disabled (40 % opacity); the gear opens the settings dialog with Base URL / Model / API key / Facts file |
| HW-131 | Save OpenRouter settings (URL, tool-capable model, `sk-or-…` key) | Status `ready · <model> @ openrouter.ai/api/v1`; Send enabled; the dialog previews `POST …/api/v1/chat/completions`; `ls -l ~/.config/picontrol/agent.json` shows mode `-rw-------` |
| HW-132 | Type `turn on the status LED` → Send | Action chip `status_led.on → on`; **LED lights**; Pi console shows exactly one `digital_write 17 = 1`; no approve step |
| HW-133 | Then type `turn it off` | The follow-up uses context (no re-explaining); LED goes dark; chip `status_led.off → off` |
| HW-134 | `blink the status LED three times` | `blink` (or `run_sequence` for a custom pattern) runs on/off ×3 with pauses; LED blinks three times; one chip per step; turn ends with a short reply |
| HW-135 | `turn on the LED` (both LEDs exist, nothing learned) | A **question card** appears with tappable `Status LED` / `RGB LED`; tapping `Status LED` shows the answer, a `remembered led = …` chip, then the action chip |
| HW-136 | Send `turn on the LED` again | No question this time — goes straight to the remembered device; `PICONTROL_FACTS_PATH` JSON contains the alias |
| HW-137 | Energise relay 1 + motor, then `turn everything off` | `all_off` chip; relay clicks off, motor stops, LED dark; one panic op on the console |
| HW-138 | `what's the room temperature?` | A `read` op (no write), reply quotes the DHT22 value in °C; the Room temp card agrees |
| HW-139 | `make me a coffee` | Polite refusal / explanation; **zero ops** on the Pi console; no invented device |
| HW-140 | `set the desk dimmer to 500%` | Either the model asks/clamps, or a red chip shows the `0..100` range error; **nothing out of range reaches the Pi** |
| HW-141 | Break the endpoint (revoke the key or disconnect the network) and send a command | Red error chip with a readable reason; GUI stays responsive; Send re-enables. Restore the key → the next turn works without restarting |
| HW-142 | Start a long `run_sequence` (e.g. 20 blinks), press **Stop** | Stops between steps (the in-flight request completes); turn ends with `stopped`; hardware is left in a safe state; Send re-enables |
| HW-143 | Trigger a question, then press **Stop** | The question resolves as unanswered, the turn ends `stopped`, no worker left blocked |
| HW-144 | `write a script that blinks GPIO17` (bridge with `--allow-code`) | A **script card** with a preview + **Open in Script**; the editor is filled and the Script page opens; **nothing runs** until you press Run on Pi. Without `--allow-code`, Run stays disabled |
| HW-145 | Press **Clear** on the Agent page | Transcript returns to the empty state with suggestions; the next `turn it off` has no prior context (the agent asks what) |
| HW-146 | Inspect the transcript, activity log and settings file after several turns | The API key never appears in the transcript/log; the settings file is `0600`; facts contain only aliases, no key |
| HW-147 | Point the GUI at an unreachable host (or stop `pi.py`) | Agent status `waiting for the Pi…`, Send disabled; restart the bridge → status `ready` and Send re-enables on the next poll |
| HW-148 | Phone portrait, Agent page | Single column, composer reachable, question options tappable, no horizontal scroll; the same turn controls the same hardware |
| HW-149 | Remember an alias, quit and relaunch the GUI | `turn on the LED` still resolves without asking (facts file persisted); with the facts file deleted it asks again |
| HW-150 | Click the **cable icon** on the Agent page, run `turn on the status LED`, click it again | The **Pi traffic** panel shows the exact `POST /op` body and reply (`{"op":"digital_write","pin":17,"value":true,…}` / `{"ok":true,…}`); hidden by default, the same icon hides it, and **Clear** empties it |
| HW-151 | With a weaker/free-router model, ask for a sequence it may malform (e.g. `blink 3 times`) | Even if the model omits a tool-call id or sends truncated arguments, the turn ends with a readable tool error or a successful blink — never OpenRouter's `tool_call_id must be non-empty` / HTTP 400 |
| HW-152 | `play Happy Birthday with C major tones` on the buzzer | A `run_sequence` of tone steps plays the melody (up to 64 steps; each tone 400 ms, waits set the rhythm); if the model exhausts its output budget, a red `finish_reason=length` chip appears instead of silence |
| HW-153 | `what's the room temperature?` | One `read` op on pin 4; reply in °C, plausible; no pin is driven. With the DHT22 driver missing, a readable fault/answer — no crash |
| HW-154 | `how bright is it?` (MCP3008 + LDR) | One `read` op on the sentinel pin -1; cover the LDR → % drops, shine a torch → % rises |
| HW-155 | `how far is the wall?` with an object at 30 cm / 1 m | One `read` op on pin 5; reply ≈ 0.30 m / ≈ 1.0 m; move your hand and the value tracks |
| HW-156 | `is there motion?` (PIR) | One `digital_read` on pin 25; `motion` while you wave, `idle` when still |
| HW-157 | `is the door open?` (push button) | One `digital_read` on pin 24; `pressed` / `released` follows the physical button |
| HW-158 | `how hot is the Pi?` / `what's the system load?` | `read` on the sentinel pins -2 / -3; values agree with `thermal_zone0` and `/proc/loadavg` |
| HW-159 | `what's the humidity?` or `what's the CO2 level?` (no such sensor) | A clear “not available” reply and **zero ops** — the agent must not invent a reading |
| HW-160 | `read all the sensors` | Several read chips (read-only); no writes; the GUI stays responsive and the Pi console shows only `read` / `digital_read` lines |

---

## Cross-checking tools

- **Pi console** (`pi.py`): one line per op — timestamps for latency, `ERR`
  for failures, never a traceback for hardware faults.
- **Multimeter:** continuity on relay contacts, 3.3 V logic levels.
- **Phone camera:** PWM artefacts (HW-82), catching short pulses.
- **Direct shell:** `gpioinfo` / `cat /proc/loadavg` / thermal zone files to
  independently confirm what the cards report.

## Pass criteria

- **Bench sign-off:** HW-10…HW-24, HW-30, HW-40…HW-44 (LED, relays, RGB,
  buzzer, inputs) all pass with no bridge tracebacks.
- **Full rig sign-off:** additionally HW-50…HW-115 on the actual wired set.
- **Script sign-off:** HW-120…HW-126 with pi.py started with `--allow-code`.
- **Agent sign-off:** HW-130…HW-160 with a tool-capable model; every action the
  agent takes is visible as a chip **and** as one op on the Pi console.
- Any `ERR` badge must show a *readable reason* (missing driver, SPI0 not
  enabled, echo missing) — a raw traceback on a card is a failure even if the
  rest works.

---

Automated (no-hardware) coverage — `uv run pytest` — is listed in the
developer docs; it mocks the pins and does not replace the physical checks
above.
