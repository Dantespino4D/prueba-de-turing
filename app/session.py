"""
app/session.py — Gestión del estado de la sesión activa.

Una sola sesión activa a la vez. El estado se mantiene en memoria y
se serializa a JSON al terminar.

Roles posibles (quién responde al profesor):
  "ia"     — el modelo RKLLM responde
  "humano" — el cómplice responde

Campos extra del historial (A1):
  Mensaje: "source" (ia | humano | takeover | ia_fallback),
           "model_latency_s", "delay_applied_s"
  Sesión:  "events" (lista de {time_iso, type}),
           "model_name", "temperature", "system_prompt_name",
           "end_time", "end_time_iso", "end_reason"
  Todas las marcas de tiempo se incluyen también en ISO 8601 (America/Mexico_City).
"""
import asyncio
import json
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from pathlib import Path
from typing import Optional

from .config import config

# Zona horaria de Ciudad de México (UTC-6, sin ajuste de verano simplificado)
# Usamos zoneinfo si está disponible; si no, caemos a un offset fijo UTC-6.
try:
    from zoneinfo import ZoneInfo
    _MX_TZ = ZoneInfo("America/Mexico_City")
except ImportError:
    _MX_TZ = timezone(timedelta(hours=-6))


def _now_iso() -> str:
    """Hora actual en ISO 8601 con zona America/Mexico_City."""
    return datetime.now(_MX_TZ).isoformat(timespec="seconds")


def _ts_to_iso(ts: float) -> str:
    """Convierte un timestamp Unix a ISO 8601 con zona America/Mexico_City."""
    return datetime.fromtimestamp(ts, tz=_MX_TZ).isoformat(timespec="seconds")


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
    # A1: fuente de la respuesta del interlocutor
    source: Optional[str] = None   # "ia" | "humano" | "takeover" | "ia_fallback" | None (para el profesor)
    model_latency_s: Optional[float] = None
    delay_applied_s: Optional[float] = None
    # Texto original del modelo antes del filtro (solo para source ia/ia_fallback)
    text_raw: Optional[str] = None


@dataclass
class SessionEvent:
    """Un evento notable dentro de la sesión (para el historial)."""
    event_type: str   # timeout | error_modelo | reintento | takeover | reset | vote | ia_fallback
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "type": self.event_type,
            "time": self.timestamp,
            "time_iso": _ts_to_iso(self.timestamp),
        }


