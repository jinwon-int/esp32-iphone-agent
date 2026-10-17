"""Home Screen layout helper for the hidagent server (local operator tool, 2026-10-17, esp32-iphone-agent #35).

Uses only the server API: /api/frame.jpg, /api/longpress, /api/drag, /api/carry, /api/swipe, /api/tap.
Every step saves its frame under log/ so a run can be reviewed afterwards.
"""
import io, json, sys, time, urllib.request
from pathlib import Path
from PIL import Image, ImageChops

TOKEN = open("/home/gongmyoung/.hidagent/ui_token").read().strip()
LOG = Path("/root/hidagent-tools/log")
SIG = Path("/root/hidagent-tools/sig")
SIG.mkdir(exist_ok=True)
EMPTY_BAND = (0.25, 0.795)   # wallpaper between the last icon row's labels and the search pill / page dots


def api(path, body=None, raw=False, timeout=60):
    req = urllib.request.Request("http://127.0.0.1:8890" + path,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"},
                                 method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read() if raw else json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "detail": e.read().decode()[:300]}


def frame(step=None):
    data = api("/api/frame.jpg", raw=True)
    if step:
        (LOG / f"{time.strftime('%H%M%S')}-{step}.jpg").write_bytes(data)
    return data


def gray(data):
    return Image.open(io.BytesIO(data)).convert("L")


def is_edit(data):
    """Edit mode: the dock's phone icon shows a grey minus badge (mean ~115, black minus line) at its top-left."""
    im = gray(data); w, h = im.size
    x0, y0, s = int(0.093 * w), int(0.887 * h), int(0.018 * w)
    px = list(im.crop((x0 - s, y0 - s, x0 + s, y0 + s)).getdata())
    return min(px) < 25 and sum(px) / len(px) > 100


def folder_edit(data):
    """An open folder in edit mode: grey minus badges (black line) at its first-row items' top-left corners."""
    im = gray(data); w, h = im.size; s = int(0.016 * w); hits = 0
    for fx in (0.195, 0.43, 0.655):
        x0, y0 = int(fx * w), int(0.33 * h)
        px = list(im.crop((x0 - s, y0 - s, x0 + s, y0 + s)).getdata())
        hits += min(px) < 25 and 105 < sum(px) / len(px) < 160 and max(px) < 190  # mean 112-150 seen (blur varies)
    return hits >= 2 or (hits >= 1 and folder_clear(data))  # a last-item folder shows one badge only


def folder_clear(data):
    """The rename field's clear button right of an open folder's title: shown only in edit mode."""
    im = gray(data); w, h = im.size; s = int(0.016 * w)
    x0, y0 = int(0.818 * w), int(0.241 * h)
    px = list(im.crop((x0 - s, y0 - s, x0 + s, y0 + s)).getdata())
    return min(px) < 70 and 120 < sum(px) / len(px) < 180 and max(px) < 215


def diff(a, b):
    """Content-band difference (same as vision.page_change)."""
    A, B = gray(a), gray(b); w, h = A.size
    box = (0, int(0.06 * h), w, int(0.78 * h))
    d = ImageChops.difference(A.crop(box).resize((32, 48)), B.crop(box).resize((32, 48))).tobytes()
    return sum(d) / len(d)


def ensure_edit(step="edit"):
    data = frame()
    if is_edit(data):
        return True
    api("/api/longpress", {"x": EMPTY_BAND[0], "y": EMPTY_BAND[1], "seconds": 1.5})
    time.sleep(2.5)  # right after entering edit mode a press did not lift the icon (#35)
    data = frame(step)
    return is_edit(data)


def save_sig(page):
    (SIG / f"p{page}.jpg").write_bytes(frame(f"sig-p{page}"))


def which_page(data):
    scores = {p.stem: diff((SIG / p.name).read_bytes(), data) for p in sorted(SIG.glob("p*.jpg"))}
    best = min(scores, key=scores.get) if scores else None
    return best, {k: round(v, 1) for k, v in scores.items()}


def swipe(direction):  # direction +1: to the next page (finger right->left), -1: previous
    x1, x2 = (0.85, 0.2) if direction > 0 else (0.2, 0.85)
    api("/api/swipe", {"x1": x1, "y1": EMPTY_BAND[1], "x2": x2, "y2": EMPTY_BAND[1]})
    time.sleep(0.6)


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "edit":
        print("edit mode:", ensure_edit())
    elif cmd == "is-edit":
        print("edit mode:", is_edit(frame("check")))
    elif cmd == "sig":
        save_sig(sys.argv[2]); print("saved sig", sys.argv[2])
    elif cmd == "where":
        print(which_page(frame("where")))
    elif cmd == "swipe":
        swipe(int(sys.argv[2])); print(which_page(frame("swipe")))
    elif cmd == "carry":  # carry x1 y1 x2 y2 pages [expected-page, e.g. p2]
        x1, y1, x2, y2, pages = map(float, sys.argv[2:7]); expect = sys.argv[7] if len(sys.argv) > 7 else None
        if not ensure_edit("pre-carry"):
            print("STOP: not in edit mode"); sys.exit(2)
        r = api("/api/carry", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "pages": int(pages)})
        time.sleep(0.8)
        page = which_page(frame("post-carry"))
        print(r, page)
        if expect and page[0] != expect:
            print(f"MISMATCH: expected {expect}, on {page[0]}"); sys.exit(3)
    elif cmd == "drag":  # drag x1 y1 x2 y2 [rest]  (same page; drop on a folder to add into it)
        x1, y1, x2, y2 = map(float, sys.argv[2:6]); rest = float(sys.argv[6]) if len(sys.argv) > 6 else 1.0
        if not ensure_edit("pre-drag"):
            print("STOP: not in edit mode"); sys.exit(2)
        r = api("/api/drag", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "pick": 0.5, "lift": 0.6, "rest": rest})
        time.sleep(0.8)
        print(r, which_page(frame("post-drag")))
