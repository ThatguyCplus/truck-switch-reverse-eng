#!/usr/bin/env python3
"""
Scania 2545507 steering-wheel panel  -  live button monitor

Pairs with panel_reader.ino.  Reads the three analog ladder lines,
decodes which of the twelve buttons is down, and lights up a drawing
of the real panel.

    pip install pyserial
    python panel_gui.py
"""

import queue
import threading
import time
import tkinter as tk
from tkinter import ttk
from collections import deque

import serial
import serial.tools.list_ports

# --------------------------------------------------------------------------
# palette  -  Honda Insight instrument cluster
#   near-black glass, amber VFD numerals, green LCD readout,
#   orange arc segments, single red warning lamp
# --------------------------------------------------------------------------
GLASS     = "#0A0A08"      # cluster face
VFD_AMBER = "#FFB300"      # speedometer digits
VFD_GOLD  = "#FFD24A"      # amber highlight
LCD_GREEN = "#7DE046"      # mpg / CHRG readout
ARC_ORANGE= "#E8721C"      # tachometer + fuel segments
LAMP_RED  = "#E5342A"      # warning lamp

BG        = GLASS
CARD      = "#15150F"      # instrument bezel
CARD_HI   = "#201E14"      # raised fill
EDGE      = "#3A3524"
WELL      = "#060604"      # unlit LCD glass: log, scope, meter track
SEG_OFF   = "#191710"      # an unlit bar segment
TXT       = "#EDE7D2"      # silkscreened labels
TXT_DIM   = "#8D8564"
FAINT     = "#57512F"
INK       = GLASS          # dark text on a lit fill

ACCENT    = VFD_AMBER      # primary interactive / live value
HILITE    = ARC_ORANGE     # attention, third series
LEAF      = LCD_GREEN      # second series
GOOD      = LCD_GREEN      # healthy / at rest
ALERT     = LAMP_RED

FONT   = "Segoe UI"
MONO   = "Consolas"


def _work_area():
    """Primary monitor work area (excludes the taskbar), as (x, y, w, h).

    winfo_screenwidth() reports the whole virtual desktop on a multi-monitor
    setup, so centring against it drops the window on the seam between two
    screens.  Windows reports this API in the same virtualised coordinates
    Tk uses, so the two agree whether or not the process is DPI aware.
    """
    try:
        import ctypes
        from ctypes import wintypes
        r = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0,
                                                      ctypes.byref(r), 0):
            return r.left, r.top, r.right - r.left, r.bottom - r.top
    except Exception:
        pass
    return None


