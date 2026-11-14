#!/usr/bin/env python3
"""studIA: la app web del tutor (servidor + API JSON). Solo librería estándar.

    python3 studia_server.py --mock          # portátil, sin placa ni túneles
    python3 studia_server.py                 # en la placa (o con túnel a 8081/8082)

Abre http://127.0.0.1:8001. Necesita data/index.db, data/studia.db (build_studia_db.py)
y crea data/users.db sola. No modifica db_manager.py ni copilot/.

Seguridad anti-spoiler: el navegador solo recibe problemas pasados por DBManager.public()
(sin "label"). La respuesta se corrige SIEMPRE aquí, releyendo el problema con get_problem().
Al navegador tampoco llega nunca raw_text (la salida del LLM antes del guardrail).
"""
import argparse
import json
import os
import random
import sqlite3
import sys
import threading
import time
import traceback
import urllib.error
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from copilot import llm_helper  # noqa: E402
from copilot.client import get  # noqa: E402
from copilot.config import CONFIG  # noqa: E402
from db_manager import DBManager, MAX_LEVEL, VECTOR_DIM  # noqa: E402

STATIC = os.path.join(ROOT, "static")
LANGS = ("cat", "esp", "eng")
MODES = ("practice", "test")
COINS_PER_CORRECT = 10
GACHA_COST = 30  # monedas GANADAS por tirada: DBManager.add_coins no admite restar
AVATARS = ("fox", "owl", "cat", "robot", "rocket", "dino", "panda", "unicorn")
# Eventos de la matriz LED 8x13 (Rol 1). Los envía el servidor o, los dos últimos, la página.
HARDWARE_EVENTS = ("correct", "wrong", "pomodoro", "dyscalculia")
PAGE_EVENTS = ("pomodoro", "dyscalculia")
LLM_FIELDS = ("text", "seconds", "leaked_and_blocked", "block_reason", "status",
              "prompt_tokens", "cached_tokens")  # lista blanca: raw_text NUNCA sale de aquí


# ------------------------------------------------------------------ hardware

def notify_hardware(event, payload=None):
    """Punto de enlace con la placa. Rol 1: conecta aquí el Bridge de la matriz LED.

    event: "correct" | "wrong" | "pomodoro" | "dyscalculia".
    payload: dict opcional, p. ej. {"action": "start", "minutes": 25} para "pomodoro".

        # from arduino.app_utils import Bridge
        # Bridge.call("show_event", event)

    Nunca debe lanzar: un fallo de la matriz no puede romper la clase. Se ignora el error.
    """
    if event not in HARDWARE_EVENTS:
        raise ValueError(f"evento de hardware desconocido: {event}")
    try:
        print(f"  [hardware] {event}" + (f" {json.dumps(payload)}" if payload else ""),
              file=sys.stderr)
        # >>> Rol 1: Bridge aquí <<<
    except Exception as error:  # noqa: BLE001
        print(f"  [hardware] error ignorado: {error}", file=sys.stderr)


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


def llm_view(result):
    return {key: result.get(key) for key in LLM_FIELDS}


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
        self.rewarded = set()          # (alumno, problema) ya premiados: sin granja de monedas
        self.lock = threading.Lock()   # check y gacha cambian monedas: de uno en uno
        self._names = {}

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
        if query.get("id"):
            problem = self.db.get_problem(clean_id(query["id"]), language)
        else:
            level = clean_int(query.get("level", 0), 0, MAX_LEVEL, "level")
            problem = self._pick(language, level, query.get("topic") or None, query.get("skip"))
        if problem is None:
            raise ApiError(404, "no hay problemas con esos filtros")
        if mode == "practice":
            # Mientras el alumno lee, la placa ya procesa el enunciado (KV cache, 1 solo slot).
            # En el test no hay pistas: no gastamos el slot.
            llm_helper.prewarm_problem_cache(problem, async_mode=True)
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
        return llm_view(llm_helper.get_socratic_hint(problem, student_question=question))

    def similar(self, query):
        if clean_mode(query.get("mode")) == "test":
            raise ApiError(403, "no hay ayudas en el modo test")
        problem_id = clean_id(query.get("problem_id"))
        language = clean_language(query.get("language"))
        started = time.perf_counter()
        try:
            # Primero uno más fácil; si no lo hay (nivel 0) vale uno del mismo nivel.
            found = (self.db.find_similar_easier(problem_id, language=language, strictly_easier=True)
                     or self.db.find_similar_easier(problem_id, language=language))
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
        with self.lock:
            correct = choice == problem["label"]
            self.db.record_problem_attempt(self.student_id, problem["id"], correct, language=language)
            out = {"correct": correct, "mode": mode, "coins_earned": 0}
            if correct:
                key = (self.student_id, problem["id"])
                if key not in self.rewarded:
                    self.db.add_coins(self.student_id, COINS_PER_CORRECT)
                    self.rewarded.add(key)
                    out["coins_earned"] = COINS_PER_CORRECT
                notify_hardware("correct")
            else:
                notify_hardware("wrong")
        if mode == "practice":
            if correct:
                out["answer_text"] = problem["options"].get(choice, "")
            else:
                # Sin regalar la letra: el helper pasa su salida por el guardrail.
                out["explanation"] = llm_view(llm_helper.explain_wrong_answer(problem, choice))
        else:
            out["next"] = self._next_test(problem["id"], correct, language)
        return out

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

    def progress(self):
        data = self.db.get_progress(self.student_id)
        owned = [a for a in data["avatars"] if a in AVATARS]
        data["avatars"] = owned
        data["all_avatars"] = list(AVATARS)
        data["gacha_cost"] = GACHA_COST
        data["pulls"] = max(0, data["coins"] // GACHA_COST - len(owned))
        return data

    def gacha(self):
        with self.lock:
            data = self.progress()
            if data["pulls"] <= 0:
                raise ApiError(403, f"necesitas {GACHA_COST} monedas ganadas por cada tirada")
            locked = [a for a in AVATARS if a not in data["avatars"]]
            if not locked:
                raise ApiError(409, "ya tienes todos los avatares")
            won = random.choice(locked)
            self.db.unlock_avatar(self.student_id, won)
        return {"avatar": won, "progress": self.progress()}

    def event(self, body):
        event = body.get("event")
        if event not in PAGE_EVENTS:
            raise ApiError(400, f"la página solo puede enviar: {', '.join(PAGE_EVENTS)}")
        payload = body.get("payload")
        if not isinstance(payload, dict) or len(json.dumps(payload)) > 300:
            payload = None
        notify_hardware(event, payload)
        return {"ok": True}


ROUTES = {
    ("GET", "/api/status"): lambda app, q, b: app.status(),
    ("GET", "/api/problem"): lambda app, q, b: app.problem(q),
    ("GET", "/api/theory"): lambda app, q, b: app.theory(q),
    ("GET", "/api/similar"): lambda app, q, b: app.similar(q),
    ("GET", "/api/progress"): lambda app, q, b: app.progress(),
    ("POST", "/api/hint"): lambda app, q, b: app.hint(b),
    ("POST", "/api/check"): lambda app, q, b: app.check(b),
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
    args = p.parse_args()
    os.chdir(ROOT)

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
    print(f"Abre http://{args.host}:{args.port} (Ctrl+C para parar)", flush=True)
    ThreadingHTTPServer((args.host, args.port), make_handler(app)).serve_forever()


if __name__ == "__main__":
    main()