@dataclass
class SessionData:
    session_id: str
    responder: str                          # "ia" | "humano" (decidido al inicio)
    state: SessionState = SessionState.WAITING
    history: list[Message] = field(default_factory=list)
    events: list[SessionEvent] = field(default_factory=list)
    start_time: Optional[float] = None
    duration_seconds: int = field(default_factory=lambda: config.session.duration_seconds)
    professor_vote: Optional[str] = None   # "ia" | "humano"
    result_correct: Optional[bool] = None
    forced_mode: ForcedMode = ForcedMode.RANDOM
    # A1: campos de sesión
    model_name: Optional[str] = None       # id del modelo según /v1/models
    temperature: Optional[float] = None
    system_prompt_name: Optional[str] = None
    end_time: Optional[float] = None
    end_reason: Optional[str] = None       # "vote" | "timeout" | "reset"

    def remaining_seconds(self) -> float:
        if self.start_time is None:
            return self.duration_seconds
        elapsed = time.time() - self.start_time
        return max(0.0, self.duration_seconds - elapsed)

    def is_expired(self) -> bool:
        return self.remaining_seconds() <= 0

    def add_event(self, event_type: str):
        """Registra un evento con la hora actual."""
        self.events.append(SessionEvent(event_type=event_type))

    def to_dict(self) -> dict:
        d = {
            "session_id": self.session_id,
            "responder": self.responder,
            "state": self.state.value,
            "forced_mode": self.forced_mode.value,
            # Tiempos Unix + ISO
            "start_time": self.start_time,
            "start_time_iso": _ts_to_iso(self.start_time) if self.start_time else None,
            "end_time": self.end_time,
            "end_time_iso": _ts_to_iso(self.end_time) if self.end_time else None,
            "end_reason": self.end_reason,
            "duration_seconds": self.duration_seconds,
            # Voto
            "professor_vote": self.professor_vote,
            "result_correct": self.result_correct,
            # Modelo
            "model_name": self.model_name,
            "temperature": self.temperature,
            "system_prompt_name": self.system_prompt_name,
            # Mensajes con campos extendidos
            "history": [
                {
                    "role": m.role,
                    "content": m.content,
                    "timestamp": m.timestamp,
                    "timestamp_iso": _ts_to_iso(m.timestamp),
                    **({"source": m.source} if m.source is not None else {}),
                    **({"model_latency_s": round(m.model_latency_s, 3)} if m.model_latency_s is not None else {}),
                    **({"delay_applied_s": round(m.delay_applied_s, 3)} if m.delay_applied_s is not None else {}),
                    **({"text_raw": m.text_raw} if m.text_raw is not None else {}),
                }
                for m in self.history
            ],
            # Eventos
            "events": [e.to_dict() for e in self.events],
        }
        return d


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
        # ID del modelo según /v1/models (None si no disponible)
        self.model_id: Optional[str] = None
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
                # A1: poblar campos de modelo en el momento de crear la sesión
                model_name=self.model_id,
                temperature=config.rkllm.temperature,
                system_prompt_name=_system_prompt_name(),
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

    async def add_message(
        self,
        role: str,
        content: str,
        source: Optional[str] = None,
        model_latency_s: Optional[float] = None,
        delay_applied_s: Optional[float] = None,
        text_raw: Optional[str] = None,
    ) -> Message:
        async with self._lock:
            msg = Message(
                role=role,
                content=content,
                source=source,
                model_latency_s=model_latency_s,
                delay_applied_s=delay_applied_s,
                text_raw=text_raw,
            )
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
            # A1: registrar end_time y end_reason
            self._session.end_time = time.time()
            self._session.end_reason = "vote"
            self._session.add_event("vote")
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
            # A1: registrar evento de timeout
            if self._session:
                self._session.add_event("timeout")
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
        # A1: registrar evento de takeover
        if self._session:
            self._session.add_event("takeover")
        # Respaldo: también señala el canal de respuesta humana
        await self.deliver_human_response(text)

    async def reset_session(self, end_reason: str = "reset"):
        """
        Guarda el historial si hay conversación en curso y luego reinicia el estado.
        Devuelve el error como string si el guardado falla, o None si fue bien.
        """
        save_error: Optional[str] = None
        if self._session and self._session.history:
            # A1: registrar end_time y end_reason antes de guardar
            if self._session.end_time is None:
                self._session.end_time = time.time()
                self._session.end_reason = end_reason
                self._session.add_event("reset")
            save_error = await self.save_history()
        async with self._lock:
            self._session = None
            self._human_response_event = None
            self._human_response_text = None
            self._takeover_event.clear()
            self._takeover_text = None
        return save_error

    async def save_history(self) -> Optional[str]:
        """
        Guarda el historial de la sesión activa en un archivo JSON.
        Retorna None si fue exitoso, o un string de error si falló.
        """
        async with self._lock:
            if self._session is None:
                return None
            data = self._session.to_dict()

        try:
            output_dir = Path(config.history.output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)

            timestamp = time.strftime("%Y%m%d_%H%M%S")
            filename = output_dir / f"sesion_{timestamp}_{data['session_id']}.json"
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print(f"[session] Historial guardado en {filename}")
        except Exception as e:
            err = f"Error al guardar historial: {e}"
            print(f"[session] {err}")
            return err

        # Webhook opcional (errores no bloquean el flujo principal)
        if config.webhook.enabled and config.webhook.url:
            await _send_webhook(config.webhook.url, data)

        return None

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


def _system_prompt_name() -> Optional[str]:
    """
    Devuelve el nombre del archivo system_prompt (sin ruta) si tiene contenido,
    o None si está vacío.
    """
    from pathlib import Path as _Path
    p = _Path(__file__).parent.parent / "system_prompt.txt"
    if p.exists() and p.read_text(encoding="utf-8").strip():
        return p.name
    return None


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
