import sys, time, threading, hashlib, json
args = json.loads(sys.argv[1]); sys.argv = ["x"]
exec(open("/root/hidagent-tools/layout.py").read().split("if __name__")[0])
sys.path.insert(0, "/home/gongmyoung/src/esp32-iphone-agent/server")
from hidagent import vision
if not ensure_edit("m-edit"): print("not edit"); sys.exit(2)
rec, stop = [], False
def poll():
    last = None
    while not stop:
        d = api("/api/frame.jpg", raw=True); h = hashlib.md5(d).digest()
        if h != last: rec.append((time.time(), d)); last = h
        time.sleep(0.02)
th = threading.Thread(target=poll); th.start(); time.sleep(0.3)
t0 = time.time()
r = api("/api/drag", {"x1": args["from"][0], "y1": args["from"][1], "x2": args["to"][0], "y2": args["to"][1],
                      "pick": 0.5, "lift": 0.6, "rest": 1.2, "via": [{"x": args["edge"], "y": 0.80, "rest": args["hold"]}]})
time.sleep(1.0); stop = True; th.join()
print("drag ok", r.get("ok"))
base = rec[0][1]; prev = None
for t, d in rec:
    pc = vision.page_change(prev, d) if prev else 0
    cc = vision.cells_changed(base, d)
    print(f"{t-t0:6.2f}s  step={pc:6.1f}  cells_vs_start={cc:4.2f}  page={which_page(d)[0]}")
    prev = d
frame("m-after")
