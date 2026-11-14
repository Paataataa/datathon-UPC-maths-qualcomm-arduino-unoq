#!/usr/bin/env python3
"""studIA: la app web del tutor (servidor + API JSON). Solo librería estándar.

    python3 studia_server.py --mock          # portátil, sin placa ni túneles
    python3 studia_server.py                 # en la placa (o con túnel a 8081/8082)

Abre http://127.0.0.1:8001. Necesita data/index.db, data/studia.db (build_studia_db.py)
y crea data/users.db sola. No modifica copilot/.

Seguridad anti-spoiler: el navegador solo recibe problemas pasados por DBManager.public()
(sin "label"). La respuesta se corrige SIEMPRE aquí, releyendo el problema con get_problem().
Al navegador tampoco llega nunca raw_text (la salida del LLM antes del guardrail).

Matriz LED: los eventos (acierto, fallo, pensar, pomodoro, gacha, avatar) se envían a la app
de App Lab de matrix_app/ (puerto 7000 de la placa). Si no está en marcha, se ignoran.
"""
import argparse
import json
import os
import queue
import random
import secrets
import sqlite3
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from copilot import llm_helper  # noqa: E402
from copilot.client import get  # noqa: E402
from copilot.config import CONFIG  # noqa: E402
from db_manager import DBManager, MAX_LEVEL, VECTOR_DIM  # noqa: E402

STATIC = os.path.join(ROOT, "static")
LANGS = ("cat", "eng")  # los idiomas que prepara el README (paso 9: --languages eng,cat)
MODES = ("practice", "test")

# Monedas. Práctica: una vez por problema acertado. Test: solo al acabarlo, en proporción a
# la nota y solo si apruebas. Un test perfecto da 1,5 veces lo que darían sus 8 problemas
# acertados en la práctica.
COINS_PER_CORRECT = 10
TEST_TOTAL = 8
TEST_PASS = 0.5
TEST_MAX_COINS = 120
GACHA_COST = 30

# Catálogo de la tienda. Los avatares conservan sus ids antiguos (sin prefijo) por
# compatibilidad con users.db. "led" es el icono que dibuja la matriz (matrix_app).
AVATARS = ("fox", "owl", "cat", "robot", "rocket", "dino", "panda", "unicorn")
SLOTS = ("avatar", "theme", "frame", "title")
ITEMS = {}
for _id in AVATARS:
    ITEMS[_id] = {"kind": "avatar", "price": 40, "led": _id}
for _id in ("ocean", "forest", "sunset", "candy"):
    ITEMS["theme:" + _id] = {"kind": "theme", "price": 60, "led": "palette"}
for _id in ("gold", "rainbow", "stars", "fire"):
    ITEMS["frame:" + _id] = {"kind": "frame", "price": 50, "led": "frame"}
for _id in ("explorer", "logic", "numbers", "geometry"):
    ITEMS["title:" + _id] = {"kind": "title", "price": 30, "led": "crown"}

# Eventos de la matriz LED 8x13. Los dos últimos los envía la página; el resto, el servidor.
HARDWARE_EVENTS = ("correct", "wrong", "thinking", "gacha", "avatar", "pomodoro", "dyscalculia")
PAGE_EVENTS = ("pomodoro", "dyscalculia")
POMODORO_ACTIONS = ("start", "pause", "reset", "end")
LLM_FIELDS = ("text", "seconds", "leaked_and_blocked", "block_reason", "status",
              "prompt_tokens", "cached_tokens")  # lista blanca: raw_text NUNCA sale de aquí


# ------------------------------------------------------------------ hardware

LED = {"url": "http://127.0.0.1:7000", "queue": queue.Queue(), "warned": False}


def notify_hardware(event, arg=None):
    """Envía un evento a la matriz LED (matrix_app, en App Lab) sin bloquear.

    event: uno de HARDWARE_EVENTS. arg: texto corto, p. ej. "start" para "thinking",
    "start-1500-1500" para "pomodoro" o el icono "fox" para "gacha" y "avatar".
    Un solo hilo los envía en orden. Nunca lanza: un fallo de la matriz no rompe la clase.
    """
    if event not in HARDWARE_EVENTS:
        raise ValueError(f"evento de hardware desconocido: {event}")
    arg = str(arg) if arg else "-"
    print(f"  [hardware] {event} {arg}", file=sys.stderr)
    if LED["url"]:
        LED["queue"].put(f"{LED['url']}/led/{quote(event, safe='')}/{quote(arg, safe='')}")


