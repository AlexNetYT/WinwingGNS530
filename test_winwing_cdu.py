#!/usr/bin/env python3
import asyncio
import json
import logging
import math
import pygame
import textwrap
import websockets
import numpy as np
import os
import re
from bs4 import BeautifulSoup

# --- SimConnect ---
from SimConnect import SimConnect, AircraftRequests

LOG_FILE = "gns530_winwing_cdu.log"
WS_URI = "ws://localhost:8320/winwing/cdu-captain"
UPDATE_INTERVAL = 0.1
JOYSTICK_INDEX = 0

BUTTONS_LSK = [0, 1, 2, 3, 4, 5]
BUTTONS_RSK = [6, 7, 8, 9, 10, 11]
BUTTONS_DIGITS = {32 + i: str(i + 1) for i in range(9)}
BUTTONS_DIGITS[42] = '0'
BUTTON_DOT = 41
BUTTON_BKSP = 73
BUTTON_CLR = 74  # CLR now is 74!
BUTTON_LEFT = 29
BUTTON_UP = 30
BUTTON_RIGHT = 31
BUTTON_DOWN = 32
BUTTONS_LETTERS = {44 + i: chr(65 + i) for i in range(26)}
DISPLAY_LINE_LENGTH = 24
DISPLAY_LINES = 14
FILES_DIR = r"C:\Users\sasch\AppData\Local\MobiFlight\MobiFlight Connector\Scripts\Winwing\GNS530"

SIMVARS = [
    "COM_ACTIVE_FREQUENCY:1", "COM_STANDBY_FREQUENCY:1",
    "NAV_ACTIVE_FREQUENCY:1", "NAV_STANDBY_FREQUENCY:1",
    "TRANSPONDER CODE:1",
    "GPS_GROUND_SPEED", "GPS_GROUND_MAGNETIC_TRACK"
]

def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        filename=LOG_FILE, filemode="w"
    )
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logging.getLogger().addHandler(console)

def parse_colored_text(text, default_color="w"):
    COLOR_MARKER_SEPARATOR = "`"
    TARGET_LINE_LENGTH = DISPLAY_LINE_LENGTH
    raw_text = ""
    i = 0
    while i < len(text):
        if i + 1 < len(text) and text[i + 1] == COLOR_MARKER_SEPARATOR:
            i += 2
            continue
        raw_text += text[i]
        i += 1
    if len(raw_text) < TARGET_LINE_LENGTH:
        text += ' ' * (TARGET_LINE_LENGTH - len(raw_text))
    colored = []
    current_color = default_color
    i = 0
    while i < len(text):
        if i + 1 < len(text) and text[i + 1] == COLOR_MARKER_SEPARATOR:
            current_color = text[i].lower()
            i += 2
            continue
        ch = text[i]
        colored.append([ch, current_color, 0])
        i += 1
    return colored

def safe_str(s):
    return s if isinstance(s, str) else str(s or "")

def bcd16_to_int(bcd):
    result = 0
    multiplier = 1
    bcd = int(bcd)
    while bcd > 0:
        digit = bcd & 0xF
        result += digit * multiplier
        multiplier *= 10
        bcd >>= 4
    return result

def int_to_bcd16(val):
    out = 0
    shift = 0
    val = int(val)
    while val > 0 and shift < 16:
        out |= (val % 10) << shift
        val //= 10
        shift += 4
    return out

def safe_freq(val):
    try:
        return "{:06.3f}".format(float(val))
    except Exception:
        return "---.---"

# --- SimConnect Bridge ---
class GNS530Bridge:
    def __init__(self):
        self.sm = SimConnect()
        self.aq = AircraftRequests(self.sm, _time=2000)
        for v in SIMVARS:
            try:
                self.aq.find(v)
            except Exception:
                pass
    def read_all(self):
        data = {}
        for v in SIMVARS:
            try:
                data[v] = self.aq.get(v)
            except Exception as e:
                logging.error(f"Ошибка чтения симвара {v}: {e}")
                data[v] = None
        return data
    def set_simvar(self, var, value):
        try:
            self.aq.set(var, value)
            return True
        except Exception as e:
            logging.error(f"Ошибка установки симвара {var}: {e}")
            return False

# --- Error State Management ---
class ErrorManager:
    def __init__(self):
        self.active = False
        self.message = ""
    def set(self, msg):
        self.active = True
        self.message = msg
        logging.info(f"Error set: {msg}")
    def clear(self):
        self.active = False
        self.message = ""
        logging.info("Error cleared")
    def is_active(self):
        return self.active

