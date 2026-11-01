"""Filtro determinista para las respuestas del modelo (y opcionalmente del complice).

Uso:  texto_final = limpiar_respuesta(texto_crudo)
Guardar siempre el texto crudo en el JSON del historial (campo "text_raw").
"""
import random
import re
import unicodedata

# Frases tipicas de asistente: si una frase las contiene, se descarta entera.
PATRONES_ASISTENTE = re.compile(
    r"(hay alg[uú]n otro tema|hay algo (m[aá]s|espec[ií]fico)|en qu[eé] puedo ayudar|"
    r"te gustar[ií]a saber m[aá]s|puedo ayudarte|si necesitas|estoy aqu[ií] para|"
    r"como modelo|modelo de lenguaje|inteligencia artificial|espero que te)",
    re.IGNORECASE,
)

RESPALDOS = ["mmm no se", "ni idea la verdad", "no me acuerdo", "q?"]

# Palabras de España que delatan a un modelo para un publico mexicano.
ESPANOLISMOS = [
    (r"\bordenador(es)?\b", lambda m: "computadora" + ("s" if m.group(1) else "")),
    (r"\bchaval(es)?\b", lambda m: "chavo" + ("s" if m.group(1) else "")),
]


def quitar_tildes(s: str) -> str:
    """Quita tildes pero conserva la n con tilde (enie)."""
    s = s.replace("ñ", "\x00n").replace("Ñ", "\x00N")
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.replace("\x00n", "ñ").replace("\x00N", "Ñ")
    return unicodedata.normalize("NFC", s)


# --- Risas con variacion y errores de dedo -----------------------------------------
# La "s" queda junto a la "a" en el teclado, asi que mucha gente escribe "jsjs" o "jajsjs".
# Se sortea cada vez: cuantas veces se repite, cuantas letras salen mal y en que posicion.
def _risa_con_error(rng):
    unidades = rng.choice([2, 2, 3, 3, 3, 4])
    vocales = ["a" if rng.random() < 0.5 else "s"]            # la primera unidad puede ser "ja" o "js"
    vocales += ["s" if rng.random() < 0.55 else "a" for _ in range(unidades - 1)]
    if "s" not in vocales:                                    # que de verdad lleve al menos un error
        vocales[rng.randrange(unidades)] = "s"
    risa = "".join("j" + v for v in vocales)
    if rng.random() < 0.25:                                   # a veces queda cortada: "jajsj"
        risa += "j"
    return risa


def _risa_limpia(rng):
    return "ja" * rng.choice([2, 2, 3, 3, 4]) + ("j" if rng.random() < 0.15 else "")


def variar_risas(texto, prob_error=0.5, rng=random):
    """Reemplaza cada "jaja..." por una risa de longitud aleatoria; con probabilidad
    prob_error lleva errores de dedo (jsjs, jajsjs, jsjaj...)."""
    def rep(m):
        return _risa_con_error(rng) if rng.random() < prob_error else _risa_limpia(rng)
    return re.sub(r"(?<![a-zñ])(?:ja){2,}j?(?![a-zñ])", rep, texto, flags=re.IGNORECASE)


def limpiar_respuesta(texto: str, max_words: int = 18, minusculas: bool = True,
                      max_palabras: int = None, prob_risas: float = 0.5) -> str:
    # prob_risas: probabilidad de que una risa lleve errores de dedo; None = no tocar las risas.
    # max_words es el nombre que usa app/main.py; max_palabras se acepta como alias.
    if max_palabras is not None:
        max_words = max_palabras
    t = (texto or "").strip()
    # prefijos tipo "Alex:" y formato
    t = re.sub(r"^\s*(alex|otro|usuario)\s*:\s*", "", t, flags=re.IGNORECASE)
    # Tokens especiales de plantillas (<|im_end|>, <|eot_id|>...): lo que sigue es el modelo
    # inventando turnos, se descarta todo desde la primera etiqueta.
    t = t.split("<|")[0]
    # Turnos inventados en lineas nuevas ("Otro: ...", "Usuario: ...")
    t = re.split(r"\n\s*(?:otro|usuario|user|alex)\s*:", t, flags=re.IGNORECASE)[0]
    # Acotaciones tipo *pausa* o (pausa): no las escribe una persona por chat
    t = re.sub(r"\*[^*]{1,40}\*", "", t)
    t = re.sub(r"\([^)]{0,40}\)", "", t)
    t = re.sub(r"[*_`#>]+", "", t)
    t = t.replace("¿", "").replace("¡", "")
    t = re.sub(r"\s+([?!.,])", r"\1", t)          # "hola ?" -> "hola?"
    t = re.sub(r"([?!]){2,}", r"\1", t)             # "??" -> "?"
    # Españolismos -> mexicano
    for pat, rep in ESPANOLISMOS:
        t = re.sub(pat, rep, t, flags=re.IGNORECASE)
    # Cierre de asistente: ", alguna pregunta sobre X?" al final
    t = re.sub(r"[,;.]?\s*\b(alguna pregunta|tienes alguna pregunta|hablemos de|te interesa saber)\b[^?.!]*\?\s*$",
               "", t, flags=re.IGNORECASE)
    t = t.strip(' "\'“”«»')

    # Si no termina en puntuacion, la ultima frase quedo cortada (max_tokens)
    cortada = not re.search(r"[.!?…]\s*$", t)
    frases = [f.strip() for f in re.split(r"(?<=[.!?])\s+", t) if f.strip()]
    if cortada and len(frases) > 1:
        frases = frases[:-1]

    # Quitar frases de asistente
    frases = [f for f in frases if not PATRONES_ASISTENTE.search(f)]
    if not frases:
        return random.choice(RESPALDOS)

    # Mantener la primera frase y sumar las siguientes mientras quepan
    elegidas, total = [], 0
    for f in frases:
        n = len(f.split())
        if elegidas and total + n > max_words:
            break
        elegidas.append(f)
        total += n
        if len(elegidas) == 2:
            break

    out = " ".join(elegidas)
    out = quitar_tildes(out)
    if prob_risas is not None:
        out = variar_risas(out, prob_risas)
    out = re.sub(r"\.+\s*$", "", out)      # sin punto final (ni puntos suspensivos)
    out = re.sub(r"\s+", " ", out).strip()
    return out.lower() if minusculas else out


if __name__ == "__main__":
    pruebas = [
        "hola soy alex, y tu? jaja no me acuerdo si es un bot o algo así. ¿Cómo estás?",
        'La novena sinfonía de Mozart se llama "La Cuna del Amor". Es una obra maestra que combina elementos clásicos con un toque romántico. Mmm, eso ya no me acuerdo bien, pero creo que te puede interesar saber más sobre ella. ¿Hay algún otro tema sobre el que',
        "sí, seguro. ¿Hay algún otro tema sobre el que quieras saber?",
        "Alex: Sí, los sinosodontesaurios son interesantes. Son una raza de dinosaurios que vivieron hace unos 90 millones de años y tienen un aspecto único. Eso suena muy emocionante. ¿Hay algo específico sobre ellos que te gustaría saber más?",
        "sabes un poquito. ley de ohm, microcontroladores y RTOS me dan algo. cosas avanzadas como circuitos integrados o circuitos RC no estoy tan seguro. kichof... jaja, eso lo desconozco. pero creo que entiendo la idea general.",
        "jaja no, soy persona. por que? preguntaste?",
    ]
    for p in pruebas:
        print("IN :", p[:90])
        print("OUT:", limpiar_respuesta(p))
        print()
