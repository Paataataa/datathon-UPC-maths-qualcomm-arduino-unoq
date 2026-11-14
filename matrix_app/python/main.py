"""studIA matrix: la matriz LED 8x13 del tutor (App Lab, parte Linux).

studia_server.py envía eventos a  GET http://127.0.0.1:7000/led/<evento>/<arg>
    correct / -              tick que se dibuja
    wrong / -                cruz que tiembla
    thinking / start|stop    un punto que da vueltas mientras el LLM piensa
    pomodoro / start-S-T     cuenta atrás (S segundos que quedan de T): los minutos en grande
                             y un punto que da una vuelta al borde por minuto
    pomodoro / pause-S-T | reset | end
    gacha / <icono>          rodillo de tragaperras que se para en el objeto ganado
    avatar / <icono>         el avatar equipado (sale en la animación de espera)

Nunca se superponen dos imágenes: las animaciones se ponen en cola y cada una se ve
entera; al acabar vuelve el fondo (el pomodoro si está en marcha; si no, la animación
de espera: el avatar bailando, "STUDIA" desplazándose, un monigote bailando y estrellas).

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
LETTERS = {
    "S": ["###", "#..", "###", "..#", "###"], "T": ["###", ".#.", ".#.", ".#.", ".#."],
    "U": ["#.#", "#.#", "#.#", "#.#", "###"], "D": ["##.", "#.#", "#.#", "#.#", "##."],
    "I": ["###", ".#.", ".#.", ".#.", "###"], "A": [".#.", "#.#", "###", "#.#", "#.#"],
}
# Monigote 5x8 que baila: brazos arriba y piernas abiertas / brazos abajo y piernas juntas
DANCER = [
    ["#.#.#", ".###.", "..#..", "..#..", ".#.#.", ".#.#.", "#...#", "....."],
    ["..#..", ".###.", "#.#.#", "..#..", "..#..", ".#.#.", ".#.#.", "....."],
]
# Muñeco de espaldas meneando el culete: el trasero a la izquierda / a la derecha
WIGGLE = [
    ["......##.....", "......##.....", ".....###.....", ".....##......",
     "...##.##.....", "...#####.....", "....#..#.....", "....#..#....."],
    [".....##......", ".....##......", ".....###.....", "......##.....",
     "......##.##..", "......#####..", ".....#..#....", ".....#..#...."],
]
# Guerrero con espada de luz: la empuñadura está a la derecha de la mano
WARRIOR = ["....", ".#..", "###.", ".#.#", ".#..", "#.#.", "#.#.", "...."]
HILT = (3, 4)
SWINGS = ((-1, 1), (0, 1), (1, 1), (0, 1), (-1, 0), (-1, 1))  # (dy, dx) de la hoja
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
DURATION = {"correct": 2.0, "wrong": 2.0, "gacha": 5.7, "pomo_end": 4.0, "avatar_show": 3.0}
MAX_QUEUE = 4          # si llegan muchos eventos seguidos, los más antiguos se descartan
STANDBY = (("dance_avatar", 6.0), ("title", None), ("dancer", 6.0), ("wiggle", 5.0),
           ("saber", 6.2), ("stars", 4.0))


def blank():
    return [[0] * COLS for _ in range(ROWS)]


def stamp(grid, rows, top, left, value=ON):
    for r, line in enumerate(rows):
        for c, ch in enumerate(line):
            y, x = top + r, left + c
            if ch != "." and 0 <= y < ROWS and 0 <= x < COLS:
                grid[y][x] = value if ch == "#" else min(value, MID)


def title_width():
    return len("STUDIA") * 4 - 1


class Matrix:
    """Estado de la matriz. frame() devuelve el fotograma actual (8 filas x 13 columnas)."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.events = deque()   # los añaden los hilos de la API; los consume frame()
        self.idle = "smile"
        self.pomo = None        # {"left", "total", "running", "since"}
        self.anim = None        # la animación que se ve ahora: {"name", "start", ...}
        self.queue = deque()    # las que esperan su turno
        self.standby_start = None

    def push(self, event, arg):
        if event not in EVENTS or not isinstance(arg, str) or len(arg) > 40:
            return False
        self.events.append((event, arg))
        return True

    def state(self):
        return {"idle": self.idle, "pomodoro": self.pomo, "animation": self.anim and self.anim["name"],
                "queued": [a["name"] for a in self.queue]}

    def _enqueue(self, anim):
        if len(self.queue) >= MAX_QUEUE:
            self.queue.popleft()
        self.queue.append(anim)

    def _handle(self, event, arg, now):
        if event in ("correct", "wrong"):
            self._enqueue({"name": event})
        elif event == "thinking":
            if arg == "start":
                self._enqueue({"name": "thinking", "stop": None})
            else:  # "stop": la última animación de pensar (en curso o en cola) termina
                for a in [self.anim, *reversed(self.queue)]:
                    if a and a["name"] == "thinking" and a["stop"] is None:
                        a["stop"] = now
                        break
        elif event == "gacha":
            won = arg if arg in ICONS else "smile"
            pool = [k for k in ICONS if k != "smile"]
            rng = random.Random(now)
            self._enqueue({"name": "gacha", "reel": [rng.choice(pool) for _ in range(12)] + [won]})
        elif event == "avatar":
            # Al equipar algo, el avatar sale enseguida (y la espera empieza por él)
            self.idle = arg if arg in ICONS else "smile"
            self.queue = deque(a for a in self.queue if a["name"] != "avatar_show")
            self._enqueue({"name": "avatar_show"})
        elif event == "pomodoro":
            parts = arg.split("-")
            if parts[0] in ("start", "pause") and len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
                self.pomo = {"left": int(parts[1]), "total": max(1, int(parts[2])),
                             "running": parts[0] == "start", "since": now}
            elif parts[0] == "reset":
                self.pomo = None
            elif parts[0] == "end":
                self.pomo = None
                self._enqueue({"name": "pomo_end"})
        # "dyscalculia" se acepta, pero de momento no dibuja nada

    def frame(self):
        now = self.clock()
        while self.events:
            self._handle(*self.events.popleft(), now)
        while True:
            if self.anim is None:
                if not self.queue:
                    break
                self.anim = self.queue.popleft()
                self.anim["start"] = now  # empieza cuando le toca, no cuando llegó
            grid = self._animation(now)
            if grid is not None:
                return grid
            self.anim = None
        return self._base(now)

    # -- fondo: cuenta atrás del pomodoro, o la animación de espera --
    def pomo_remaining(self, now):
        p = self.pomo
        return max(0.0, p["left"] - (now - p["since"]) if p["running"] else p["left"])

    def _base(self, now):
        if self.pomo is None:
            return self._standby(now)
        self.standby_start = None
        grid = blank()
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

    def _standby(self, now):
        """Animación de espera: el avatar bailando, el título, un monigote y estrellas."""
        if self.standby_start is None:
            self.standby_start = now
        phases = [(name, title_width() / 5 + 2.6 if length is None else length) for name, length in STANDBY]
        t = (now - self.standby_start) % sum(length for _, length in phases)
        for name, length in phases:
            if t < length:
                break
            t -= length
        grid = blank()
        if name == "dance_avatar":
            dy = 0 if int(t / 0.4) % 2 == 0 else 1          # sube y baja
            dx = (0, 1, 0, -1)[int(t / 0.8) % 4]            # y se balancea
            stamp(grid, ICONS[self.idle], dy, 2 + dx)
        elif name == "title":
            x = COLS - int(t * 5)                           # 5 columnas por segundo: sin parpadeos
            for i, ch in enumerate("STUDIA"):
                stamp(grid, LETTERS[ch], 1, x + i * 4)
        elif name == "dancer":
            pose = DANCER[int(t / 0.4) % 2]
            dx = (0, 2, 0, -2)[int(t / 0.8) % 4]
            stamp(grid, pose, 0, 4 + dx)
        elif name == "wiggle":
            stamp(grid, WIGGLE[int(t / 0.35) % 2], 0, 0)
        elif name == "saber":
            stamp(grid, WARRIOR, 0, 0)
            # 0,8 s quieto; 1 s encendiéndola; la blande; 0,8 s apagándola
            if t < 0.8:
                length, swing = 0, 0
            elif t < 1.8:
                length, swing = math.ceil((t - 0.8) / 1.0 * 8), 0
            elif t < 1.8 + 0.45 * len(SWINGS):
                length, swing = 8, int((t - 1.8) / 0.45)
            else:
                length = max(0, 8 - math.ceil((t - 1.8 - 0.45 * len(SWINGS)) / 0.8 * 8))
                swing = 0
            y, x = HILT
            grid[y][x] = MID
            dy, dx = SWINGS[swing]
            for i in range(1, length + 1):
                by, bx = y + dy * i, x + dx * i
                if 0 <= by < ROWS and 0 <= bx < COLS:
                    grid[by][bx] = ON
        else:  # estrellas: cada segundo, 6 en otro sitio, que se encienden y apagan suave
            rng = random.Random(int(now))
            level = round(math.sin(math.pi * (now % 1.0)) * ON)  # 0 al cambiar de sitio
            for _ in range(6):
                grid[rng.randrange(ROWS)][rng.randrange(COLS)] = level
        return grid

    # -- animaciones cortas, una cada vez --
    def _animation(self, now):
        a = self.anim
        t = now - a["start"]
        grid = blank()
        if a["name"] in DURATION and t > DURATION[a["name"]]:
            if a["name"] == "avatar_show":
                self.standby_start = None  # la espera vuelve a empezar por el avatar bailando
            return None
        if a["name"] == "avatar_show":
            top = min(0, round(-8 + 8 * t / 0.4))  # baja desde arriba en 0,4 s
            stamp(grid, ICONS[self.idle], top, 2)
            if t > 0.4:  # destellos a los lados, 1 vez por segundo
                side = ((1, 0), (6, 12)) if int(t * 2) % 2 else ((1, 12), (6, 0))
                for y, x in side:
                    grid[y][x] = ON
            return grid
        if a["name"] == "correct":
            for y, x in TICK[:max(1, math.ceil(len(TICK) * min(1.0, t / 0.5)))]:
                grid[y][x] = ON
            return grid
        if a["name"] == "wrong":
            shift = (0, -1, 0, 1)[int(t / 0.2) % 4] if t < 0.8 else 0
            for i in range(6):
                for y, x in ((1 + i, 4 + i + shift), (1 + i, 9 - i + shift)):
                    if 0 <= x < COLS:
                        grid[y][x] = ON
            return grid
        if a["name"] == "thinking":
            # Se ve al menos 1,5 s aunque la respuesta ya estuviera preparada
            if (a["stop"] is not None and now >= max(a["stop"], a["start"] + 1.5)) or t > 60:
                return None
            head = int(t / 0.1) % len(ORBIT)
            for back, value in ((2, LOW), (1, MID), (0, ON)):
                y, x = ORBIT[(head - back) % len(ORBIT)]
                grid[y][x] = value
            return grid
        if a["name"] == "gacha":
            spin = 3.2
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
