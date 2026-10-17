"""Record a page turn during a carry-style drag and print per-frame motion (how the #54 numbers were measured).

Usage: python tools/measure_turn.py '{"from": [x, y], "to": [x, y], "edge": 0.97, "hold": 1.5}'
Needs the hidagent package importable (run from server/ with its virtualenv) and edit mode reachable.
"""
import hashlib
import json
import sys
import threading
import time

from layout import api, ensure_edit, frame, which_page

from hidagent import vision


def measure(args):
    if not ensure_edit("m-edit"):
        print("not edit")
        sys.exit(2)
    rec, stop = [], threading.Event()

    def poll():
        last = None
        while not stop.is_set():
            d = api("/api/frame.jpg", raw=True)
            h = hashlib.md5(d).digest()
            if h != last:
                rec.append((time.time(), d))
                last = h
            time.sleep(0.02)

    th = threading.Thread(target=poll)
    th.start()
    time.sleep(0.3)
    t0 = time.time()
    r = api("/api/drag", {"x1": args["from"][0], "y1": args["from"][1], "x2": args["to"][0], "y2": args["to"][1],
                          "pick": 0.5, "lift": 0.6, "rest": 1.2,
                          "via": [{"x": args["edge"], "y": 0.80, "rest": args["hold"]}]})
    time.sleep(1.0)
    stop.set()
    th.join()
    print("drag ok", r.get("ok"))
    base = rec[0][1]
    prev = None
    for t, d in rec:
        pc = vision.page_change(prev, d) if prev else 0
        cc = vision.cells_changed(base, d)
        print(f"{t - t0:6.2f}s  step={pc:6.1f}  cells_vs_start={cc:4.2f}  page={which_page(d)[0]}")
        prev = d
    frame("m-after")


if __name__ == "__main__":
    measure(json.loads(sys.argv[1]))
