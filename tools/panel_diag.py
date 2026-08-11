#!/usr/bin/env python3
"""
Scania 2545507 panel  -  ladder diagnostics

Converts each line's raw ADC back into the actual resistance the panel
is presenting, which maps directly onto a physical node in the ladder.
That turns "a button is stuck" into "node C3 is shorted to ground".

    python panel_diag.py

Uses the same panel_reader.ino firmware as panel_gui.py.
"""

import os
import sys
import time
import queue
import tkinter as tk
from tkinter import ttk
from collections import deque, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import serial.tools.list_ports
from panel_gui import (SerialWorker, BUTTONS, blend, rr_points,
                       center_window, BG, CARD, CARD_HI, EDGE, WELL, TXT,
                       TXT_DIM, FAINT, INK, ACCENT, HILITE, LEAF, GOOD,
                       ALERT, FONT, MONO)

# --------------------------------------------------------------------------
# the ladder, as reverse-engineered
# --------------------------------------------------------------------------
SERIES   = [62, 162, 312, 702]      # cumulative ohms from J1 pin to each tap
PULLDOWN = 2000.0                   # R13 / R14 / R15
NODE     = ["1", "2", "3", "4"]
LINE_ID  = ["A", "B", "C"]
LINE_PIN = ["J1.2 / A0", "J1.3 / A1", "J1.4 / A2"]
TRACE_COL  = [ACCENT, LEAF, HILITE]      # amber / green / orange
TRACE_DASH = [None, (7, 4), (2, 3)]      # hue alone cannot separate
                                         # three greens; dash does


def par(r, p=PULLDOWN):
    return r * p / (r + p)


def expected_for(pd):
    """tap0..3 resistances, then idle, for a given pulldown"""
    return [par(s, pd) for s in SERIES] + [pd]


EXPECTED = expected_for(PULLDOWN)


def leak_for(pd):
    """what parallel resistance drags a healthy 2k pulldown down to pd"""
    if pd >= PULLDOWN:
        return float("inf")
    return PULLDOWN * pd / (PULLDOWN - pd)


def cluster(vals, tol=7, floor=3):
    """group raw adc samples into stable levels -> [(mean, count)]"""
    if not vals:
        return []
    vals = sorted(vals)
    groups, cur = [], [vals[0]]
    for v in vals[1:]:
        if v - cur[-1] <= tol:
            cur.append(v)
        else:
            groups.append(cur)
            cur = [v]
    groups.append(cur)
    out = [(sum(g) / len(g), len(g)) for g in groups if len(g) >= floor]
    return sorted(out, key=lambda t: t[0])


def adc_to_ohms(adc, rpu):
    if adc >= 1022:
        return float("inf")
    if adc <= 0:
        return 0.0
    return rpu * adc / (1023.0 - adc)


def ohms_to_adc(r, rpu):
    if r == float("inf"):
        return 1023
    return 1023.0 * r / (r + rpu)


def fmt_ohms(r):
    if r == float("inf"):
        return "  open"
    if r >= 10000:
        return "%5.1fk" % (r / 1000.0)
    return "%6.1f" % r


def classify(r, pd=PULLDOWN, tol=0.16):
    """resistance -> (kind, tap_index_or_None) against a given pulldown"""
    if r == float("inf"):
        return ("open", None)
    if r < 20:
        return ("gnd", None)
    exp = expected_for(pd)
    for k, e in enumerate(exp[:4]):
        if abs(r - e) / e <= tol:
            return ("tap", k)
    if abs(r - pd) / pd <= tol:
        return ("idle", None)
    return ("odd", None)