def _led_sender():
    while True:
        url = LED["queue"].get()
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                response.read()
            LED["warned"] = False
        except OSError as error:
            if not LED["warned"]:  # un aviso, no uno por evento
                print(f"  [hardware] matriz LED no disponible ({error}); sigo sin ella",
                      file=sys.stderr)
                LED["warned"] = True


threading.Thread(target=_led_sender, name="led-sender", daemon=True).start()


# ------------------------------------------------------------- precálculo LLM

class Precompute:
    """Prepara las respuestas del LLM en cuanto se carga un problema de práctica.

    La placa tiene un solo slot de LLM: una cola con un único hilo. Primero la pista,
    después la explicación de cada opción incorrecta (el servidor conoce la respuesta;
    el navegador no). Al cargar otro problema se descarta lo pendiente del anterior.

    El alumno va siempre primero: si pide algo que no es lo que la cola está calculando,
    la tarea en curso se corta (llm_helper.Cancel) y vuelve a la cola para más tarde.
    """

    def __init__(self):
        self.cv = threading.Condition()
        self.jobs = []        # [(clave, función(cancel))]; la primera es la siguiente
        self.results = {}     # clave -> resultado de llm_helper
        self.running = None   # (clave, función, Cancel) en curso
        self.current = None   # (id, idioma) del problema actual
        self.held = 0         # llamadas directas del alumno en curso: la cola espera
        self.direct_calls = []  # [(id, idioma), Cancel] de esas llamadas directas
        threading.Thread(target=self._worker, name="precompute", daemon=True).start()

    def _cut_running(self, unless=None):
        """Corta la tarea de fondo en curso (salvo que sea `unless`). Con self.cv cogido."""
        if self.running and self.running[0] != unless:
            self.running[2].cancel()

    def direct(self, base, compute):
        """Llamada directa al LLM (una duda libre, o algo que no estaba en la cola).
        Corta la tarea de fondo y la cola no empieza otra hasta que acaba: el alumno va primero.
        compute(cancel) recibe un Cancel: si el alumno cambia de problema, se corta."""
        cancel = llm_helper.Cancel()
        with self.cv:
            self.held += 1
            self._cut_running()
            self.direct_calls.append((base, cancel))
        try:
            return compute(cancel)
        finally:
            with self.cv:
                self.held -= 1
                self.direct_calls = [(b, c) for b, c in self.direct_calls if c is not cancel]
                self.cv.notify_all()

    def start_problem(self, problem):
        base = (problem["id"], problem["language"])
        jobs = [(base + ("hint",), lambda c: llm_helper.get_socratic_hint(problem, cancel=c))]
        for letter in sorted(problem["options"]):
            if letter != problem["label"]:
                jobs.append((base + ("wrong", letter),
                             lambda c, l=letter: llm_helper.explain_wrong_answer(problem, l, cancel=c)))
        with self.cv:
            self.current = base
            if self.running and self.running[0][:2] != base:
                self.running[2].cancel()  # el problema anterior ya no interesa
            for b, cancel in self.direct_calls:
                if b != base:
                    cancel.cancel()  # tampoco lo que el alumno pidió de él
            self.results = {k: v for k, v in self.results.items() if k[:2] == base}
            busy = self.running[0] if self.running else None
            self.jobs = [(k, fn) for k, fn in jobs if k not in self.results and k != busy]
            self.cv.notify_all()

    def get(self, key, compute, timeout=60):
        """Devuelve (resultado, precalculado). Si está en cola lo adelanta, corta lo que
        se esté calculando y lo espera; si no está en la cola, lo calcula aquí (compute(cancel)).
        Devuelve (None, False) si mientras tanto el alumno ha cambiado de problema."""
        deadline = time.monotonic() + timeout
        with self.cv:
            if key in self.results:
                return self.results[key], True
            for i, (k, _fn) in enumerate(self.jobs):
                if k == key:
                    self.jobs.insert(0, self.jobs.pop(i))
                    self._cut_running(unless=key)
                    self.cv.notify_all()
                    break
            while key not in self.results:
                pending = (self.running and self.running[0] == key) or any(k == key for k, _fn in self.jobs)
                left = deadline - time.monotonic()
                if not pending or left <= 0:
                    break
                self.cv.wait(left)
            if key in self.results:
                return self.results[key], True
            if key[:2] != self.current:
                return None, False
        return self.direct(key[:2], compute), False

    def _worker(self):
        while True:
            with self.cv:
                while not self.jobs or self.held:
                    self.cv.wait()
                key, fn = self.jobs.pop(0)
                cancel = llm_helper.Cancel()
                self.running = (key, fn, cancel)
            try:
                result = fn(cancel)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                result = None
            with self.cv:
                self.running = None
                status = str((result or {}).get("status", "error"))
                if cancel.is_set() or status == "cancelled":
                    # Cortada para atender al alumno: vuelve a la cola, detrás de lo que
                    # él necesita (que ya estará el primero), si sigue siendo de este problema.
                    if key[:2] == self.current and key not in self.results:
                        self.jobs.insert(1 if self.jobs else 0, (key, fn))
                elif result is not None and not status.startswith("error") and key[:2] == self.current:
                    # Un error (modelo caído) no se guarda: se reintenta cuando el alumno lo pida.
                    self.results[key] = result
                self.cv.notify_all()


