"""Run a list of Home Screen steps (JSON on stdin); stop at the first failure. Uses layout.py helpers.

Every step first checks that edit mode (or, with "infolder", an open folder's edit mode) is visible, and afterwards
that it still is: a menu, a share sheet or a closed folder stops the run (exit 2 before a step, 4 after it, 3 when
the step itself failed or landed on an unexpected page).
"""
import json
import sys
import time

from layout import api, ensure_edit, folder_clear, folder_edit, frame, is_edit, swipe, which_page


def run(steps):
    for n, s in enumerate(steps, 1):
        op = s["op"]
        if s.get("infolder"):  # an open folder: never long-press to re-enter edit mode here
            pre = frame(f"s{n}-folder-check")
            if not (folder_edit(pre) or (op == "fswipe" and folder_clear(pre))):  # an empty folder page has no badges
                print(f"[{n}] STOP: the open folder is not in edit mode")
                sys.exit(2)
        elif not ensure_edit(f"s{n}-edit"):
            print(f"[{n}] STOP: not in edit mode")
            sys.exit(2)
        if op == "carry":
            r = api("/api/carry", {"x1": s["from"][0], "y1": s["from"][1], "x2": s["to"][0], "y2": s["to"][1],
                                   "pages": s["pages"]})
            time.sleep(0.8)
        elif op == "drag":
            body = {"x1": s["from"][0], "y1": s["from"][1], "x2": s["to"][0], "y2": s["to"][1],
                    "pick": 0.5, "lift": 0.6, "rest": s.get("rest", 1.2)}
            if "via" in s:
                body["via"] = [{"x": v[0], "y": v[1], "rest": v[2]} for v in s["via"]]
            r = api("/api/drag", body)
            time.sleep(0.8)
        elif op == "tap":
            r = api("/api/tap", {"x": s["at"][0], "y": s["at"][1]})
            time.sleep(1.2)
        elif op == "fswipe":  # inside an open folder, to its first page (it may open on the empty edit-mode page)
            for _ in range(s.get("times", 4)):  # extra swipes on the first page do nothing
                r = api("/api/swipe", {"x1": 0.15, "y1": 0.69, "x2": 0.85, "y2": 0.69})
                time.sleep(0.9)
        elif op == "swipe":
            swipe(s["dir"])
            r = {"ok": True}
        else:
            print(f"[{n}] unknown op {op}")
            sys.exit(2)
        data = frame(f"s{n}-{op}-{s.get('name', '')}")
        pg = which_page(data)[0]
        ok = r.get("ok", False) and (s.get("expect") in (None, pg))
        if op == "fswipe":
            still = folder_edit(data)  # the first page must show the badges
        elif s.get("opens_folder"):
            still = folder_edit(data) or folder_clear(data)
        else:
            still = is_edit(data)  # a menu or a share sheet hides both
        if not still:
            print(f"[{n}] {op} {s.get('name', '')}: STOP unexpected screen (edit mode not visible after the step)")
            sys.exit(4)
        print(f"[{n}] {op} {s.get('name', '')}: ok={r.get('ok')} page={pg} expect={s.get('expect')} "
              f"{'' if ok else 'FAIL ' + str(r)[:200]}", flush=True)
        if not ok:
            sys.exit(3)
    print("all steps done")


if __name__ == "__main__":
    run(json.load(sys.stdin))