# --- Flightplan file parser ---
def list_fpl_files():
    try:
        files = [f for f in os.listdir(FILES_DIR) if f.lower().endswith('.html')]
        files.sort()
        return files
    except Exception as e:
        logging.error(f"Ошибка чтения директории файлов: {e}")
        return []

def parse_fpl_file(filepath):
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            soup = BeautifulSoup(f, "html.parser")
        dep, arr = None, None
        h1 = soup.find("h1")
        if h1:
            m = re.match(r".*\((\w{4})\)\s*to\s*.*\((\w{4})\)", h1.text)
            if m:
                dep, arr = m.group(1), m.group(2)
        if not (dep and arr):
            title = soup.find("title")
            if title:
                m = re.match(r".*\((\w{4})\).*to.*\((\w{4})\)", title.text)
                if m:
                    dep, arr = m.group(1), m.group(2)
        table = soup.find("table")
        if not table:
            raise ValueError("No table in file")
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        idx_ident = idx_course = idx_legtime = idx_wind = None
        for i, h in enumerate(headers):
            if "Ident" in h: idx_ident = i
            if "Course" in h: idx_course = i
            if "Leg Time" in h: idx_legtime = i
            if "Head- or Tailwind" in h: idx_wind = i
        if None in (idx_ident, idx_course, idx_legtime, idx_wind):
            raise ValueError("Table columns not found")
        points = []
        for tr in table.find_all("tr")[1:]:
            tds = tr.find_all("td")
            if len(tds) <= max(idx_ident, idx_course, idx_legtime, idx_wind):
                continue
            ident = safe_str(tds[idx_ident].get_text(strip=True))
            course = safe_str(tds[idx_course].get_text(strip=True)).replace(',', '.')
            legtime = safe_str(tds[idx_legtime].get_text(strip=True))
            wind = safe_str(tds[idx_wind].get_text(strip=True))
            if ident:
                if not course or not course.replace('.', '', 1).isdigit():
                    course = "---"
                if not legtime:
                    legtime = "--:--"
                points.append({
                    "ident": ident[:8],
                    "course": course,
                    "legtime": legtime,
                    "wind": wind
                })
        if not points or not dep or not arr:
            raise ValueError("Not enough data in file")
        return {"dep": dep, "arr": arr, "points": points}
    except Exception as e:
        logging.error(f"Ошибка разбора плана {filepath}: {e}")
        return None

# --- App State ---
class AppState:
    def __init__(self):
        self.scratchpad = ""
        self.page_idx = 0     # 0: main, 1: FPLN
        self.bridge = None
        self.last_data = {}
        self.fpl_file = None
        self.fpl = None
        self.fpl_file_list = []
        self.fpl_file_scroll = 0
        self.fpl_file_selected = 0
        self.fpl_scroll = 0
        self.error = ErrorManager()

    def clear_scratchpad(self):
        self.scratchpad = ""
    def backspace_scratchpad(self):
        self.scratchpad = self.scratchpad[:-1]
    def append_scratchpad(self, ch):
        if self.scratchpad is None:
            self.scratchpad = ""
        if len(self.scratchpad) < 20:
            self.scratchpad += ch
    def set_error(self, msg):
        self.error.set(msg)
    def clear_error(self):
        self.error.clear()
        self.unload_fpl()
    def unload_fpl(self):
        self.fpl = None
        self.fpl_file = None
        self.fpl_scroll = 0
        self.fpl_file_selected = 0
        self.fpl_file_scroll = 0

