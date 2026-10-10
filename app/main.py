"""
app/main.py — Aplicación FastAPI principal para la Prueba de Turing.

Rutas:
  GET  /                  → chat del profesor
  WS   /ws                → WebSocket del profesor
  GET  {panel_path}       → panel del cómplice (URL dinámica desde config)
  GET  {panel_path}/auth  → formulario de login del panel
  POST {panel_path}/auth  → validar contraseña → cookie httpOnly
  WS   {panel_path}/ws    → WebSocket del cómplice

Protocolo WebSocket (JSON):
  Mensajes del servidor → cliente (ambos lados):
    { "type": "message",     "role": "interlocutor"|"profesor", "content": "..." }
    { "type": "typing",      "state": true|false }
    { "type": "status",      "state": "waiting"|"active"|"voting"|"finished",
                              "remaining": <segundos>, "session_id": "..." }
    { "type": "panel_status","panel_ready": bool, "model_ready": bool }
    { "type": "role",        "responder": "ia"|"humano" }   ← solo al panel
    { "type": "error",       "message": "..." }
    { "type": "result",      "vote": "...", "responder": "...", "correct": bool,
                              "history": [...] }
    { "type": "ping_result", "ok": bool }    ← respuesta al probar modelo
    { "type": "pong" }

  Mensajes cliente → servidor:
    Profesor:
      { "type": "message",  "content": "..." }
      { "type": "vote",     "answer": "ia"|"humano" }
      { "type": "ping" }
    Panel:
      { "type": "response", "content": "..." }
      { "type": "take_control", "content": "..." }
      { "type": "set_mode", "mode": "aleatorio"|"ia"|"humano" }
      { "type": "start_session" }
      { "type": "reset_session" }
      { "type": "test_model" }
      { "type": "ping" }
"""
import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

# Heartbeat: intervalo de ping (s) y tolerancia de fallos consecutivos antes de cerrar.
# Fix 2: toleramos HEARTBEAT_MAX_MISSES pongs perdidos antes de considerar muerta la conexión.
HEARTBEAT_INTERVAL  = 20
HEARTBEAT_TIMEOUT   = 10
HEARTBEAT_MAX_MISSES = 3   # 3 pings perdidos (~90 s) → cierre definitivo

from fastapi import Cookie, Depends, FastAPI, Form, HTTPException, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import config
from .filtro_respuesta import limpiar_respuesta
from .rkllm_client import rkllm_client
from .session import ForcedMode, SessionState, session_manager
from .timing import ai_response_delay, human_response_delay, reading_delay, typing_speed_chars_per_sec

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent.parent
STATIC_DIR = BASE_DIR / "static"

PANEL_PATH = config.app.panel_path.rstrip("/")
PANEL_AUTH_PATH = f"{PANEL_PATH}/auth"
PANEL_WS_PATH = f"{PANEL_PATH}/ws"

# Nombre de la cookie de sesión del panel
COOKIE_NAME = "turing_panel_session"
# Expiración de la cookie: 8 horas
COOKIE_MAX_AGE = 8 * 3600

# Serializador de cookies firmadas
_signer = URLSafeTimedSerializer(config.app.secret_key)

# Fix 3: mensajes del interlocutor que no pudieron enviarse al profesor
# porque el WS estaba cerrado. Se reenvían cuando el profesor reconecta.
# Lista de dicts: {"index": int, "content": str}  (index en session.history)
_pending_professor_deliveries: list[dict] = []


# ---------------------------------------------------------------------------
# Lifespan (startup/shutdown)
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ping inicial al modelo en segundo plano
    asyncio.create_task(_initial_model_ping())
    yield


async def _initial_model_ping():
    """Comprueba disponibilidad del modelo al arrancar."""
    ok, model_id = await rkllm_client.ping()
    session_manager.model_ready = ok
    session_manager.model_id = model_id
    print(f"[startup] Modelo {'disponible' if ok else 'NO disponible'}"
          + (f": {model_id}" if model_id else ""))
    await _broadcast_panel_status()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="Prueba de Turing", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Helpers de autenticación del panel
