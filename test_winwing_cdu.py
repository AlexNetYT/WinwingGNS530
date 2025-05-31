#!/usr/bin/env python3
import asyncio
import json
import logging
import time
import math
import pygame
import textwrap
from SimConnect import SimConnect, AircraftRequests
import websockets
import numpy as np
from bs4 import BeautifulSoup
import os  # Для работы с файловой системой

# —————————————————————————————————————————————
#  Настройки
# —————————————————————————————————————————————
LOG_FILE        = "gns530_winwing_cdu.log"
WS_URI          = "ws://localhost:8320/winwing/cdu-captain"
UPDATE_INTERVAL = 0   # секунды между обновлениями
JOYSTICK_INDEX  = 0     # индекс джойстика
BUTTON_NEXT     = 28    # кнопка: следующая страница
BUTTON_PREV     = 30    # кнопка: предыдущая страница
BUTTON_UP = 31
BUTTON_DOWN = 29
BUTTONS_SELECT   = [0,1,2,3,4,5]    # кнопка: выбрать файл (новая кнопка для выбора файла)
DIR = r"""C:\Users\sasch\AppData\Local\MobiFlight\MobiFlight Connector\Scripts\Winwing\GNS530"""
global files  # Глобальная переменная для хранения списка файлов
global selected_file  # Глобальная переменная для хранения выбранного файла
global FILE_SELECTED  # Глобальная переменная для хранения флага файла
global flightplan  # Глобальная переменная для хранения плана полета
flightplan = None  # Переменная для хранения плана полета
FILE_SELECTED = False
selected_file = None  # Переменная для хранения выбранного файла
files = [f for f in os.listdir(DIR) if f.split(".")[-1] in ["html", "htm"]]
# —————————————————————————————————————————————

# Цвет текста для MobiFlight
COLORS = {"white": "w", "cyan": "c", "green": "g"}
ERROR_SCREEN_ACTIVE = False
ERROR_TYPE = "error"  # Тип ошибки (error/warn/success)
# Сообщение об ошибке
ERROR_MESSAGE = "SOME ERROR OCCURRED"

# СимVars для GNS530
SIMVARS = [
    "COM_ACTIVE_FREQUENCY:1", "COM_STANDBY_FREQUENCY:1",
    "COM_ACTIVE_FREQUENCY:2", "COM_STANDBY_FREQUENCY:2",
    "NAV_ACTIVE_FREQUENCY:1", "NAV_STANDBY_FREQUENCY:1",
    "NAV_ACTIVE_FREQUENCY:2", "NAV_STANDBY_FREQUENCY:2",
    "GPS_GROUND_SPEED", "GPS_ETE", "GPS_ETA",
    "GPS_FLIGHT_PLAN_WP_COUNT", "GPS_FLIGHT_PLAN_WP_INDEX",
    "GPS_WP_NEXT_ID", "GPS_GROUND_MAGNETIC_TRACK",
]

# Текущая страница
current_page = 0
selected_file = None  # Храним выбранный файл

def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        filename=LOG_FILE, filemode="w"
    )
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logging.getLogger().addHandler(console)
def parse_file(filename):
    file = open(os.path.join(DIR, filename), 'r', encoding="utf8")
    html = file.read()
    soup = BeautifulSoup(html, 'html.parser')

    rows = soup.find_all('tr')

    fpln = []
    # Пример использования:
    
    for row in rows[1:]:
        cells = row.find_all('td')
        ident = cells[1].text.strip()
        hdg = int(cells[9].text.strip()) if cells[9].text.strip() != '' else ''
        distance_nm =  '' if cells[10].text.strip() == '' else float(cells[10].text.strip().replace(',', '.'))
        fpln.append({"ident": ident, "hdg": hdg, "distance_nm": distance_nm})
    return fpln