def center_window(win, w, h):
    """place the window in the middle of the primary monitor, clamped to fit"""
    win.update_idletasks()
    area = _work_area()
    if area:
        ax, ay, aw, ah = area
    else:
        ax, ay = 0, 0
        aw, ah = win.winfo_screenwidth(), win.winfo_screenheight()
    w = min(w, aw - 40)
    h = min(h, ah - 40)
    x = ax + (aw - w) // 2
    y = ay + max(0, (ah - h) // 2 - 10)
    win.geometry("%dx%d+%d+%d" % (w, h, x, y))

# --------------------------------------------------------------------------
# decode tables  -  derived from the reverse-engineered netlist
#
# Each line: 62 / 100 / 150 / 390 ohm ladder taps, 2k pulldown, 1k pullup
# to 5V at the Arduino.  Expected ADC centres:
#     tap1  60R   -> 58     tap3  270R  -> 217
#     tap2 150R   -> 133    tap4  520R  -> 350
#     idle 2k     -> 682    open        -> 1023
# --------------------------------------------------------------------------
IDLE, FAULT, UNKNOWN = -1, -2, -3

CENTRES = [58, 133, 217, 350, 682]      # tap0..tap3, then idle
#  Acceptance radius around each centre.  Readings that land in the gaps
#  BETWEEN bands are reported as UNKNOWN rather than snapped to whichever
#  window happens to be nearest - an out-of-range value is a real state and
#  should be named, not guessed at.
TOL      = [30,  30,  30,  45,  60]
OPEN_MIN = 950                          # at or above this the line is open

LINE_LABELS = ["A  ·  J1.2  ·  A0", "B  ·  J1.3  ·  A1", "C  ·  J1.4  ·  A2"]

# (line, tap) -> (button id, display name)
BUTTONS = {
    (0, 0): ("cruise_minus_big",   "CRUISE  −  BIG"),
    (0, 1): ("cruise_minus_small", "CRUISE  −  SMALL"),
    (0, 2): ("cruise_plus_big",    "CRUISE  +  BIG"),
    (0, 3): ("cruise_plus_small",  "CRUISE  +  SMALL"),
    (1, 0): ("cruise_cancel",      "CRUISE  CANCEL"),
    (1, 1): ("follow_plus",        "FOLLOW DIST  +"),
    (1, 2): ("cruise_resume",      "CRUISE  RESUME"),
    (1, 3): ("follow_minus",       "FOLLOW DIST  −"),
    (2, 0): ("downhill_cancel",    "DOWNHILL  CANCEL"),
    (2, 1): ("downhill_minus",     "DOWNHILL  −"),
    (2, 2): ("downhill_resume",    "DOWNHILL  RESUME"),
    (2, 3): ("downhill_plus",      "DOWNHILL  +"),
}

# Physical zones drawn left->right, matching the real panel.
# Each zone has an upper and lower half; a half may be driven by more
# than one switch (the cruise rocker is two-stage).
ZONES = [
    dict(key="cruise_rc", w=124, glyph="round",
         top=(["cruise_resume"], "↺", "RESUME"),
         bot=(["cruise_cancel"], "0", "CANCEL"),
         mid="CRUISE"),
    dict(key="cruise_pm", w=150, glyph="rocker",
         top=(["cruise_plus_big", "cruise_plus_small"], "+", "SET +"),
         bot=(["cruise_minus_big", "cruise_minus_small"], "−", "SET −"),
         mid="SPEED"),
    dict(key="follow", w=150, glyph="lanes",
         top=(["follow_plus"], "▲", "FARTHER"),
         bot=(["follow_minus"], "▼", "CLOSER"),
         mid="GAP"),
    dict(key="dh_pm", w=150, glyph="rocker",
         top=(["downhill_plus"], "+", "DH +"),
         bot=(["downhill_minus"], "−", "DH −"),
         mid="DOWNHILL"),
    dict(key="dh_rc", w=124, glyph="round",
         top=(["downhill_resume"], "↺", "RESUME"),
         bot=(["downhill_cancel"], "0", "CANCEL"),
         mid="RETARDER"),
]

ZONE_ACCENT = {"cruise_rc": ACCENT, "cruise_pm": ACCENT,
               "follow": LEAF,
               "dh_pm": HILITE, "dh_rc": HILITE}


def decode(adc):
    """raw ADC -> tap index 0-3, IDLE, FAULT (open) or UNKNOWN"""
    if adc >= OPEN_MIN:
        return FAULT
    for i, (centre, tol) in enumerate(zip(CENTRES, TOL)):
        if abs(adc - centre) <= tol:
            return i if i < 4 else IDLE
    return UNKNOWN


def state_name(line, st):
    """every decoder output has a name; nothing is ever left unlabelled"""
    if st == IDLE:
        return "idle"
    if st == FAULT:
        return "OPEN CIRCUIT"
    if st == UNKNOWN:
        return "OUT OF RANGE"
    return BUTTONS[(line, st)][1]


def masked_by(line, tap):
    """
    Buttons on `line` that cannot be seen while `tap` is held.

    The ladder is a series chain, so grounding a tap shorts out every tap
    beyond it.  Pressing those buttons is electrically invisible - the
    reading is bit-for-bit identical to holding `tap` alone.  This is a
    property of the panel, not a limitation of the decoder.
    """
    return [BUTTONS[(line, k)][1] for k in range(tap + 1, 4)]


def resolve(pressed):
    """
    pressed: set of (line, tap) actually held down
    returns: {line: reported state} - what the ADC can actually see
    """
    out = {}
    for line in range(3):
        taps = sorted(t for (l, t) in pressed if l == line)
        out[line] = taps[0] if taps else IDLE
    return out


def blend(c1, c2, t):
    """linear blend between two #rrggbb colours"""
    a = tuple(int(c1[i:i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(c2[i:i + 2], 16) for i in (1, 3, 5))
    m = tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b))
    return "#%02x%02x%02x" % m


def rr_points(x1, y1, x2, y2, r):
    return [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]


# --------------------------------------------------------------------------
# serial worker
# --------------------------------------------------------------------------
class SerialWorker(threading.Thread):
    def __init__(self, port, out_q):
        super().__init__(daemon=True)
        self.port, self.q, self.stop = port, out_q, threading.Event()
        self.ser = None

    def run(self):
        try:
            self.ser = serial.Serial(self.port, 115200, timeout=0.4)
        except Exception as e:
            self.q.put(("error", str(e)))
            return
        self.q.put(("open", self.port))
        time.sleep(1.8)                      # Uno resets on DTR
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass

        while not self.stop.is_set():
            try:
                raw = self.ser.readline().decode("ascii", "ignore").strip()
            except Exception as e:
                self.q.put(("error", str(e)))
                break
            if not raw:
                continue
            if raw.startswith("#"):
                self.q.put(("info", raw[1:]))
                continue
            parts = raw.split(",")
            if len(parts) != 3:
                continue
            try:
                self.q.put(("data", tuple(int(p) for p in parts)))
            except ValueError:
                continue

        try:
            if self.ser and self.ser.is_open:
                self.ser.close()
        except Exception:
            pass
        self.q.put(("closed", None))

    def send(self, text):
        try:
            if self.ser and self.ser.is_open:
                self.ser.write(text.encode())
        except Exception:
            pass


# --------------------------------------------------------------------------
# app
# --------------------------------------------------------------------------
class App:
    def __init__(self, root):
        self.root = root
        root.title("Scania 2545507  ·  Panel Monitor")
        root.configure(bg=BG)
        center_window(root, 1080, 800)
        root.minsize(980, 700)

        self.q = queue.Queue()
        self.worker = None
        self.values = [682, 682, 682]
        self.shown = [682.0, 682.0, 682.0]      # smoothed for the meters
        self.state = [IDLE, IDLE, IDLE]
        self.recent = [deque(maxlen=3) for _ in range(3)]
        self.active = set()
        self.masked = set()          # held but electrically invisible
        self.items = {}
        self._segs = [[], [], []]
        self._meter_w = [-1, -1, -1]
        self._log_lines = 0
        self.pkts = 0
        self.pps = 0
        self._last_pps = time.time()

        self._build_header()
        self._build_panel()
        self._build_meters()
        self._build_log()

        self.refresh_ports()
        self.root.after(30, self._tick)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- header ----------------
    def _build_header(self):
        bar = tk.Frame(self.root, bg=BG)
        bar.pack(fill="x", padx=22, pady=(18, 6))

        tk.Label(bar, text="SCANIA 2545507", bg=BG, fg=TXT,
                 font=(FONT, 17, "bold")).pack(side="left")
        tk.Label(bar, text="  SKNY_FXP1 V1.1  ·  steering-wheel switch panel",
                 bg=BG, fg=TXT_DIM, font=(FONT, 10)).pack(side="left", pady=(6, 0))

        self.link = tk.Label(bar, text="● OFFLINE", bg=BG, fg=TXT_DIM,
                             font=(FONT, 10, "bold"))
        self.link.pack(side="right", pady=(4, 0))

        ctl = tk.Frame(self.root, bg=CARD, highlightthickness=1,
                       highlightbackground=EDGE)
        ctl.pack(fill="x", padx=22, pady=(6, 14))
        inner = tk.Frame(ctl, bg=CARD)
        inner.pack(fill="x", padx=14, pady=12)

        tk.Label(inner, text="PORT", bg=CARD, fg=TXT_DIM,
                 font=(FONT, 9, "bold")).pack(side="left", padx=(0, 8))

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("P.TCombobox", fieldbackground=CARD_HI,
                        background=CARD_HI, foreground=TXT,
                        arrowcolor=ACCENT, bordercolor=EDGE,
                        lightcolor=EDGE, darkcolor=EDGE, selectbackground=CARD_HI,
                        selectforeground=TXT, padding=6)
        # readonly comboboxes otherwise render with the platform selection
        # highlight, which looks like a white blob on a dark theme
        style.map("P.TCombobox",
                  fieldbackground=[("readonly", CARD_HI)],
                  background=[("readonly", CARD_HI)],
                  foreground=[("readonly", TXT)],
                  selectbackground=[("readonly", CARD_HI)],
                  selectforeground=[("readonly", TXT)],
                  bordercolor=[("focus", ACCENT)],
                  arrowcolor=[("active", ACCENT)])
        self.root.option_add("*TCombobox*Listbox.background", CARD_HI)
        self.root.option_add("*TCombobox*Listbox.foreground", TXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", INK)
        self.root.option_add("*TCombobox*Listbox.font", (MONO, 10))

        self.port_var = tk.StringVar()
        self.port_cb = ttk.Combobox(inner, textvariable=self.port_var, width=42,
                                    state="readonly", style="P.TCombobox",
                                    font=(MONO, 10), takefocus=False)
        self.port_cb.pack(side="left")
        # drop the highlight that lingers after a selection
        self.port_cb.bind("<<ComboboxSelected>>",
                          lambda e: self.root.focus_set())

        self._mkbtn(inner, "REFRESH", self.refresh_ports, CARD_HI, TXT).pack(
            side="left", padx=8)
        self.conn_btn = self._mkbtn(inner, "CONNECT", self.toggle, ACCENT, INK)
        self.conn_btn.pack(side="left")

        tk.Label(inner, text="BACKLIGHT", bg=CARD, fg=TXT_DIM,
                 font=(FONT, 9, "bold")).pack(side="left", padx=(24, 8))
        self.bl = tk.Scale(inner, from_=0, to=255, orient="horizontal",
                           length=150, bg=CARD, fg=TXT, troughcolor=CARD_HI,
                           highlightthickness=0, bd=0, sliderrelief="flat",
                           activebackground=ACCENT, font=(MONO, 8),
                           command=self._backlight)
        self.bl.set(255)
        self.bl.pack(side="left")

        self.rate = tk.Label(inner, text="—", bg=CARD, fg=TXT_DIM,
                             font=(MONO, 9))
        self.rate.pack(side="right")

    def _mkbtn(self, parent, text, cmd, bg, fg):
        b = tk.Label(parent, text=text, bg=bg, fg=fg, font=(FONT, 9, "bold"),
                     padx=16, pady=7, cursor="hand2")
        b.bind("<Button-1>", lambda e: cmd())
        b.bind("<Enter>", lambda e: b.configure(bg=blend(bg, "#ffffff", .12)))
        b.bind("<Leave>", lambda e: b.configure(bg=b._base))
        b._base = bg
        return b

    # ---------------- panel drawing ----------------
    def _build_panel(self):
        wrap = tk.Frame(self.root, bg=CARD, highlightthickness=1,
                        highlightbackground=EDGE)
        wrap.pack(fill="x", padx=22)
        self.cv = tk.Canvas(wrap, height=250, bg=CARD, highlightthickness=0)
        self.cv.pack(fill="x", padx=16, pady=16)
        # The panel is only redrawn on state changes, so without this it keeps
        # whatever x-offset it was given before the layout settled - and drifts
        # off the left edge as soon as the window is resized or maximised.
        self._cv_w = -1
        self.cv.bind("<Configure>", self._on_canvas_resize)

    def _on_canvas_resize(self, ev):
        if ev.width != self._cv_w:
            self._cv_w = ev.width
            self._draw_panel()

    def _draw_panel(self):
        """(re)build the canvas items - only on resize, never per frame"""
        cv = self.cv
        cv.delete("all")
        self.items = {}
        W = cv.winfo_width()
        if W <= 1:                      # not laid out yet; Configure calls back
            return
        total = sum(z["w"] for z in ZONES) + 14 * (len(ZONES) - 1)
        x = max(0, (W - total) / 2)
        y0, h = 22, 190

        for z in ZONES:
            w = z["w"]
            acc = ZONE_ACCENT[z["key"]]
            cv.create_polygon(rr_points(x - 4, y0 - 4, x + w + 4, y0 + h + 4, 20),
                              smooth=True, fill="#0E1F0D", outline=EDGE)

            for half, (ids, glyph, cap) in (("top", z["top"]), ("bot", z["bot"])):
                if half == "top":
                    a, b = y0 + 6, y0 + h / 2 - 3
                else:
                    a, b = y0 + h / 2 + 3, y0 + h - 6

                glows = [cv.create_polygon(
                            rr_points(x + 2 - gr, a - gr, x + w - 2 + gr, b + gr, 16),
                            smooth=True, fill="", outline="")
                         for gr in (10, 6, 3)]
                body = cv.create_polygon(rr_points(x + 2, a, x + w - 2, b, 14),
                                         smooth=True, fill=CARD_HI,
                                         outline=EDGE, width=2)
                gl = cv.create_text((x + w / 2), (a + b) / 2 - 8, text=glyph,
                                    fill=TXT_DIM, font=(FONT, 26, "bold"))
                cp = cv.create_text((x + w / 2), (a + b) / 2 + 20, text=cap,
                                    fill=TXT_DIM, font=(FONT, 8, "bold"))
                self.items[(z["key"], half)] = dict(
                    ids=ids, acc=acc, glows=glows, body=body,
                    glyph=gl, cap=cp, caption=cap)

            cv.create_text(x + w / 2, y0 + h + 22, text=z["mid"],
                           fill=blend(TXT_DIM, acc, .5), font=(FONT, 9, "bold"))
            x += w + 14
        self._paint_panel()

    def _paint_panel(self):
        """recolour existing items - cheap enough to run on every change"""
        cv = self.cv
        for it in self.items.values():
            on     = any(i in self.active for i in it["ids"])
            masked = not on and any(i in self.masked for i in it["ids"])
            acc = it["acc"]
            if on:
                for k, g in enumerate(it["glows"]):
                    cv.itemconfigure(g, outline=blend(CARD, acc, .10 * (3 - k)))
                cv.itemconfigure(it["body"], fill=blend("#0E1F0D", acc, .30),
                                 outline=acc, dash=())
                cv.itemconfigure(it["glyph"], fill="#ffffff")
                cv.itemconfigure(it["cap"], fill="#ffffff", text=it["caption"])
            else:
                for g in it["glows"]:
                    cv.itemconfigure(g, outline="")
                if masked:
                    # electrically invisible right now - say so rather than
                    # letting it look like an idle button that just won't work
                    cv.itemconfigure(it["body"], fill=CARD_HI,
                                     outline=FAINT, dash=(3, 3))
                    cv.itemconfigure(it["glyph"], fill=FAINT)
                    cv.itemconfigure(it["cap"], fill=HILITE, text="MASKED")
                else:
                    cv.itemconfigure(it["body"], fill=CARD_HI, outline=EDGE,
                                     dash=())
                    cv.itemconfigure(it["glyph"], fill=TXT_DIM)
                    cv.itemconfigure(it["cap"], fill=TXT_DIM, text=it["caption"])

    # ---------------- meters ----------------
    def _build_meters(self):
        wrap = tk.Frame(self.root, bg=BG)
        wrap.pack(fill="x", padx=22, pady=(14, 0))
        self.meters = []
        for i in range(3):
            card = tk.Frame(wrap, bg=CARD, highlightthickness=1,
                            highlightbackground=EDGE)
            card.pack(side="left", expand=True, fill="both",
                      padx=(0 if i == 0 else 10, 0))
            tk.Label(card, text=LINE_LABELS[i], bg=CARD, fg=TXT_DIM,
                     font=(MONO, 8)).pack(anchor="w", padx=12, pady=(9, 0))
            val = tk.Label(card, text="—", bg=CARD, fg=ACCENT,
                           font=(MONO, 22, "bold"))
            val.pack(anchor="w", padx=12)
            c = tk.Canvas(card, height=14, bg=CARD, highlightthickness=0)
            c.pack(fill="x", padx=12, pady=(2, 4))
            name = tk.Label(card, text="idle", bg=CARD, fg=TXT_DIM,
                            font=(FONT, 10, "bold"))
            name.pack(anchor="w", padx=12, pady=(0, 10))
            self.meters.append((val, c, name))

    SEGMENTS = 30

    def _draw_meter(self, i):
        """segmented bar in the style of the cluster arcs; items are created
        once per width and then only recoloured"""
        val, c, name = self.meters[i]
        w = c.winfo_width() or 300
        if self._meter_w[i] != w:
            self._meter_w[i] = w
            c.delete("all")
            gap, n = 2, self.SEGMENTS
            sw = (w - (n - 1) * gap) / n
            self._segs[i] = [c.create_rectangle(k * (sw + gap), 4,
                                                k * (sw + gap) + sw, 13,
                                                outline="", fill=SEG_OFF)
                             for k in range(n)]
            for centre in CENTRES:
                c.create_line(w * centre / 1023.0, 0,
                              w * centre / 1023.0, 3, fill=FAINT)

        frac = max(0.0, min(1.0, self.shown[i] / 1023.0))
        lit = int(round(frac * self.SEGMENTS))
        st = self.state[i]
        col = (ALERT if st in (FAULT, UNKNOWN)
               else GOOD if st == IDLE else ACCENT)
        for k, seg in enumerate(self._segs[i]):
            c.itemconfigure(seg, fill=col if k < lit else SEG_OFF)

    # ---------------- log ----------------
    def _build_log(self):
        wrap = tk.Frame(self.root, bg=CARD, highlightthickness=1,
                        highlightbackground=EDGE)
        wrap.pack(fill="both", expand=True, padx=22, pady=14)
        head = tk.Frame(wrap, bg=CARD)
        head.pack(fill="x", padx=14, pady=(10, 4))
        tk.Label(head, text="EVENT LOG", bg=CARD, fg=TXT_DIM,
                 font=(FONT, 9, "bold")).pack(side="left")
        self._mkbtn(head, "CLEAR", self._clear_log,
                    CARD_HI, TXT_DIM).pack(side="right")

        self.log_w = tk.Text(wrap, bg=WELL, fg=TXT, font=(MONO, 10),
                             bd=0, highlightthickness=0, height=8,
                             insertbackground=TXT, wrap="none")
        self.log_w.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        for tag, col in (("dn", ACCENT), ("up", TXT_DIM), ("err", ALERT),
                         ("sys", GOOD), ("ts", FAINT)):
            self.log_w.tag_configure(tag, foreground=col)

    LOG_MAX = 500

    def _clear_log(self):
        self.log_w.delete("1.0", "end")
        self._log_lines = 0

    def log(self, text, tag="sys"):
        ts = time.strftime("%H:%M:%S") + ".%03d" % (int(time.time() * 1000) % 1000)
        self.log_w.insert("end", ts + "  ", "ts")
        self.log_w.insert("end", text + "\n", tag)
        # an uncapped Text widget is the other thing that grinds a long
        # button-mashing session to a halt
        self._log_lines += 1
        if self._log_lines > self.LOG_MAX:
            self.log_w.delete("1.0", "%d.0" % (self._log_lines - self.LOG_MAX + 1))
            self._log_lines = self.LOG_MAX
        self.log_w.see("end")

    # ---------------- serial plumbing ----------------
    def refresh_ports(self):
        ports = list(serial.tools.list_ports.comports())
        items = ["%-6s  %s" % (p.device, (p.description or "")[:46]) for p in ports]
        self._devs = [p.device for p in ports]
        self.port_cb["values"] = items or ["no serial ports found"]
        if items and not self.port_var.get().strip().split(" ")[0] in self._devs:
            self.port_cb.current(0)
        elif not items:
            self.port_cb.current(0)

    def toggle(self):
        if self.worker and self.worker.is_alive():
            self.worker.stop.set()
            self.worker = None
            return
        idx = self.port_cb.current()
        if idx < 0 or not getattr(self, "_devs", None):
            self.log("no port selected", "err")
            return
        dev = self._devs[idx]
        self.worker = SerialWorker(dev, self.q)
        self.worker.start()
        self.conn_btn.configure(text="DISCONNECT", bg=ALERT, fg="#3B1610")
        self.conn_btn._base = ALERT

    def _backlight(self, v):
        if self.worker and self.worker.is_alive():
            self.worker.send("B%d\n" % int(float(v)))

    def _set_link(self, text, colour):
        self.link.configure(text=text, fg=colour)

    # ---------------- main loop ----------------
    def _tick(self):
        redraw = False
        while True:
            try:
                kind, payload = self.q.get_nowait()
            except queue.Empty:
                break
            if kind == "data":
                self.values = list(payload)
                self.pkts += 1
                if self._update_states():
                    redraw = True
            elif kind == "open":
                self._set_link("● LINKED  " + payload, GOOD)
                self.log("opened %s @115200" % payload, "sys")
            elif kind == "info":
                self.log("device: " + payload, "sys")
            elif kind == "error":
                self.log(payload, "err")
                self._set_link("● ERROR", ALERT)
            elif kind == "closed":
                self._set_link("● OFFLINE", TXT_DIM)
                self.log("port closed", "sys")
                self.conn_btn.configure(text="CONNECT", bg=ACCENT, fg=INK)
                self.conn_btn._base = ACCENT
                self.active.clear()
                redraw = True

        # smooth the meters toward the live values
        for i in range(3):
            self.shown[i] += (self.values[i] - self.shown[i]) * 0.35
            v, _, name = self.meters[i]
            st = self.state[i]
            v.configure(text="%4d" % self.values[i])
            if st in (FAULT, UNKNOWN):
                name.configure(text="%s  adc=%d" % (state_name(i, st),
                                                    self.values[i]), fg=ALERT)
            elif st == IDLE:
                name.configure(text="idle", fg=TXT_DIM)
            else:
                hidden = masked_by(i, st)
                txt = state_name(i, st)
                if hidden:
                    txt += "   (+%d masked)" % len(hidden)
                name.configure(text=txt, fg=ACCENT)
            self._draw_meter(i)

        now = time.time()
        if now - self._last_pps >= 1.0:
            self.pps, self.pkts = self.pkts, 0
            self._last_pps = now
            self.rate.configure(text="%d pkt/s" % self.pps)

        if redraw:
            self._paint_panel()
        self.root.after(30, self._tick)

    def _update_states(self):
        """
        Commit a state once it holds 2 of the last 3 samples.

        The previous rule needed two *consecutive* identical decodes and reset
        its counter on any disagreement, so one bouncing sample could stall the
        decode indefinitely - which is why fast mashing sometimes registered
        nothing at all.
        """
        changed = False
        for i in range(3):
            self.recent[i].append(decode(self.values[i]))
            counts = {}
            for v in self.recent[i]:
                counts[v] = counts.get(v, 0) + 1
            best = max(counts, key=counts.get)
            if counts[best] < 2 or best == self.state[i]:
                continue

            old_st = self.state[i]
            self.state[i] = best
            changed = True
            if old_st not in (IDLE, FAULT, UNKNOWN):
                self.active.discard(BUTTONS[(i, old_st)][0])
                self.log("▲  %s" % BUTTONS[(i, old_st)][1], "up")
            if best == FAULT:
                self.log("!  line %s open circuit" % "ABC"[i], "err")
            elif best == UNKNOWN:
                self.log("?  line %s OUT OF RANGE  adc=%d  (matches no ladder "
                         "node)" % ("ABC"[i], self.values[i]), "err")
            elif best != IDLE:
                self.active.add(BUTTONS[(i, best)][0])
                hidden = masked_by(i, best)
                msg = "▼  %-22s  adc=%d" % (BUTTONS[(i, best)][1],
                                                 self.values[i])
                if hidden:
                    msg += "   masks: " + ", ".join(hidden)
                self.log(msg, "dn")

        if changed:
            self.masked = set()
            for i in range(3):
                st = self.state[i]
                if 0 <= st < 4:
                    for k in range(st + 1, 4):
                        self.masked.add(BUTTONS[(i, k)][0])
        return changed

    def _on_close(self):
        if self.worker:
            self.worker.stop.set()
            time.sleep(0.25)
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    root.mainloop()
