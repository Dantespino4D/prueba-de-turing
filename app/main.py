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

# Intervalo de heartbeat y timeout (segundos)
HEARTBEAT_INTERVAL = 20
HEARTBEAT_TIMEOUT  = 10

from fastapi import Cookie, Depends, FastAPI, Form, HTTPException, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import config
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
    ok = await rkllm_client.ping()
    session_manager.model_ready = ok
    print(f"[startup] Modelo {'disponible' if ok else 'NO disponible'}")
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
    """Informa al panel el estado de conectividad (panel_ready, model_ready)."""
    msg = {
        "type": "panel_status",
        "panel_ready": session_manager.panel_ready,
        "model_ready": session_manager.model_ready,
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

    # Si hay sesión activa, reenviar historial
    sess = session_manager.session
    if sess and sess.state in (SessionState.ACTIVE, SessionState.VOTING, SessionState.FINISHED):
        for m in sess.history:
            # El profesor solo ve los mensajes del interlocutor (no los propios ya los tiene)
            if m.role == "interlocutor":
                await _send(websocket, {
                    "type": "message",
                    "role": "interlocutor",
                    "content": m.content,
                })

    # Evento para detectar pong del heartbeat
    _pong_event = asyncio.Event()

    async def _heartbeat():
        """Envía ping periódico; cierra la conexión si no llega pong."""
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            if websocket.client_state.value >= 2:  # CLOSING or CLOSED
                break
            _pong_event.clear()
            await _send(websocket, {"type": "ping"})
            try:
                await asyncio.wait_for(_pong_event.wait(), timeout=HEARTBEAT_TIMEOUT)
            except asyncio.TimeoutError:
                print("[ws/profesor] Heartbeat timeout — cerrando")
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

            elif mtype == "message":
                await _handle_professor_message(websocket, msg.get("content", "").strip())

            elif mtype == "vote":
                await _handle_professor_vote(websocket, msg.get("answer", ""))

    except WebSocketDisconnect:
        pass
    finally:
        hb_task.cancel()
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

    # Guardar mensaje del profesor
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
    """
    # Pausa de lectura
    read_wait = reading_delay()
    await asyncio.sleep(read_wait)

    # Indicador de escritura
    await _send(ws, {"type": "typing", "state": True})
    if session_manager.panel_ws:
        await _send(session_manager.panel_ws, {"type": "typing", "state": True})

    # Lanzar inferencia y espera de take_control en paralelo
    history = session_manager.get_history_for_rkllm()

    t0 = time.monotonic()

    # Reiniciar el evento directo de takeover antes de lanzar la inferencia
    session_manager._takeover_event.clear()
    session_manager._takeover_text = None

    # Evento de respaldo via parche de deliver_human_response
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

    # Comprobar takeover por cualquiera de las dos vías
    takeover_text = None
    if session_manager._takeover_event.is_set() and session_manager._takeover_text:
        takeover_text = session_manager._takeover_text
        print(f"[respond_as_ai] Takeover directo detectado: {takeover_text!r}")
    elif take_control_event.is_set() and _pending_takeover:
        takeover_text = _pending_takeover[0]
        print(f"[respond_as_ai] Takeover via parche detectado: {takeover_text!r}")

    if takeover_text is not None:
        # El cómplice tomó el control: cancelar la IA y usar su respuesta
        ai_task.cancel()
        elapsed = time.monotonic() - t0
        extra = human_response_delay(takeover_text, elapsed)
        if extra > 0:
            await asyncio.sleep(extra)
        await _deliver_response(ws, takeover_text, sess)
        return

    # Obtener resultado de la IA
    result = ai_task.result() if not ai_task.cancelled() else (None, 0.0)
    if result is None:
        result = (None, 0.0)
    ai_text, model_elapsed = result

    if ai_text is None:
        # Error en el modelo
        await _send(session_manager.professor_ws, {"type": "typing", "state": False})
        if session_manager.panel_ws:
            await _send(session_manager.panel_ws, {
                "type": "error",
                "message": "El modelo no respondió. Puedes tomar el control.",
            })
        return

    # Retraso adicional para simular escritura (descontando tiempo del modelo)
    extra = ai_response_delay(ai_text, model_elapsed + read_wait)
    if extra > 0:
        await asyncio.sleep(extra)

    await _deliver_response(ws, ai_text, sess)


async def _respond_as_human(ws: WebSocket, sess):
    """
    Espera la respuesta del cómplice, aplica retrasos mínimos y la entrega.
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
        # Timeout: avisar al panel
        if session_manager.panel_ws:
            await _send(session_manager.panel_ws, {
                "type": "error",
                "message": "Tiempo de espera agotado.",
            })
        return

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

    await _deliver_response(ws, human_text, sess)


async def _deliver_response(ws: WebSocket, text: str, sess):
    """Apaga el indicador de escritura y entrega el mensaje al profesor."""
    await _send(session_manager.professor_ws, {"type": "typing", "state": False})
    if session_manager.panel_ws:
        await _send(session_manager.panel_ws, {"type": "typing", "state": False})

    await session_manager.add_message("interlocutor", text)

    # Enviar al profesor
    await _send(session_manager.professor_ws, {"type": "message", "role": "interlocutor", "content": text})

    # Enviar también al panel (para que el cómplice vea la conversación completa)
    if session_manager.panel_ws:
        await _send(session_manager.panel_ws, {
            "type": "message",
            "role": "interlocutor",
            "content": text,
        })

    # Comprobar si la sesión expiró después de este mensaje
    if sess.is_expired():
        await _trigger_voting()


async def _handle_professor_vote(ws: WebSocket, answer: str):
    """Registra el voto y finaliza la sesión."""
    if answer not in ("ia", "humano"):
        return
    ok = await session_manager.set_vote(answer)
    if not ok:
        return

    sess = session_manager.session
    await session_manager.save_history()

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

    # Evento para detectar pong del heartbeat
    _pong_event = asyncio.Event()

    async def _heartbeat():
        """Envía ping periódico; cierra la conexión si no llega pong."""
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            if websocket.client_state.value >= 2:
                break
            _pong_event.clear()
            await _send(websocket, {"type": "ping"})
            try:
                await asyncio.wait_for(_pong_event.wait(), timeout=HEARTBEAT_TIMEOUT)
            except asyncio.TimeoutError:
                print("[ws/panel] Heartbeat timeout — cerrando")
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
                await session_manager.reset_session()
                await _broadcast_status()
                await _send(websocket, {"type": "session_reset"})

            elif mtype == "test_model":
                # Probar el modelo con una inferencia corta
                result = await rkllm_client.test_inference()
                ok = result[0] is not None if isinstance(result, tuple) else result is not None
                session_manager.model_ready = ok
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
