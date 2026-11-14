#!/usr/bin/env python3
"""Base de datos del tutor: problemas con tema, versiones adaptadas y teoría.

    python3 build_studia_db.py     # data/train.jsonl + data/val.jsonl -> data/studia.db

Guarda train y val, con su respuesta (label). Es solo para la app del tutor:
- No la uses para recuperar ejemplos en eval.py o predict.py: los problemas
  de validación se encontrarían a sí mismos. Para eso está data/index.db.
- Nunca envíes "label" a la página web antes de que el alumno responda.
"""
import json
import sqlite3
import os
import re
import sys
from collections import Counter

DB_PATH = 'data/studia.db'
TRAIN_PATH = 'data/train.jsonl'
VAL_PATH = 'data/val.jsonl'

# 3. Teoría trilingüe
THEORY = {
    "esp": {
        "Geometría": "Recuerda las fórmulas básicas: Área del triángulo = (b*h)/2. Los ángulos de un triángulo suman 180°. Dibuja siempre la figura si no te la dan.",
        "Combinatoria": "Para contar combinaciones, usa el principio multiplicativo. Si tienes dudas, intenta listar los primeros casos a mano.",
        "Lógica": "Lee despacio. Si hay contradicciones, asume que una afirmación es falsa y comprueba si el resto tiene sentido.",
        "Aritmética": "Calcula paso a paso. Recuerda la prioridad de operaciones: primero paréntesis, luego multiplicación/división, y al final suma/resta.",
        "Patrones": "Busca la diferencia entre números consecutivos. Si no es constante, busca multiplicaciones o números alternos."
    },
    "eng": {
        "Geometría": "Remember basic formulas: Triangle area = (b*h)/2. The angles of a triangle add up to 180°. Always draw the figure if not provided.",
        "Combinatoria": "To count combinations, use the multiplicative principle. If in doubt, try listing the first cases by hand.",
        "Lógica": "Read slowly. If there are contradictions, assume one statement is false and check if the rest makes sense.",
        "Aritmética": "Calculate step by step. Remember the order of operations: parentheses first, then multiplication/division, and finally addition/subtraction.",
        "Patrones": "Look for the difference between consecutive numbers. If it's not constant, look for multiplications or alternating numbers."
    },
    "cat": {
        "Geometría": "Recorda les fórmules bàsiques: Àrea del triangle = (b*h)/2. Els angles d'un triangle sumen 180°. Dibuixa sempre la figura si no te la donen.",
        "Combinatoria": "Per comptar combinacions, utilitza el principi multiplicatiu. Si tens dubtes, intenta llistar els primers casos a mà.",
        "Lógica": "Llegeix a poc a poc. Si hi ha contradiccions, assumeix que una afirmació és falsa i comprova si la resta té sentit.",
        "Aritmética": "Calcula pas a pas. Recorda la prioritat d'operacions: primer parèntesis, després multiplicació/divisió, i al final suma/resta.",
        "Patrones": "Busca la diferència entre nombres consecutius. Si no és constant, busca multiplicacions o nombres alterns."
    }
}

# Nombre de cada tema en el idioma del alumno (las claves de THEORY son internas)
TOPIC_NAMES = {
    "esp": {"Geometría": "Geometría", "Combinatoria": "Combinatoria", "Lógica": "Lógica",
            "Aritmética": "Aritmética", "Patrones": "Patrones"},
    "eng": {"Geometría": "Geometry", "Combinatoria": "Counting", "Lógica": "Logic",
            "Aritmética": "Arithmetic", "Patrones": "Patterns"},
    "cat": {"Geometría": "Geometria", "Combinatoria": "Combinatòria", "Lógica": "Lògica",
            "Aritmética": "Aritmètica", "Patrones": "Patrons"},
}

# Nombres neutros de los modos para la página: describen el formato, no al alumno
MODE_NAMES = {
    "esp": {"text_dyslexia": "Frases cortas", "text_tdah": "Paso a paso"},
    "eng": {"text_dyslexia": "Short sentences", "text_tdah": "Step by step"},
    "cat": {"text_dyslexia": "Frases curtes", "text_tdah": "Pas a pas"},
}

STEP_LABELS = {
    "esp": ("Paso", "Objetivo final"),
    "eng": ("Step", "Final goal"),
    "cat": ("Pas", "Objectiu final"),
}

# 1 y 2. Clasificador por Regex (\b) en inglés, castellano y catalán.
# Cada tema suma un punto por palabra clave distinta y gana el que más tiene.
# Sin ninguna coincidencia, Aritmética. Palabras demasiado generales ("cm",
# "next", "always", "true") se quitaron: metían problemas en el tema equivocado.
# Es aproximado: mídelo con ~50 problemas etiquetados a mano antes de publicar
# el análisis de errores por tema.
KEYWORDS = {
    "Geometría": [
        r'triangles?', r'triángulos?', r'rectangles?', r'rectángulos?', r'squares?', r'cuadrados?', r'quadrats?',
        r'circles?', r'círculos?', r'cercles?', r'angles?', r'ángulos?', r'areas?', r'áreas?', r'àrea',
        r'perimeters?', r'perímetros?', r'perímetres?', r'cubes?', r'cubos?', r'cubs?', r'polygons?',
        r'polígonos?', r'polígons?', r'hexagons?', r'hexágonos?', r'hexàgons?', r'radius', r'radio', r'radi',
        r'diagonals?', r'diagonales?',
    ],
    "Combinatoria": [
        r'ways', r'maneras', r'formas', r'maneres', r'combinations?', r'combinaciones?', r'combinacions?',
        r'arrangements?', r'dice', r'dados?', r'daus?', r'digits?', r'cifras?', r'xifres?',
        r'how many (?:\w+ ){0,3}numbers', r'cu[aá]ntos (?:\w+ ){0,3}n[uú]meros', r'quants (?:\w+ ){0,3}nombres',
        r'in how many', r'de cu[aá]ntas', r'de quantes',
    ],
    "Lógica": [
        r'liars?', r'lies', r'mentirosos?', r'mentiders?', r'mentira', r'mentida', r'knights?', r'caballeros?',
        r'cavallers?', r'statements?', r'afirmaci[oó]n(?:es)?', r'afirmacions?', r'truth', r'verdad', r'veritat',
    ],
    "Patrones": [
        r'sequences?', r'secuencias?', r'seqüències?', r'patterns?', r'patr[oó]n(?:es)?', r'patrons?',
    ],
}
KEYWORDS = {topic: [re.compile(r'\b' + k + r'\b', re.IGNORECASE) for k in words]
            for topic, words in KEYWORDS.items()}

