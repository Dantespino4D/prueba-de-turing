/**
 * panel.js — Frontend del cómplice (panel de control).
 *
 * Recupera sesión activa al reconectar (historial, rol, tiempo).
 * Muestra indicador de turno, modo forzado, botón "tomar control".
 * Funciona bien en móvil.
 *
 * A3: Cuando llega un mensaje del profesor durante una ronda activa:
 *   - Sonido corto (beep generado con Web Audio API, sin archivos externos).
 *   - Título de la pestaña parpadea con "🔔 Mensaje del profesor — Panel".
 *   - Ambos se silencian con el botón 🔔/🔕 del encabezado del chat.
 *   - El parpadeo se detiene al hacer foco en la pestaña.
 */

"use strict";

// --- Config de reconexión ---
const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS  = 16000;

// La ruta del WebSocket se construye a partir de la URL actual del panel.
// Si el panel está en /panel-secreto → WS en /panel-secreto/ws
const PANEL_WS_PATH = window.location.pathname.replace(/\/?$/, "") + "/ws";

// --- Estado ---
let ws = null;
let reconnectDelay = RECONNECT_BASE_MS;
let reconnectTimer = null;
let timerInterval  = null;
let remainingSeconds = 0;
let currentRole    = null;  // "ia" | "humano" | null
let sessionActive  = false;
let myTurn         = false; // Es el turno del cómplice (humano round)

// --- DOM ---
const dotPanel      = document.getElementById("dot-panel");
const dotModel      = document.getElementById("dot-model");
const dotProfessor  = document.getElementById("dot-professor");
const lblPanel      = document.getElementById("lbl-panel");
const lblModel      = document.getElementById("lbl-model");
const lblProfessor  = document.getElementById("lbl-professor");
const sessionInfo   = document.getElementById("session-info");
const panelTimer    = document.getElementById("panel-timer");
const panelClock    = document.getElementById("panel-clock");
const roleBadge     = document.getElementById("role-badge");
const roundIndicator = document.getElementById("round-indicator");
const messagesEl    = document.getElementById("panel-messages");
const typingEl      = document.getElementById("panel-typing");
const responseArea  = document.getElementById("response-area");
const aiWaitingArea = document.getElementById("ai-waiting-area");
const panelInput    = document.getElementById("panel-input");
const panelSendBtn  = document.getElementById("panel-send-btn");
const typingTargetLbl = document.getElementById("typing-target-lbl");
const btnStart      = document.getElementById("btn-start");
const btnReset      = document.getElementById("btn-reset");
const btnTestModel  = document.getElementById("btn-test-model");
const testModelResult = document.getElementById("test-model-result");
const btnTakeControl = document.getElementById("btn-take-control");
const btnOverride   = document.getElementById("btn-override");
const takeoverModal = document.getElementById("takeover-modal");
const takeoverInput = document.getElementById("takeover-input");
const takeoverConfirm = document.getElementById("takeover-confirm");
const takeoverCancel  = document.getElementById("takeover-cancel");
const modeSelect    = document.getElementById("mode-select");
const btnMuteNotify = document.getElementById("btn-mute-notify");

// ─── A3: Notificaciones de mensaje del profesor ───────────────────────────────

let _notifyMuted = false;
let _titleBlinkInterval = null;
const _originalTitle = document.title;

/** Reproduce un beep corto mediante Web Audio API. */
function _playBeep() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = "sine";
    osc.frequency.value = 880;        // La5 — tono suave y audible
    gain.gain.setValueAtTime(0.18, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.18);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + 0.18);
  } catch (_) {
    // Web Audio no disponible; ignorar
  }
}

/** Inicia el parpadeo del título de la pestaña. */
function _startTitleBlink() {
  if (_titleBlinkInterval) return;  // ya parpadeando
  let visible = true;
  _titleBlinkInterval = setInterval(() => {
    document.title = visible ? "🔔 Mensaje — Panel" : _originalTitle;
    visible = !visible;
  }, 700);
}

/** Detiene el parpadeo y restaura el título original. */
function _stopTitleBlink() {
  if (_titleBlinkInterval) {
    clearInterval(_titleBlinkInterval);
    _titleBlinkInterval = null;
  }
  document.title = _originalTitle;
}