# ----------------------------------------------------------------------- app

class ApiError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def ensure_student(db, username):
    """Id del alumno por defecto de la demo (lo crea si no existe)."""
    try:
        return db.create_student(username)
    except sqlite3.IntegrityError:
        pass
    for student_id in range(1, 500):
        student = db.get_student(student_id)
        if student and student["username"] == username:
            return student_id
    raise SystemExit(f"No encuentro al alumno {username!r} en users.db")


def is_up(base):
    try:
        return get(base + "/health", timeout=2).get("status") == "ok"
    except (OSError, ValueError):
        return False


def fake_vector(problem):
    """Vector determinista de 384 dimensiones: sustituye al servidor 8082 en modo --mock."""
    seed = int.from_bytes(sha256(problem["id"].encode()).digest()[:8], "big")
    rng = random.Random(seed)
    vec = [rng.gauss(0, 1) for _ in range(VECTOR_DIM)]
    norm = sum(x * x for x in vec) ** 0.5
    return [x / norm for x in vec]


def clean_id(value):
    if not isinstance(value, str) or not value or len(value) > 80:
        raise ApiError(400, "problem_id inválido")
    return value


def clean_language(value):
    if value not in LANGS:
        raise ApiError(400, f"language debe ser uno de {', '.join(LANGS)}")
    return value


def clean_mode(value):
    value = value or "practice"
    if value not in MODES:
        raise ApiError(400, "mode debe ser practice o test")
    return value


def clean_int(value, low, high, name):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ApiError(400, f"{name} debe ser un entero") from None
    if not low <= number <= high:
        raise ApiError(400, f"{name} fuera de rango")
    return number


def llm_view(result, precomputed=False, waited=None):
    out = {key: result.get(key) for key in LLM_FIELDS}
    out["precomputed"] = precomputed
    if waited is not None:
        out["waited"] = round(waited, 2)  # lo que esperó el alumno, no lo que tardó el modelo
    return out