class GNS530Bridge:
    def __init__(self):
        self.sm = SimConnect()
        self.aq = AircraftRequests(self.sm, _time=2000)
        for v in SIMVARS:
            try: self.aq.find(v)
            except: pass

    def read_all(self):
        data = {}
        for v in SIMVARS:
            try: data[v] = self.aq.get(v)
            except: data[v] = None
        return data

    def get_flightplan(self):
        wps = []
        count = int(self.aq.get("GPS_FLIGHT_PLAN_WP_COUNT") or 0)
        current = int(self.aq.get("GPS_FLIGHT_PLAN_WP_INDEX") or 0)
        for idx in range(count):
            try:
                self.aq.set("GPS_FLIGHT_PLAN_WP_INDEX", idx)
                time.sleep(0.05)
                wp = self.aq.get("GPS_WP_NEXT_ID") or ""
            except:
                wp = ""
            wps.append(str(wp))
        try:
            self.aq.set("GPS_FLIGHT_PLAN_WP_INDEX", current)
        except: pass
        return wps


def safe_fmt(val, fmt="{:06.2f}", default="----.--"):
    try: return fmt.format(val)
    except: return default

def safe_time(sec, default="--:--"):
    try:
        m = int(sec)//60; s = int(sec)%60
        return f"{m:02}:{s:02}"
    except: return default




def parse_colored_text(text, default_color="w"):
    СOLOR_MARKER_SEPARATOR = "`"  # Символ-разделитель между цветом и текстом
    TARGET_LINE_LENGTH = 24       # Желаемая длина строки без цветовых кодов
    # Сначала считаем длину строки без цветовых кодов
    raw_text = ""
    i = 0
    while i < len(text):
        if i + 1 < len(text) and text[i+1] == СOLOR_MARKER_SEPARATOR:
            i += 2  # Пропускаем код цвета и разделитель
            continue
        raw_text += text[i]
        i += 1

    # Если длина строки без цветов меньше 24, добавляем пробелы в конец
    if len(raw_text) < TARGET_LINE_LENGTH:
        text += ' ' * (TARGET_LINE_LENGTH - len(raw_text))

    # Теперь парсим текст обратно с цветами
    colored = []
    current_color = default_color
    i = 0
    while i < len(text):
        if i + 1 < len(text) and text[i+1] == СOLOR_MARKER_SEPARATOR:
            current_color = text[i].lower()
            i += 2
            continue
        ch = text[i]
        colored.append([ch, current_color, 0])
        i += 1
    return colored


def format_display_line(ident: str, hdg: str, dista1: str, dista2: str) -> str:
    # Приводим каждую часть к нужной длине
    ident = ident.ljust(5)[:5]      # IDENT — дополняем пробелами справа до 5 символов
    hdg = hdg.zfill(3)[-3:]          # HDG — дополняем нулями слева до 3 символов
    dista1 = dista1.split(".")[0].rjust(4)[:4]     # DISTA1 — дополняем пробелами слева до 5 символов
    dista2 = dista2.split(".")[0].rjust(4)[:4]     # DISTA2 — дополняем пробелами слева до 5 символов

    # Форматируем HDG, добавляя символ градуса
    hdg = f"{hdg}°"  # Например, 180 -> 180°

    # Форматируем дистанции, добавляя "NM"
    dista1 = f"{dista1}NM" if dista1.strip() else dista1
    dista2 = f"{dista2}NM" if dista2.strip() else dista2

    parts = [ident, hdg, dista1, dista2]
    print(parts)
    total_content_length = sum(len(part) for part in parts)
    total_spaces = 24 - total_content_length

    # Распределяем пробелы между словами, оставляя минимум 1 пробел между частями
    if total_spaces >= 3:
        space_between = total_spaces // 3
        extra = total_spaces % 3

        spaces = [' ' * (space_between + (1 if i < extra else 0)) for i in range(3)]
        # Соединяем части с пробелами
        line = parts[0] + spaces[0] + parts[1] + spaces[1] + parts[2] + spaces[2] + parts[3]
    else:
        # если очень мало пробелов — просто склеиваем
        line = ''.join(parts).ljust(24)

    # Возвращаем строку длиной 24 символа
    return line[:24]


