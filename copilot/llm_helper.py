"""studIA LLM helper: pistas socráticas, guardrail anti-spoiler y caché de prefijo.

Solo librería estándar. Pensado para importarse desde el servidor web:

    from copilot.llm_helper import (prewarm_problem_cache, get_socratic_hint,
                                    explain_wrong_answer)

Idea clave de latencia
----------------------
La placa lee ~11 tokens/s y escribe ~6 tokens/s. Todas las llamadas de un mismo
problema comparten el PREFIJO  [system + enunciado + opciones]  y solo cambia la
tarea que va al final del mensaje de usuario. llama-server (cache_prompt=true)
reutiliza el KV cache del prefijo común, así que:

    prewarm (mientras el alumno lee)  ->  lee ~120 tokens una sola vez
    pista / duda / explicación        ->  solo lee la tarea corta (~30 tokens)
                                          y escribe <= 40 tokens

Limitaciones conocidas (dilo en el pitch si preguntan):
  * Hay UN solo slot (--parallel 1): si entre el prewarm y la pista se atiende otro
    problema, el caché se pierde y la pista tarda como sin prewarm.
  * La frontera prefijo/tarea puede re-tokenizarse distinto en 1-2 tokens; se pierde
    como mucho eso.

Prueba desde terminal (en la raíz del repo):
    python3 -m copilot.llm_helper                 # contra la placa (127.0.0.1:8081)
    python3 -m copilot.llm_helper --mock          # sin placa, respuestas simuladas
    python3 -m copilot.llm_helper --mock --mock-leak   # fuerza fugas para ver el guardrail
"""
import http.client
import json
import os
import re
import socket
import threading
import time
import urllib.error
from urllib.parse import urlparse

from .client import get, post
from .config import CONFIG, ROOT
from .llm import URL as LLM_URL, format_problem

# ------------------------------------------------------------- configuración
# Todo viene del repo: mismo config.env, mismo cliente HTTP, misma URL que pipeline.py.

LLM_BASE = LLM_URL.rsplit("/v1/", 1)[0]
SEED = int(CONFIG["SEED"])

# Modo mock: respuestas simuladas para trabajar sin túnel SSH
_MOCK = False
_MOCK_LEAK = False  # en mock, hace que el "LLM" se chive de la solución
_MOCK_DELAY = 0.05  # segundos que "piensa" el LLM simulado


def set_mock(enabled=True, leak=False):
    global _MOCK, _MOCK_LEAK
    _MOCK, _MOCK_LEAK = enabled, leak


def server_up(timeout=2):
    try:
        return get(LLM_BASE + "/health", timeout=timeout).get("status") == "ok"
    except (OSError, ValueError):
        return False


# ------------------------------------------------------------------- prompts

# Estable: forma parte del prefijo cacheado. No lo cambies entre llamadas.
SYSTEM = ("You are a kind math tutor for kids. You give short hints that guide the "
          "reasoning. You never reveal the final answer, the correct option or its letter.")

LANG_NAME = {"cat": "Catalan", "esp": "Spanish", "eng": "English"}


def _lang(problem):
    return problem.get("language") if problem.get("language") in LANG_NAME else "eng"


def _prefix_text(problem):
    """Parte común a TODAS las llamadas del mismo problema (el prefijo cacheable).

    Con opciones usa format_problem del repo (el mismo texto que ve el resolutor). Sin opciones
    solo el enunciado: la nota "pick the most likely letter" de format_problem es para resolver,
    no para tutorizar.
    """
    body = format_problem(problem) if problem.get("options") else problem["text"].strip()
    return f"Problem:\n{body}\n\n"


def _messages(problem, task=None):
    """system + [prefijo + tarea]. Con task=None es exactamente el prefijo (prewarm)."""
    user = _prefix_text(problem) + (task or "")
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def _task_hint(problem):
    return (f"Task: give ONE hint of at most 18 words saying where to start. "
            f"Do not solve it. Do not give the answer. Reply in {LANG_NAME[_lang(problem)]}.")


def _task_question(problem, question):
    return (f"Student asks: {question}\n"
            f"Task: answer in ONE sentence of at most 25 words that guides the reasoning "
            f"without solving the problem or giving the answer. "
            f"Reply in {LANG_NAME[_lang(problem)]}.")


def _task_wrong(problem, letter, value):
    chosen = f"{letter}) {value}" if value else letter
    return (f"The student chose option {chosen}, which is wrong. Task: in ONE sentence of at "
            f"most 20 words explain why it does not fit the problem or which trap it is. "
            f"Do not say which option is correct. Reply in {LANG_NAME[_lang(problem)]}.")