class App:
    def __init__(self, data_dir, mock, username):
        for name in ("index.db", "studia.db"):
            path = os.path.join(data_dir, name)
            if not os.path.isfile(path):
                hint = "build_index.py" if name == "index.db" else "build_studia_db.py"
                raise SystemExit(f"Falta {path}. Se genera con {hint} (o cópiala de la placa).")
        self.mock = mock
        self.db = DBManager(os.path.join(data_dir, "index.db"), os.path.join(data_dir, "studia.db"),
                            os.path.join(data_dir, "users.db"))
        if mock:
            # Sin servidor de embeddings (8082): los problemas que no están en index.db
            # (los de validación) usan un vector falso, solo para poder probar la web.
            self.db._live_english_vector = fake_vector
            llm_helper.set_mock(True)
        self.student_id = ensure_student(self.db, username)
        self.rewarded = set()          # problemas de práctica ya premiados: sin granja de monedas
        self.tried = {}                # (id, idioma) -> letras falladas: solo esas se explican
        self.tests = {}                # sesiones de test en curso
        self.lock = threading.Lock()   # todo lo que cambia monedas, de uno en uno
        self.ai_lock = threading.Lock()  # una sola petición del alumno a la IA a la vez
        self.pre = Precompute()

    def _ai_call(self, fn):
        """Ejecuta fn() si no hay otra petición del alumno a la IA en curso; si la hay, 429.
        La placa solo atiende al modelo de uno en uno: dos clics no deben encolar dos llamadas."""
        if not self.ai_lock.acquire(blocking=False):
            raise ApiError(429, "studIA ya está pensando: espera a que termine")
        try:
            return fn()
        finally:
            self.ai_lock.release()

    # -- lectura de studia.db (solo lectura, tablas theory y modes) --
    def _studia(self):
        uri = self.db.studia_path.resolve().as_uri() + "?mode=ro"
        con = sqlite3.connect(uri, uri=True)
        con.row_factory = sqlite3.Row
        return con

    def status(self):
        base = f"http://127.0.0.1:{CONFIG['LLM_PORT']}"
        embed = f"http://127.0.0.1:{CONFIG['EMBED_PORT']}"
        return {"mock": self.mock, "llm": True if self.mock else is_up(base),
                "embed": True if self.mock else is_up(embed), "student_id": self.student_id}

    def theory(self, query):
        language = clean_language(query.get("language"))
        con = self._studia()
        try:
            names = [(r["topic"], r["name"]) for r in
                     con.execute("SELECT topic, name FROM theory WHERE language=? ORDER BY rowid", (language,))]
            modes = {r["mode"]: r["name"] for r in
                     con.execute("SELECT mode, name FROM modes WHERE language=?", (language,))}
        finally:
            con.close()
        topics = [{"topic": t, "name": n, "content": self.db.get_theory(t, language)} for t, n in names]
        out = {"language": language, "topics": topics, "modes": modes}
        if query.get("topic"):
            chosen = next((t for t in topics if t["topic"] == query["topic"]), None)
            if chosen is None:
                raise ApiError(404, "tema desconocido")
            out["selected"] = chosen
        return out

    def problem(self, query):
        language = clean_language(query.get("language"))
        mode = clean_mode(query.get("mode"))
        if mode == "test":
            # Solo para cambiar de idioma la pregunta ACTUAL; las nuevas las da el test.
            session = self.tests.get(query.get("session"))
            if session is None or session["done"] or session["current"] != query.get("id"):
                raise ApiError(403, "los problemas del test los da /api/test/start")
            problem = self.db.get_problem(clean_id(query["id"]), language)
            if problem is None:
                raise ApiError(404, "problema desconocido")
            return self.db.public(problem)
        if query.get("id"):
            problem = self.db.get_problem(clean_id(query["id"]), language)
        else:
            level = clean_int(query.get("level", 0), 0, MAX_LEVEL, "level")
            problem = self._pick(language, level, query.get("topic") or None, query.get("skip"))
        if problem is None:
            raise ApiError(404, "no hay problemas con esos filtros")
        # Mientras el alumno lee, la placa ya prepara la pista y las explicaciones.
        self.pre.start_problem(problem)
        return self.db.public(problem)

    def _pick(self, language, level, topic, skip):
        sql = ("SELECT id FROM problems WHERE language=? AND level=? "
               "AND options NOT IN ('', '{}', 'null') "
               "AND id IN (SELECT id FROM problems WHERE language='eng')")
        args = [language, level]
        if topic:
            sql += " AND topic=?"
            args.append(topic)
        con = self._studia()
        try:
            ids = [row["id"] for row in con.execute(sql, args)]
        finally:
            con.close()
        if not ids:
            return None
        seen = set(self.db.attempted_problem_ids(self.student_id)) | {skip}
        fresh = [i for i in ids if i not in seen] or [i for i in ids if i != skip] or ids
        return self.db.get_problem(random.choice(fresh), language)

    def _full(self, query_or_body):
        """Problema CON label, solo para uso interno del servidor."""
        problem = self.db.get_problem(clean_id(query_or_body.get("problem_id")),
                                      clean_language(query_or_body.get("language")))
        if problem is None:
            raise ApiError(404, "problema desconocido")
        return problem

    def hint(self, body):
        if clean_mode(body.get("mode")) == "test":
            raise ApiError(403, "no hay pistas en el modo test")
        problem = self._full(body)
        question = body.get("question")
        if question is not None and not isinstance(question, str):
            raise ApiError(400, "question debe ser texto")
        question = (question or "").strip()[:80] or None
        started = time.perf_counter()

        def run():
            notify_hardware("thinking", "start")
            try:
                if question:
                    return self.pre.direct((problem["id"], problem["language"]), lambda c: llm_helper.get_socratic_hint(
                        problem, student_question=question, cancel=c)), False
                key = (problem["id"], problem["language"], "hint")
                return self.pre.get(key, lambda c: llm_helper.get_socratic_hint(problem, cancel=c))
            finally:
                notify_hardware("thinking", "stop")

        result, pre = self._ai_call(run)
        return self._answer(result, pre, started)

    @staticmethod
    def _answer(result, pre, started):
        if result is None or result.get("status") == "cancelled":
            # El alumno cambió de problema mientras la IA pensaba: esta respuesta ya no sirve.
            raise ApiError(409, "has cambiado de problema")
        return llm_view(result, pre, time.perf_counter() - started)

    def explain(self, body):
        """Por qué falla una opción que el alumno YA eligió (nunca otra: no se puede sondear)."""
        if clean_mode(body.get("mode")) == "test":
            raise ApiError(403, "no hay explicaciones en el modo test")
        problem = self._full(body)
        choice = body.get("choice")
        if choice not in self.tried.get((problem["id"], problem["language"]), set()):
            raise ApiError(403, "solo se explican las opciones que ya has elegido")
        started = time.perf_counter()
        key = (problem["id"], problem["language"], "wrong", choice)
        result, pre = self._ai_call(
            lambda: self.pre.get(key, lambda c: llm_helper.explain_wrong_answer(problem, choice, cancel=c)))
        return self._answer(result, pre, started)

    def similar(self, query):
        if clean_mode(query.get("mode")) == "test":
            raise ApiError(403, "no hay ayudas en el modo test")
        problem_id = clean_id(query.get("problem_id"))
        language = clean_language(query.get("language"))
        started = time.perf_counter()
        source = self.db.get_problem(problem_id, language)
        if source is None:
            raise ApiError(404, "problema desconocido")
        level = source["level"]
        try:
            # Primero uno de un curso por debajo (solo uno: no de cursos mucho más fáciles);
            # si no lo hay (o es el primer curso), uno del mismo curso.
            found = ((level > 0 and self.db.find_similar_easier(problem_id, language=language,
                                                                exact_level=level - 1))
                     or self.db.find_similar_easier(problem_id, language=language, exact_level=level))
        except ValueError as error:
            raise ApiError(404, str(error)) from None
        except (ConnectionError, RuntimeError, FileNotFoundError) as error:
            raise ApiError(503, f"búsqueda no disponible: {error}") from None
        seconds = round(time.perf_counter() - started, 3)
        if found is None:
            return {"example": None, "seconds": seconds}
        example = found["problem"]
        # Es OTRO problema ya resuelto (exclude_id): enseñar su solución es el objetivo de la pista.
        shown = self.db.public(example)
        shown["answer"] = example["label"]
        shown["answer_text"] = (example.get("options") or {}).get(example["label"], "")
        return {"example": shown, "similarity": found["similarity"], "seconds": seconds}

    def check(self, body):
        mode = clean_mode(body.get("mode"))
        language = clean_language(body.get("language"))
        problem = self._full(body)
        choice = body.get("choice")
        if choice not in problem["options"]:
            raise ApiError(400, "choice debe ser una de las opciones del problema")
        correct = choice == problem["label"]
        if mode == "test":
            return self._check_test(body.get("session"), problem, correct, language)
        # Práctica: respuesta al instante. La explicación del fallo la pide la página
        # aparte (/api/explain) y suele estar ya precalculada.
        with self.lock:
            self.db.record_problem_attempt(self.student_id, problem["id"], correct, language=language)
            out = {"correct": correct, "mode": mode, "coins_earned": 0}
            if correct:
                key = (self.student_id, problem["id"])
                if key not in self.rewarded:
                    self.db.add_coins(self.student_id, COINS_PER_CORRECT)
                    self.rewarded.add(key)
                    out["coins_earned"] = COINS_PER_CORRECT
            else:
                self.tried.setdefault((problem["id"], problem["language"]), set()).add(choice)
        notify_hardware("correct" if correct else "wrong")
        if correct:
            out["answer_text"] = problem["options"].get(choice, "")
        else:
            out["explain"] = True
        return out

    # -- test con sesión: el servidor sabe qué pregunta toca y premia una sola vez --
    def test_start(self, body):
        language = clean_language(body.get("language"))
        level = clean_int(body.get("level", 0), 0, MAX_LEVEL, "level")
        problem = self._pick(language, level, None, None)
        if problem is None:
            raise ApiError(404, "no hay problemas con esos filtros")
        session = secrets.token_hex(8)
        with self.lock:
            if len(self.tests) > 50:  # solo hay un alumno: no hace falta guardar más
                self.tests.clear()
            self.tests[session] = {"current": problem["id"], "language": language, "n": 0,
                                   "right": 0, "done": False}
        return {"session": session, "total": TEST_TOTAL, "pass_pct": round(100 * TEST_PASS),
                "max_coins": TEST_MAX_COINS, "problem": self.db.public(problem)}

    def _check_test(self, session_id, problem, correct, language):
        with self.lock:
            session = self.tests.get(session_id) if isinstance(session_id, str) else None
            if session is None or session["done"]:
                raise ApiError(403, "no hay ningún test en marcha con esa sesión")
            if session["current"] != problem["id"]:
                raise ApiError(409, "esa no es la pregunta actual del test")
            session["current"] = None  # nadie puede responderla dos veces
            self.db.record_problem_attempt(self.student_id, problem["id"], correct, language=language)
            session["n"] += 1
            session["right"] += int(correct)
            finished = session["n"] >= TEST_TOTAL
        notify_hardware("correct" if correct else "wrong")
        out = {"correct": correct, "mode": "test", "coins_earned": 0}
        nxt = None if finished else self._next_test(problem["id"], correct, language)
        with self.lock:
            if nxt is None:
                out["test_done"] = self._finish_test(session)
            else:
                session["current"] = nxt["problem"]["id"]
                out["next"] = nxt
        return out

    def test_abandon(self, body):
        """Abandonar el test: se cierra la sesión sin monedas. Los intentos ya respondidos
        quedan guardados en el progreso, como cualquier otro intento."""
        with self.lock:
            session = self.tests.get(body.get("session")) if isinstance(body.get("session"), str) else None
            if session is None or session["done"]:
                raise ApiError(404, "no hay ningún test en marcha con esa sesión")
            session["done"] = True
            return {"abandoned": True, "right": session["right"], "n": session["n"]}

    def _finish_test(self, session):
        """Nota = aciertos / preguntas respondidas. Monedas proporcionales a los aciertos
        sobre las 8 preguntas, y solo si apruebas. Se llama con self.lock cogido."""
        n, right = session["n"], session["right"]
        grade = right / n if n else 0.0
        passed = grade >= TEST_PASS
        coins = round(TEST_MAX_COINS * right / TEST_TOTAL) if passed else 0
        if coins:
            self.db.add_coins(self.student_id, coins)
        session["done"] = True
        return {"right": right, "n": n, "total": TEST_TOTAL, "grade_pct": round(100 * grade),
                "passed": passed, "pass_pct": round(100 * TEST_PASS), "coins_earned": coins,
                "ran_out": n < TEST_TOTAL}

    def _next_test(self, problem_id, correct, language):
        """Siguiente problema adaptativo (+1 / -1 de nivel), sin repetir. None si no hay."""
        try:
            result = self.db.next_test_problem(problem_id, was_correct=correct, language=language,
                                               student_id=self.student_id)
        except (ValueError, ConnectionError, RuntimeError, FileNotFoundError) as error:
            print(f"  [test] sin siguiente problema: {error}", file=sys.stderr)
            return None
        if result is None:
            return None
        return {"problem": result["problem"], "target_level": result["target_level"]}

    # -- perfil, tienda y gacha --
    def progress(self):
        data = self.db.get_progress(self.student_id)
        data["owned"] = [i for i in data.pop("avatars") if i in ITEMS]
        equipped = self.db.get_equipped(self.student_id)
        data["equipped"] = {slot: item for slot, item in equipped.items() if item in ITEMS}
        data["gacha_cost"] = GACHA_COST
        data["test_max_coins"] = TEST_MAX_COINS
        data["test_pass_pct"] = round(100 * TEST_PASS)
        data["coins_per_correct"] = COINS_PER_CORRECT
        return data

    def shop(self):
        data = self.progress()
        owned, equipped = set(data["owned"]), data["equipped"]
        data["items"] = [{"id": item_id, "kind": meta["kind"], "price": meta["price"],
                          "owned": item_id in owned, "equipped": equipped.get(meta["kind"]) == item_id}
                         for item_id, meta in ITEMS.items()]
        return data

    def buy(self, body):
        item_id = body.get("item")
        if item_id not in ITEMS:
            raise ApiError(400, "objeto desconocido")
        with self.lock:
            if item_id in self.db.get_avatars(self.student_id):
                raise ApiError(409, "ya tienes este objeto")
            try:
                self.db.spend_coins(self.student_id, ITEMS[item_id]["price"])
            except ValueError as error:
                raise ApiError(403, str(error)) from None
            self.db.unlock_avatar(self.student_id, item_id)
        return self.shop()

    def equip(self, body):
        slot, item_id = body.get("slot"), body.get("item")
        if slot not in SLOTS:
            raise ApiError(400, f"slot debe ser uno de {', '.join(SLOTS)}")
        if item_id is not None and (item_id not in ITEMS or ITEMS[item_id]["kind"] != slot):
            raise ApiError(400, "ese objeto no va en ese hueco")
        try:
            self.db.equip(self.student_id, slot, item_id)
        except ValueError as error:
            raise ApiError(403, str(error)) from None
        if slot == "avatar":
            notify_hardware("avatar", ITEMS[item_id]["led"] if item_id else "smile")
        return self.shop()

    def gacha(self):
        with self.lock:
            owned = set(self.db.get_avatars(self.student_id))
            locked = [i for i in ITEMS if i not in owned]
            if not locked:
                raise ApiError(409, "ya lo tienes todo")
            try:
                self.db.spend_coins(self.student_id, GACHA_COST)
            except ValueError as error:
                raise ApiError(403, str(error)) from None
            won = random.choice(locked)
            self.db.unlock_avatar(self.student_id, won)
        notify_hardware("gacha", ITEMS[won]["led"])  # la tirada se ve en la matriz
        return {"item": won, "kind": ITEMS[won]["kind"], "shop": self.shop()}

    def event(self, body):
        event = body.get("event")
        if event not in PAGE_EVENTS:
            raise ApiError(400, f"la página solo puede enviar: {', '.join(PAGE_EVENTS)}")
        payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
        if event == "pomodoro":
            action = payload.get("action")
            if action not in POMODORO_ACTIONS:
                raise ApiError(400, f"action debe ser una de {', '.join(POMODORO_ACTIONS)}")
            if action in ("start", "pause"):
                seconds = clean_int(payload.get("seconds"), 0, 7200, "seconds")
                total = clean_int(payload.get("total"), 1, 7200, "total")
                action = f"{action}-{seconds}-{total}"
            notify_hardware("pomodoro", action)
        else:
            notify_hardware("dyscalculia", "on" if payload.get("on") else "off")
        return {"ok": True}