/** Lanza la notificación (sonido + parpadeo) si no está silenciada. */
function _notifyProfessorMessage() {
  if (_notifyMuted) return;
  _playBeep();
  _startTitleBlink();
}

// Detener parpadeo al volver a la pestaña
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) _stopTitleBlink();
});
window.addEventListener("focus", _stopTitleBlink);

// Botón de silenciar / activar notificaciones
btnMuteNotify.addEventListener("click", () => {
  _notifyMuted = !_notifyMuted;
  btnMuteNotify.textContent = _notifyMuted ? "🔕" : "🔔";
  btnMuteNotify.title = _notifyMuted ? "Activar notificaciones" : "Silenciar notificaciones";
  if (_notifyMuted) _stopTitleBlink();
});

// ─────────────────────────────────────────────────────────────────────────────

// --- Temporizador ---
function startTimer(seconds) {
  remainingSeconds = seconds;
  clearInterval(timerInterval);
  timerInterval = setInterval(() => {
    remainingSeconds = Math.max(0, remainingSeconds - 1);
    renderTimer();
  }, 1000);
  renderTimer();
}

function renderTimer() {
  const m = Math.floor(remainingSeconds / 60);
  const s = Math.floor(remainingSeconds % 60);
  const txt = `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  panelTimer.textContent = txt;
  panelClock.textContent = txt;
  panelClock.classList.toggle("urgent", remainingSeconds <= 30 && remainingSeconds > 0);
}

function stopTimer() {
  clearInterval(timerInterval);
}

// --- Indicadores de estado ---
function setDot(dot, lbl, ok, text) {
  dot.className = "dot " + (ok ? "green" : "red");
  lbl.textContent = text;
}

function updatePanelStatus(data) {
  setDot(dotPanel, lblPanel, true, "Panel: conectado");
  let modelLabel = `Modelo: ${data.model_ready ? "disponible" : "no disponible"}`;
  if (data.model_ready && data.model_id) {
    modelLabel += ` (${data.model_id})`;
  }
  setDot(dotModel, lblModel, data.model_ready, modelLabel);
}

function updateProfessorStatus(connected) {
  setDot(dotProfessor, lblProfessor, connected, `Profesor: ${connected ? "conectado" : "desconectado"}`);
}

// --- Rol ---
function setRole(role) {
  currentRole = role;
  if (role === "ia") {
    roleBadge.className = "badge ia";
    roleBadge.textContent = "🤖 Responde la IA";
  } else if (role === "humano") {
    roleBadge.className = "badge humano";
    roleBadge.textContent = "👤 Respondes tú";
  } else {
    roleBadge.className = "badge neutral";
    roleBadge.textContent = "—";
  }
}

// --- Visibilidad del botón "Tomar el control" ---
function updateControlButtonVisibility(role) {
  if (role === "ia") {
    // La IA está respondiendo: el cómplice puede interceptarla
    btnTakeControl.style.display = "inline-block";
    roundIndicator.textContent = "🤖 La IA está respondiendo… (puedes tomar el control)";
  } else if (role === "humano") {
    // El cómplice ya tiene el control: ocultar el botón
    btnTakeControl.style.display = "none";
    roundIndicator.textContent = "🟢 Te toca a ti responder";
  } else {
    // Estado neutro: ocultar el botón y resetear indicador
    btnTakeControl.style.display = "none";
    roundIndicator.textContent = "Esperando mensaje del profesor…";
  }
}

// --- Mensajes del chat ---
function appendMessage(role, content, tag) {
  const bubble = document.createElement("div");
  bubble.classList.add("bubble", role === "profesor" ? "me" : "them");
  if (tag) {
    const tagEl = document.createElement("small");
    tagEl.style.cssText = "display:block;font-size:0.7rem;opacity:0.6;margin-bottom:2px;";
    tagEl.textContent = tag;
    bubble.appendChild(tagEl);
  }
  const textNode = document.createTextNode(content);
  bubble.appendChild(textNode);
  messagesEl.appendChild(bubble);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

// --- Área de input del cómplice ---
function showResponseArea(show) {
  myTurn = show;
  responseArea.style.display  = show ? "flex" : "none";
  aiWaitingArea.style.display = "none";
  if (show) {
    panelInput.focus();
    panelInput.value = "";
    autoResize(panelInput);
  }
}

function showAiWaiting(show) {
  aiWaitingArea.style.display = show ? "flex" : "none";
  responseArea.style.display  = "none";
}

function hideInputAreas() {
  responseArea.style.display  = "none";
  aiWaitingArea.style.display = "none";
  myTurn = false;
}

// --- Enviar respuesta ---
function sendResponse() {
  const text = panelInput.value.trim();
  if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({ type: "response", content: text }));
  hideInputAreas();
  roundIndicator.textContent = "Respuesta enviada.";
}

// --- Tomar control ---
function openTakeoverModal() {
  takeoverInput.value = "";
  takeoverModal.style.display = "flex";
  takeoverInput.focus();
}

function closeTakeoverModal() {
  takeoverModal.style.display = "none";
}

function sendTakeover() {
  const text = takeoverInput.value.trim();
  if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({ type: "take_control", content: text }));
  closeTakeoverModal();
  hideInputAreas();
  roundIndicator.textContent = "Control tomado — respuesta enviada.";
}

// --- Auto-resize textarea ---
function autoResize(el) {
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, 120) + "px";
}

// --- Manejar mensajes del servidor ---
function handleMessage(msg) {
  switch (msg.type) {

    case "ping": {
      // El servidor envía ping como heartbeat; responder con pong
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "pong" }));
      }
      break;
    }

    case "professor_status": {
      updateProfessorStatus(msg.connected);
      break;
    }

    case "status": {
      const state = msg.state;
      sessionActive = state === "active";

      if (state === "active") {
        sessionInfo.textContent = `Sesión: ${msg.session_id || ""}`;
        startTimer(msg.remaining);
        btnStart.disabled = true;
      } else if (state === "waiting") {
        sessionInfo.textContent = "Sin sesión activa";
        stopTimer();
        panelTimer.textContent = "--:--";
        panelClock.textContent = "--:--";
        btnStart.disabled = false;
        setRole(null);
        hideInputAreas();
      } else if (state === "voting") {
        stopTimer();
        panelTimer.textContent = "00:00";
        sessionInfo.textContent = "Esperando voto del profesor…";
        hideInputAreas();
      } else if (state === "finished") {
        sessionInfo.textContent = "Sesión terminada";
        hideInputAreas();
        btnStart.disabled = false;
      }

      // Actualizar tiempo si está en curso
      if (state === "active") {
        remainingSeconds = msg.remaining;
      }

      break;
    }

    case "panel_status": {
      updatePanelStatus(msg);
      break;
    }

    case "role": {
      setRole(msg.responder);
      break;
    }

    case "ai_turn": {
      // La IA va a responder: mostrar botón "tomar control"
      updateControlButtonVisibility("ia");
      showAiWaiting(true);
      break;
    }

    case "message": {
      const isAiResponse = msg.role === "interlocutor" && currentRole === "ia";
      const tag = isAiResponse ? "(IA)" : null;
      appendMessage(msg.role, msg.content, tag);

      // A3: Notificar al cómplice cuando llega un mensaje del profesor
      if (msg.role === "profesor") {
        _notifyProfessorMessage();
        updateControlButtonVisibility("neutral");
      }

      // Si era turno humano y llega la respuesta del interlocutor → cerrar área
      if (msg.role === "interlocutor") {
        hideInputAreas();
        roundIndicator.textContent = "";
      }
      break;
    }

    case "typing": {
      typingEl.classList.toggle("hidden", !msg.state);
      if (msg.state) messagesEl.scrollTop = messagesEl.scrollHeight;
      break;
    }

    case "your_turn": {
      // El servidor indica que es el turno del cómplice (ronda humana)
      updateControlButtonVisibility("humano");
      if (msg.typing_target) {
        typingTargetLbl.textContent = `Ritmo objetivo: ~${msg.typing_target.toFixed(1)} car/s`;
      }
      showResponseArea(true);
      break;
    }

    case "result": {
      stopTimer();
      const correct = msg.correct;
      const respLabel = msg.responder === "ia" ? "IA" : "Humano";
      const voteLabel = msg.vote      === "ia" ? "IA" : "Humano";
      sessionInfo.textContent =
        `Resultado: votó "${voteLabel}" — era ${respLabel} — ${correct ? "✅ Acertó" : "❌ Falló"}`;
      hideInputAreas();
      btnStart.disabled = false;
      break;
    }

    case "ping_result": {
      testModelResult.textContent = msg.ok
        ? "✅ Modelo respondió OK"
        : "❌ El modelo no responde";
      btnTestModel.disabled = false;
      break;
    }

    case "session_reset": {
      messagesEl.innerHTML = "";
      setRole(null);
      hideInputAreas();
      roundIndicator.textContent = "";
      sessionInfo.textContent = "Sesión reiniciada";
      break;
    }

    case "model_busy": {
      // Fix 7: indicar al cómplice si el modelo está ocupado con una inferencia
      const busyEl = document.getElementById("model-busy-indicator");
      if (busyEl) {
        busyEl.style.display = msg.busy ? "block" : "none";
      }
      break;
    }

    case "mode_set": {
      modeSelect.value = msg.mode;
      break;
    }

    case "error": {
      // Mostrar en el panel sin interrumpir al profesor
      const errEl = document.createElement("div");
      errEl.style.cssText = "color:var(--error);font-size:0.8rem;padding:0.3rem 1rem;";
      errEl.textContent = "⚠ " + msg.message;
      messagesEl.appendChild(errEl);
      messagesEl.scrollTop = messagesEl.scrollHeight;

      // Si el modelo falló, ofrecer tomar control
      if (msg.message.includes("modelo")) {
        showResponseArea(true);
        roundIndicator.textContent = "⚠ Modelo falló — responde tú";
      }
      break;
    }

    case "pong":
      break;
  }
}

// --- Botones de la barra lateral ---
btnStart.addEventListener("click", () => {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  const mode = modeSelect.value;
  ws.send(JSON.stringify({ type: "start_session", mode }));
  btnStart.disabled = true;
});

btnReset.addEventListener("click", () => {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  if (!confirm("¿Reiniciar la sesión? Se perderá el historial no guardado.")) return;
  ws.send(JSON.stringify({ type: "reset_session" }));
});

btnTestModel.addEventListener("click", () => {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  btnTestModel.disabled = true;
  testModelResult.textContent = "Probando…";
  ws.send(JSON.stringify({ type: "test_model" }));
});

// Respuesta del cómplice
panelSendBtn.addEventListener("click", sendResponse);
panelInput.addEventListener("keydown", evt => {
  if (evt.key === "Enter" && !evt.shiftKey) {
    evt.preventDefault();
    sendResponse();
  }
});
panelInput.addEventListener("input", () => autoResize(panelInput));

// Tomar control
btnTakeControl.addEventListener("click", openTakeoverModal);
btnOverride.addEventListener("click", openTakeoverModal);
takeoverConfirm.addEventListener("click", sendTakeover);
takeoverCancel.addEventListener("click", closeTakeoverModal);
takeoverInput.addEventListener("keydown", evt => {
  if (evt.key === "Enter" && evt.ctrlKey) sendTakeover();
  if (evt.key === "Escape") closeTakeoverModal();
});

// --- WebSocket ---
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url = `${proto}://${location.host}${PANEL_WS_PATH}`;
  ws = new WebSocket(url);

  ws.addEventListener("open", () => {
    reconnectDelay = RECONNECT_BASE_MS;
    setDot(dotPanel, lblPanel, true, "Panel: conectado");
    startPing();
  });

  ws.addEventListener("message", evt => {
    try {
      handleMessage(JSON.parse(evt.data));
    } catch (e) {
      console.error("[panel] JSON inválido:", evt.data);
    }
  });

  ws.addEventListener("close", evt => {
    stopPing();
    setDot(dotPanel, lblPanel, false, "Panel: desconectado");
    if (evt.code === 4401) {
      // No autenticado: redirigir al login
      window.location.href = window.location.pathname + "/auth";
      return;
    }
    scheduleReconnect();
  });

  ws.addEventListener("error", () => ws.close());
}

// El servidor maneja el heartbeat desde su lado (ping → pong).
// Solo necesitamos responder a los pings del servidor (ya manejado en handleMessage).
function startPing() {}
function stopPing() {}

function scheduleReconnect() {
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(() => {
    connect();
    reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_MS);
  }, reconnectDelay);
}

// --- Arrancar ---
// Botón "tomar control" oculto por defecto; se muestra solo en rondas de IA
btnTakeControl.style.display = "none";

connect();