# ---------------------------------------------------------------------------
def _sign_panel_token() -> str:
    return _signer.dumps("panel_ok")


def _verify_panel_token(token: str) -> bool:
    try:
        _signer.loads(token, max_age=COOKIE_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


def _panel_auth_required(
    panel_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
):
    """Dependencia FastAPI: redirige al login si no hay cookie válida."""
    if not panel_session or not _verify_panel_token(panel_session):
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": PANEL_AUTH_PATH},
        )


# ---------------------------------------------------------------------------
# Rutas HTTP
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def professor_page():
    """Página del profesor."""
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get(PANEL_AUTH_PATH, response_class=HTMLResponse)
async def panel_login_page():
    """Formulario de login del panel."""
    return (STATIC_DIR / "login.html").read_text(encoding="utf-8")


@app.post(PANEL_AUTH_PATH)
async def panel_login(password: str = Form(...)):
    """Valida la contraseña y establece la cookie de sesión."""
    if password != config.app.panel_password:
        return HTMLResponse(
            content=_login_error_html("Contraseña incorrecta."),
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    token = _sign_panel_token()
    response = RedirectResponse(url=PANEL_PATH, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        max_age=COOKIE_MAX_AGE,
        samesite="lax",
    )
    return response


@app.get(PANEL_PATH, response_class=HTMLResponse)
async def panel_page(_auth=Depends(_panel_auth_required)):
    """Panel del cómplice (protegido)."""
    return (STATIC_DIR / "panel.html").read_text(encoding="utf-8")


def _login_error_html(msg: str) -> str:
    """Devuelve el HTML del formulario con un mensaje de error."""
    return (STATIC_DIR / "login.html").read_text(encoding="utf-8").replace(
        "<!-- ERROR_PLACEHOLDER -->",
        f'<p class="error">{msg}</p>',
    )


# ---------------------------------------------------------------------------
# Broadcast helpers
# ---------------------------------------------------------------------------

async def _send(ws: WebSocket, msg: dict):
    """Envía un mensaje JSON por WebSocket, ignorando errores de desconexión."""
    try:
        await ws.send_json(msg)
    except Exception:
        pass


async def _broadcast_status():
    """Envía el estado actual a ambos lados (si están conectados)."""
    sess = session_manager.session
    if sess is None:
        msg = {"type": "status", "state": "waiting", "remaining": 0, "session_id": None}
    else:
        msg = {
            "type": "status",
            "state": sess.state.value,
            "remaining": sess.remaining_seconds(),
            "session_id": sess.session_id,
        }
    for ws in [session_manager.professor_ws, session_manager.panel_ws]:
        if ws:
            await _send(ws, msg)


async def _broadcast_panel_status():
    """Informa al panel el estado de conectividad (panel_ready, model_ready, model_id)."""
    msg = {
        "type": "panel_status",
        "panel_ready": session_manager.panel_ready,
        "model_ready": session_manager.model_ready,
        "model_id": session_manager.model_id,
    }
    if session_manager.panel_ws:
        await _send(session_manager.panel_ws, msg)


async def _notify_professor_status(connected: bool):
    """Avisa al panel si el profesor está conectado o no."""
    if session_manager.panel_ws:
        await _send(session_manager.panel_ws, {
            "type": "professor_status",
            "connected": connected,
        })


# ---------------------------------------------------------------------------
# Fix 7: estado de ocupación del modelo
# ---------------------------------------------------------------------------

_model_busy: bool = False  # True mientras hay una inferencia en curso

async def _set_model_busy(busy: bool):
    """Actualiza el flag de ocupación y notifica al panel."""
    global _model_busy
    _model_busy = busy
    if session_manager.panel_ws:
        await _send(session_manager.panel_ws, {
            "type": "model_busy",
            "busy": busy,
        })


# ---------------------------------------------------------------------------
# WebSocket del profesor
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def professor_ws(websocket: WebSocket):
    await websocket.accept()
    session_manager.professor_ws = websocket
    print("[ws/profesor] Conectado")

    # Avisar al panel que el profesor está conectado
    await _notify_professor_status(True)

    # Enviar estado actual para recuperar sesión si existe
    await _broadcast_status()

    # Fix 5: si hay sesión activa, enviar señal de "limpiar chat" PRIMERO,
    # luego el historial completo, para que el cliente no duplique mensajes.
    sess = session_manager.session
    if sess and sess.state in (SessionState.ACTIVE, SessionState.VOTING, SessionState.FINISHED):
        # Señal al cliente para que limpie su chat antes de recibir el historial
        await _send(websocket, {"type": "history_start"})
        for m in sess.history:
            await _send(websocket, {
                "type": "message",
                "role": m.role,
                "content": m.content,
            })
        await _send(websocket, {"type": "history_end"})

        # Fix 3: reenviar mensajes que no pudieron entregarse antes
        if _pending_professor_deliveries:
            for item in list(_pending_professor_deliveries):
                await _send(websocket, {
                    "type": "message",
                    "role": "interlocutor",
                    "content": item["content"],
                })
            _pending_professor_deliveries.clear()

    # Fix 2: heartbeat con tolerancia de múltiples fallos
    _pong_event = asyncio.Event()

    async def _heartbeat():
        """Envía ping periódico; cierra solo tras HEARTBEAT_MAX_MISSES fallos seguidos."""
        misses = 0
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            if websocket.client_state.value >= 2:  # CLOSING or CLOSED
                break
            _pong_event.clear()
            await _send(websocket, {"type": "ping"})
            try:
                await asyncio.wait_for(_pong_event.wait(), timeout=HEARTBEAT_TIMEOUT)
                misses = 0  # pong recibido: resetear contador
            except asyncio.TimeoutError:
                misses += 1
                print(f"[ws/profesor] Heartbeat miss {misses}/{HEARTBEAT_MAX_MISSES}")
                if misses >= HEARTBEAT_MAX_MISSES:
                    print("[ws/profesor] Demasiados misses — cerrando")
                    await websocket.close()
                    break

    hb_task = asyncio.create_task(_heartbeat())

    # Fix 1: guardamos el task de respuesta en curso para poder cancelarlo si el WS cierra
    _active_response_task: asyncio.Task | None = None

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            mtype = msg.get("type")

            if mtype == "ping":
                await _send(websocket, {"type": "pong"})

            elif mtype == "pong":
                _pong_event.set()

            elif mtype == "message":
                content = msg.get("content", "").strip()
                # Fix 1: lanzar la lógica de respuesta como tarea independiente,
                # de modo que el loop siga libre para recibir pongs y take_control.
                _active_response_task = asyncio.create_task(
                    _handle_professor_message(websocket, content)
                )

            elif mtype == "vote":
                await _handle_professor_vote(websocket, msg.get("answer", ""))

    except WebSocketDisconnect:
        pass
    finally:
        hb_task.cancel()
        # No cancelamos _active_response_task: si la IA está generando, queremos
        # que termine y quede en el historial aunque el WS esté caído.
        print("[ws/profesor] Desconectado")
        session_manager.professor_ws = None
        await _notify_professor_status(False)


async def _handle_professor_message(ws: WebSocket, content: str):
    """Procesa un mensaje del profesor y genera la respuesta del interlocutor."""
    sess = session_manager.session

    if not content:
        return

    if sess is None or sess.state != SessionState.ACTIVE:
        await _send(ws, {"type": "error", "message": "No hay sesión activa."})
        return

    # Guardar mensaje del profesor (sin source: viene del profesor)
    await session_manager.add_message("profesor", content)

    # Enviar mensaje al panel también (el cómplice ve la conversación completa)
    if session_manager.panel_ws:
        await _send(session_manager.panel_ws, {
            "type": "message",
            "role": "profesor",
            "content": content,
        })

    # Decidir quién responde (con posible override de "tomar control")
    responder = sess.responder

    if responder == "ia":
        # Avisar al panel que es turno de la IA (para mostrar botón "tomar control")
        if session_manager.panel_ws:
            await _send(session_manager.panel_ws, {"type": "ai_turn"})
        await _respond_as_ai(ws, sess)
    else:
        await _respond_as_human(ws, sess)


async def _respond_as_ai(ws: WebSocket, sess):
    """
    Pide respuesta al modelo, aplica retrasos y la entrega al profesor.
    Permite que el cómplice tome el control durante la generación.

    Fix 1: corre como tarea independiente → el WS sigue libre para pong/take_control.
    Fix 4: el takeover funciona en cualquier momento, incluso tras timeout del modelo.
    Fix 7: notifica al panel cuando el modelo está ocupado.
    """
    # Pausa de lectura
    read_wait = reading_delay()
    await asyncio.sleep(read_wait)

    # Indicador de escritura
    await _send(ws, {"type": "typing", "state": True})
    if session_manager.panel_ws:
        await _send(session_manager.panel_ws, {"type": "typing", "state": True})

    # Fix 7: indicar al panel que el modelo está ocupado
    await _set_model_busy(True)

    # Lanzar inferencia y espera de take_control en paralelo
    history = session_manager.get_history_for_rkllm()

    t0 = time.monotonic()

    # Reiniciar el evento directo de takeover antes de lanzar la inferencia
    session_manager._takeover_event.clear()
    session_manager._takeover_text = None

    # Fix 4: evento de respaldo via parche de deliver_human_response
    take_control_event = asyncio.Event()
    _pending_takeover: list[str] = []

    original_deliver = session_manager.deliver_human_response

    async def patched_deliver(text: str):
        _pending_takeover.append(text)
        take_control_event.set()
        await original_deliver(text)

    session_manager.deliver_human_response = patched_deliver

    # Inferencia en background
    ai_task = asyncio.create_task(
        rkllm_client.chat(history, system_prompt=config.system_prompt)
    )

    # Espera directa del evento de takeover (mecanismo principal)
    async def wait_takeover_direct():
        await session_manager._takeover_event.wait()

    done, _ = await asyncio.wait(
        [
            ai_task,
            asyncio.create_task(take_control_event.wait()),
            asyncio.create_task(wait_takeover_direct()),
        ],
        return_when=asyncio.FIRST_COMPLETED,
    )

    # Restaurar función original
    session_manager.deliver_human_response = original_deliver

    # Fix 7: modelo ya no está ocupado (ya sea porque terminó o porque se canceló)
    await _set_model_busy(False)

    # Comprobar takeover por cualquiera de las dos vías
    takeover_text = None
    if session_manager._takeover_event.is_set() and session_manager._takeover_text:
        takeover_text = session_manager._takeover_text
        print(f"[respond_as_ai] Takeover directo detectado: {takeover_text!r}")
    elif take_control_event.is_set() and _pending_takeover:
        takeover_text = _pending_takeover[0]
        print(f"[respond_as_ai] Takeover via parche detectado: {takeover_text!r}")

    if takeover_text is not None:
        # Fix 4: El cómplice tomó el control.
        # Cancelar la tarea de IA si todavía está corriendo.
        if not ai_task.done():
            ai_task.cancel()
            try:
                await ai_task
            except (asyncio.CancelledError, Exception):
                pass

        elapsed = time.monotonic() - t0
        extra = human_response_delay(takeover_text, elapsed)
        if extra > 0:
            await asyncio.sleep(extra)
        # A1: source=takeover (el evento ya fue registrado en take_control)
        await _deliver_response(ws, takeover_text, sess, source="takeover", delay_applied_s=extra)
        return

    # Obtener resultado de la IA
    if ai_task.cancelled():
        result = (None, 0.0)
    else:
        try:
            result = ai_task.result()
        except Exception:
            result = (None, 0.0)
    if result is None:
        result = (None, 0.0)
    ai_text, model_elapsed = result

    if ai_text is None:
        # A1: registrar evento de error de modelo
        if sess:
            sess.add_event("error_modelo")
        # Apagar "escribiendo..." en ambos lados
        await _send_typing_off()
        if session_manager.panel_ws:
            await _send(session_manager.panel_ws, {
                "type": "error",
                "message": "El modelo no respondió. Puedes tomar el control.",
            })
        # Fix 6: habilitar el input del profesor para que no quede bloqueado
        await _send(session_manager.professor_ws, {"type": "input_enabled"})
        return

    # Retraso adicional para simular escritura (descontando tiempo del modelo)
    extra = ai_response_delay(ai_text, model_elapsed + read_wait)
    if extra > 0:
        await asyncio.sleep(extra)

    # A1: source=ia, latencia del modelo y retraso aplicado
    await _deliver_response(ws, ai_text, sess, source="ia",
                            model_latency_s=model_elapsed, delay_applied_s=extra)


async def _respond_as_human(ws: WebSocket, sess):
    """
    Espera la respuesta del cómplice, aplica retrasos mínimos y la entrega.

    A2: si el cómplice no responde en human_max_wait:
      - fallback="ia"   → el modelo responde automáticamente (source=ia_fallback).
      - fallback="none" → mantiene "escribiendo..." generic_typing_extra_seconds más
                          y luego entrega un mensaje genérico (nunca silencio ni error
                          visible para el profesor).
    En ambos casos se avisa al cómplice en el panel.
    """
    if session_manager.panel_ws:
        await _send(session_manager.panel_ws, {
            "type": "your_turn",
            "typing_target": typing_speed_chars_per_sec(),
        })

    t0 = time.monotonic()
    human_text = await session_manager.request_human_response()
    elapsed = time.monotonic() - t0

    if human_text is None:
        # --- A2: timeout del cómplice ---
        # Avisar siempre al panel
        fallback = config.timing.human_timeout_fallback

        if session_manager.panel_ws:
            fallback_label = "el modelo responderá automáticamente" if fallback == "ia" \
                             else "se enviará un mensaje genérico al profesor"
            await _send(session_manager.panel_ws, {
                "type": "error",
                "message": f"Tiempo de espera agotado ({fallback_label}).",
            })

        if fallback == "ia":
            # El modelo responde esta ronda; la fuente queda como ia_fallback
            await _set_model_busy(True)
            history = session_manager.get_history_for_rkllm()
            result = await rkllm_client.chat(history, system_prompt=config.system_prompt)
            await _set_model_busy(False)
            if result is None:
                result = (None, 0.0)
            ai_text, model_elapsed = result

            if ai_text is None:
                # Modelo también falló: caer al mensaje genérico
                if sess:
                    sess.add_event("error_modelo")
                ai_text = config.timing.generic_timeout_message
                model_elapsed = 0.0

            # Mostrar "escribiendo..." con retraso normal antes de entregar
            await _send(ws, {"type": "typing", "state": True})
            if session_manager.panel_ws:
                await _send(session_manager.panel_ws, {"type": "typing", "state": True})

            extra = ai_response_delay(ai_text, model_elapsed)
            if extra > 0:
                await asyncio.sleep(extra)

            # A1: ia_fallback; registrar evento
            if sess:
                sess.add_event("ia_fallback")
            await _deliver_response(ws, ai_text, sess,
                                    source="ia_fallback",
                                    model_latency_s=model_elapsed,
                                    delay_applied_s=extra)
        else:
            # fallback="none": mantener "escribiendo..." y entregar mensaje genérico
            await _send(ws, {"type": "typing", "state": True})
            if session_manager.panel_ws:
                await _send(session_manager.panel_ws, {"type": "typing", "state": True})

            extra_wait = config.timing.generic_typing_extra_seconds
            if extra_wait > 0:
                await asyncio.sleep(extra_wait)

            generic_msg = config.timing.generic_timeout_message
            # A1: ia_fallback (también es un substituto generado, no vino del humano)
            if sess:
                sess.add_event("ia_fallback")
            await _deliver_response(ws, generic_msg, sess,
                                    source="ia_fallback",
                                    delay_applied_s=extra_wait)
        return

    # --- Camino normal: el cómplice respondió a tiempo ---
    # Pausa de lectura antes de "escribiendo..."
    read_wait = reading_delay()
    await asyncio.sleep(read_wait)

    await _send(ws, {"type": "typing", "state": True})
    if session_manager.panel_ws:
        await _send(session_manager.panel_ws, {"type": "typing", "state": True})

    # Retener si el cómplice fue más rápido que el mínimo
    extra = human_response_delay(human_text, elapsed + read_wait)
    if extra > 0:
        await asyncio.sleep(extra)

    # A1: source=humano, retraso aplicado
    await _deliver_response(ws, human_text, sess, source="humano", delay_applied_s=extra)


async def _send_typing_off():
    """Apaga el indicador de escritura en ambos lados (seguro aunque el WS esté caído)."""
    if session_manager.professor_ws:
        await _send(session_manager.professor_ws, {"type": "typing", "state": False})
    if session_manager.panel_ws:
        await _send(session_manager.panel_ws, {"type": "typing", "state": False})


async def _deliver_response(
    ws: WebSocket,
    text: str,
    sess,
    source: str = "ia",
    model_latency_s: float | None = None,
    delay_applied_s: float | None = None,
):
    """
    Apaga el indicador de escritura y entrega el mensaje al profesor.

    Fix 3: el mensaje se guarda en el historial ANTES de intentar enviarlo.
    Si el WS del profesor está caído, se encola en _pending_professor_deliveries
    y se entrega la próxima vez que reconecte.
    Cada envío va en try/except para que un socket muerto no aborte el flujo.

    Para source "ia" e "ia_fallback" aplica limpiar_respuesta() si está habilitado.
    El texto original se guarda como text_raw en el historial; el filtrado como content.
    """
    await _send_typing_off()

    # Aplicar filtro solo a respuestas del modelo (no a humano ni takeover)
    text_raw: str | None = None
    if source in ("ia", "ia_fallback") and config.timing.filter_enabled:
        filtered = limpiar_respuesta(text, max_words=config.timing.filter_max_words)
        if filtered != text:
            text_raw = text   # guardar original solo cuando el filtro cambió algo
        text = filtered

    # Fix 3: PRIMERO guardar en historial, LUEGO intentar enviar
    await session_manager.add_message(
        "interlocutor", text,
        source=source,
        model_latency_s=model_latency_s,
        delay_applied_s=delay_applied_s,
        text_raw=text_raw,
    )

    # Enviar al profesor
    prof_ws = session_manager.professor_ws
    delivered = False
    if prof_ws is not None:
        try:
            await prof_ws.send_json({"type": "message", "role": "interlocutor", "content": text})
            delivered = True
        except Exception:
            delivered = False

    if not delivered:
        # Fix 3: WS cerrado — encolar para reenvío al reconectar y avisar al panel
        _pending_professor_deliveries.append({"content": text})
        if sess:
            sess.add_event("entrega_fallida")
        print(f"[deliver] Mensaje encolado para reenvío: {text[:60]!r}")
        if session_manager.panel_ws:
            await _send(session_manager.panel_ws, {
                "type": "error",
                "message": "Mensaje no pudo entregarse al profesor (WS caído). Se reenviará al reconectar.",
            })

    # Fix 6: señal para que el frontend del profesor habilite el input
    # (se envía siempre; si el WS está caído, lo procesará al reconectar junto con el historial)
    if session_manager.professor_ws:
        await _send(session_manager.professor_ws, {"type": "input_enabled"})

    # Enviar también al panel (para que el cómplice vea la conversación completa)
    if session_manager.panel_ws:
        await _send(session_manager.panel_ws, {
            "type": "message",
            "role": "interlocutor",
            "content": text,
        })

    # Comprobar si la sesión expiró después de este mensaje
    if sess and sess.is_expired():
        await _trigger_voting()


async def _handle_professor_vote(ws: WebSocket, answer: str):
    """Registra el voto y finaliza la sesión."""
    if answer not in ("ia", "humano"):
        return
    ok = await session_manager.set_vote(answer)
    if not ok:
        return

    sess = session_manager.session
    save_error = await session_manager.save_history()
    if save_error:
        if session_manager.panel_ws:
            await _send(session_manager.panel_ws, {
                "type": "error",
                "message": f"El historial NO se guardó: {save_error}",
            })

    result_msg = {
        "type": "result",
        "vote": sess.professor_vote,
        "responder": sess.responder,
        "correct": sess.result_correct,
        "history": [
            {"role": m.role, "content": m.content}
            for m in sess.history
        ],
    }
    await _send(ws, result_msg)
    if session_manager.panel_ws:
        await _send(session_manager.panel_ws, result_msg)


async def _trigger_voting():
    """Pasa la sesión al estado de votación."""
    sess = session_manager.session
    if sess is None or sess.state != SessionState.ACTIVE:
        return
    sess.state = SessionState.VOTING
    msg = {"type": "status", "state": "voting", "remaining": 0, "session_id": sess.session_id}
    for ws in [session_manager.professor_ws, session_manager.panel_ws]:
        if ws:
            await _send(ws, msg)


# ---------------------------------------------------------------------------
# WebSocket del panel
# ---------------------------------------------------------------------------

@app.websocket(PANEL_WS_PATH)
async def panel_ws_endpoint(websocket: WebSocket):
    # Verificar cookie antes de aceptar
    cookies = dict(websocket.cookies)
    token = cookies.get(COOKIE_NAME, "")
    if not _verify_panel_token(token):
        await websocket.close(code=4401)
        return

    await websocket.accept()
    session_manager.panel_ws = websocket
    session_manager.panel_ready = True
    print("[ws/panel] Cómplice conectado")

    # Enviar estado actual
    await _broadcast_status()
    await _broadcast_panel_status()

    # Enviar estado del modelo (ocupado o no)
    await _send(websocket, {"type": "model_busy", "busy": _model_busy})

    # Enviar estado del profesor en este momento
    await _send(websocket, {
        "type": "professor_status",
        "connected": session_manager.professor_ws is not None,
    })

    # Enviar rol si hay sesión
    sess = session_manager.session
    if sess:
        await _send(websocket, {"type": "role", "responder": sess.responder})
        # Reenviar historial al cómplice
        for m in sess.history:
            await _send(websocket, {
                "type": "message",
                "role": m.role,
                "content": m.content,
            })

    # Fix 2: heartbeat con tolerancia de múltiples fallos (igual que el del profesor)
    _pong_event = asyncio.Event()

    async def _heartbeat():
        """Envía ping periódico; cierra solo tras HEARTBEAT_MAX_MISSES fallos seguidos."""
        misses = 0
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            if websocket.client_state.value >= 2:
                break
            _pong_event.clear()
            await _send(websocket, {"type": "ping"})
            try:
                await asyncio.wait_for(_pong_event.wait(), timeout=HEARTBEAT_TIMEOUT)
                misses = 0
            except asyncio.TimeoutError:
                misses += 1
                print(f"[ws/panel] Heartbeat miss {misses}/{HEARTBEAT_MAX_MISSES}")
                if misses >= HEARTBEAT_MAX_MISSES:
                    print("[ws/panel] Demasiados misses — cerrando")
                    await websocket.close()
                    break

    hb_task = asyncio.create_task(_heartbeat())

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            mtype = msg.get("type")

            if mtype == "ping":
                await _send(websocket, {"type": "pong"})

            elif mtype == "pong":
                _pong_event.set()

            elif mtype == "response":
                # El cómplice envía su respuesta (ronda humana)
                await session_manager.deliver_human_response(msg.get("content", "").strip())

            elif mtype == "take_control":
                # El cómplice toma el control de la ronda actual (aunque sea IA)
                await session_manager.take_control(msg.get("content", "").strip())

            elif mtype == "set_mode":
                # Cambiar modo forzado (solo afecta la próxima sesión)
                mode_str = msg.get("mode", "aleatorio")
                try:
                    new_mode = ForcedMode(mode_str)
                except ValueError:
                    new_mode = ForcedMode.RANDOM
                if session_manager.session:
                    session_manager.session.forced_mode = new_mode
                await _send(websocket, {"type": "mode_set", "mode": new_mode.value})

            elif mtype == "start_session":
                mode_str = msg.get("mode", "aleatorio")
                try:
                    forced_mode = ForcedMode(mode_str)
                except ValueError:
                    forced_mode = ForcedMode.RANDOM

                sess = await session_manager.create_session(forced_mode)
                started = await session_manager.start_session()

                if not started:
                    await _send(websocket, {
                        "type": "error",
                        "message": "No se puede iniciar: panel o modelo no listos.",
                    })
                else:
                    # Notificar rol al panel
                    await _send(websocket, {"type": "role", "responder": sess.responder})
                    await _broadcast_status()
                    # Iniciar temporizador de sesión
                    asyncio.create_task(_session_timer(sess.session_id))

            elif mtype == "reset_session":
                save_error = await session_manager.reset_session()
                # Limpiar entregas pendientes al reiniciar sesión
                _pending_professor_deliveries.clear()
                await _broadcast_status()
                if save_error:
                    await _send(websocket, {
                        "type": "error",
                        "message": f"Sesión reiniciada, pero el historial NO se guardó: {save_error}",
                    })
                await _send(websocket, {"type": "session_reset"})

            elif mtype == "test_model":
                # Probar el modelo con una inferencia corta; actualiza también model_id
                ping_ok, model_id = await rkllm_client.ping()
                if ping_ok:
                    result = await rkllm_client.test_inference()
                    ok = result[0] is not None if isinstance(result, tuple) else result is not None
                else:
                    ok = False
                session_manager.model_ready = ok
                session_manager.model_id = model_id if ok else None
                await _send(websocket, {"type": "ping_result", "ok": ok})
                await _broadcast_panel_status()

    except WebSocketDisconnect:
        pass
    finally:
        hb_task.cancel()
        print("[ws/panel] Cómplice desconectado")
        session_manager.panel_ws = None
        session_manager.panel_ready = False
        await _broadcast_panel_status()


# ---------------------------------------------------------------------------
# Temporizador de sesión
# ---------------------------------------------------------------------------

async def _session_timer(session_id: str):
    """
    Envía actualizaciones de tiempo restante al profesor y al panel.
    Termina la sesión cuando el tiempo llega a cero.
    """
    while True:
        await asyncio.sleep(1)
        sess = session_manager.session
        if sess is None or sess.session_id != session_id:
            break  # La sesión fue reiniciada

        remaining = sess.remaining_seconds()
        tick_msg = {
            "type": "status",
            "state": sess.state.value,
            "remaining": remaining,
            "session_id": sess.session_id,
        }

        for ws in [session_manager.professor_ws, session_manager.panel_ws]:
            if ws:
                await _send(ws, tick_msg)

        if sess.state == SessionState.ACTIVE and remaining <= 0:
            await _trigger_voting()
            break
        elif sess.state in (SessionState.VOTING, SessionState.FINISHED):
            break