# En empate gana el tema más específico: "¿de cuántas maneras se puede pintar
# el cuadrado?" es Combinatoria aunque hable de un cuadrado.
TIE_ORDER = ["Combinatoria", "Lógica", "Patrones", "Geometría"]

def classify_topic(text):
    scores = {topic: sum(1 for k in patterns if k.search(text)) for topic, patterns in KEYWORDS.items()}
    best = max(TIE_ORDER, key=lambda t: scores[t])  # max se queda con el primero en empate
    return best if scores[best] else "Aritmética"

# 4. Solución al parseo de TDAH y Dislexia
def clean_and_split(text):
    # Eliminar numeración inicial tipo "13. " o "1. "
    text = re.sub(r'^\d+\.\s+', '', text)
    # Separar por puntuación final manteniendo el signo (?.!)
    return [s.strip() for s in re.split(r'(?<=[.!?])\s+', text) if s.strip()]

def format_dyslexia(text):
    sentences = clean_and_split(text)
    if not sentences: return text
    return "\n\n".join(sentences)

def format_tdah(text, lang="esp"):
    sentences = clean_and_split(text)
    if not sentences: return text
    if len(sentences) == 1:
        return sentences[0]
    step, goal = STEP_LABELS.get(lang, STEP_LABELS["eng"])
    steps = "\n".join([f"{step} {i+1}: {s}" for i, s in enumerate(sentences[:-1])])
    steps += f"\n\n{goal}: {sentences[-1]}"
    return steps

def process_file(filepath, cursor, split, counts):
    if not os.path.exists(filepath):
        print(f"Aviso: {filepath} no existe, se salta", file=sys.stderr)
        return

    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            if not line.strip(): continue
            p = json.loads(line)
            
            lang = p.get("language", "esp")
            # Normalizar esp a spa si hiciera falta, M3Kang usa eng, cat, esp
            topic = classify_topic(p.get("text", ""))
            text_dyslexia = format_dyslexia(p.get("text", ""))
            text_tdah = format_tdah(p.get("text", ""), lang)
            options_json = json.dumps(p.get("options", {}))

            cursor.execute('''
                INSERT INTO problems (id, language, split, level, year, original_text, options, label, topic, text_dyslexia, text_tdah)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (p["id"], lang, split, p["level"], p.get("year"), p["text"], options_json, p.get("label", ""), topic, text_dyslexia, text_tdah))
            counts[(split, lang, topic)] += 1

def main():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute('''
        CREATE TABLE problems (
            id TEXT,
            language TEXT,
            split TEXT,  -- train o val: filtra por split si mides algo
            level INTEGER,
            year INTEGER,
            original_text TEXT,
            options TEXT,
            label TEXT,
            topic TEXT,
            text_dyslexia TEXT,
            text_tdah TEXT
        )
    ''')
    
    # Modificamos la clave primaria para soportar los tres idiomas
    # name: el nombre del tema para mostrar en ese idioma
    c.execute('CREATE TABLE theory (topic TEXT, language TEXT, name TEXT, content TEXT, PRIMARY KEY (topic, language))')
    # mode: la columna de problems (text_dyslexia, text_tdah); name: el nombre neutro para la página
    c.execute('CREATE TABLE modes (mode TEXT, language TEXT, name TEXT, PRIMARY KEY (mode, language))')

    for lang, topics in THEORY.items():
        for topic, content in topics.items():
            c.execute('INSERT INTO theory (topic, language, name, content) VALUES (?, ?, ?, ?)',
                      (topic, lang, TOPIC_NAMES[lang][topic], content))
    for lang, modes in MODE_NAMES.items():
        for mode, name in modes.items():
            c.execute('INSERT INTO modes (mode, language, name) VALUES (?, ?, ?)', (mode, lang, name))

    counts = Counter()
    process_file(TRAIN_PATH, c, "train", counts)
    process_file(VAL_PATH, c, "val", counts)

    conn.commit()
    conn.close()

    total = sum(counts.values())
    if not total:
        sys.exit(f"No se guardó ningún problema en {DB_PATH}. Prepara antes {TRAIN_PATH} y {VAL_PATH} (paso 9 del README).")
    print(f"Escrito {DB_PATH}: {total} problemas")
    for split in ("train", "val"):
        for lang in sorted({l for s, l, _ in counts if s == split}):
            by_topic = {t: n for (s, l, t), n in counts.items() if s == split and l == lang}
            print(f"  {split:5} {lang}: {sum(by_topic.values()):5}  " +
                  "  ".join(f"{t} {n}" for t, n in sorted(by_topic.items(), key=lambda x: -x[1])))

if __name__ == '__main__':
    main()