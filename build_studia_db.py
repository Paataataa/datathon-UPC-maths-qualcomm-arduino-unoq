#!/usr/bin/env python3
import json
import sqlite3
import os
import re

DB_PATH = 'data/studia.db'
TRAIN_PATH = 'data/train.jsonl'
VAL_PATH = 'data/val.jsonl'

# 3. Teoría trilingüe
THEORY = {
    "esp": {
        "Geometría": "Recuerda las fórmulas básicas: Área del triángulo = (b*h)/2. Suma de ángulos = 180º. Dibuja siempre la figura si no te la dan.",
        "Combinatoria": "Para contar combinaciones, usa el principio multiplicativo. Si tienes dudas, intenta listar los primeros casos a mano.",
        "Lógica": "Lee despacio. Si hay contradicciones, asume que una afirmación es falsa y comprueba si el resto tiene sentido.",
        "Aritmética": "Calcula paso a paso. Recuerda la prioridad de operaciones: primero paréntesis, luego multiplicación/división, y al final suma/resta.",
        "Patrones": "Busca la diferencia entre números consecutivos. Si no es constante, busca multiplicaciones o números alternos."
    },
    "eng": {
        "Geometría": "Remember basic formulas: Triangle area = (b*h)/2. Sum of angles = 180º. Always draw the figure if not provided.",
        "Combinatoria": "To count combinations, use the multiplicative principle. If in doubt, try listing the first cases by hand.",
        "Lógica": "Read slowly. If there are contradictions, assume one statement is false and check if the rest makes sense.",
        "Aritmética": "Calculate step by step. Remember the order of operations: parentheses first, then multiplication/division, and finally addition/subtraction.",
        "Patrones": "Look for the difference between consecutive numbers. If it's not constant, look for multiplications or alternating numbers."
    },
    "cat": {
        "Geometría": "Recorda les fórmules bàsiques: Àrea del triangle = (b*h)/2. Suma d'angles = 180º. Dibuixa sempre la figura si no te la donen.",
        "Combinatoria": "Per comptar combinacions, utilitza el principi multiplicatiu. Si tens dubtes, intenta llistar els primers casos a mà.",
        "Lógica": "Llegeix a poc a poc. Si hi ha contradiccions, assumeix que una afirmació és falsa i comprova si la resta té sentit.",
        "Aritmética": "Calcula pas a pas. Recorda la prioritat d'operacions: primer parèntesis, després multiplicació/divisió, i al final suma/resta.",
        "Patrones": "Busca la diferència entre nombres consecutius. Si no és constant, busca multiplicacions o nombres alterns."
    }
}

# 1 y 2. Clasificador por Regex (\b) incluyendo catalán
KEYWORDS = {
    "Geometría": re.compile(r'\b(triángul[oa]s?|área|ángulos?|círculos?|cuadrados?|cm|triangles?|area|angles?|squares?|circles?|cercles?|quadrats?)\b', re.IGNORECASE),
    "Combinatoria": re.compile(r'\b(maneras|combinaciones|monedas|dados?|ways|combinations|coins|dice|maneres|daus?|monedes)\b', re.IGNORECASE),
    "Lógica": re.compile(r'\b(verdad|mentira|siempre|lógica|true|false|always|logic|veritat|mentida|sempre|lògica)\b', re.IGNORECASE),
    "Patrones": re.compile(r'\b(secuencia|patrón|siguiente|sequence|pattern|next|seqüència|patró|següent)\b', re.IGNORECASE)
}

def classify_topic(text):
    for topic, pattern in KEYWORDS.items():
        if pattern.search(text):
            return topic
    return "Aritmética"

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

def format_tdah(text):
    sentences = clean_and_split(text)
    if not sentences: return text
    if len(sentences) == 1:
        return sentences[0]
    steps = "\n".join([f"Paso {i+1}: {s}" for i, s in enumerate(sentences[:-1])])
    steps += f"\n\nObjetivo final: {sentences[-1]}"
    return steps

def process_file(filepath, cursor):
    if not os.path.exists(filepath):
        return
        
    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            if not line.strip(): continue
            p = json.loads(line)
            
            lang = p.get("language", "esp")
            # Normalizar esp a spa si hiciera falta, M3Kang usa eng, cat, esp
            topic = classify_topic(p.get("text", ""))
            text_dyslexia = format_dyslexia(p.get("text", ""))
            text_tdah = format_tdah(p.get("text", ""))
            options_json = json.dumps(p.get("options", {}))
            
            cursor.execute('''
                INSERT INTO problems (id, language, level, year, original_text, options, label, topic, text_dyslexia, text_tdah)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (p["id"], lang, p["level"], p["year"], p["text"], options_json, p.get("label", ""), topic, text_dyslexia, text_tdah))

def main():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute('''
        CREATE TABLE problems (
            id TEXT,
            language TEXT,
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
    c.execute('CREATE TABLE theory (topic TEXT, language TEXT, content TEXT, PRIMARY KEY (topic, language))')
    
    for lang, topics in THEORY.items():
        for topic, content in topics.items():
            c.execute('INSERT INTO theory (topic, language, content) VALUES (?, ?, ?)', (topic, lang, content))
        
    process_file(TRAIN_PATH, c)
    process_file(VAL_PATH, c)
    
    conn.commit()
    conn.close()

if __name__ == '__main__':
    main()