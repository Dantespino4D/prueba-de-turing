"""
app/rkllm_client.py — Cliente async para el servidor RKLLM real.

El servidor RKLLM (flask_server.py) expone una API compatible con OpenAI:
  GET  /v1/models              → lista el modelo cargado
  POST /v1/chat/completions    → inferencia (stream o no-stream)

Formato del payload (confirmado en flask_server.py y chat_api_flask.py):
  {
    "model": "<nombre>",
    "messages": [{"role": "...", "content": "..."}],
    "stream": false,
    "temperature": 0.8,
    "top_p": 0.9,
    "top_k": 1,
    "max_tokens": 200,
    "repeat_penalty": 1.1,
    "frequency_penalty": 0.0,
    "presence_penalty": 0.0,
    "enable_thinking": false
  }

El servidor maneja UNA petición a la vez y devuelve HTTP 503 si está ocupado.
Usamos stream=false porque la app aplica su propio retraso de escritura.

Filtrado de bloques <think>...</think> que emiten algunos modelos (Qwen2.5).
"""
import asyncio
import re
import socket
import time
from typing import Optional

import httpx

from .config import config


# Patrón para bloques de razonamiento que deben filtrarse
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def strip_think_blocks(text: str) -> str:
    """Elimina bloques <think>...</think> del texto generado por el modelo."""
    return _THINK_RE.sub("", text).strip()


class RkllmClient:
    """
    Cliente async para el servidor RKLLM (OpenAI-compatible).

    Maneja:
    - Ping de disponibilidad (TCP + HTTP GET /v1/models)
    - Inferencia completa (no streaming, POST /v1/chat/completions)
    - Filtrado de bloques <think>
    - Error 503 cuando el servidor está ocupado
    """

    def __init__(self):
        self._base_url = config.rkllm.base_url.rstrip("/")
        self._timeout = config.rkllm.timeout_seconds
        # Un solo lock: el servidor RKLLM no admite concurrencia
        self._lock = asyncio.Lock()

    def _build_headers(self) -> dict:
        # Header confirmado en chat_api_flask.py
        return {
            "Content-Type": "application/json",
            "Authorization": "not_required",
        }

    async def ping(self) -> bool:
        """
        Comprueba disponibilidad del servidor RKLLM.
        Intenta primero una conexión TCP, luego GET /v1/models.
        No hace inferencia (no ocupa la NPU).
        """
        try:
            # Extrae host y puerto de la URL
            from urllib.parse import urlparse
            parsed = urlparse(self._base_url)
            host = parsed.hostname or "localhost"
            port = parsed.port or 8080

            # Conexión TCP (rápida)
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, lambda: socket.create_connection((host, port), timeout=3)
            )

            # Confirma con HTTP GET /v1/models (endpoint confirmado en flask_server.py)
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(f"{self._base_url}/v1/models")
                return r.status_code == 200
        except Exception:
            return False

    async def test_inference(self, prompt: str = "Hola") -> Optional[str]:
        """
        Lanza una inferencia corta para verificar que el modelo responde.
        Solo para el botón "probar modelo" del panel.
        """
        messages = [{"role": "user", "content": prompt}]
        result = await self.chat(messages, max_tokens=30)
        return result

    async def chat(
        self,
        messages: list[dict],
        system_prompt: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> Optional[tuple]:
        """
        Envía un historial al modelo y devuelve (respuesta, elapsed_seconds).

        Antepone el system prompt si se provee.
        Filtra bloques <think> de la respuesta.
        Retorna (None, 0.0) si hay error, timeout o servidor ocupado.

        El servidor RKLLM devuelve 503 cuando está procesando otra petición.
        El lock local evita que esto ocurra en condiciones normales.
        """
        async with self._lock:
            full_messages = []
            sp = system_prompt or config.system_prompt
            if sp:
                full_messages.append({"role": "system", "content": sp})
            full_messages.extend(messages)

            # Payload confirmado con flask_server.py y chat_api_flask.py
            payload = {
                "model": "rkllm",
                "messages": full_messages,
                "stream": False,
                "temperature": config.rkllm.temperature,
                "top_p": config.rkllm.top_p,
                "top_k": config.rkllm.top_k,
                "max_tokens": max_tokens or config.rkllm.max_tokens,
                "repeat_penalty": config.rkllm.repeat_penalty,
                "frequency_penalty": 0.0,
                "presence_penalty": 0.0,
                "enable_thinking": False,  # Filtramos <think> nosotros de todas formas
            }

            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    t0 = time.monotonic()
                    r = await client.post(
                        f"{self._base_url}/v1/chat/completions",
                        json=payload,
                        headers=self._build_headers(),
                    )
                    elapsed = time.monotonic() - t0

                    if r.status_code == 503:
                        # El servidor RKLLM está ocupado con otra petición
                        print("[rkllm] Servidor ocupado (503). Reintentar más tarde.")
                        return None, 0.0

                    r.raise_for_status()
                    data = r.json()
                    raw = data["choices"][0]["message"]["content"]
                    cleaned = strip_think_blocks(raw)
                    print(f"[rkllm] Respuesta en {elapsed:.1f}s: {cleaned[:80]!r}")
                    return cleaned, elapsed

            except httpx.TimeoutException:
                print(f"[rkllm] Timeout ({self._timeout}s) esperando respuesta del modelo.")
                return None, 0.0
            except Exception as e:
                print(f"[rkllm] Error: {e}")
                return None, 0.0


# Instancia global
rkllm_client = RkllmClient()
