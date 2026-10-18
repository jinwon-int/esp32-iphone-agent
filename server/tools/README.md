# Home Screen operator tools

Small scripts used to rearrange a real iPhone Home Screen through a running hidagent server (#35). They call only the
server's HTTP API (`/api/frame.jpg`, `/api/longpress`, `/api/drag`, `/api/carry`, `/api/swipe`, `/api/tap`); they do
not start, stop or configure the server.

| Script | What it does |
|---|---|
| `layout.py` | Edit-mode checks (`is_edit`, `folder_edit`, `folder_clear`), page signatures (`sig`) and `where`, single `carry`/`drag`/`swipe` commands |
| `steps.py` | Runs a JSON list of steps from stdin and stops at the first unexpected screen |
| `measure_turn.py` | Records per-frame motion during a carry-style drag (the #54 measurements) |

Run them from `server/` with its virtualenv (`measure_turn.py` imports `hidagent.vision`):

```sh
cd server
export HIDAGENT_DATA=~/.hidagent          # the server's data dir, for ui_token (or set UI_TOKEN)
export HIDAGENT_TOOLS_DIR=~/hidagent-tools # frames go to log/, page signatures to sig/
.venv/bin/python tools/layout.py edit
.venv/bin/python tools/layout.py sig 1     # once per page, again whenever the page layout changes
.venv/bin/python tools/layout.py where
echo '[{"op":"carry","from":[0.2,0.3],"to":[0.4,0.3],"pages":1,"expect":"p2"}]' | .venv/bin/python tools/steps.py
```

| Setting | Default |
|---|---|
| `HIDAGENT_URL` | `http://127.0.0.1:$PORT` (`PORT` defaults to 8890) |
| `UI_TOKEN` | read from `$HIDAGENT_DATA/ui_token` (default `~/.hidagent/ui_token`); never created by these tools |
| `HIDAGENT_TOOLS_DIR` | `~/hidagent-tools` |

## Safety

- Each `steps.py` step checks edit mode before it runs and again after it. A menu, a share sheet or a closed folder
  stops the run (exit 2 before a step, 4 after, 3 when the step failed or landed on another page).
- When the live screen drops out (the screen app reconnecting), the tools ask again for up to 5 s, then stop
  instead of acting blind.
- Steps marked `"infolder": true` never long-press, because a long press inside an open folder can open an app's menu.
- There is no remove/delete operation. Agents driving these tools must still never choose Remove App, Delete App or
  Remove from Home Screen in a menu.
- `log/` and `sig/` hold screenshots of the phone (app names, notifications). Keep `HIDAGENT_TOOLS_DIR` outside the
  repository and do not commit or publish its contents.
- After the page layout changes, take the `sig` frames again; stale signatures misidentify pages.
- Known gesture limit: [#54](https://github.com/jinwon-int/esp32-iphone-agent/issues/54) (a 3→2 carry turned two
  pages). Fixed in #55 with tests; check on the device before relying on multi-page carries.
