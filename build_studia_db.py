#!/usr/bin/env python3
import json
import sqlite3
import os

DB_PATH = 'data/studia.db'
TRAIN_PATH = 'data/train.jsonl'
VAL_PATH = 'data/val.jsonl'

# Fichas estáticas de teoría para inyectar en SQLite
THEORY = {
    "Geometría": "Recuerda las fórmulas básicas: Área del triángulo = (b*h)/2. Suma de ángulos = 180º. Dibuja siempre la figura si no te la dan.",
    "Combinatoria": "Para contar combinaciones, usa el principio multiplicativo. Si tienes dudas, intenta listar los primeros casos a mano.",
    "Lógica": "Lee despacio. Si hay contradicciones, asume que una afirmación es falsa y comprueba si el resto tiene sentido.",
    "Aritmética": "Calcula paso a paso. Recuerda la prioridad de operaciones: primero paréntesis, luego multiplicación/división, y al final suma/resta.",
    "Patrones": "Busca la diferencia entre números consecutivos. Si no es constante, busca multiplicaciones o números alternos."
}

def classify_topic(text):
    text_lower = text.lower()
    if any(w in text_lower for w in ["triángulo", "área", "ángulo", "triangle", "area", "angle", "cm", "círculo", "cuadrado", "square"]):
        return "Geometría"
    elif any(w in text_lower for w in ["maneras", "combinaciones", "monedas", "ways", "combinations", "coins", "dados", "dice"]):
        return "Combinatoria"
    elif any(w in text_lower for w in ["verdad", "mentira", "siempre", "true", "false", "always", "lógica", "logic"]):
        return "Lógica"
    elif any(w in text_lower for w in ["secuencia", "patrón", "siguiente", "sequence", "pattern", "next"]):
        return "Patrones"
    else:
        return "Aritmética"

def format_dyslexia(text):
    # Una idea por línea, pregunta aislada al final
    sentences = [s.strip() for s in text.split('. ') if s.strip()]
    if not sentences: return text
    formatted = ".\n\n".join(sentences[:-1])
    if len(sentences) > 1:
        formatted += ".\n\n" + sentences[-1]
    else:
        formatted = sentences[0]
    return formatted

def format_tdah(text):
    # Enunciado troceado en pasos
    sentences = [s.strip() for s in text.split('. ') if s.strip()]
    if not sentences: return text
    steps = "\n".join([f"Paso {i+1}: {s}." for i, s in enumerate(sentences[:-1])])
    if len(sentences) > 1:
        steps += f"\n\nObjetivo final: {sentences[-1]}"
    else:
        steps = sentences[0]
    return steps

def process_file(filepath, cursor):
    if not os.path.exists(filepath):
        return
        
    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            if not line.strip(): continue
            p = json.loads(line)
            
            topic = classify_topic(p.get("text", ""))
            text_dyslexia = format_dyslexia(p.get("text", ""))
            text_tdah = format_tdah(p.get("text", ""))
            options_json = json.dumps(p.get("options", {}))
            
            cursor.execute('''
                INSERT INTO problems (id, language, level, year, original_text, options, label, topic, text_dyslexia, text_tdah)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (p["id"], p["language"], p["level"], p["year"], p["text"], options_json, p.get("label", ""), topic, text_dyslexia, text_tdah))

def main():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Crear tablas
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
    
    c.execute('CREATE TABLE theory (topic TEXT PRIMARY KEY, content TEXT)')
    
    # Inyectar teoría
    for topic, content in THEORY.items():
        c.execute('INSERT INTO theory (topic, content) VALUES (?, ?)', (topic, content))
        
    # Procesar datasets
    process_file(TRAIN_PATH, c)
    process_file(VAL_PATH, c)
    
    conn.commit()
    
    # Comprobar resultados
    c.execute('SELECT COUNT(*) FROM problems')
    total = c.fetchone()[0]
    print(f"Base de datos studia.db generada con {total} problemas.")
    
    c.execute('SELECT topic, COUNT(*) FROM problems GROUP BY topic')
    for row in c.fetchall():
        print(f" - {row[0]}: {row[1]} problemas")
        
    conn.close()

if __name__ == '__main__':
    main()