ROUTES = {
    ("GET", "/api/status"): lambda app, q, b: app.status(),
    ("GET", "/api/problem"): lambda app, q, b: app.problem(q),
    ("GET", "/api/theory"): lambda app, q, b: app.theory(q),
    ("GET", "/api/similar"): lambda app, q, b: app.similar(q),
    ("GET", "/api/progress"): lambda app, q, b: app.progress(),
    ("GET", "/api/shop"): lambda app, q, b: app.shop(),
    ("POST", "/api/hint"): lambda app, q, b: app.hint(b),
    ("POST", "/api/explain"): lambda app, q, b: app.explain(b),
    ("POST", "/api/check"): lambda app, q, b: app.check(b),
    ("POST", "/api/test/start"): lambda app, q, b: app.test_start(b),
    ("POST", "/api/test/abandon"): lambda app, q, b: app.test_abandon(b),
    ("POST", "/api/buy"): lambda app, q, b: app.buy(b),
    ("POST", "/api/equip"): lambda app, q, b: app.equip(b),
    ("POST", "/api/gacha"): lambda app, q, b: app.gacha(),
    ("POST", "/api/event"): lambda app, q, b: app.event(b),
}


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "studIA"

        def log_message(self, fmt, *args):
            sys.stderr.write("  " + fmt % args + "\n")

        def send(self, code, body, kind="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > 16384:
                raise ApiError(413, "cuerpo demasiado grande")
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                raise ApiError(400, "JSON inválido") from None
            if not isinstance(body, dict):
                raise ApiError(400, "se esperaba un objeto JSON")
            return body

        def handle_request(self, method):
            url = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if method == "GET" and url.path in ("/", "/index.html"):
                    with open(os.path.join(STATIC, "index.html"), "rb") as f:
                        return self.send(200, f.read(), "text/html; charset=utf-8")
                route = ROUTES.get((method, url.path))
                if route is None:
                    raise ApiError(404, "not found")
                body = self.read_json() if method == "POST" else {}
                return self.send(200, route(app, query, body))
            except ApiError as error:
                return self.send(error.code, {"error": error.message})
            except ValueError as error:
                return self.send(400, {"error": str(error)})
            except (ConnectionError, RuntimeError, urllib.error.URLError) as error:
                return self.send(502, {"error": f"servidor de modelos: {error}"})
            except FileNotFoundError as error:
                return self.send(500, {"error": str(error)})
            except OSError as error:
                return self.send(502, {"error": f"servidor de modelos: {error}"})
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                return self.send(500, {"error": "error interno"})

        def do_GET(self):
            self.handle_request("GET")

        def do_POST(self):
            self.handle_request("POST")

    return Handler


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mock", action="store_true", help="LLM y embeddings simulados (sin placa)")
    p.add_argument("--data-dir", default="data")
    p.add_argument("--student", default="demo", help="alumno por defecto de la demo")
    p.add_argument("--host", default="127.0.0.1", help="0.0.0.0 para abrirlo a tu red")
    p.add_argument("--port", type=int, default=8001)
    p.add_argument("--led-url", default=LED["url"],
                   help="app de la matriz (matrix_app, App Lab). '' para no usarla")
    args = p.parse_args()
    os.chdir(ROOT)
    LED["url"] = args.led_url.rstrip("/")

    app = App(args.data_dir, args.mock, args.student)
    status = app.status()
    mode = "MOCK (simulado)" if args.mock else "real"
    print(f"studIA, modo {mode}. Alumno {args.student!r} (id {status['student_id']})")
    if not args.mock:
        print(f"  modelo de lenguaje {'activo' if status['llm'] else 'CAÍDO'}, "
              f"embeddings {'activos' if status['embed'] else 'CAÍDOS'}")
        if not status["llm"]:
            print("  Sin LLM las pistas devuelven la pista segura de reserva. "
                  "¿Túnel SSH a 8081/8082, o usa --mock?")
    print(f"  matriz LED: {LED['url'] or 'desactivada'}")
    avatar = app.db.get_equipped(app.student_id).get("avatar")
    notify_hardware("avatar", ITEMS[avatar]["led"] if avatar in ITEMS else "smile")
    print(f"Abre http://{args.host}:{args.port} (Ctrl+C para parar)", flush=True)
    ThreadingHTTPServer((args.host, args.port), make_handler(app)).serve_forever()


if __name__ == "__main__":
    main()
