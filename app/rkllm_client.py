"""
app/rkllm_client.py — Cliente async para el servidor RKLLM.

El servidor RKLLM expone una API compatible con OpenAI:
  POST /v1/chat/completions
  GET  /v1/models

Usamos el modo NO-streaming (stream=false) porque:
  1. El modelo maneja una petición a la vez.
  2. La app aplica su propio retraso de escritura antes de entregar al profesor.

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
    - Ping de disponibilidad (TCP + HTTP)
    - Inferencia completa (no streaming)
    - Filtrado de bloques <think>
    """

    def __init__(self):
        self._base_url = config.rkllm.base_url.rstrip("/")
        self._timeout = config.rkllm.timeout_seconds
        # Un solo lock: el servidor RKLLM no admite concurrencia
        self._lock = asyncio.Lock()

    def _build_headers(self) -> dict:
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

            # Confirma con HTTP
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
        return await self.chat(messages, max_tokens=30)

    async def chat(
        self,
        messages: list[dict],
        system_prompt: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> Optional[str]:
        """
        Envía un historial al modelo y devuelve la respuesta completa.

        Antepone el system prompt si se provee.
        Filtra bloques <think> de la respuesta.
        Retorna None si hay error o timeout.
        """
        async with self._lock:
            full_messages = []
            sp = system_prompt or config.system_prompt
            if sp:
                full_messages.append({"role": "system", "content": sp})
            full_messages.extend(messages)

            payload = {
                "model": "rkllm",
                "messages": full_messages,
                "stream": False,
                "temperature": config.rkllm.temperature,
                "top_p": config.rkllm.top_p,
                "top_k": config.rkllm.top_k,
                "max_tokens": max_tokens or config.rkllm.max_tokens,
                "repeat_penalty": config.rkllm.repeat_penalty,
                "enable_thinking": False,
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
                    r.raise_for_status()
                    data = r.json()
                    raw = data["choices"][0]["message"]["content"]
                    cleaned = strip_think_blocks(raw)
                    print(f"[rkllm] Respuesta en {elapsed:.1f}s: {cleaned[:80]!r}")
                    return cleaned, elapsed
            except httpx.TimeoutException:
                print("[rkllm] Timeout esperando respuesta del modelo.")
                return None, 0.0
            except Exception as e:
                print(f"[rkllm] Error: {e}")
                return None, 0.0


# Instancia global
rkllm_client = RkllmClient()
