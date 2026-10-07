"""
app/session.py — Gestión del estado de la sesión activa.

Una sola sesión activa a la vez. El estado se mantiene en memoria y
se serializa a JSON al terminar.

Roles posibles (quién responde al profesor):
  "ia"     — el modelo RKLLM responde
  "humano" — el cómplice responde
"""
import asyncio
import json
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from .config import config


class SessionState(str, Enum):
    WAITING = "waiting"       # Esperando que ambos lados estén listos
    ACTIVE = "active"         # Sesión en curso
    VOTING = "voting"         # Tiempo terminado, esperando voto
    FINISHED = "finished"     # Sesión terminada, resultado revelado


class ForcedMode(str, Enum):
    RANDOM = "aleatorio"
    AI = "ia"
    HUMAN = "humano"


@dataclass
class Message:
    role: str        # "profesor" | "interlocutor"
    content: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class SessionData:
    session_id: str
    responder: str                          # "ia" | "humano" (decidido al inicio)
    state: SessionState = SessionState.WAITING
    history: list[Message] = field(default_factory=list)
    start_time: Optional[float] = None
    duration_seconds: int = field(default_factory=lambda: config.session.duration_seconds)
    professor_vote: Optional[str] = None   # "ia" | "humano"
    result_correct: Optional[bool] = None
    forced_mode: ForcedMode = ForcedMode.RANDOM

    def remaining_seconds(self) -> float:
        if self.start_time is None:
            return self.duration_seconds
        elapsed = time.time() - self.start_time
        return max(0.0, self.duration_seconds - elapsed)

    def is_expired(self) -> bool:
        return self.remaining_seconds() <= 0

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "responder": self.responder,
            "state": self.state.value,
            "start_time": self.start_time,
            "duration_seconds": self.duration_seconds,
            "professor_vote": self.professor_vote,
            "result_correct": self.result_correct,
            "forced_mode": self.forced_mode.value,
            "history": [
                {"role": m.role, "content": m.content, "timestamp": m.timestamp}
                for m in self.history
            ],
        }


class SessionManager:
    """
    Gestiona la única sesión activa.

    Usa asyncio.Lock para acceso seguro en el contexto async de FastAPI.
    """

    def __init__(self):
        self._session: Optional[SessionData] = None
        self._lock = asyncio.Lock()
        # WebSocket del profesor (si está conectado)
        self.professor_ws = None
        # WebSocket del cómplice (si está conectado)
        self.panel_ws = None
        # Cómplice listo
        self.panel_ready: bool = False
        # Modelo listo (resultado del último ping)
        self.model_ready: bool = False
        # Evento que el cómplice señala cuando envía una respuesta (ronda humana)
        self._human_response_event: Optional[asyncio.Event] = None
        self._human_response_text: Optional[str] = None
        # Evento directo de takeover (ronda IA interceptada por el cómplice)
        self._takeover_event: asyncio.Event = asyncio.Event()
        self._takeover_text: Optional[str] = None
        # Semáforo: solo un informe al modelo a la vez
        self._model_lock = asyncio.Lock()

    @property
    def session(self) -> Optional[SessionData]:
        return self._session

    async def create_session(self, forced_mode: ForcedMode = ForcedMode.RANDOM) -> SessionData:
        """Crea una nueva sesión, descartando cualquier sesión anterior."""
        async with self._lock:
            # Decidir quién responde
            if forced_mode == ForcedMode.AI:
                responder = "ia"
            elif forced_mode == ForcedMode.HUMAN:
                responder = "humano"
            else:
                responder = secrets.choice(["ia", "humano"])

            self._session = SessionData(
                session_id=secrets.token_hex(8),
                responder=responder,
                forced_mode=forced_mode,
            )
            self._human_response_event = None
            self._human_response_text = None
            # Reiniciar el evento de takeover para la nueva sesión
            self._takeover_event.clear()
            self._takeover_text = None
            return self._session

    async def start_session(self) -> bool:
        """
        Inicia la sesión (activa el temporizador).
        Retorna False si no hay sesión creada o no están listos.
        """
        async with self._lock:
            if self._session is None:
                return False
            if not (self.panel_ready and self.model_ready):
                return False
            self._session.state = SessionState.ACTIVE
            self._session.start_time = time.time()
            return True

    async def add_message(self, role: str, content: str) -> Message:
        async with self._lock:
            msg = Message(role=role, content=content)
            if self._session:
                self._session.history.append(msg)
            return msg

    async def set_vote(self, vote: str) -> bool:
        """Registra el voto del profesor. vote = 'ia' | 'humano'."""
        async with self._lock:
            if self._session is None:
                return False
            self._session.professor_vote = vote
            self._session.result_correct = (vote == self._session.responder)
            self._session.state = SessionState.FINISHED
            return True

    async def request_human_response(self) -> Optional[str]:
        """
        Espera la respuesta del cómplice.
        Retorna el texto o None si se agota el tiempo máximo.
        """
        event = asyncio.Event()
        async with self._lock:
            self._human_response_event = event
            self._human_response_text = None

        try:
            await asyncio.wait_for(event.wait(), timeout=config.timing.human_max_wait)
        except asyncio.TimeoutError:
            return None

        return self._human_response_text

    async def deliver_human_response(self, text: str):
        """El cómplice entrega su respuesta."""
        async with self._lock:
            self._human_response_text = text
            if self._human_response_event:
                self._human_response_event.set()

    async def take_control(self, text: str):
        """
        El cómplice toma el control de esta ronda:
        dispara el evento directo de takeover (para rondas de IA)
        y también señala el evento de respuesta humana como respaldo.
        """
        print(f"[take_control] Recibido: {text!r}")
        async with self._lock:
            self._takeover_text = text
            self._takeover_event.set()
        print("[take_control] Evento disparado")
        # Respaldo: también señala el canal de respuesta humana
        await self.deliver_human_response(text)

    async def reset_session(self):
        """Reinicia todo el estado de la sesión."""
        async with self._lock:
            self._session = None
            self._human_response_event = None
            self._human_response_text = None
            self._takeover_event.clear()
            self._takeover_text = None

    async def save_history(self):
        """Guarda el historial de la sesión activa en un archivo JSON."""
        async with self._lock:
            if self._session is None:
                return
            data = self._session.to_dict()

        output_dir = Path(config.history.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = output_dir / f"sesion_{timestamp}_{data['session_id']}.json"
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"[session] Historial guardado en {filename}")

        # Webhook opcional
        if config.webhook.enabled and config.webhook.url:
            await _send_webhook(config.webhook.url, data)

    def get_history_for_rkllm(self) -> list[dict]:
        """
        Devuelve el historial en formato OpenAI messages para enviar al modelo.
        Incluye solo los turnos del professor (user) e interlocutor (assistant).
        """
        if self._session is None:
            return []
        messages = []
        for m in self._session.history:
            if m.role == "profesor":
                messages.append({"role": "user", "content": m.content})
            elif m.role == "interlocutor":
                messages.append({"role": "assistant", "content": m.content})
        return messages


async def _send_webhook(url: str, data: dict):
    """Envía el resumen de la sesión a un webhook (n8n). Fire-and-forget."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json=data)
        print(f"[webhook] Enviado a {url}")
    except Exception as e:
        print(f"[webhook] Error al enviar: {e}")


# Instancia global del gestor de sesiones
session_manager = SessionManager()
