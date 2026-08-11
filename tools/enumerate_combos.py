#!/usr/bin/env python3
"""
Enumerate every button combination on the Scania 2545507 panel and work out
what the ADC can actually see for each one.

The three ladders are independent, but within a line the taps are in series:
grounding a tap shorts out every tap beyond it, so the reading is identical
to holding the nearest-to-connector button alone.  Combinations are therefore
not "unknown" - they resolve to a known, named state, with a known set of
buttons rendered invisible.

Writes button_combinations.csv (all 4096) and prints the summary.
"""

import csv
import itertools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from panel_gui import BUTTONS, CENTRES, IDLE, decode, masked_by, state_name

LINES = "ABC"
SERIES = [62, 162, 312, 702]
PULLDOWN, PULLUP = 2000.0, 1000.0


def adc_for(tap):
    if tap == IDLE:
        r = PULLDOWN
    else:
        r = SERIES[tap] * PULLDOWN / (SERIES[tap] + PULLDOWN)
    return round(1023 * r / (r + PULLUP))


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(here, "button_combinations.csv")

    rows = []
    # every subset of the 12 buttons
    all_btns = [(l, t) for l in range(3) for t in range(4)]
    for n in range(len(all_btns) + 1):
        for combo in itertools.combinations(all_btns, n):
            held = set(combo)
            reported, hidden, adcs = [], [], []
            for line in range(3):
                taps = sorted(t for (l, t) in held if l == line)
                st = taps[0] if taps else IDLE
                reported.append(state_name(line, st))
                adcs.append(adc_for(st))
                # held on this line but electrically invisible
                hidden += [BUTTONS[(line, t)][1] for t in taps[1:]]
            rows.append({
                "n_pressed": n,
                "held": " + ".join(BUTTONS[b][1] for b in sorted(held)) or "(none)",
                "line_A": reported[0], "line_B": reported[1], "line_C": reported[2],
                "adc_A": adcs[0], "adc_B": adcs[1], "adc_C": adcs[2],
                "n_masked": len(hidden),
                "masked": ", ".join(hidden),
                "fully_observable": "yes" if not hidden else "no",
            })

    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    total = len(rows)
    clean = sum(1 for r in rows if r["fully_observable"] == "yes")
    distinct = len({(r["adc_A"], r["adc_B"], r["adc_C"]) for r in rows})

    print("total combinations      : %d" % total)
    print("fully observable        : %d" % clean)
    print("at least one masked     : %d" % (total - clean))
    print("distinct ADC signatures : %d   (5 states ^ 3 lines)" % distinct)
    print()
    print("per line - what each subset resolves to:")
    print("  %-34s %-22s %s" % ("held on one line", "reported", "masked"))
    for line in range(1):
        for n in range(1, 5):
            for combo in itertools.combinations(range(4), n):
                names = [BUTTONS[(line, t)][1].replace("  ", " ") for t in combo]
                st = combo[0]
                print("  %-34s %-22s %s"
                      % (" + ".join(names),
                         BUTTONS[(line, st)][1].replace("  ", " "),
                         ", ".join(BUTTONS[(line, t)][1].replace("  ", " ")
                                   for t in combo[1:]) or "-"))
    print()
    print("verifying every resolved state decodes to itself ...")
    bad = 0
    for tap in list(range(4)) + [IDLE]:
        a = adc_for(tap)
        if decode(a) != tap:
            print("  MISMATCH tap %s adc %d -> %s" % (tap, a, decode(a)))
            bad += 1
    print("  %s" % ("all 5 states round-trip" if not bad else "%d BAD" % bad))
    print()
    print("written: %s" % out)


if __name__ == "__main__":
    main()
