"""studIA matrix: la matriz LED 8x13 del tutor (App Lab, parte Linux).

studia_server.py envía eventos a  GET http://127.0.0.1:7000/led/<evento>/<arg>
    correct / -              tick que se dibuja
    wrong / -                cruz que tiembla
    thinking / start|stop    un punto que da vueltas mientras el LLM piensa
    pomodoro / start-S-T     cuenta atrás (S segundos que quedan de T): los minutos en grande
                             y un punto que da una vuelta al borde por minuto
    pomodoro / pause-S-T | reset | end
    gacha / <icono>          rodillo de tragaperras que se para en el objeto ganado
    avatar / <icono>         el avatar equipado, que se ve cuando no pasa nada más

Todo se decide aquí, en Python: el sketch solo dibuja el fotograma (brillo 0..7).
Nada parpadea más de tres veces por segundo.
"""
import math
import random
import time
from collections import deque

ROWS, COLS = 8, 13
ON, MID, LOW = 7, 4, 2

# Iconos 8x8 (columnas 2..9 de la matriz). "#" encendido, "+" a media luz.
ICONS = {
    "smile":   ["..####..", ".#....#.", "#.#..#.#", "#......#", "#.#..#.#", "#..##..#", ".#....#.", "..####.."],
    "fox":     ["#......#", "##....##", "#.#..#.#", "#......#", ".#.##.#.", "..#..#..", "...##...", "........"],
    "owl":     ["#......#", ".######.", "#+#..#+#", "#..##..#", "#......#", ".#.##.#.", ".######.", "..#..#.."],
    "cat":     ["#......#", "##....##", "########", "#.#..#.#", "#......#", "#..##..#", ".#....#.", "..####.."],
    "robot":   ["...##...", ".######.", "#......#", "#.#..#.#", "#......#", "#.####.#", ".######.", ".#....#."],
    "rocket":  ["...##...", "..####..", "..#..#..", "..####..", "..####..", ".######.", "#.####.#", "..+..+.."],
    "dino":    ["....####", "....#.##", "....####", "#...###.", "##.####.", ".######.", "..####..", "..#..#.."],
    "panda":   ["##....##", "#.####.#", ".#....#.", ".##..##.", ".##..##.", ".#.##.#.", ".#....#.", "..####.."],
    "unicorn": [".......#", "......#.", "..####..", ".#....#.", "#.#....#", "#......#", ".#..##.#", "..##..#."],
    "palette": ["..####..", ".#....#.", "#.#..#.#", "#......#", "#.#...#.", "#....#..", ".#..#...", "..##...."],
    "frame":   ["########", "#......#", "#.####.#", "#.#..#.#", "#.#..#.#", "#.####.#", "#......#", "########"],
    "crown":   ["........", "#..##..#", "##.##.##", "########", "########", "#.#..#.#", "########", "........"],
}
DIGITS = {
    "0": ["###", "#.#", "#.#", "#.#", "###"], "1": [".#.", "##.", ".#.", ".#.", "###"],
    "2": ["###", "..#", "###", "#..", "###"], "3": ["###", "..#", "###", "..#", "###"],
    "4": ["#.#", "#.#", "###", "..#", "..#"], "5": ["###", "#..", "###", "..#", "###"],
    "6": ["###", "#..", "###", "#.#", "###"], "7": ["###", "..#", ".#.", ".#.", ".#."],
    "8": ["###", "#.#", "###", "#.#", "###"], "9": ["###", "#.#", "###", "..#", "###"],
}
TICK = [(4, 3), (5, 4), (6, 5), (5, 6), (4, 7), (3, 8), (2, 9), (1, 10)]
# El borde de la matriz (38 LEDs) en el sentido del reloj, desde arriba en el centro:
# el "segundero" del pomodoro da una vuelta por minuto.
BORDER = ([(0, x) for x in range(6, COLS)] + [(y, COLS - 1) for y in range(1, ROWS)]
          + [(ROWS - 1, x) for x in range(COLS - 2, -1, -1)] + [(y, 0) for y in range(ROWS - 2, -1, -1)]
          + [(0, x) for x in range(1, 6)])
# 12 puntos en una elipse alrededor del centro, para la animación de pensar
ORBIT = [(round(3.5 - 3 * math.cos(2 * math.pi * k / 12)), round(6 + 4 * math.sin(2 * math.pi * k / 12)))
         for k in range(12)]
EVENTS = ("correct", "wrong", "thinking", "pomodoro", "gacha", "avatar", "dyscalculia")


def blank():
    return [[0] * COLS for _ in range(ROWS)]


def stamp(grid, rows, top, left, value=ON):
    for r, line in enumerate(rows):
        for c, ch in enumerate(line):
            y, x = top + r, left + c
            if ch != "." and 0 <= y < ROWS and 0 <= x < COLS:
                grid[y][x] = value if ch == "#" else min(value, MID)