def build_error_screen():
    global ERROR_MESSAGE
    global ERROR_TYPE
    error_description = ERROR_MESSAGE  # Сообщение об ошибке
    type = ERROR_TYPE  # Тип ошибки (error/warn/success)
    lines = [" " * 24 for _ in range(14)]  # Инициализация пустого экрана
    color = "R" if type == 'error' else "A" if type == 'warn' else "C"  # Цвет текста (красный для ошибки, зеленый для успеха)
    # Заголовок
    title = "R`ERROR OCCURRED"
    lines[0] = title.center(24)  # Центрируем заголовок по ширине экрана

    # Ошибка
    error_description = "SOME ERROR OCCURRED"  # Описание ошибки
    i = 3
    for line in textwrap.wrap(error_description, 24, break_long_words=True):
        lines[i] = f"C`{line.ljust(24)[:24]}".center(24)  # Описание ошибки
        i += 1 
    lines[13] = "G`CLR TO CLOSE".center(24)  # Пустая строка внизу
    # Дополнительная информация
    # lines[2] = " " * 24  # Пустая строка после описания
    # lines[3] = " " * 24  # Еще одна пустая строка для красоты

    return lines



def build_normal_display(data, wps):
    if ERROR_SCREEN_ACTIVE:
        return build_error_screen()  # Показываем экран ошибки

    lines = []
    gs = round(float(data.get("GPS_GROUND_SPEED") or 0) * 1.94384)
    track = data.get("GPS_GROUND_MAGNETIC_TRACK") or 0
    trk = int(np.degrees(float(track))) if track else 0

    # Строки с цветовой разметкой
    lines.append(" " * 24)
    lines.append(f"W`COM1 G`{safe_fmt(data['COM_ACTIVE_FREQUENCY:1'])} W`| GS: B`{gs:03d}KT")
    lines.append(f"С`STBY G`{safe_fmt(data['COM_STANDBY_FREQUENCY:1'])} W`| TRK: B`{trk}°")
    lines.append(" " * 24)
    lines.append(f"W`NAV1 G`{safe_fmt(data['NAV_ACTIVE_FREQUENCY:1'])}"+"      CLR  ")
    lines.append(f"С`STBY G`{safe_fmt(data['NAV_STANDBY_FREQUENCY:1'])}"+"     FPLN ")
    lines.append("-" * 24)
    lines.append(f"W`ETE G`{safe_time(data.get('GPS_ETE'))} W`ETA G`{safe_time(data.get('GPS_ETA'))}")

    for i in range(0, len(wps), 4):
        chunk = wps[i:i+4]
        line = ",".join(chunk)[:24]
        lines.append(line)

    while len(lines) < 14:
        lines.append(" " * 24)

    # Теперь красим
    return lines

# Глобальные переменные
# files = []
# flightplan = []
# selected_file = None
# FILE_SELECTED = False
flightplan_scroll_index = 0  # Индекс для скроллинга

def build_file_list_display(data, wps):
    if ERROR_SCREEN_ACTIVE:
        return build_error_screen()  # Показываем экран ошибки

    lines = [" " * 24 for _ in range(14)]

    if not FILE_SELECTED:
        draw_file_selection(lines)
    else:
        draw_flightplan(lines)
    
    return lines

def draw_file_selection(lines):
    lines[0] = 'A`   SELECT FLIGHTPLAN    '
    
    limited_files = files[:6]  # Ограничиваем 6 файлами
    file_positions = {0: 2, 1: 4, 2: 6, 3: 8, 4: 10, 5: 12}

    for idx, file in enumerate(limited_files):
        line_idx = file_positions.get(idx)
        if line_idx is not None:
            lines[line_idx] = file.ljust(24)[:24]

def draw_flightplan(lines):
    global flightplan_scroll_index

    if not flightplan:
        lines[0] = 'C`FLIGHTPLAN EMPTY         '
        return

    start_point = flightplan[0].split(" ")[0]
    end_point = flightplan[-1].split(" ")[0]
    lines[0] = f"C`FLIGHTPLAN  {start_point} - {end_point}".ljust(24)[:24]

    # Берем только нужную порцию для отображения, с шагом по 3 строки
    plan_slice = flightplan[flightplan_scroll_index:flightplan_scroll_index + 6]  # Слайс для 6 строк

    display_positions = [2, 4, 6, 8, 10, 12]  # Индексы для отображения

    for i, point in enumerate(plan_slice):
        lines[display_positions[i]] = point.ljust(24)[:24]

