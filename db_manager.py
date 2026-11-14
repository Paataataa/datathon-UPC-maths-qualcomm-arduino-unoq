"""Rol 4: búsqueda de problemas y persistencia de alumnado (solo stdlib).

El índice es compatible con copilot/rag.py del repositorio oficial:
  problems(key TEXT PRIMARY KEY, problem TEXT NOT NULL, vec BLOB NOT NULL)
studia.db utiliza el esquema multilingüe de build_studia_db.py (Frente 3A).
users.db lo gestiona exclusivamente este módulo (Frente 3B).

Los problemas que devuelve este módulo llevan "label" (la respuesta), porque el
servidor la necesita para corregir y para el guardrail de pistas. Antes de
enviar un problema al navegador, pásalo por DBManager.public().
"""
from array import array
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3

VECTOR_DIM = 384
MAX_LEVEL = 7


class DBManager:
    def __init__(self, index_path, studia_path, users_path):
        self.index_path = Path(index_path)
        self.studia_path = Path(studia_path)
        self.users_path = Path(users_path)
        self.users_path.parent.mkdir(parents=True, exist_ok=True)
        self._index_cache = None  # (mtime, [(problem, vec, norm)]): se lee index.db una sola vez
        self._init_users()

    @staticmethod
    def public(problem):
        """Copia del problema sin la respuesta, para enviarla a la página web."""
        return {k: v for k, v in problem.items() if k not in ("label", "split")}

    @staticmethod
    @contextmanager
    def _connect(path):
        """Confirma los cambios y CIERRA el archivo SQLite también en Windows."""
        con = sqlite3.connect(path)
        try:
            with con:
                yield con
        finally:
            con.close()

    @contextmanager
    def _connect_existing(self, path):
        if not path.is_file():
            raise FileNotFoundError(f"No existe {path}. Genera los datos de prueba o copia la BD real.")
        with self._connect(path) as con:
            yield con

    def _init_users(self):
        with self._connect(self.users_path) as con:
            con.executescript("""
            CREATE TABLE IF NOT EXISTS students (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                current_level INTEGER NOT NULL DEFAULT 0 CHECK(current_level BETWEEN 0 AND 7),
                coins INTEGER NOT NULL DEFAULT 0 CHECK(coins >= 0)
            );
            CREATE TABLE IF NOT EXISTS unlocked_avatars (
                student_id INTEGER NOT NULL,
                avatar_id TEXT NOT NULL,
                unlocked_at TEXT NOT NULL,
                PRIMARY KEY (student_id, avatar_id),
                FOREIGN KEY (student_id) REFERENCES students(id)
            );
            CREATE TABLE IF NOT EXISTS attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id INTEGER NOT NULL,
                problem_id TEXT NOT NULL,
                topic TEXT NOT NULL,
                correct INTEGER NOT NULL CHECK(correct IN (0, 1)),
                attempted_at TEXT NOT NULL,
                FOREIGN KEY (student_id) REFERENCES students(id)
            );
            """)

    def get_problem(self, problem_id, language="cat"):
        """Lee studia.db SIN modificarla. Respeta build_studia_db.py (Frente 3A).

        La BD tiene 'original_text' y 'options' (JSON); se añade el alias
        'text' para no romper el código existente del buscador y del frontend.
        """
        with self._connect_existing(self.studia_path) as con:
            con.row_factory = sqlite3.Row
            row = con.execute(
                "SELECT id, language, split, level, year, original_text, options, "
                "label, topic, text_dyslexia, text_tdah "
                "FROM problems WHERE id=? AND language=? LIMIT 1",
                (problem_id, language)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["options"] = json.loads(result["options"] or "{}")
        result["text"] = result["original_text"]  # Alias de compatibilidad
        return result

    def get_theory(self, topic, language="cat"):
        """Devuelve la teoría del tema en el idioma solicitado (esquema 3A)."""
        with self._connect_existing(self.studia_path) as con:
            row = con.execute(
                "SELECT content FROM theory WHERE topic=? AND language=?",
                (topic, language),
            ).fetchone()
        return row[0] if row else None

    @staticmethod
    def _vector_from_blob(blob):
        vec = array("f")
        vec.frombytes(blob)
        if len(vec) != VECTOR_DIM:
            raise ValueError(f"Se esperaban {VECTOR_DIM} dimensiones, hay {len(vec)}")
        return vec

    @staticmethod
    def cosine(a, b):
        if len(a) != len(b):
            raise ValueError("Los vectores deben tener la misma dimensión")
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0

    def _english_index(self):
        """Los problemas ingleses con opciones de index.db, en memoria.

        Se leen una vez (como copilot/rag.py) y se vuelven a leer solo si
        index.db cambia, así cada búsqueda es un bucle sin SQLite ni JSON.
        """
        if not self.index_path.is_file():
            raise FileNotFoundError(f"No existe {self.index_path}. Genera los datos de prueba o copia la BD real.")
        mtime = self.index_path.stat().st_mtime
        if self._index_cache is None or self._index_cache[0] != mtime:
            rows = {}
            with self._connect_existing(self.index_path) as con:
                for _key, problem_json, blob in con.execute("SELECT key, problem, vec FROM problems"):
                    p = json.loads(problem_json)
                    if p.get("language") != "eng":
                        continue
                    vec = self._vector_from_blob(blob)
                    rows[p["id"]] = (p, vec, math.sqrt(sum(x * x for x in vec)))
            self._index_cache = (mtime, rows)
        return self._index_cache[1]

    def _indexed_english_vector(self, problem_id):
        row = self._english_index().get(problem_id)
        if row is None:
            raise ValueError(f"No hay embedding inglés indexado para {problem_id}")
        return row[1]

    def _query_vector(self, source, use_server):
        """Vector de búsqueda del problema de origen (su versión inglesa).

        index.db solo tiene problemas de train: si el problema no está (p. ej.
        uno de validación), se calcula con el servidor de embeddings.
        """
        if not use_server:
            try:
                return self._indexed_english_vector(source["id"])
            except ValueError:
                pass
        return self._live_english_vector(source)

    def _attempted_ids(self, student_id):
        with self._connect(self.users_path) as con:
            return {row[0] for row in con.execute(
                "SELECT DISTINCT problem_id FROM attempts WHERE student_id=?", (student_id,))}

    def _search(self, query_vec, max_level, language, exclude_ids=(), strictly_easier=False, exact_level=None):
        if len(query_vec) != VECTOR_DIM:
            raise ValueError(f"La consulta debe tener {VECTOR_DIM} dimensiones")
        if not 0 <= max_level <= MAX_LEVEL:
            raise ValueError(f"El nivel debe estar entre 0 y {MAX_LEVEL}")
        exclude_ids = set(exclude_ids)
        query_norm = math.sqrt(sum(x * x for x in query_vec))
        ranked = []
        for pid, (p, vec, norm) in self._english_index().items():
            if pid in exclude_ids:
                continue
            level = p["level"]
            if exact_level is not None:
                if level != exact_level:
                    continue
            elif strictly_easier:
                if level >= max_level:
                    continue
            else:
                if level > max_level:
                    continue
            if not p.get("options"):
                continue
            dot = sum(x * y for x, y in zip(query_vec, vec))
            score = dot / (query_norm * norm) if query_norm and norm else 0.0
            ranked.append((score, p))
        ranked.sort(key=lambda item: (-item[0], item[1]["id"]))
        for score, p in ranked:
            recommended = self.get_problem(p["id"], language)
            if recommended is None:
                recommended = self.get_problem(p["id"], "eng")
            if recommended is not None:
                return {"similarity": round(score, 5), "problem": recommended,
                        "searched_in": "eng"}
        return None

    def _live_english_vector(self, english_problem):
        """Vector de CONSULTA bge-small (prefijo oficial), vía 127.0.0.1:8082.

        Ejecutar desde la raíz del repositorio oficial. En un portátil sin el
        servidor local, abrir antes un túnel SSH al puerto 8082 del Arduino.
        """
        try:
            from copilot.embed import embed
            from copilot.llm import format_problem
        except ImportError as exc:
            raise RuntimeError(
                "No se encuentra el paquete 'copilot' oficial. "
                "Ejecuta desde la raíz del repositorio de la datathon."
            ) from exc
        try:
            # query=True añade EMBED_QUERY_PREFIX, igual que copilot/rag.py.
            return embed([format_problem(english_problem)], query=True, timeout=60)[0]
        except OSError as exc:
            raise ConnectionError(
                "No puedo conectar con bge-small en 127.0.0.1:8082. "
                "Comprueba math-embed o crea el túnel SSH desde el portátil."
            ) from exc

    def find_similar_easier(self, problem_id, language="cat", strictly_easier=False,
                            use_server=False):
        """Pista 2: busca por la versión inglesa, excluyendo el mismo ID.

        use_server=False: emplea el vector almacenado si el problema está en
                         index.db (train); si no, recurre al servidor.
        use_server=True:  usa siempre el servidor oficial bge-small para crear
                         un vector de CONSULTA.
        """
        source = self.get_problem(problem_id, "eng")
        if source is None:
            raise ValueError(f"No encuentro el problema {problem_id} en inglés")
        vec = self._query_vector(source, use_server)
        return self._search(vec, source["level"], language, {problem_id}, strictly_easier)

    def find_similar_from_vector(self, query_vector, max_level, language="cat", exclude_id=None):
        """Para un ejercicio nuevo: el vector deberá provenir después del servidor 8082."""
        return self._search(query_vector, max_level, language, {exclude_id} if exclude_id else ())

    def next_test_problem(self, problem_id, was_correct, language="cat", use_server=False,
                          student_id=None):
        """Modo Test: +1/-1 nivel (en límites 0..7), selecciona por similitud.

        Con student_id se saltan los problemas que el alumno ya intentó (si no,
        acierto en A -> B, fallo en B -> A otra vez) y se guarda su nuevo nivel.
        Si no hay candidatos del nivel objetivo devuelve None. La aplicación
        decide qué mostrar; no inventamos un problema de otro nivel.
        """
        source = self.get_problem(problem_id, "eng")
        if source is None:
            raise ValueError(f"Problema desconocido: {problem_id}")
        target = min(MAX_LEVEL, source["level"] + 1) if was_correct else max(0, source["level"] - 1)
        exclude = {problem_id}
        if student_id is not None:
            if self.get_student(student_id) is None:
                raise ValueError("Alumno inexistente")
            exclude |= self._attempted_ids(student_id)
            self.set_level(student_id, target)
        vec = self._query_vector(source, use_server)
        result = self._search(vec, MAX_LEVEL, language, exclude, exact_level=target)
        if result is not None:
            result["target_level"] = target
        return result

    def create_student(self, username, level=0):
        if not username.strip() or not 0 <= level <= MAX_LEVEL:
            raise ValueError("Nombre vacío o nivel inválido")
        with self._connect(self.users_path) as con:
            cur = con.execute(
                "INSERT INTO students(username,current_level) VALUES(?,?)", (username.strip(), level))
            return cur.lastrowid

    def get_student(self, student_id):
        with self._connect(self.users_path) as con:
            con.row_factory = sqlite3.Row
            row = con.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()
        return dict(row) if row else None

    def set_level(self, student_id, level):
        if not 0 <= level <= MAX_LEVEL:
            raise ValueError("Nivel fuera de rango")
        with self._connect(self.users_path) as con:
            cur = con.execute("UPDATE students SET current_level=? WHERE id=?", (level, student_id))
            if cur.rowcount == 0:
                raise ValueError("Alumno inexistente")

    def add_coins(self, student_id, amount):
        if not isinstance(amount, int) or amount < 0:
            raise ValueError("Las monedas añadidas deben ser un entero positivo o cero")
        with self._connect(self.users_path) as con:
            cur = con.execute("UPDATE students SET coins=coins+? WHERE id=?", (amount, student_id))
            if cur.rowcount == 0:
                raise ValueError("Alumno inexistente")

    def unlock_avatar(self, student_id, avatar_id):
        if self.get_student(student_id) is None:
            raise ValueError("Alumno inexistente")
        if not avatar_id.strip():
            raise ValueError("Avatar vacío")
        with self._connect(self.users_path) as con:
            con.execute(
                "INSERT OR IGNORE INTO unlocked_avatars VALUES(?,?,?)",
                (student_id, avatar_id, datetime.now(timezone.utc).isoformat()))

    def get_avatars(self, student_id):
        with self._connect(self.users_path) as con:
            return [row[0] for row in con.execute(
                "SELECT avatar_id FROM unlocked_avatars WHERE student_id=? ORDER BY avatar_id",
                (student_id,))]

    def record_attempt(self, student_id, problem_id, topic, correct):
        if self.get_student(student_id) is None:
            raise ValueError("Alumno inexistente")
        if not problem_id or not topic or not isinstance(correct, bool):
            raise ValueError("Intento inválido")
        with self._connect(self.users_path) as con:
            cur = con.execute(
                "INSERT INTO attempts(student_id,problem_id,topic,correct,attempted_at) VALUES(?,?,?,?,?)",
                (student_id, problem_id, topic, int(correct), datetime.now(timezone.utc).isoformat()))
            return cur.lastrowid

    def record_problem_attempt(self, student_id, problem_id, correct, language="cat"):
        """Guarda un intento tomando el tema de studia.db, no del frontend.

        Así el profesor verá los mismos temas que definió el Frente 3A.
        """
        problem = self.get_problem(problem_id, language)
        if problem is None and language != "eng":
            problem = self.get_problem(problem_id, "eng")
        if problem is None:
            raise ValueError(f"No existe el problema {problem_id} en studia.db")
        return self.record_attempt(student_id, problem_id, problem["topic"], correct)

    def errors_by_topic(self, student_id):
        with self._connect(self.users_path) as con:
            return dict(con.execute(
                "SELECT topic, COUNT(*) FROM attempts "
                "WHERE student_id=? AND correct=0 GROUP BY topic ORDER BY topic", (student_id,)))

    def get_progress(self, student_id):
        student = self.get_student(student_id)
        if student is None:
            return None
        with self._connect(self.users_path) as con:
            total, correct = con.execute(
                "SELECT COUNT(*), COALESCE(SUM(correct),0) FROM attempts WHERE student_id=?",
                (student_id,)).fetchone()
        return {**student, "attempts": total, "correct": correct,
                "errors_by_topic": self.errors_by_topic(student_id),
                "avatars": self.get_avatars(student_id)}