# --------------------------------------------------------------------------
class Diag:
    def __init__(self, root):
        self.root = root
        root.title("Scania 2545507  ·  Ladder Diagnostics")
        root.configure(bg=BG)
        center_window(root, 1150, 880)

        self.q = queue.Queue()
        self.worker = None
        self.rpu = 1000.0
        self.values = [682, 682, 682]
        self.hist = [deque(maxlen=420) for _ in range(3)]
        self.capturing = False
        self.pd_fit = [PULLDOWN]*3
        self.seen = [[] for _ in range(3)]
        self.hold = [(None, 0)] * 3

        self._header()
        self._cards()
        self._scope()
        self._report()

        self.refresh_ports()
        self.say("Ladder model loaded.  Expected resistance at the connector pin:", "hd")
        for k, e in enumerate(EXPECTED[:4]):
            self.say("    node %s  tap %-5s  %s ohm   ->  ADC %4.0f"
                     % (NODE[k], SERIES[k], fmt_ohms(e), ohms_to_adc(e, 1000)), "dim")
        self.say("    idle       (2k pulldown)  %s ohm   ->  ADC %4.0f"
                 % (fmt_ohms(PULLDOWN), ohms_to_adc(PULLDOWN, 1000)), "dim")
        self.say("")
        self.say("Connect, keep hands OFF the panel, then press IDLE TEST.", "hd")

        root.after(30, self._tick)
        root.protocol("WM_DELETE_WINDOW", self._close)

    # ---------------- widgets ----------------
    def _btn(self, parent, text, cmd, bg, fg):
        b = tk.Label(parent, text=text, bg=bg, fg=fg, font=(FONT, 9, "bold"),
                     padx=14, pady=7, cursor="hand2")
        b.bind("<Button-1>", lambda e: cmd())
        b.bind("<Enter>", lambda e: b.configure(bg=blend(b._base, "#ffffff", .12)))
        b.bind("<Leave>", lambda e: b.configure(bg=b._base))
        b._base = bg
        return b

    def _header(self):
        top = tk.Frame(self.root, bg=BG)
        top.pack(fill="x", padx=20, pady=(16, 4))
        tk.Label(top, text="LADDER DIAGNOSTICS", bg=BG, fg=TXT,
                 font=(FONT, 16, "bold")).pack(side="left")
        tk.Label(top, text="   resistance-domain fault finder", bg=BG,
                 fg=TXT_DIM, font=(FONT, 10)).pack(side="left", pady=(5, 0))
        self.link = tk.Label(top, text="● OFFLINE", bg=BG, fg=TXT_DIM,
                             font=(FONT, 10, "bold"))
        self.link.pack(side="right", pady=(4, 0))

        bar = tk.Frame(self.root, bg=CARD, highlightthickness=1,
                       highlightbackground=EDGE)
        bar.pack(fill="x", padx=20, pady=(4, 12))
        inner = tk.Frame(bar, bg=CARD)
        inner.pack(fill="x", padx=14, pady=11)

        tk.Label(inner, text="PORT", bg=CARD, fg=TXT_DIM,
                 font=(FONT, 9, "bold")).pack(side="left", padx=(0, 8))
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("D.TCombobox", fieldbackground=CARD_HI, background=CARD_HI,
                     foreground=TXT, arrowcolor=ACCENT, bordercolor=EDGE,
                     lightcolor=EDGE, darkcolor=EDGE, padding=6)
        st.map("D.TCombobox",
               fieldbackground=[("readonly", CARD_HI)],
               background=[("readonly", CARD_HI)],
               foreground=[("readonly", TXT)],
               selectbackground=[("readonly", CARD_HI)],
               selectforeground=[("readonly", TXT)])
        self.root.option_add("*TCombobox*Listbox.background", CARD_HI)
        self.root.option_add("*TCombobox*Listbox.foreground", TXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.cb = ttk.Combobox(inner, width=34, state="readonly",
                               style="D.TCombobox", font=(MONO, 10),
                               takefocus=False)
        self.cb.pack(side="left")
        self._btn(inner, "REFRESH", self.refresh_ports, CARD_HI, TXT).pack(
            side="left", padx=8)
        self.conn = self._btn(inner, "CONNECT", self.toggle, ACCENT, INK)
        self.conn.pack(side="left")

        tk.Label(inner, text="PULL-UP Ω", bg=CARD, fg=TXT_DIM,
                 font=(FONT, 9, "bold")).pack(side="left", padx=(22, 6))
        self.rpu_var = tk.StringVar(value="1000")
        e = tk.Entry(inner, textvariable=self.rpu_var, width=7, bg=CARD_HI,
                     fg=TXT, insertbackground=TXT, relief="flat",
                     font=(MONO, 10), justify="center")
        e.pack(side="left", ipady=5)
        e.bind("<Return>", lambda ev: self._set_rpu())
        self._btn(inner, "CAL FROM A", lambda: self.calibrate(0),
                  CARD_HI, TXT_DIM).pack(side="left", padx=(6, 0))
        self._btn(inner, "B", lambda: self.calibrate(1),
                  CARD_HI, TXT_DIM).pack(side="left", padx=3)
        self._btn(inner, "C", lambda: self.calibrate(2),
                  CARD_HI, TXT_DIM).pack(side="left")

    def _cards(self):
        wrap = tk.Frame(self.root, bg=BG)
        wrap.pack(fill="x", padx=20)
        self.cards = []
        for i in range(3):
            c = tk.Frame(wrap, bg=CARD, highlightthickness=1,
                         highlightbackground=EDGE)
            c.pack(side="left", expand=True, fill="both",
                   padx=(0 if i == 0 else 10, 0))
            hd = tk.Frame(c, bg=CARD)
            hd.pack(fill="x", padx=12, pady=(9, 0))
            tk.Label(hd, text="LINE " + LINE_ID[i], bg=CARD, fg=TRACE_COL[i],
                     font=(FONT, 11, "bold")).pack(side="left")
            tk.Label(hd, text="  " + LINE_PIN[i], bg=CARD, fg=TXT_DIM,
                     font=(MONO, 8)).pack(side="left", pady=(3, 0))
            badge = tk.Label(hd, text="—", bg=CARD_HI, fg=TXT_DIM,
                             font=(FONT, 8, "bold"), padx=8, pady=2)
            badge.pack(side="right")

            ohm = tk.Label(c, text="—", bg=CARD, fg=ACCENT, font=(MONO, 22, "bold"))
            ohm.pack(anchor="w", padx=12, pady=(4, 0))
            adc = tk.Label(c, text="adc —", bg=CARD, fg=TXT_DIM, font=(MONO, 9))
            adc.pack(anchor="w", padx=12)
            node = tk.Label(c, text="", bg=CARD, fg=TXT_DIM,
                            font=(FONT, 10, "bold"))
            node.pack(anchor="w", padx=12, pady=(2, 10))
            self.cards.append((ohm, adc, node, badge))

    def _scope(self):
        wrap = tk.Frame(self.root, bg=CARD, highlightthickness=1,
                        highlightbackground=EDGE)
        wrap.pack(fill="x", padx=20, pady=12)
        hd = tk.Frame(wrap, bg=CARD)
        hd.pack(fill="x", padx=14, pady=(9, 0))
        tk.Label(hd, text="TRACE", bg=CARD, fg=TXT_DIM,
                 font=(FONT, 9, "bold")).pack(side="left")
        tk.Label(hd, text="   bands = expected tap windows",
                 bg=CARD, fg=FAINT, font=(FONT, 8)).pack(side="left")
        for i in range(3):
            tk.Label(hd, text="■ " + LINE_ID[i], bg=CARD, fg=TRACE_COL[i],
                     font=(FONT, 9, "bold")).pack(side="right", padx=4)
        self.scope = tk.Canvas(wrap, height=190, bg=WELL,
                               highlightthickness=0)
        self.scope.pack(fill="x", padx=14, pady=(6, 12))

    def _report(self):
        wrap = tk.Frame(self.root, bg=BG)
        wrap.pack(fill="both", expand=True, padx=20, pady=(0, 16))

        side = tk.Frame(wrap, bg=CARD, highlightthickness=1,
                        highlightbackground=EDGE, width=210)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        tk.Label(side, text="TESTS", bg=CARD, fg=TXT_DIM,
                 font=(FONT, 9, "bold")).pack(anchor="w", padx=14, pady=(12, 8))
        self._btn(side, "IDLE  TEST", self.idle_test, ACCENT, INK).pack(
            fill="x", padx=14, pady=3)
        self.cap_btn = self._btn(side, "START  CAPTURE", self.toggle_capture,
                                 CARD_HI, TXT)
        self.cap_btn.pack(fill="x", padx=14, pady=3)
        self._btn(side, "ANALYSE", self.analyse, GOOD, INK).pack(
            fill="x", padx=14, pady=3)
        self._btn(side, "CLEAR LOG", lambda: self.rep.delete("1.0", "end"),
                  CARD_HI, TXT_DIM).pack(fill="x", padx=14, pady=3)
        tk.Label(side, text="1  Idle test with hands off\n"
                           "2  Capture, then press every\n"
                           "    button on the panel\n"
                           "3  Analyse",
                 bg=CARD, fg=FAINT, font=(FONT, 8), justify="left").pack(
            anchor="w", padx=14, pady=(14, 0))

        rc = tk.Frame(wrap, bg=CARD, highlightthickness=1,
                      highlightbackground=EDGE)
        rc.pack(side="left", fill="both", expand=True, padx=(10, 0))
        self.rep = tk.Text(rc, bg=WELL, fg=TXT, font=(MONO, 10), bd=0,
                           highlightthickness=0, wrap="none",
                           insertbackground=TXT)
        self.rep.pack(fill="both", expand=True, padx=12, pady=12)
        for tag, col in (("ok", GOOD), ("bad", ALERT), ("warn", HILITE),
                         ("hd", ACCENT), ("dim", FAINT)):
            self.rep.tag_configure(tag, foreground=col)

    def say(self, text="", tag=None):
        self.rep.insert("end", text + "\n", tag or ())
        self.rep.see("end")

    # ---------------- serial ----------------
    def refresh_ports(self):
        ports = list(serial.tools.list_ports.comports())
        self._devs = [p.device for p in ports]
        self.cb["values"] = ["%-6s %s" % (p.device, (p.description or "")[:34])
                             for p in ports] or ["no ports"]
        self.cb.current(0)

    def toggle(self):
        if self.worker and self.worker.is_alive():
            self.worker.stop.set()
            self.worker = None
            return
        i = self.cb.current()
        if i < 0 or not getattr(self, "_devs", None):
            self.say("no port selected", "bad")
            return
        self.worker = SerialWorker(self._devs[i], self.q)
        self.worker.start()
        self.conn.configure(text="DISCONNECT", bg=ALERT, fg="#3B1610")
        self.conn._base = ALERT

    def _set_rpu(self):
        try:
            self.rpu = max(1.0, float(self.rpu_var.get()))
            self.say("pull-up set to %.0f ohm" % self.rpu, "hd")
        except ValueError:
            pass

    def calibrate(self, line):
        """assume the given line is idle (2k pulldown) and back out the pull-up"""
        adc = self.values[line]
        if adc <= 0 or adc >= 1022:
            self.say("line %s reads %d - cannot calibrate from it"
                     % (LINE_ID[line], adc), "bad")
            return
        rpu = PULLDOWN * (1023.0 - adc) / adc
        self.rpu = rpu
        self.rpu_var.set("%.0f" % rpu)
        self.say("calibrated pull-up = %.0f ohm from line %s idling at adc %d"
                 % (rpu, LINE_ID[line], adc), "hd")
        if not (700 < rpu < 1400):
            self.say("  that is far from 1k - either the resistor is not 1k "
                     "or line %s was not idle" % LINE_ID[line], "warn")

    # ---------------- tests ----------------
    def idle_test(self):
        self.say("")
        self.say("── IDLE TEST ─────────────────────────────────", "hd")
        self.say("pull-up %.0f ohm,  hands off assumed" % self.rpu, "dim")
        any_bad = False
        for i in range(3):
            adc = self.values[i]
            r = adc_to_ohms(adc, self.rpu)
            head = "  line %s  adc %4d   %s ohm   " % (LINE_ID[i], adc, fmt_ohms(r))

            if r == float("inf"):
                any_bad = True
                self.pd_fit[i] = PULLDOWN
                self.say(head + "OPEN CIRCUIT", "bad")
                self.say("      no path to ground: check J1 pin %d and the harness."
                         % (i + 2), "dim")
                continue
            if r < 20:
                any_bad = True
                self.pd_fit[i] = PULLDOWN
                self.say(head + "SHORTED TO GROUND", "bad")
                continue

            # resting resistance IS the effective pulldown on this line
            self.pd_fit[i] = r
            if abs(r - PULLDOWN) / PULLDOWN <= 0.12:
                self.say(head + "OK  (2k pulldown present)", "ok")
                continue

            any_bad = True
            self.say(head + "PULLDOWN LOW", "bad")
            self.say("      effective pulldown %s ohm, should be 2000."
                     % fmt_ohms(r), "warn")
            self.say("      implies a stray %s ohm leak in parallel with it."
                     % fmt_ohms(leak_for(r)), "warn")

            # is it instead consistent with a stuck tap on a healthy 2k line?
            best, berr = None, 1e9
            for k, e in enumerate(expected_for(PULLDOWN)[:4]):
                err = abs(r - e) / e
                if err < berr:
                    best, berr = k, err
            self.say("      two candidate causes:", "hd")
            self.say("        A  leak / contamination to ground on this line "
                     "(all 4 switches would still work,", "dim")
            self.say("           but every reading is compressed toward zero)",
                     "dim")
            if berr < 0.25:
                self.say("        B  node %s stuck closed  (fit %+.0f%% - %s)"
                         % (NODE[best], berr * 100,
                            "plausible" if berr < 0.08 else "poor fit"),
                         "dim")
                self.say("           would make '%s' read as held and kill %s"
                         % (BUTTONS[(i, best)][1],
                            ", ".join(BUTTONS[(i, k)][1]
                                      for k in range(best + 1, 4)) or "nothing"),
                         "dim")
            else:
                self.say("        B  stuck tap: no node fits (closest is %+.0f%%)"
                         % (berr * 100), "dim")
            self.say("      -> run CAPTURE then ANALYSE to decide between them.",
                     "hd")
        if not any_bad:
            self.say("  all three lines idle correctly.", "ok")
        self.say("")

    def toggle_capture(self):
        self.capturing = not self.capturing
        if self.capturing:
            self.seen = [[] for _ in range(3)]
            self.cap_btn.configure(text="STOP  CAPTURE", bg=HILITE, fg=INK)
            self.cap_btn._base = HILITE
            self.say("")
            self.say("── CAPTURE RUNNING ───────────────────────────", "hd")
            self.say("press every button on the panel, then STOP and ANALYSE.",
                     "dim")
        else:
            self.cap_btn.configure(text="START  CAPTURE", bg=CARD_HI, fg=TXT)
            self.cap_btn._base = CARD_HI
            self.say("capture stopped.", "dim")

    @staticmethod
    def _fit(levels, pd, rpu):
        """assign observed levels to taps/idle for a given pulldown.
        returns (assignment, mean_abs_rel_error, n_matched)"""
        exp = expected_for(pd)
        names = list(range(4)) + [None]          # None = idle
        out, errs = {}, []
        for mean_adc, n in levels:
            r = adc_to_ohms(mean_adc, rpu)
            best, berr = None, 1e9
            for idx, e in zip(names, exp):
                err = abs(r - e) / e
                if err < berr:
                    best, berr = idx, err
            out[best] = (mean_adc, r, berr, n)
            errs.append(berr)
        return out, (sum(errs) / len(errs) if errs else 1e9), len(out)

    def analyse(self):
        self.say("")
        self.say("── ANALYSIS ──────────────────────────────────", "hd")
        for i in range(3):
            self.say("")
            self.say("  LINE %s   %s" % (LINE_ID[i], LINE_PIN[i]), "hd")
            levels = cluster(self.seen[i])
            if not levels:
                self.say("    nothing captured", "dim")
                continue

            self.say("    observed levels:", "dim")
            for mean_adc, n in levels:
                self.say("        adc %4.0f   %s ohm   n=%d"
                         % (mean_adc, fmt_ohms(adc_to_ohms(mean_adc, self.rpu)),
                            n), "dim")

            # resting = the level held for the most samples
            rest_adc, rest_n = max(levels, key=lambda t: t[1])
            pd_fit = adc_to_ohms(rest_adc, self.rpu)

            fitA, errA, nA = self._fit(levels, pd_fit, self.rpu)     # leak model
            fitB, errB, nB = self._fit(levels, PULLDOWN, self.rpu)   # healthy 2k

            leak = abs(pd_fit - PULLDOWN) / PULLDOWN > 0.12
            use, err, model = ((fitA, errA, "A") if leak and errA < errB
                               else (fitB, errB, "B"))

            self.say("")
            self.say("    model fit:  healthy-2k %.1f%%   fitted-pulldown "
                     "(%s ohm) %.1f%%" % (errB * 100, fmt_ohms(pd_fit).strip(),
                                          errA * 100), "dim")

            exp = expected_for(pd_fit if model == "A" else PULLDOWN)
            self.say("")
            for k in range(4):
                name = BUTTONS[(i, k)][1]
                if k in use:
                    _, r, e, n = use[k]
                    tag = "ok" if e < 0.12 else "warn"
                    self.say("    node %s  %-20s seen  %s ohm  (exp %s, %+.0f%%)"
                             "  n=%d" % (NODE[k], name, fmt_ohms(r),
                                         fmt_ohms(exp[k]), e * 100, n), tag)
                else:
                    self.say("    node %s  %-20s NOT MATCHED"
                             % (NODE[k], name), "bad")
            if None in use:
                _, r, e, n = use[None]
                self.say("    idle                       %s ohm  (%+.0f%%)  n=%d"
                         % (fmt_ohms(r), e * 100, n),
                         "ok" if e < 0.12 else "warn")
            else:
                self.say("    idle                       NOT MATCHED", "bad")

            self.say("")
            matched = [k for k in range(4) if k in use]
            if not leak and len(matched) == 4 and None in use:
                self.say("    VERDICT: pulldown 2k, all four taps present - "
                         "line healthy.", "ok")
            elif leak and len(matched) == 4:
                self.say("    VERDICT: ALL FOUR SWITCHES WORK.", "ok")
                self.say("    The fault is a %s ohm leak to ground on this line,"
                         % fmt_ohms(leak_for(pd_fit)).strip(), "bad")
                self.say("    dragging the 2k pulldown down to %s ohm and "
                         "compressing" % fmt_ohms(pd_fit).strip(), "bad")
                self.say("    every reading. Nothing is stuck - the decoder is "
                         "just", "bad")
                self.say("    matching the wrong windows.", "bad")
                self.say("    Fix the leak, or set the pulldown to %s in the "
                         "decoder." % fmt_ohms(pd_fit).strip(), "hd")
            elif leak:
                self.say("    VERDICT: pulldown is low (%s ohm) AND %d tap(s) "
                         "unmatched." % (fmt_ohms(pd_fit).strip(),
                                         4 - len(matched)), "bad")
                self.say("    Suspect a stuck node rather than a clean leak.",
                         "warn")
            else:
                self.say("    VERDICT: %d tap(s) never registered - check those "
                         "switch contacts." % (4 - len(matched)), "warn")
        self.say("")

    # ---------------- loop ----------------
    def _tick(self):
        while True:
            try:
                kind, payload = self.q.get_nowait()
            except queue.Empty:
                break
            if kind == "data":
                self.values = list(payload)
                for i in range(3):
                    self.hist[i].append(payload[i])
                self._accumulate()
            elif kind == "open":
                self.link.configure(text="● LINKED  " + payload, fg=GOOD)
            elif kind == "info":
                self.say("device: " + payload, "dim")
            elif kind == "error":
                self.say(payload, "bad")
                self.link.configure(text="● ERROR", fg=ALERT)
            elif kind == "closed":
                self.link.configure(text="● OFFLINE", fg=TXT_DIM)
                self.conn.configure(text="CONNECT", bg=ACCENT, fg=INK)
                self.conn._base = ACCENT

        for i in range(3):
            adc = self.values[i]
            r = adc_to_ohms(adc, self.rpu)
            kind, tap = classify(r, self.pd_fit[i])
            ohm, adcl, node, badge = self.cards[i]
            ohm.configure(text=fmt_ohms(r).strip() + " Ω")
            adcl.configure(text="adc %4d" % adc)
            if kind == "idle":
                if abs(self.pd_fit[i] - PULLDOWN) / PULLDOWN > 0.12:
                    node.configure(text="at rest - pulldown low", fg=ALERT)
                    badge.configure(text="LEAK", bg=CARD_HI, fg=ALERT)
                else:
                    node.configure(text="idle", fg=TXT_DIM)
                    badge.configure(text="IDLE", bg=CARD_HI, fg=GOOD)
            elif kind == "tap":
                node.configure(text="node %s · %s" % (NODE[tap],
                                                      BUTTONS[(i, tap)][1]),
                               fg=TRACE_COL[i])
                badge.configure(text="NODE " + NODE[tap], bg=CARD_HI,
                                fg=TRACE_COL[i])
            elif kind == "open":
                node.configure(text="open circuit", fg=ALERT)
                badge.configure(text="OPEN", bg=CARD_HI, fg=ALERT)
            elif kind == "gnd":
                node.configure(text="shorted to ground", fg=ALERT)
                badge.configure(text="SHORT", bg=CARD_HI, fg=ALERT)
            else:
                node.configure(text="unclassified", fg=HILITE)
                badge.configure(text="ODD", bg=CARD_HI, fg=HILITE)

        self._draw_scope()
        self.root.after(30, self._tick)

    def _accumulate(self):
        """record raw adc of stable states while capture is running"""
        for i in range(3):
            v = self.values[i]
            prev, n = self.hold[i]
            n = n + 1 if prev is not None and abs(v - prev) <= 4 else 1
            self.hold[i] = (v, n)
            if self.capturing and n >= 3:
                self.seen[i].append(v)

    def _draw_scope(self):
        c = self.scope
        c.delete("all")
        w = c.winfo_width() or 1000
        h = 190

        def y(adc):
            return h - 8 - (h - 20) * adc / 1023.0

        for k, e in enumerate(expected_for(PULLDOWN)[:4]):
            a = ohms_to_adc(e * 1.16, self.rpu)
            b = ohms_to_adc(e * 0.84, self.rpu)
            c.create_rectangle(0, y(a), w, y(b), fill="#1A1509", outline="")
            c.create_text(6, y(ohms_to_adc(e, self.rpu)), anchor="w",
                          text="node %s" % NODE[k], fill=FAINT,
                          font=(MONO, 7))
        yi = y(ohms_to_adc(PULLDOWN, self.rpu))
        c.create_line(0, yi, w, yi, fill=EDGE, dash=(4, 4))
        c.create_text(6, yi - 8, anchor="w", text="idle 2k", fill=FAINT,
                      font=(MONO, 7))

        n = max(len(hh) for hh in self.hist) or 1
        step = max(1.0, w / 420.0)
        for i in range(3):
            pts = []
            for j, v in enumerate(self.hist[i]):
                pts += [j * step, y(v)]
            if len(pts) >= 4:
                kw = {"dash": TRACE_DASH[i]} if TRACE_DASH[i] else {}
                c.create_line(pts, fill=TRACE_COL[i], width=2, smooth=False, **kw)

    def _close(self):
        if self.worker:
            self.worker.stop.set()
            time.sleep(0.2)
        self.root.destroy()


if __name__ == "__main__":
    r = tk.Tk()
    Diag(r)
    r.mainloop()