class Matrix:
    """Estado de la matriz. frame() devuelve el fotograma actual (8 filas x 13 columnas)."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.events = deque()   # los añaden los hilos de la API; los consume frame()
        self.idle = "smile"
        self.pomo = None        # {"left", "total", "running", "since"}
        self.anim = None        # {"name", "start", ...}

    def push(self, event, arg):
        if event not in EVENTS or not isinstance(arg, str) or len(arg) > 40:
            return False
        self.events.append((event, arg))
        return True

    def state(self):
        return {"idle": self.idle, "pomodoro": self.pomo, "animation": self.anim and self.anim["name"]}

    def _handle(self, event, arg, now):
        if event in ("correct", "wrong"):
            self.anim = {"name": event, "start": now}
        elif event == "thinking":
            if arg == "start":
                self.anim = {"name": "thinking", "start": now, "stop": None}
            elif self.anim and self.anim["name"] == "thinking":
                self.anim["stop"] = max(now, self.anim["start"] + 1.5)  # que se vea al menos 1,5 s
        elif event == "gacha":
            won = arg if arg in ICONS else "smile"
            pool = [k for k in ICONS if k != "smile"]
            rng = random.Random(now)
            self.anim = {"name": "gacha", "start": now, "reel": [rng.choice(pool) for _ in range(12)] + [won]}
        elif event == "avatar":
            self.idle = arg if arg in ICONS else "smile"
        elif event == "pomodoro":
            parts = arg.split("-")
            if parts[0] in ("start", "pause") and len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
                self.pomo = {"left": int(parts[1]), "total": max(1, int(parts[2])),
                             "running": parts[0] == "start", "since": now}
            elif parts[0] == "reset":
                self.pomo = None
            elif parts[0] == "end":
                self.pomo = None
                self.anim = {"name": "pomo_end", "start": now}
        # "dyscalculia" se acepta, pero de momento no dibuja nada

    def frame(self):
        now = self.clock()
        while self.events:
            self._handle(*self.events.popleft(), now)
        if self.anim:
            grid = self._animation(now)
            if grid is not None:
                return grid
            self.anim = None
        return self._base(now)

    # -- fondo: cuenta atrás del pomodoro, o el avatar --
    def pomo_remaining(self, now):
        p = self.pomo
        return max(0.0, p["left"] - (now - p["since"]) if p["running"] else p["left"])

    def _base(self, now):
        grid = blank()
        if self.pomo is None:
            stamp(grid, ICONS[self.idle], 0, 2)
            return grid
        left = self.pomo_remaining(now)
        running = self.pomo["running"]
        # Como un reloj MM:SS: los dígitos son los minutos enteros que quedan y un punto
        # con estela recorre el borde una vez por minuto (los segundos).
        text = f"{min(99, int(left // 60)):02d}"
        value = ON if running else LOW  # en pausa, a media luz
        stamp(grid, DIGITS[text[0]], 1, 3, value)
        stamp(grid, DIGITS[text[1]], 1, 7, value)
        done = 1 - (left % 60) / 60 if left > 0 else 0.0  # parte del minuto ya pasada
        head = min(len(BORDER) - 1, int(done * len(BORDER)))
        trail = ((0, ON), (1, MID), (2, LOW)) if running else ((0, LOW),)
        for back, v in trail:
            y, x = BORDER[(head - back) % len(BORDER)]
            grid[y][x] = v
        return grid

    # -- animaciones cortas encima del fondo --
    def _animation(self, now):
        a, t = self.anim, now - self.anim["start"]
        grid = blank()
        if a["name"] == "correct":
            if t > 2.0:
                return None
            for y, x in TICK[:max(1, math.ceil(len(TICK) * min(1.0, t / 0.5)))]:
                grid[y][x] = ON
            return grid
        if a["name"] == "wrong":
            if t > 2.0:
                return None
            shift = (0, -1, 0, 1)[int(t / 0.2) % 4] if t < 0.8 else 0
            for i in range(6):
                for y, x in ((1 + i, 4 + i + shift), (1 + i, 9 - i + shift)):
                    if 0 <= x < COLS:
                        grid[y][x] = ON
            return grid
        if a["name"] == "thinking":
            if (a["stop"] is not None and now >= a["stop"]) or t > 60:
                return None
            head = int(t / 0.1) % len(ORBIT)
            for back, value in ((2, LOW), (1, MID), (0, ON)):
                y, x = ORBIT[(head - back) % len(ORBIT)]
                grid[y][x] = value
            return grid
        if a["name"] == "gacha":
            spin, hold = 3.2, 2.5
            if t > spin + hold:
                return None
            travel = (len(a["reel"]) - 1) * 9  # icono de 8 filas + 1 de separación
            offset = round((1 - (1 - min(1.0, t / spin)) ** 3) * travel)  # frena al final
            for i, icon in enumerate(a["reel"]):
                top = i * 9 - offset
                if -8 < top < ROWS:
                    stamp(grid, ICONS[icon], top, 2)
            if t > spin:  # destellos en las esquinas, 1 vez por segundo
                corners = ((0, 0), (7, 12)) if int(t * 2) % 2 else ((0, 12), (7, 0))
                for y, x in corners:
                    grid[y][x] = ON
            return grid
        if a["name"] == "pomo_end":
            if t > 4.0:
                return None
            value = ON if int(t) % 2 == 0 else LOW  # late una vez por segundo
            stamp(grid, DIGITS["0"], 1, 3, value)
            stamp(grid, DIGITS["0"], 1, 7, value)
            for y, x in BORDER:
                grid[y][x] = value
            return grid
        return None


matrix = Matrix()

try:
    import numpy as np
    from arduino.app_bricks.web_ui import WebUI
    from arduino.app_utils import App, Bridge, Frame
except ImportError:  # fuera de App Lab, p. ej. al probar las animaciones en el portátil
    App = None

if App is not None:
    last = {"frame": None}

    def on_event(event: str, arg: str):
        return {"ok": matrix.push(event, arg)}

    def loop():
        grid = matrix.frame()
        key = tuple(map(tuple, grid))
        if key != last["frame"]:
            Bridge.call("draw", Frame(np.array(grid, dtype=np.uint8)).to_board_bytes())
            last["frame"] = key
        time.sleep(0.04)

    ui = WebUI()
    ui.expose_api("GET", "/led/{event}/{arg}", on_event)
    ui.expose_api("GET", "/state", matrix.state)
    App.run(user_loop=loop)
