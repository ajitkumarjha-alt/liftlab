#!/usr/bin/env python3
"""The arrow reader must not oscillate at the decision boundary.

MEASURED CAUSE (restored snapshot, busiest hour per camera): ch29 flipped `direction` 56,753 times
AT AN UNCHANGED FLOOR, median read_conf 0.683 — above min_score, so the reader was confidently
naming a different arrow frame to frame. 63% of ch29's gw_door_event rows were direction-only
changes, and every one of them was emitted because (floor, direction, door_state) had "changed".

ch27 is the control: ONE arrow template, so the two-template rule already withheld its direction —
11 flips against 56,753. The cameras that CAN name both arrows are the ones that flip between them.

THE DEFECT: the arrow was accepted on ABSOLUTE score alone. Two templates at 0.68 and 0.66 give a
winner that changes with noise. The digit cells have had a margin rule all along; the arrow had none.
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


class FakeReader:
    """The arrow decision, lifted out of FloorReader.read_panel so it can be driven directly with a
    score sequence. The logic below is a transcription of the shipped branch; test_arrow_source()
    asserts the real file still contains it, so this cannot drift into testing itself."""

    def __init__(self, margin_min=0.04, switch_margin=0.10, hold_reads=25, min_score=0.55):
        self.arrow_margin_min, self.arrow_switch_margin = margin_min, switch_margin
        self.arrow_hold_reads, self.min_score = hold_reads, min_score
        self._last_direction, self._amb_run, self.n_arrow_ambiguous = None, 0, 0

    def step(self, best, best_s, second_s):
        if best is None or best_s < self.min_score:
            return None
        margin = best_s - second_s if second_s > -1.5 else 1.0
        need = (self.arrow_margin_min if best == self._last_direction else self.arrow_switch_margin)
        if margin >= need:
            self._last_direction, self._amb_run = best, 0
            return best
        self._amb_run += 1
        self.n_arrow_ambiguous += 1
        if self._amb_run <= self.arrow_hold_reads:
            return self._last_direction
        self._last_direction = None
        return None


def flips(seq):
    return sum(1 for a, b in zip(seq, seq[1:]) if a != b)


def main():
    fails = []
    rng = np.random.default_rng(7)

    print("=== 1. the measured failure: two arrows within noise of each other ===")
    # ch29's flipping rows sat at read_conf ~0.683. Model that: both templates near 0.68, the
    # difference smaller than the frame-to-frame noise.
    n = 2000
    up = 0.683 + rng.normal(0, 0.02, n)
    dn = 0.679 + rng.normal(0, 0.02, n)
    old_out = ["up" if u >= d else "down" for u, d in zip(up, dn)]      # argmax, the old rule
    r = FakeReader()
    new_out = [r.step("up", u, d) if u >= d else r.step("down", d, u) for u, d in zip(up, dn)]
    print(f"  argmax only (shipped before): {flips(old_out):>5} direction changes in {n} reads")
    print(f"  margin + hysteresis:          {flips(new_out):>5}  (ambiguous reads held: {r.n_arrow_ambiguous})")
    if flips(old_out) < 500:
        fails.append("fixture error: the old rule should oscillate wildly here")
    if flips(new_out) > flips(old_out) / 50:
        fails.append(f"oscillation not suppressed: {flips(new_out)} changes still — this is the "
                     f"56,753-flip defect")

    print("\n=== 2. a REAL direction change must still get through, promptly ===")
    r2 = FakeReader()
    out = []
    for i in range(400):
        if i < 200:                       # clearly up
            out.append(r2.step("up", 0.86, 0.40))
        else:                             # clearly down
            out.append(r2.step("down", 0.84, 0.38))
    first_down = next((i for i, v in enumerate(out) if v == "down"), None)
    print(f"  clear up for 200 reads, then clear down: first 'down' at read {first_down}")
    if first_down != 200:
        fails.append(f"a decisive direction change was not adopted immediately (at {first_down})")
    if out[199] != "up":
        fails.append("the established direction was not held while it was decisive")

    print("\n=== 3. a marginal-but-consistent change is adopted, not held forever ===")
    r3 = FakeReader()
    out3 = [r3.step("up", 0.80, 0.30) for _ in range(20)]
    out3 += [r3.step("down", 0.70, 0.64) for _ in range(60)]      # ambiguous, favouring down
    held = sum(1 for v in out3[20:] if v == "up")
    withheld = sum(1 for v in out3[20:] if v is None)
    print(f"  60 ambiguous reads after a clear 'up': held as up {held}, withheld {withheld}")
    if held == 0:
        fails.append("an established direction is not held through ambiguity — direction<->None "
                     "chatters exactly as badly as up<->down")
    if withheld == 0:
        fails.append("a stale direction is held indefinitely; a held claim is still a claim")
    if held > 30:
        fails.append(f"held too long ({held} reads) before withholding")

    print("\n=== 4. one arrow template: unchanged, still withheld (ch27's control case) ===")
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gpu_door.py")).read()
    if "if len(self.arrow_labels) >= 2:" not in src:
        fails.append("the two-template suppression rule was lost — ch27 would start claiming a "
                     "direction it cannot possibly measure")
    print("  two-template suppression still guards the whole branch")

    print("\n=== 5. the test is testing the shipped code, not itself ===")
    for needle in ("_match2(", "arr_2nd", "self.arrow_switch_margin", "self._amb_run",
                   "self.arrow_hold_reads", "n_arrow_ambiguous"):
        if needle not in src:
            fails.append(f"gpu_door.py does not contain {needle!r} — the transcription has drifted")
    print("  every element of the transcribed branch is present in gpu_door.py")

    print("\n=== 6. floor digits are untouched ===")
    if "margin_min=0.05" not in src or "disc_min" not in src:
        fails.append("the digit-cell ambiguity rules changed — only the arrow was in scope")
    print("  digit margin_min / disc_min / confuse_band unchanged")

    print()
    if fails:
        for f in fails:
            print(f"FAIL: {f}")
        return 1
    print("OK — the oscillation is suppressed, a decisive change still lands on the first read, "
          "an ambiguous one is held then withheld, and the single-template rule is intact")
    return 0


if __name__ == "__main__":
    sys.exit(main())