def scroll_flightplan(direction: int):
    """
    Скроллинг flightplan вверх/вниз.
    direction: 1 - вниз, -1 - вверх
    """
    global flightplan_scroll_index

    max_scroll = max(0, len(flightplan) - 6)
    flightplan_scroll_index = max(0, min(flightplan_scroll_index + direction * 3, max_scroll))

def load_flightplan(fpn):
    global flightplan
    flightplan = []
    for i in fpn:
        fp_str = format_display_line(i["ident"], str(i["hdg"]), str(i["distance_nm"]), "0.0")
        flightplan.append(fp_str)
    return flightplan
def build_empty_display():
    return [[] for _ in range(14*24)]

# Массив страниц (можно добавлять сюда дальше)
pages = [
    build_normal_display,  # Страница 0: нормальный экран
    build_file_list_display,  # Страница 1: список файлов
]

def next_page():
    global current_page
    current_page = (current_page + 1) % len(pages)
    logging.info(f"Переключение на страницу {current_page}")

def prev_page():
    global current_page
    current_page = (current_page - 1) % len(pages)
    logging.info(f"Переключение на страницу {current_page}")
def clear_flightplan():
    global flightplan
    global FILE_SELECTED
    FILE_SELECTED = False
    flightplan = []
    logging.info("План полета сброшен")
def select_file(file_name):
    global selected_file
    global flightplan
    global FILE_SELECTED
    global ERROR_MESSAGE
    global ERROR_TYPE
    if file_name in os.listdir(DIR):
        selected_file = file_name
        parsed = parse_file(file_name)
        if parsed == []:
            logging.error(f"Ошибка: файл {file_name} пуст")
            global ERROR_SCREEN_ACTIVE
            ERROR_SCREEN_ACTIVE = True
            ERROR_MESSAGE = f"File {file_name} is empty"
            ERROR_TYPE = "warn"
            return None
        flightplan = load_flightplan(parsed)
        logging.info(f"Файл {file_name} выбран")
        FILE_SELECTED = True
        global current_page
        current_page = 0  # Возвращаемся на страницу с нормальным экраном

async def joystick_listener():
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
                if event.button == BUTTON_NEXT:
                    next_page()
                elif event.button == BUTTON_PREV:
                    prev_page()
                elif event.button == BUTTON_UP:
                    scroll_flightplan(-1)  # Прокрутить вверх
                elif event.button == BUTTON_DOWN:
                    scroll_flightplan(1)  # Прокрутить низ
                elif event.button == 7:  # Кнопка 7 сбрасывает план полета
                    clear_flightplan()
                elif event.button == 73:  # Кнопка для закрытия экрана ошибки
                    global ERROR_SCREEN_ACTIVE
                    ERROR_SCREEN_ACTIVE = False  # Закрыть экран ошибки
                    logging.info("Ошибка закрыта")
                elif event.button in BUTTONS_SELECT and current_page == 1:  # Выбор файла на странице с файлами
                    print(f"Выбрана кнопка {event.button}")
                    try:
                        selected_file = files[event.button]
                        select_file(selected_file)
                    except IndexError:
                        logging.error(f"Ошибка: индекс {event.button} вне диапазона файлов")
        await asyncio.sleep(0.05)

async def main_loop():
    setup_logging()
    logging.info("Запуск моста GNS530 → WinWing CDU с переключением страниц")

    bridge = GNS530Bridge()

    # Запускаем слушатель джойстика
    asyncio.create_task(joystick_listener())

    async with websockets.connect(WS_URI) as ws:
        while True:
            data = bridge.read_all()
            wps  = bridge.get_flightplan()
            disp = []
            for text in pages[current_page](data, wps):

                disp.extend(parse_colored_text(text.ljust(24)[:24]))

            
            # if selected_file:
            #     disp.append([f"Selected File: {selected_file}"])  # Показываем выбранный файл
            message = {"Target": "Display", "Data": disp}
            await ws.send(json.dumps(message))
            await asyncio.sleep(UPDATE_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main_loop())
