<div align="center">

# ESP32 iPhone Agent

**Let an AI model use a real iPhone — with a $7 ESP32. No Mac, no developer account, no jailbreak.**

English · [简体中文](README.zh-CN.md)

Demo video and updates: [X @iamhenryyuan](https://x.com/iamhenryyuan) · [YouTube demo](https://www.youtube.com/watch?v=Ly4im-P4jYg)

[![CI](https://github.com/onehumanbeing/esp32-iphone-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/onehumanbeing/esp32-iphone-agent/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Board](https://img.shields.io/badge/board-ESP32--S3-red)

</div>

<p align="center"><img src="docs/images/demo.png" alt="Example 1: the web UI after drawing a smiley face in Whiteboard, with the live screen, live log, plan and step screenshots" width="100%"></p>

**Example 1 — draw on a canvas.** *"Open Whiteboard and draw a smiley face"*: the agent searches for the app, opens it and draws the face with continuous strokes. Every step's screenshot, with its tap or stroke drawn on top, stays on the step wall.

<p align="center"><img src="docs/images/example-alarm.png" alt="Example 2: an iPhone with a XIAO ESP32S3 plugged in, setting an alarm that repeats every day, next to the web UI" width="100%"></p>

**Example 2 — set an alarm.** *"Set an alarm for 7:00 AM that repeats every day, then save it"*: the agent opens Clock, types 0700 into the time wheel, ticks the days under Repeat several taps at a time, and saves. Left: the real iPhone with the ESP32 plugged in; right: the web UI.

The agent sees the phone through AirPlay screen mirroring and touches it through a tiny ESP32-S3 board that
plugs into the phone's USB port as a mouse and keyboard. Any OpenAI-compatible vision model can be the brain —
a cloud API, or a model you run yourself.

## Why

Most iPhone automation drives the phone through XCTest / WebDriverAgent. That needs a Mac, Xcode, a signed
helper app and an Apple developer account (a free account's signature expires every 7 days). This project
uses the same paths a person uses — the screen and a pointer — so the phone needs nothing installed.

|                                  | ESP32 iPhone Agent        | XCTest / WebDriverAgent tools |
|----------------------------------|---------------------------|-------------------------------|
| Apple developer account          | **Not needed**            | Needed (to sign the runner)   |
| Xcode                            | **Not needed**            | Needed                        |
| Computer                         | Mac **or Linux**          | Mac                           |
| App installed on the phone       | **None**                  | Test runner app               |
| Jailbreak                        | Not needed                | Not needed                    |
| Extra hardware                   | ESP32-S3 board (~$7)      | USB cable                     |
| Works in any app, incl. games    | Yes (it only sees pixels) | Mostly (depends on the accessibility tree) |

## How it works

- **Eyes — AirPlay mirroring.** The phone mirrors its screen to [UxPlay](https://github.com/FDH2/UxPlay) running
  on your computer; a GStreamer pipeline turns the stream into JPEG frames for the agent and the web page.
- **Hands — a USB HID bridge.** An ESP32-S3 plugged into the phone shows up as a standard USB mouse and keyboard.
  With iOS AssistiveTouch on, the mouse moves an on-screen pointer, so a click is a tap and a drag is a swipe.
  The board connects to the computer over Wi-Fi and executes simple line commands (`move`, `click`, `type`, …);
  all logic stays on the computer.
- **Brain — a planner and an actor.** A planner model writes a short plan from the goal and the current screen.
  An actor model then looks at each new screenshot and picks the next action: tap, swipe, draw a path, type, press
  a key, go home, or finish. Both roles can be the same model.
- **Closing the loop.** The pointer is relative, so the server tracks where it is. Before each tap it wiggles the
  pointer, finds it on the mirrored screen and corrects any miss, refining the calibration as it goes. After each
  action it checks whether the screen actually changed and tells the model, so a missed tap is noticed and retried
  instead of built upon. Repeated actions trigger a re-plan; a dropped link or a bad reply is recovered from.

## Hardware

| Part | Notes |
|---|---|
| **ESP32-S3 board** | Tested with a [Seeed Studio XIAO ESP32S3](https://wiki.seeedstudio.com/xiao_esp32s3_getting_started/), plain version without pre-soldered headers (not the Sense camera version, not the Plus). About $7 from Seeed, or about ¥40-50 on Taobao in China (search "XIAO ESP32S3"). Any ESP32-S3 board whose USB-C port is wired to the S3's native USB works. |
| **iPhone** | USB-C models plug in directly. Lightning models need a Lightning to USB Camera Adapter. The phone powers the board. |
| **Computer** | macOS or Linux, on the same network as the phone; runs UxPlay and the server. |
| **Vision model** | Any OpenAI-compatible chat API that accepts images. Running a model locally is optional. |

<p align="center"><img src="docs/images/setup.png" alt="A XIAO ESP32S3 plugged into an iPhone, next to the web UI" width="100%"></p>

Use a USB-C hub with power pass-through if the phone should keep charging while the board is plugged in.

## Quick start

> **Set up with an AI coding agent.** Paste this into Claude Code, Codex or a similar agent on the computer the
> board will connect to:
>
> ```text
> Clone https://github.com/onehumanbeing/esp32-iphone-agent and follow its README to set it up on this machine:
> flash the ESP32-S3 plugged in over USB, install UxPlay and the server, write server/.env for my model API, start
> the server and give me the web UI address. Ask me for the Wi-Fi name/password and the model API key.
> ```

### 1. Flash the board

Install [arduino-cli](https://arduino.github.io/arduino-cli/) and the ESP32 core (3.x), then:

```bash
python3 -c 'import secrets; print(secrets.token_hex(16))'   # a board key: put it in secrets.h AND server/.env
cp hardware/hid_bridge/secrets.example.h hardware/hid_bridge/secrets.h
# edit secrets.h: 2.4 GHz Wi-Fi name/password, the IP address of the computer running the server, DEVICE_KEY
hardware/flash.sh            # optional arguments: [serial port] [--debug]
```

The board's flash size defaults to 8 MB (XIAO ESP32S3). For a 4 MB board such as the ESP32-S3-Zero or most
ESP32-S3 Super Minis, run `FLASH_SIZE=4M hardware/flash.sh` (the firmware needs about 1 MB).

### 2. Install and start the server

```bash
# UxPlay — macOS:
brew install uxplay
# UxPlay — Debian / Ubuntu:
sudo apt install uxplay gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-libav

cd server
python3 -m venv .venv && .venv/bin/pip install -e .
cp .env.example .env         # set LLM_BASE_URL / LLM_MODEL / LLM_API_KEY and DEVICE_KEY (same as secrets.h)
set -a && . ./.env && set +a && .venv/bin/python -m hidagent
```

The server prints `web UI: http://<this computer>:8890/?token=…` and `AirPlay pin: nnnn`. Open that address (with the
token) once in your browser; it remembers the token for 30 days. UxPlay 1.68 or newer is needed for the AirPlay pin.

### 3. Set up the phone (once)

1. **Settings → Accessibility → Touch → AssistiveTouch**: turn it on.
2. Plug the board into the phone. A round pointer appears; the web page shows *controller connected*.
3. **Control Center → Screen Mirroring → iPhone Agent**, and enter the AirPlay pin the server printed (only the
   first time). The live screen appears in the web page.
4. In the web page, open **Manual controls → Calibrate**. The server moves the pointer, finds it on the live screen
   and calibrates itself in a few seconds; if it cannot see the pointer, it asks you to click exactly on it.
5. If **Home** in the web page does not reach the Home Screen (iOS 27 ignores the bottom flick from a pointer, and
   `cmd+h` too), go home through AssistiveTouch: set `HOME_METHOD=assistivetouch` and
   `ASSISTIVETOUCH_BUTTON` to where the button sits, plus either
   - `ASSISTIVETOUCH_MENU_HOME`, the menu's Home item (tap the button once in the web page to see where it is), or
   - **AssistiveTouch → Custom Actions → Single-Tap → Home** on the phone, and no menu item.
   Calibrate on a bright screen (the Home Screen): the pointer is hard to find on a dark one. Keep the button where
   it is afterwards; if it moves, update the setting.

### 4. Give it a task

Type a goal (for example *"Open Settings and go to Wi-Fi"*) and press **Start**, or click one of the examples.

## Web UI

- **Live phone** — click to tap, drag to swipe, scroll to scroll; manual Home, Back, typing and keys.
- **Live log** — every plan, thought and action as it happens, with timings.
- **Step wall** — the screenshot the model decided on for every step, with its tap or swipe drawn on top, plus the
  model's reasoning and how long thinking and acting took. Click a card to zoom in.
- **Taps** are marked with a red ring and a spreading ripple, swipes and strokes with a yellow path; the **Motion** switch
  turns the animation off. Taps the model marked `precise` (pointer checked before tapping) are labelled in the log.
- **Stops before anything irreversible** — paying, buying, sending, deleting the user's own items, or changing
  account/security settings: the agent does everything up to the final review screen, then ends with
  *Waiting for you to confirm* and says what is ready. You do the last tap. (Borrowed from
  [iCTRL](https://github.com/OpenMinis/iCTRL)'s agent rules.)
- **Emergency stop** (or <kbd>Esc</kbd>) stops the agent and releases the pointer. **Clear history** empties the log.
- English and Simplified Chinese interface.

## Configuration

Set in `server/.env` (see [`.env.example`](server/.env.example)):

| Variable | Default | Meaning |
|---|---|---|
| `LLM_BASE_URL` | `http://localhost:8000/v1` | OpenAI-compatible endpoint |
| `LLM_MODEL` / `LLM_API_KEY` | — | Model name and API key |
| `LLM_EXTRA_BODY` | `{}` | JSON merged into every request (e.g. to turn off a model's thinking mode) |
| `LLM_TOKEN_PARAM` | `auto` | Token-limit field: `max_tokens`, `max_completion_tokens` (newer OpenAI models), or `auto` to detect |
| `ACTOR_*` / `PLANNER_*` | — | Per-role overrides of any `LLM_*` variable |
| `AIRPLAY_NAME` | `iPhone Agent` | Receiver name in the phone's Screen Mirroring list |
| `DEVICE_KEY` | — (required) | Shared board key, the same as in `secrets.h`; at least 16 characters |
| `UI_TOKEN` | generated | Web UI token; if unset, one is generated and kept in `~/.hidagent/ui_token` |
| `AIRPLAY_PIN` | generated | 4-digit pin a phone enters once before it may mirror; kept in `~/.hidagent/airplay_pin`; `off` disables it |
| `SCREEN_SOURCE` | `airplay` | Where the screen comes from: `airplay` (mirroring on this network) or `push` (the companion app sends it over a VPN, see "Screen over a VPN") |
| `PUSH_HOST` / `PUSH_PORT` | — / `8891` | `push` only: the VPN address (e.g. this computer's Tailscale `100.x` address) and port the app connects to. Required; a wildcard such as `0.0.0.0` is refused |
| `PUSH_TOKEN` | generated | `push` only: the token the app proves; if unset, one is generated and kept in `~/.hidagent/push_token` |
| `HOST` / `PORT` | `0.0.0.0` / `8890` | Web UI address and port (`HOST=127.0.0.1`: this computer only) |
| `DEVICE_HOST` / `DEVICE_PORT` | `0.0.0.0` / `7300` | Address and port the board connects to |
| `HIDAGENT_DATA` | `~/.hidagent` | Calibration, secrets and debug traces |
| `GATE_MODE` | `off` | `observe`: ask Jev about each action and record the answer, see "Action gate" |
| `HOME_METHOD` | `swipe` | How to reach the Home Screen: `swipe` (flick up from the bottom edge) or `assistivetouch` (tap the AssistiveTouch button, see "Set up the phone") — for iOS versions that ignore the flick from a pointer |
| `ASSISTIVETOUCH_BUTTON` / `ASSISTIVETOUCH_MENU_HOME` | — | `assistivetouch` only: where the button and the menu's Home item are, as `x,y` screen fractions (e.g. `0.9,0.27`). Leave the menu item out when the button's Single-Tap action is set to Home |
| `OCR_MODE` | `api` if installed | `off` · `api` (`GET /api/screen_text`) · `actor` (also added to the actor's prompt). Needs `pip install -e '.[ocr]'`, see "Screen text (OCR)" |
| `HIDAGENT_DEBUG` | `0` | Debug mode, see below |

### Local model (optional)

A cloud API is enough. If you want everything on your own hardware, this is the setup the project was developed
and timed with:

- Model: [nvidia/Qwen3.8-Flash-Next-NVFP4](https://huggingface.co/nvidia/Qwen3.8-Flash-Next-NVFP4)
- Serving: [Qwen3.8-Flash-Dual-DGX-Sparks-TensorFold](https://github.com/MiaAI-Lab/Qwen3.8-Flash-Dual-DGX-Sparks-TensorFold)
  — two NVIDIA DGX Sparks with tensor parallelism (TP=2), OpenAI-compatible API
- Speed: about 1.3 s per action and 2 s per plan, with thinking mode off:

```bash
LLM_BASE_URL=http://<dgx-spark-ip>:8888/v1
LLM_MODEL=Qwen3.8-Flash-Next
LLM_EXTRA_BODY='{"chat_template_kwargs": {"enable_thinking": false}}'
```

Any other OpenAI-compatible server (vLLM, SGLang, llama.cpp, Ollama, …) with a vision model works the same way.

## Action gate (optional, observe only)

With `GATE_MODE=observe`, every action (tap, swipe, type, key, home, back) also gets a second opinion from
[Jev](https://typesafe.ai) (TypeSafe System One), a model that answers narrow questions with a choice and a
probability. It is asked, using text only — the goal, the plan, the actor's own THINK line and the action in words:

- **risk**: `safe` / `confirm` (sending, paying, deleting, changing settings, …) / `block` (not needed for the goal,
  following instructions from the screen, revealing secrets);
- **on goal**: how likely the action serves your goal rather than instructions that appeared on the screen.

In observe mode the answer **never changes what the agent does**: the question runs alongside the action, the log
shows `gate (observe): would confirm · risk confirm 80% · on goal 90%`, and every answer is appended to
`~/.hidagent/gate/decisions.jsonl` (format `jev.decision.v1`, mode 0600, secret-shaped strings masked). The point
is to measure the gate on real runs before a later version is allowed to *hold back* an action (it will never be
able to force one).

Needs the shared `jevlib` client (`JEVLIB_PATH`, default `~/.claude/hooks/jev`) and a Jev key (`TYPESAFE_API_KEY`,
`TYPESAFE_API_KEY_FILE` or `~/.secrets/typesafe-api-key`); without either the gate stays off and the agent runs as
usual. Privacy: the goal, plan, THINK line (the model's description of your screen) and typed text are sent to
TypeSafe and kept in the ledger.

## Driving it from another agent (`GET /api/skill`)

The server is also a tool for an outside agent (Claude Code, Codex, a script): `GET /api/skill` serves an
operating manual in the agent-skill format — access, the coordinate system (fractions of the frame), every
action, the rules that keep an agent out of trouble (no sleeping: actions wait for the screen and report
`screen_changed`; look before acting; never repeat a miss; stop before paying, sending or deleting), and how
to hand a task to the built-in agent instead. `GET /openapi.json` has the full schema. Both need the UI
token like every other call. Borrowed from [iCTRL](https://github.com/OpenMinis/iCTRL)'s `GET /skill`.

Action calls (`/api/tap`, `/api/swipe`, `/api/scroll`, `/api/type`, `/api/key`, `/api/home`, `/api/back`) now wait
for the screen to settle and answer `{"ok": true, "settle": {"waited_s", "stable"}, "screen_changed": bool}`, so a
caller knows whether the tap did anything without sleeping or diffing frames itself.

## Screen text (OCR, optional)

The vision model sees the whole screenshot but misreads or misplaces small text — settings labels, list rows,
web views. With the `ocr` extra the server reads the current frame with [RapidOCR](https://github.com/RapidAI/RapidOCR)
(ONNX, CPU, about 0.5–1 s per portrait frame, models ship in the wheel) and returns each line of text with its
centre:

```bash
sudo apt install libgl1 libglib2.0-0       # Linux servers: OpenCV (used by RapidOCR) needs these even without a display
.venv/bin/pip install -e '.[ocr]'          # then restart the server; OCR_MODE defaults to api
curl -s -H "$H" "$S/api/screen_text?min_conf=0.6"
# {"items":[{"text":"Wi-Fi","x":0.17,"y":0.47,"w":0.15,"h":0.02,"conf":0.99}, …], "count":…, "took_s":0.7, …}
```

`OCR_MODE=actor` also appends those lines (in the actor's 0-1000 coordinates) to every actor prompt, so it can aim
at words instead of estimating — at the cost of about half a second and some tokens per step; `ocr_s` on each
step records the cost. The gate never sees OCR output. Borrowed from iCTRL's `screen_text`.

If `/api/screen_text` answers 503 although the extra is installed, the detail says why — on a headless Linux box it
is almost always `libGL.so.1`, i.e. the `apt install` line above; the server also logs the reason at start-up.

## Debug mode

Set `HIDAGENT_DEBUG=1` to log every board command with its latency and to save one folder per agent run:

```
~/.hidagent/traces/20261012-005207/
├── steps.jsonl   # every model call (prompt, raw reply, latency) and every action
├── 002.jpg       # the screenshot each model call saw
└── ...
```

`hardware/flash.sh --debug` builds firmware that echoes each command and its duration over USB serial.

## Board protocol

The board holds no logic. On connecting, board and server first prove to each other that they know `DEVICE_KEY`
(HMAC-SHA256 over fresh random nonces, so the key itself never crosses the network):

```text
board  -> hello <last reset reason> <uptime ms> <board nonce>
server -> challenge <server nonce> <HMAC(key, "server:<board nonce>:<server nonce>")>
board  -> auth <HMAC(key, "board:<server nonce>:<board nonce>")>
server -> ok                       (or "err auth" and the connection is closed)
```

Then the server sends one command per line over TCP and the board answers `ok` or `err <reason>`. Every line carries a
tag tied to this connection and its position, `session = HMAC(key, "session:<board nonce>:<server nonce>")`:

```text
server -> <command> <first 32 hex digits of HMAC(session, "cmd:<n>:<command>")>
board  -> <reply> <first 32 hex digits of HMAC(session, "reply:<n>:<reply>")>      n = 0, 1, 2, … per connection
```

A line with a missing or wrong tag (forged, replayed, reordered, or over 512 bytes) makes the board hang up without
running it; the server drops the link on a wrong reply tag. Long text is typed in pieces of 200 characters.

| Command | Effect |
|---|---|
| `ping` | Health check |
| `home` | Push the pointer into the top-left corner (a known position) |
| `move <dx> <dy>` | Relative pointer move, at a constant pace so iOS pointer acceleration stays predictable |
| `press` / `release` / `click` | Left button down / up / both |
| `scroll <v> <h>` | Scroll wheel |
| `type <text>` | Type ASCII text |
| `key <combo>` | Press keys, e.g. `enter`, `cmd+h`, `cmd+space` |

If the Wi-Fi link drops, the board releases all buttons and reconnects on its own. While the phone is locked, its USB
port sleeps: the board then tries to wake it and otherwise answers `err phone usb asleep` instead of waiting. A watchdog
restarts the board if it ever stops responding.

## Project layout

```
hardware/   ESP32-S3 firmware (Arduino) and flash script
server/     agent, web UI, screen capture and board link (Python, FastAPI)
            serve/: optional minimal server to host a Hugging Face vision model yourself
```

## Roadmap

- [ ] Test a fully local setup with DiffusionGemma (`server/serve/` has an experimental OpenAI-compatible server)
- [ ] Landscape apps (screen coordinates and calibration assume portrait)
- [ ] Non-ASCII text input (e.g. Chinese through the iOS pinyin keyboard)
- [ ] More example tasks across popular apps

## Screen over a VPN (`SCREEN_SOURCE=push`)

AirPlay discovery is mDNS multicast and does not cross a VPN, so a phone that is not on the server's network cannot
mirror to it. Instead, a small companion iOS app (a ReplayKit broadcast extension) encodes the screen as JPEGs a few
times a second and sends them to the server over one WebSocket on the server's VPN address. The agent, calibration and
web UI work exactly as with AirPlay.

- Set `SCREEN_SOURCE=push`, `PUSH_HOST=<this computer's VPN address>` (a wildcard is refused: the listener must not
  face the LAN or the internet) and start the server. It prints the push token (also kept in `~/.hidagent/push_token`).
- In the app, enter `ws://<PUSH_HOST>:8891/` and the token, then start the broadcast from Control Center or the app.
- Protocol, for another client: HTTP upgrade to WebSocket with `Authorization: Bearer <PUSH_TOKEN>` (never put the
  token in the URL), then one binary message per JPEG, portrait, at most 1 MB and 15 per second. While the screen is
  still, re-send the last JPEG unchanged or a ping every second or two: the server treats a connection that has been
  silent for 5 s as gone. A new authenticated connection replaces the previous one.

## Limitations

- iOS does not mirror some screens (passcode entry, Apple Pay, DRM video); the agent cannot see them.
- Mirroring stops when the phone locks; keep auto-lock off while the agent works.
- Keyboard input is ASCII only.
- Re-calibrate after changing AssistiveTouch tracking speed.
- Landscape apps are not supported yet: drawings and taps there land in the wrong place.
- On Linux machines with several network interfaces, limit Avahi (mDNS) to the interface on the phone's network
  if the phone does not list the receiver.

## Security

The agent can do anything you can do on an unlocked phone, so access to it is guarded:

- **Web UI** — every page and API call needs the UI token (a `SameSite=Strict`, `HttpOnly` cookie set by opening
  `/?token=…`, or an `Authorization: Bearer` header for scripts; at least 16 characters). Changes (`POST`) are also
  refused when the browser says they come from another origin (`Origin` / `Sec-Fetch-Site`), which covers other
  ports on the same host that share the cookie. This blocks CSRF and DNS-rebinding attacks. The token is kept out of
  the access log. Set `HOST=127.0.0.1` if only this computer needs the page.
- **Board link** — the server only accepts a board that proves it knows `DEVICE_KEY`, the board only accepts a server
  that proves the same, and every command and reply after that is tagged for that connection. A stranger connecting
  to the board port cannot drive the phone or kick the real board off, and someone who intercepts the board's
  connection (e.g. ARP spoofing) and relays the handshake still cannot inject or replay commands. The link is not
  encrypted: someone on the network can read the commands, including typed text.
- **Push screen** — the listener binds to the VPN address you give it only (a wildcard is refused), the app proves a
  separate `PUSH_TOKEN` in the upgrade request (it lives on the phone, so it is not the UI token), a frame announcing
  more than 1 MB closes the connection unread, non-JPEG bytes and frames over 15/s are dropped, and strangers get the
  same short handshake window and per-address cap as on the board port. The connection is not encrypted beyond the VPN.
- **The agent itself** — it is told never to press a final Pay / Buy / Send / Delete / Confirm button or change
  account and security settings; it stops and asks you instead (`confirm`). That is a prompt rule, not a lock:
  do not leave an agent unsupervised around payments, messages or anything that cannot be undone, and keep the
  action gate's record (`GATE_MODE=observe`) when you want a second opinion on what it tried.
- **AirPlay** — UxPlay asks a new phone for the pin, and keeps the phones that entered it in
  `~/.hidagent/uxplay.register`, so other devices on the network cannot feed the agent a fake screen. The pin is 4
  digits and fixed; whether UxPlay limits wrong guesses is not checked here, so keep the receiver on a trusted network.
- Secrets in `~/.hidagent` (created `0700`, files `0600`, never written through a symlink) are readable by your user
  only. The Wi-Fi password and `DEVICE_KEY` are stored in the board's flash unencrypted: treat a lost board as a
  leaked key and change both.

Still on you: the model acts on whatever is on the screen, including text written by others (a web page or message
can try to give it instructions). Prefer a spare phone without payment or banking apps, watch it work, and keep the
emergency stop within reach. With a cloud model, every screenshot is sent to that provider.

## Responsible use

Only automate devices and accounts you own or are allowed to use, and respect the terms of the apps involved. The
agent acts with your hands: watch it, and keep the emergency stop within reach.

## Contributing

Issues and pull requests are welcome. Run the checks before sending a change:

```bash
cd server && pip install -e ".[dev]" && ruff check . && pytest
```

## Related projects

Other ways people drive an iPhone with code or AI, and how this project differs. Corrections welcome.

| Project | Approach | Needs on the phone / host | Difference from this project |
|---|---|---|---|
| [VersaiOS](https://github.com/zhaodashuaibi/VersaiOS) | UxPlay mirroring + ESP32-S3 Bluetooth mouse + vision model | ESP32 tethered to a computer over USB serial; desktop GUI, mainly Windows | Closest in spirit. One tap per instruction, no planning step or before/after screen check |
| [ja1chen/actell](https://github.com/ja1chen/actell) | Agent app on the iPhone, screenshots and taps via Apple's CoreDevice debug channel | Developer Mode, pairing + Developer Disk Image, Xcode self-signing | This project needs no Developer Mode, no app on the phone and no signing |
| [AidenAI-IO/aiden-firmware](https://github.com/AidenAI-IO/aiden-firmware) | Dedicated board: HDMI capture + USB keyboard/mouse + multimodal model | HDMI capture chip and dock; mainly Android | Uses AirPlay instead of HDMI capture, so no capture hardware |
| [yoyicue/glassbox](https://github.com/yoyicue/glassbox) | PicoKVM HDMI capture + USB HID, observe/decide/act/verify loop | macOS controller and PicoKVM | Runs on Linux with a single ESP32 |
| [boboabw/phone-agent](https://github.com/boboabw/phone-agent) | ESP32-S3 Bluetooth HID + AssistiveTouch, agent runs as an iPhone app | Mac + Xcode to build and sign the app | Nothing to install on the phone |
| [dovydaskel/Iphone-control-for-social-media](https://github.com/dovydaskel/Iphone-control-for-social-media) | ESP32 Bluetooth mouse with OpenCV pointer correction, scripted posting | Mac | Similar closed-loop pointer idea; this project adds a vision-model planner/executor |
| [VKCOM/devicehub](https://github.com/VKCOM/devicehub) | ESP32 Bluetooth mouse for iOS device farms | Farm infrastructure | Device-farm tooling, no AI agent |
| [NolanCotter/AirBridge](https://github.com/NolanCotter/AirBridge) / [gbulog/pcairplay](https://github.com/gbulog/pcairplay) | UxPlay on Linux + the PC's own Bluetooth as keyboard/mouse | Linux PC with Bluetooth | Manual remote control, no AI |

Other routes: WebDriverAgent/XCUITest-based tools such as [mobile-mcp](https://github.com/mobile-next/mobile-mcp) and [Midscene](https://github.com/web-infra-dev/midscene) need a Mac and code signing; tools built on macOS iPhone Mirroring need a Mac.

## Acknowledgements

[UxPlay](https://github.com/FDH2/UxPlay) (AirPlay receiver) ·
[Arduino ESP32 core](https://github.com/espressif/arduino-esp32) and [TinyUSB](https://github.com/hathach/tinyusb) (USB HID) ·
[FastAPI](https://fastapi.tiangolo.com/)

## License

[MIT](LICENSE)