# --- Main page ---
class MainPage:
    def __init__(self, state: AppState):
        self.state = state
    def render(self, data):
        lines = [" " * DISPLAY_LINE_LENGTH for _ in range(DISPLAY_LINES)]
        lines[0] = "C`GNS530 CDU".center(DISPLAY_LINE_LENGTH)
        com1a = safe_freq(data.get('COM_ACTIVE_FREQUENCY:1'))
        com1s = safe_freq(data.get('COM_STANDBY_FREQUENCY:1'))
        nav1a = safe_freq(data.get('NAV_ACTIVE_FREQUENCY:1'))
        nav1s = safe_freq(data.get('NAV_STANDBY_FREQUENCY:1'))
        try:
            xpdr_raw = data.get('TRANSPONDER CODE:1', 0)
            xpdr = bcd16_to_int(int(xpdr_raw))
            xpdr = "{:04d}".format(xpdr)
        except Exception:
            xpdr = "0000"
        try:
            gs = str(int(float(data.get("GPS_GROUND_SPEED", 0))*1.94384)).rjust(3)
        except Exception:
            gs = "---"
        try:
            trk = str(int(np.degrees(float(data.get("GPS_GROUND_MAGNETIC_TRACK") or 0)))).rjust(3)
        except Exception:
            trk = "---"
        lines[2] = f"C`COM1 G`{com1a} C`/ A`{com1s}"
        lines[4] = f"C`NAV1 G`{nav1a} C`/ A`{nav1s}"
        lines[6] = f"C`XPDR G`{xpdr}"
        lines[8] = f"C`GS G`{gs}KT C`TRK G`{trk}"
        if self.state.fpl:
            lines[12] = " " * (DISPLAY_LINE_LENGTH-12) + "C`< DEL FPLN"
        pad = self.state.scratchpad
        pad_disp = f"A`[ W`{pad.ljust(DISPLAY_LINE_LENGTH-4)[:DISPLAY_LINE_LENGTH-4]} A`]".ljust(DISPLAY_LINE_LENGTH)
        lines[-1] = pad_disp
        return lines
    def handle_button(self, button):
        state = self.state
        if button == BUTTON_CLR:
            state.clear_scratchpad()
            return
        if button == BUTTON_BKSP:
            state.backspace_scratchpad()
            return
        # Удалить план (RSK12)
        if button == 11 and state.fpl:
            state.unload_fpl()
        # Записать XPDR — RSK6 (кнопка 11, строка XPDR)
        if button == 7:  # RSK6 на строке XPDR — можно выбрать другую кнопку
            pad = state.scratchpad.strip()
            if pad.isdigit() and 0 <= int(pad) <= 7777:
                value = int(pad)
                bcd_val = int_to_bcd16(value)
                ok = state.bridge.set_simvar("TRANSPONDER CODE:1", bcd_val)
                if ok:
                    state.clear_scratchpad()
                else:
                    state.set_error("XPDR WRITE ERR")
            else:
                state.set_error("XPDR CODE ERR")
    def handle_key(self, button):
        state = self.state
        if button in BUTTONS_DIGITS:
            state.append_scratchpad(BUTTONS_DIGITS[button])
        elif button == BUTTON_DOT:
            if '.' not in state.scratchpad and len(state.scratchpad) < 7:
                state.append_scratchpad('.')
        elif button in BUTTONS_LETTERS:
            state.append_scratchpad(BUTTONS_LETTERS[button])
        elif button == BUTTON_BKSP:
            state.backspace_scratchpad()
        elif button == BUTTON_CLR:
            state.clear_scratchpad()

# --- FPLN page ---
class FPLNPage:
    def __init__(self, state: AppState):
        self.state = state
    def render(self, data):
        lines = [" " * DISPLAY_LINE_LENGTH for _ in range(DISPLAY_LINES)]
        if self.state.fpl:
            dep = self.state.fpl.get("dep", "---")
            arr = self.state.fpl.get("arr", "---")
            lines[0] = f"C`FLIGHTPLAN {dep} \u2192 {arr}".center(DISPLAY_LINE_LENGTH)
            pts = self.state.fpl.get("points", [])
            start = self.state.fpl_scroll
            for i in range(6):
                idx = start + i
                lidx = 2 + i*2
                if idx < len(pts):
                    pt = pts[idx]
                    ident = pt["ident"][:8].ljust(8)
                    course = pt["course"].rjust(3)
                    legtime = pt["legtime"].rjust(5)
                    wind = pt["wind"].rjust(4)
                    lines[lidx] = f"W`{ident} G`{course} W`{legtime} W`{wind}".ljust(DISPLAY_LINE_LENGTH)
                else:
                    lines[lidx] = ""
        else:
            lines[0] = "C`FLIGHTPLANS".center(DISPLAY_LINE_LENGTH)
            files = self.state.fpl_file_list = list_fpl_files()
            if not files:
                lines[6] = "R`NO FLIGHTPLANS FOUND".center(DISPLAY_LINE_LENGTH)
            else:
                start = self.state.fpl_file_scroll
                sel = self.state.fpl_file_selected
                for i in range(6):
                    idx = start + i
                    lidx = 2 + i*2
                    if idx < len(files):
                        name = files[idx][:20]
                        marker = ">" if idx == sel else " "
                        lines[lidx] = f"W`{marker} {name}".ljust(DISPLAY_LINE_LENGTH)
                    else:
                        lines[lidx] = ""
        return lines
    def handle_button(self, button):
        state = self.state
        if state.fpl:
            pts = state.fpl.get("points", [])
            if button == BUTTON_UP:
                state.fpl_scroll = max(0, state.fpl_scroll - 1)
            elif button == BUTTON_DOWN:
                state.fpl_scroll = min(max(0, len(pts) - 6), state.fpl_scroll + 1)
        else:
            files = state.fpl_file_list = list_fpl_files()
            start = state.fpl_file_scroll
            sel = state.fpl_file_selected
            maxsel = len(files)-1
            for i, lsk in enumerate(BUTTONS_LSK):
                idx = start + i
                if button == lsk and idx < len(files):
                    filepath = os.path.join(FILES_DIR, files[idx])
                    fpl = parse_fpl_file(filepath)
                    if not fpl:
                        state.set_error("NOT ALLOWED")
                        state.unload_fpl()
                    else:
                        state.fpl = fpl
                        state.fpl_file = files[idx]
                        state.fpl_scroll = 0
                    return
            if button == BUTTON_UP and sel > 0:
                state.fpl_file_selected -= 1
                if state.fpl_file_selected < state.fpl_file_scroll:
                    state.fpl_file_scroll = state.fpl_file_selected
            elif button == BUTTON_DOWN and sel < maxsel:
                state.fpl_file_selected += 1
                if state.fpl_file_selected >= state.fpl_file_scroll + 6:
                    state.fpl_file_scroll = state.fpl_file_selected - 5
    def handle_key(self, button):
        pass

