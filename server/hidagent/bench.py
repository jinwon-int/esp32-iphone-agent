"""Pointer bench: how far off the pointer lands, and how long a move takes (#14).

Points at nine spots (corners, edges, centre), finds the real pointer on the live screen after each move and
reports the miss and the move time. Nothing is clicked, so it is safe on any screen; a bright screen (the Home
Screen) makes the pointer easiest to find. Run it once per pointer mode (relative after calibration, absolute)
and paste the table into the issue.
"""

import math
import statistics
import time

NINE = [(x, y) for y in (0.15, 0.5, 0.85) for x in (0.1, 0.5, 0.9)]


def _pct(values: list[float], q: float) -> float:
    """Nearest-rank percentile (q in 0..1) of a non-empty list."""
    ordered = sorted(values)
    return ordered[min(len(ordered), max(1, math.ceil(q * len(ordered)))) - 1]


async def pointer(phone, points=NINE, rounds: int = 1) -> dict:
    """Move to each point ``rounds`` times; return rows, a summary and a Markdown table."""
    rows = []
    for _ in range(rounds):
        for x, y in points:
            started = time.perf_counter()
            await phone.point(x, y)
            move_s = time.perf_counter() - started
            found = await phone.locate(x, y)
            row = {"x": x, "y": y, "move_s": round(move_s, 3), "found": None, "err_x": None, "err_y": None}
            if found:
                fx, fy = found
                row.update(found=[round(fx, 4), round(fy, 4)], err_x=round(fx - x, 4), err_y=round(fy - y, 4))
            rows.append(row)
    seen = [r for r in rows if r["found"]]
    errs = [max(abs(r["err_x"]), abs(r["err_y"])) for r in seen]
    moves = [r["move_s"] for r in rows]
    summary = {
        "mode": "absolute" if getattr(phone, "absolute", False) else "relative",
        "points": len(rows), "seen": len(seen),
        "within_1pct": sum(e <= 0.01 for e in errs),
        "err_max": round(max(errs), 4) if errs else None,
        "err_mean": round(statistics.fmean(errs), 4) if errs else None,
        "move_p50_s": round(_pct(moves, 0.5), 3) if moves else None,
        "move_p90_s": round(_pct(moves, 0.9), 3) if moves else None,
    }
    return {"summary": summary, "rows": rows, "markdown": markdown(summary, rows)}


def markdown(summary: dict, rows: list[dict]) -> str:
    """The result as a Markdown table for the issue: one row per move, then the summary line."""
    lines = ["| target (x, y) | found (x, y) | miss x / y (% of screen) | move |", "|---|---|---|---|"]
    for r in rows:
        found = f'{r["found"][0]:.3f}, {r["found"][1]:.3f}' if r["found"] else "not seen"
        miss = f'{r["err_x"] * 100:+.1f} / {r["err_y"] * 100:+.1f}' if r["found"] else "—"
        lines.append(f'| {r["x"]:.2f}, {r["y"]:.2f} | {found} | {miss} | {r["move_s"] * 1000:.0f} ms |')
    s = summary
    err = f'max {s["err_max"] * 100:.1f} %, mean {s["err_mean"] * 100:.1f} %' if s["err_max"] is not None else "—"
    lines.append(f'\n**{s["mode"]}**: seen {s["seen"]}/{s["points"]}, within ±1 %: {s["within_1pct"]}, miss {err}, '
                 f'move p50 {s["move_p50_s"] * 1000:.0f} ms / p90 {s["move_p90_s"] * 1000:.0f} ms')
    return "\n".join(lines)