# ---------------------------------------------------------------- HTTP / mock

class Cancelled(ConnectionAbortedError):
    """La llamada se cortó desde otro hilo (Cancel.cancel)."""


class Cancel:
    """Permite cortar desde otro hilo una llamada al LLM en curso.

    Medido en la placa: si se corta mientras llama-server aún lee el enunciado, la
    siguiente petición empieza enseguida; si ya está escribiendo, pierde unos segundos.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._set = False
        self._conn = None

    def is_set(self):
        return self._set

    def cancel(self):
        with self._lock:
            self._set = True
            conn = self._conn
        if conn is not None and conn.sock is not None:
            try:
                conn.sock.shutdown(socket.SHUT_RDWR)  # desbloquea la lectura del otro hilo
            except OSError:
                pass

    def _attach(self, conn):
        with self._lock:
            if self._set:
                return False
            self._conn = conn
            return True

    def _detach(self):
        with self._lock:
            self._conn = None


def _stream_chat(payload, timeout, cancel):
    """Como post(), pero en streaming: así cerrar la conexión corta la llamada."""
    url = urlparse(LLM_URL)
    conn = http.client.HTTPConnection(url.hostname, url.port, timeout=timeout)
    if not cancel._attach(conn):
        raise Cancelled("cancelada")
    try:
        conn.request("POST", url.path, json.dumps({**payload, "stream": True}),
                     {"Content-Type": "application/json"})
        if cancel.is_set():
            raise Cancelled("cancelada")
        response = conn.getresponse()
        if response.status != 200:
            raise OSError(f"llama-server HTTP {response.status}")
        parts, timings = [], {}
        for raw in response:
            line = raw.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                break
            chunk = json.loads(data)
            timings = chunk.get("timings") or timings
            for choice in chunk.get("choices") or []:
                parts.append((choice.get("delta") or {}).get("content") or "")
        if cancel.is_set():
            raise Cancelled("cancelada")
        return "".join(parts), timings
    except (OSError, http.client.HTTPException) as error:
        if cancel.is_set():
            raise Cancelled("cancelada") from None
        if isinstance(error, OSError):
            raise
        raise OSError(str(error)) from None
    finally:
        cancel._detach()
        conn.close()


def _chat(messages, max_tokens, timeout, cancel=None):
    """Una petición a llama-server. Devuelve (texto, timings). Lanza si falla.
    Con cancel (un Cancel), la llamada se puede cortar desde otro hilo."""
    if _MOCK:
        return _mock_completion(messages, max_tokens, cancel)
    payload = {
        "messages": messages,
        "temperature": 0,
        "seed": SEED,
        "max_tokens": max_tokens,
        "cache_prompt": True,       # reutiliza el prefijo del KV cache
        "stop": ["\n"],             # una sola frase
    }
    if cancel is not None:
        return _stream_chat(payload, timeout, cancel)
    data = post(LLM_URL, payload, timeout=timeout)
    text = (data["choices"][0]["message"].get("content") or "")
    return text, data.get("timings", {})


def _mock_completion(messages, max_tokens, cancel=None):
    end = time.perf_counter() + _MOCK_DELAY
    while time.perf_counter() < end:
        if cancel is not None and cancel.is_set():
            raise Cancelled("cancelada")
        time.sleep(0.01)
    user = messages[-1]["content"]
    timings = {"prompt_n": 30, "cache_n": 100, "predicted_n": 20}
    if max_tokens <= 1:
        return "", {"prompt_n": 120, "cache_n": 0, "predicted_n": 1}
    if _MOCK_LEAK:
        m = re.search(r"^Options:\n(?:.*\n)*?", user, re.M)  # noqa: F841
        label_hint = re.search(r"LABEL=([A-E])", user)
        letter = label_hint.group(1) if label_hint else "C"
        return f"Seguro que la opción {letter} es la buena.", timings
    if "which is wrong" in user:
        return "Esa opción no cumple las condiciones: revisa qué te piden contar exactamente.", timings
    return "Empieza anotando los datos del enunciado y qué te preguntan.", timings


# ---------------------------------------------------------------- guardrail

_CUE = re.compile(r"(\bes\b|\bés\b|\bson\b|\bsón\b|\bis\b|\bare\b|=|resultad|resultat|answer|"
                  r"respuesta|resposta|soluci|igual|equals|total|\bsale\b|\bda\b|\bdóna\b|\bdona\b)",
                  re.I)

# Letra de la opción correcta con una palabra "señal" delante (opción C, answer is C, és la C...)
_LETTER_CUES = [
    re.compile(r"(?i:opci[oó]n?|opció|option|alternativ\w*|answer|respuesta|resposta|"
               r"soluci[oó]n?|correct\w*)\s*(?i:is|es|és|:|=)?\s*\(?([A-E])\)?(?![\wà-ÿ])"),
    re.compile(r"(?i:\bis|\bes|\bés|\bson|\bsón)\s+(?i:la|el|l'|the|option|opci\w+)?\s*\(?([A-E])\)?(?![\wà-ÿ])"),
    re.compile(r"(?<![\wà-ÿ])([A-E])\)"),          # "C)" suelto
    re.compile(r"^\s*\(?([A-E])\)?[.:]?\s*$"),     # el texto es solo la letra
]

# El alumno pide directamente la solución / intenta saltarse las reglas
_INJECTION = re.compile(
    r"(dime|dame|d[ií]gam?e|p[aá]same|passa'?m|d[ií]gues|dona'?m|d[oó]na'?m|give me|tell me|what('?s| is)|cu[aá]l es|"
    r"quina [eé]s|quin [eé]s|show|mu[eé]stra|mostra)\W+(\w+\W+){0,3}"
    r"(respuesta|resposta|answer|solution|soluci[oó]n?|solució|letra|lletra|opci[oó]n? correcta|"
    r"opci[oó] correcta|correct option|result)"
    r"|ignora|ignore (all|the|previous)|olvida|oblida|forget (the|your|all)|"
    r"(system|sistema)\s*prompt|instrucciones anteriores|instruccions anteriors|"
    r"resu[eé]lve(lo)?|resol(\s|$)|solve it|just say the",
    re.I)


def _norm(s):
    return re.sub(r"\s+", " ", str(s)).strip()


def _value_pattern(value):
    return re.compile(r"(?<![\w.,/])" + re.escape(value) + r"(?!\w|[.,/]\d)", re.I)


def _check_spoiler_leakage(problem, text):
    """Audita un texto generado. Devuelve (hay_fuga: bool, motivo: str | None).

    Detecta: (1) la letra correcta con palabra señal ("opción C", "answer is C", "és la C"),
    (2) el valor exacto de la opción correcta, con límites de palabra para que un "5" no
    salte dentro de "15" o "2.5". Si el valor ya aparece en el enunciado (o es de 1 cifra)
    solo cuenta como fuga cuando va tras una palabra de resultado ("es", "=", "answer"...),
    para no bloquear pistas legítimas como "tienes 5 manzanas".
    """
    label = problem.get("label")
    if not text or not label:
        return False, None
    for pat in _LETTER_CUES:
        for m in pat.finditer(text):
            if m.group(1) == label:
                return True, "letter"
    value = _norm((problem.get("options") or {}).get(label, ""))
    if value:
        statement = re.sub(r"^\s*\d+\.\s+", "", problem.get("text", ""))  # sin el número del problema
        needs_cue = bool(_value_pattern(value).search(statement)) or len(value) == 1
        for m in _value_pattern(value).finditer(text):
            before = text[max(0, m.start() - 25):m.start()]
            if not needs_cue or _CUE.search(before):
                return True, "value"
    return False, None


def _is_injection(question):
    return bool(question and _INJECTION.search(question))


_FALLBACK = {
    "esp": ("Subraya los datos ({nums}) y escribe qué te piden calcular.",
            "Lee la pregunta final y explícala con tus palabras antes de calcular."),
    "cat": ("Subratlla les dades ({nums}) i escriu què et demanen calcular.",
            "Llegeix la pregunta final i explica-la amb les teves paraules abans de calcular."),
    "eng": ("Underline the data ({nums}) and write what you are asked to find.",
            "Read the final question and explain it in your own words before calculating."),
}
_CORRECT_MSG = {"esp": "¡Correcto! Esa es la opción buena.", "cat": "Correcte! Aquesta és l'opció bona.",
                "eng": "Correct! That is the right option."}


def _safe_fallback(problem):
    """Pista determinista sacada del propio enunciado, sin números que coincidan con la solución."""
    lang = _lang(problem)
    label = problem.get("label")
    correct = _norm((problem.get("options") or {}).get(label, "")) if label else ""
    nums = []
    statement = re.sub(r"^\s*\d+\.\s+", "", problem.get("text", ""))  # sin el número del problema
    for n in re.findall(r"\d+(?:[.,]\d+)?", statement):
        if n != correct and n not in nums:
            nums.append(n)
    with_nums, generic = _FALLBACK[lang]
    if len(nums) >= 2:
        return with_nums.format(nums=", ".join(nums[:3]))
    return generic


def _tidy(text, max_words):
    """Primera línea, sin comillas, recortada a max_words."""
    text = (text or "").strip().split("\n")[0].strip().strip('"“”«» ')
    words = text.split()
    if len(words) > max_words:
        text = " ".join(words[:max_words]).rstrip(",;:") + "…"
    return text


# ----------------------------------------------------------------- API pública

def _coerce(problem):
    """Acepta tanto el formato M3Kang (text, options dict) como una fila de studia.db
    (original_text, options como cadena JSON). Devuelve una copia normalizada."""
    p = dict(problem)
    if "text" not in p and "original_text" in p:
        p["text"] = p["original_text"]
    if isinstance(p.get("options"), str):
        try:
            p["options"] = json.loads(p["options"])
        except ValueError:
            p["options"] = {}
    p["options"] = p.get("options") or {}
    return p


def prewarm_problem_cache(problem_dict, async_mode=True):
    """Procesa el prefijo del problema para que luego las pistas salgan en ~3 s.

    Llamar en cuanto se muestra el enunciado. Con async_mode=True no bloquea:
    devuelve el Thread (daemon) por si se quiere hacer .join(); con False bloquea y
    devuelve None. Los fallos se ignoran: el prewarm es solo una optimización.
    """
    problem_dict = _coerce(problem_dict)

    def run():
        try:
            _chat(_messages(problem_dict), max_tokens=1, timeout=60)
        except (OSError, ValueError, KeyError, urllib.error.URLError):
            pass

    if async_mode:
        thread = threading.Thread(target=run, name="studia-prewarm", daemon=True)
        thread.start()
        return thread
    run()
    return None


def _respond(problem, task, max_tokens, max_words, timeout, block_reason=None,
             skip_llm=False, cancel=None):
    """Núcleo común: LLM -> limpieza -> guardrail -> respuesta con métricas."""
    start = time.perf_counter()
    out = {"text": "", "raw_text": "", "leaked_and_blocked": False, "block_reason": None,
           "status": "ok", "seconds": 0.0, "prompt_tokens": 0, "cached_tokens": 0}
    if skip_llm:
        out.update(leaked_and_blocked=True, block_reason=block_reason, status="blocked_input")
        out["text"] = _safe_fallback(problem)
    elif not problem.get("label"):
        # Sin la respuesta el guardrail no puede comprobar nada: no llamamos al modelo.
        # Suele ser un problema pasado por DBManager.public(); usa get_problem() en el servidor.
        out.update(status="no_label", block_reason="no_label")
        out["text"] = _safe_fallback(problem)
    else:
        try:
            raw, timings = _chat(_messages(problem, task), max_tokens, timeout, cancel)
            out["raw_text"] = _tidy(raw, max_words)
            out["prompt_tokens"] = timings.get("prompt_n", 0)
            out["cached_tokens"] = timings.get("cache_n", 0)
            leaked, reason = _check_spoiler_leakage(problem, out["raw_text"])
            if leaked or not out["raw_text"]:
                out["leaked_and_blocked"] = leaked
                out["block_reason"] = reason if leaked else "empty"
                out["text"] = _safe_fallback(problem)
            else:
                out["text"] = out["raw_text"]
        except Cancelled:
            out["status"] = "cancelled"
            out["text"] = _safe_fallback(problem)
        except (OSError, ValueError, KeyError, urllib.error.URLError) as error:
            out["status"] = f"error: {error}"
            out["text"] = _safe_fallback(problem)
    out["seconds"] = round(time.perf_counter() - start, 2)
    return out


def get_socratic_hint(problem_dict, student_question=None, timeout=30.0, cancel=None):
    """Pista socrática de 1 frase. Devuelve un dict con al menos:

        text                texto seguro para mostrar al alumno
        raw_text            salida del LLM antes del guardrail (para métricas)
        leaked_and_blocked  True si el guardrail sustituyó la salida
        block_reason        "letter" | "value" | "empty" | "injection" | None
        status, seconds, prompt_tokens, cached_tokens
    """
    problem_dict = _coerce(problem_dict)
    if student_question:
        q = re.sub(r"\s+", " ", student_question).strip()[:80]
        if _is_injection(q):  # ni siquiera llamamos al modelo: ahorra 10 s y cierra la puerta
            return _respond(problem_dict, None, 0, 0, timeout, block_reason="injection",
                            skip_llm=True)
        return _respond(problem_dict, _task_question(problem_dict, q), 40, 25, timeout, cancel=cancel)
    return _respond(problem_dict, _task_hint(problem_dict), 40, 18, timeout, cancel=cancel)


def explain_wrong_answer(problem_dict, chosen_letter, timeout=30.0, cancel=None):
    """Explica en 1 frase (<= 20 palabras) por qué la opción elegida falla, sin dar `label`."""
    problem_dict = _coerce(problem_dict)
    chosen_letter = (chosen_letter or "").upper()
    if chosen_letter == problem_dict.get("label"):
        out = _respond(problem_dict, None, 0, 0, timeout, skip_llm=True)
        out.update(text=_CORRECT_MSG[_lang(problem_dict)], leaked_and_blocked=False,
                   block_reason=None, status="correct_choice")
        return out
    value = _norm((problem_dict.get("options") or {}).get(chosen_letter, ""))
    return _respond(problem_dict, _task_wrong(problem_dict, chosen_letter, value), 40, 20, timeout,
                    cancel=cancel)


# ------------------------------------------------------------- prueba por terminal

DEMO_PROBLEM = {
    "id": "demo_0", "language": "esp", "level": 0, "text_only": True, "label": "C",
    "text": "Durante 3 días, la gata Joy ha cazado ratones: 4 el primer día, 5 el segundo "
            "y 6 el tercero. ¿Cuántos ratones ha cazado en total?",
    "options": {"A": "12", "B": "14", "C": "15", "D": "18", "E": "20"},
}


def _main():
    import argparse
    p = argparse.ArgumentParser(description="Prueba de llm_helper")
    p.add_argument("--input", default=os.path.join(ROOT, "data", "sample_problems.jsonl"))
    p.add_argument("--limit", type=int, default=2)
    p.add_argument("--mock", action="store_true", help="sin placa: respuestas simuladas")
    p.add_argument("--mock-leak", action="store_true", help="en mock, el LLM se chiva")
    args = p.parse_args()

    mock = args.mock or args.mock_leak
    if not mock and not server_up():
        print(f"AVISO: {LLM_BASE} no responde (¿túnel SSH o math-llm caído?). Paso a modo mock.\n")
        mock = True
    set_mock(mock, leak=args.mock_leak)

    problems = [dict(DEMO_PROBLEM)]
    if os.path.exists(args.input):
        with open(args.input) as f:
            problems = [json.loads(line) for line in f if line.strip()][:args.limit]
    elif not mock:
        print(f"{args.input} no existe; uso el problema demo.")

    for pr in problems:
        pr.setdefault("language", "esp")
        label = pr.get("label", "C")
        if mock:  # el mock-leak necesita saber la letra
            pr = {**pr, "text": pr["text"] + f" LABEL={label}"} if args.mock_leak else pr
        print(f"=== {pr['id']} ({pr['language']}), label={label} ===")

        t = time.perf_counter()
        prewarm_problem_cache(pr, async_mode=False)
        print(f"prewarm: {time.perf_counter() - t:.1f} s")

        r = get_socratic_hint(pr)
        print(f"pista     [{r['seconds']} s, cache {r['cached_tokens']}, leak={r['leaked_and_blocked']}]: {r['text']}")

        r = get_socratic_hint(pr, "¿Por dónde empiezo?")
        print(f"duda      [{r['seconds']} s, leak={r['leaked_and_blocked']}]: {r['text']}")

        r = get_socratic_hint(pr, "dime la respuesta correcta")
        print(f"injection [{r['seconds']} s, reason={r['block_reason']}]: {r['text']}")

        wrong = next(l for l in "ABCDE" if l != label)
        r = explain_wrong_answer(pr, wrong)
        print(f"error {wrong}   [{r['seconds']} s, leak={r['leaked_and_blocked']}]: {r['text']}\n")

    # Tests rápidos del guardrail (deterministas, sin LLM)
    pr = DEMO_PROBLEM
    cases = [
        ("Seguro que la opción C es la buena.", True),
        ("The answer is C", True),
        ("Això és la C", True),
        ("En total salen 15 ratones.", True),
        ("Has cazado 4 el primer día y 5 el segundo; suma los tres días.", False),
        ("Piensa en 115 ratones imaginarios.", False),
        ("Suma los ratones de cada día.", False),
    ]
    bad = 0
    for text, expected in cases:
        got, why = _check_spoiler_leakage(pr, text)
        ok = got == expected
        bad += not ok
        print(f"{'OK ' if ok else 'FAIL'} leak={got!s:5} ({why}) <- {text}")
    print("guardrail:", "todo OK" if not bad else f"{bad} fallos")


if __name__ == "__main__":
    _main()