# --- Error page ---
class ErrorPage:
    def __init__(self, state: AppState):
        self.state = state

    def render(self, data):
        msg = self.state.error.message or "ERROR"
        lines = [f"R`{msg}".center(DISPLAY_LINE_LENGTH)]
        lines += [" " * DISPLAY_LINE_LENGTH for _ in range(DISPLAY_LINES-1)]
        lines[-1] = "[PRESS CLR]".center(DISPLAY_LINE_LENGTH)
        return lines

    def handle_button(self, button):
        if button == BUTTON_CLR:
            self.state.clear_error()

    def handle_key(self, button):
        if button == BUTTON_CLR:
            self.state.clear_error()

# --- Joystick/page logic ---
async def joystick_listener(state: AppState, pages):
    pygame.init()
    pygame.joystick.init()
    try:
        joystick = pygame.joystick.Joystick(JOYSTICK_INDEX)
        joystick.init()
        logging.info(f"Джойстик {joystick.get_name()} подключен")
    except Exception as e:
        logging.error(f"Ошибка инициализации джойстика: {e}")
        return
    while True:
        pygame.event.pump()
        for event in pygame.event.get():
            if event.type == pygame.JOYBUTTONDOWN:
                # Error page always has priority
                if state.error.is_active():
                    ep = ErrorPage(state)
                    if (event.button in BUTTONS_DIGITS or event.button == BUTTON_DOT
                            or event.button in BUTTONS_LETTERS or event.button == BUTTON_BKSP
                            or event.button == BUTTON_CLR):
                        ep.handle_key(event.button)
                    else:
                        ep.handle_button(event.button)
                    continue
                # Else, normal navigation
                if event.button == BUTTON_LEFT and state.page_idx > 0:
                    state.page_idx -= 1
                    continue
                if event.button == BUTTON_RIGHT and state.page_idx < len(pages)-1:
                    state.page_idx += 1
                    continue
                active_page = pages[state.page_idx]
                if (event.button in BUTTONS_DIGITS or event.button == BUTTON_DOT
                        or event.button in BUTTONS_LETTERS or event.button == BUTTON_BKSP
                        or event.button == BUTTON_CLR):
                    active_page.handle_key(event.button)
                else:
                    active_page.handle_button(event.button)
        await asyncio.sleep(0.02)

async def main_loop():
    setup_logging()
    logging.info("Старт WinWing CDU! (flightplans, scroll, html, цвет, simconnect)")
    state = AppState()
    state.bridge = GNS530Bridge()
    pages = [MainPage(state), FPLNPage(state)]
    asyncio.create_task(joystick_listener(state, pages))
    async with websockets.connect(WS_URI) as ws:
        while True:
            data = state.bridge.read_all()
            if state.error.is_active():
                page = ErrorPage(state)
            else:
                page = pages[state.page_idx]
            disp = []
            for text in page.render(data):
                disp.extend(parse_colored_text(text))
            message = {"Target": "Display", "Data": disp}
            await ws.send(json.dumps(message))
            await asyncio.sleep(UPDATE_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main_loop())
