"""
mock_rkllm/server.py — Servidor mock del RKLLM para desarrollo.

Imita exactamente la API OpenAI-compatible de flask_server.py:
  GET  /v1/models
  POST /v1/chat/completions

Responde con frases de una lista predefinida, con latencia simulada configurable.
No requiere hardware NPU.

Uso:
  python -m mock_rkllm.server [--port 8081] [--min-latency 3] [--max-latency 10]
"""
import argparse
import random
import time
import uuid
import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

# -----------------------------------------------------------------------
# Respuestas simuladas (el mock elige una al azar)
# -----------------------------------------------------------------------
MOCK_RESPONSES = [
    "Interesante pregunta. Déjame pensarlo un momento.",
    "Desde mi perspectiva, creo que hay varios ángulos para analizar esto.",
    "Sí, estoy de acuerdo en parte, aunque habría que considerar otros factores.",
    "Eso depende mucho del contexto. ¿Podrías darme más detalles?",
    "He leído bastante sobre ese tema. Es más complejo de lo que parece.",
    "No estoy completamente seguro, pero creo que sí.",
    "Mmm, esa es una buena observación. No lo había pensado así.",
    "Prefiero no adelantarme a conclusiones sin más información.",
    "En términos generales, sí. Pero hay excepciones importantes.",
    "Eso es algo que muchos se preguntan. La respuesta corta es: depende.",
    "Me parece un tema fascinante. ¿Qué opinas tú?",
    "Hay estudios que apoyan eso, aunque también hay evidencia en contra.",
    "Creo que lo más honesto es decir que no tengo certeza absoluta.",
    "Sí, exactamente. Eso es lo que yo también pienso.",
    "No, no creo que sea tan sencillo. La realidad suele ser más matizada.",
]

MODEL_NAME = "mock-rkllm-dev"

app = FastAPI(title="Mock RKLLM Server")

# Configuración de latencia (modificada por argumentos de línea de comandos)
_min_latency: float = 3.0
_max_latency: float = 10.0


@app.get("/v1/models")
async def list_models():
    """Lista los modelos disponibles (OpenAI-compatible)."""
    return {
        "object": "list",
        "data": [
            {
                "id": MODEL_NAME,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "mock",
            }
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """
    Simula una respuesta del modelo con latencia aleatoria.
    Acepta el mismo payload que flask_server.py.
    """
    body = await request.json()
    stream = body.get("stream", False)

    # Simular latencia del modelo
    latency = random.uniform(_min_latency, _max_latency)
    await asyncio.sleep(latency)

    # Elegir respuesta aleatoria
    content = random.choice(MOCK_RESPONSES)

    # Si la petición pide streaming, responder igual pero con un solo chunk
    # (el cliente de la app usa stream=False, pero lo soportamos por completitud)
    if stream:
        # Para no complicar el mock, devolvemos non-streaming aunque pidan stream
        pass

    response = {
        "id": f"chatcmpl-mock-{uuid.uuid4().hex[:16]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": MODEL_NAME,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                },
                "logprobs": None,
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": len(content.split()),
            "total_tokens": 10 + len(content.split()),
        },
    }
    return JSONResponse(content=response)


def main():
    global _min_latency, _max_latency

    parser = argparse.ArgumentParser(description="Mock RKLLM Server para desarrollo")
    parser.add_argument("--port", type=int, default=8081, help="Puerto (default: 8081)")
    parser.add_argument("--min-latency", type=float, default=3.0, help="Latencia mínima en segundos")
    parser.add_argument("--max-latency", type=float, default=10.0, help="Latencia máxima en segundos")
    args = parser.parse_args()

    _min_latency = args.min_latency
    _max_latency = args.max_latency

    print(f"[mock-rkllm] Iniciando en puerto {args.port}")
    print(f"[mock-rkllm] Latencia simulada: {_min_latency}–{_max_latency} s")
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
