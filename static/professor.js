/**
 * professor.js — Frontend del profesor para la Prueba de Turing.
 *
 * Pantallas: waiting → chat → vote → result
 * Reconexión automática con espera creciente.
 * Recupera historial y tiempo si recarga a media sesión.
 */

"use strict";

// --- Configuración de reconexión ---
const RECONNECT_BASE_MS  = 1000;
const RECONNECT_MAX_MS   = 16000;
const RECONNECT_FACTOR   = 2;

// --- Estado ---
let ws = null;
let reconnectDelay = RECONNECT_BASE_MS;
let reconnectTimer = null;
let currentScreen = "waiting";
let timerInterval = null;
let remainingSeconds = 0;

// --- Elementos DOM ---
const screens = {
  waiting: document.getElementById("screen-waiting"),
  chat:    document.getElementById("screen-chat"),
  vote:    document.getElementById("screen-vote"),
  result:  document.getElementById("screen-result"),
};
const waitingMsg   = document.getElementById("waiting-msg");
const messagesEl   = document.getElementById("messages");
const typingEl     = document.getElementById("typing-indicator");
const inputEl      = document.getElementById("input");
const sendBtn      = document.getElementById("send-btn");
const timerEl      = document.getElementById("timer");
const resultIcon   = document.getElementById("result-icon");
const resultTitle  = document.getElementById("result-title");
const resultDetail = document.getElementById("result-detail");
const transcriptEl = document.getElementById("transcript");

// --- Pantallas ---
function showScreen(name) {
  if (currentScreen === name) return;
  currentScreen = name;
  Object.entries(screens).forEach(([k, el]) => {
    el.classList.toggle("active", k === name);
  });
}

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
  timerEl.textContent = `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  timerEl.classList.toggle("urgent", remainingSeconds <= 30 && remainingSeconds > 0);
}

function stopTimer() {
  clearInterval(timerInterval);
}

// --- Mensajes del chat ---
function appendMessage(role, content) {
  const bubble = document.createElement("div");
  bubble.classList.add("bubble", role === "profesor" ? "me" : "them");
  bubble.textContent = content;
  messagesEl.appendChild(bubble);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

// --- Enviar mensaje ---
function sendMessage() {
  const text = inputEl.value.trim();
  if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({ type: "message", content: text }));
  appendMessage("profesor", text);
  inputEl.value = "";
  autoResize();
  setInputEnabled(false);
}

function setInputEnabled(enabled) {
  inputEl.disabled = !enabled;
  sendBtn.disabled = !enabled;
}

// --- Auto-resize textarea ---
function autoResize() {
  inputEl.style.height = "auto";
  inputEl.style.height = Math.min(inputEl.scrollHeight, 120) + "px";
}

// --- Votar ---
document.querySelectorAll(".vote-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    const answer = btn.dataset.answer;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "vote", answer }));
    }
    // Deshabilitar botones para evitar doble voto
    document.querySelectorAll(".vote-btn").forEach(b => b.disabled = true);
  });
});

// --- Pantalla de resultado ---
function showResult(data) {
  stopTimer();
  showScreen("result");
  const correct = data.correct;
  resultIcon.textContent = correct ? "🎉" : "😔";
  resultTitle.textContent = correct ? "¡Acertaste!" : "No acertaste";

  const responderLabel = data.responder === "ia" ? "una Inteligencia Artificial" : "un Humano";
  const voteLabel      = data.vote      === "ia" ? "IA"                           : "Humano";
  resultDetail.textContent =
    `Votaste: ${voteLabel}. En realidad era ${responderLabel}.`;

  // Transcripción
  transcriptEl.innerHTML = "";
  (data.history || []).forEach(m => {
    const el = document.createElement("div");
    el.classList.add("bubble", m.role === "profesor" ? "me" : "them");
    el.textContent = m.content;
    transcriptEl.appendChild(el);
  });
}

// --- Manejar mensajes del servidor ---
function handleMessage(msg) {
  switch (msg.type) {

    case "status": {
      const state = msg.state;
      if (state === "active") {
        showScreen("chat");
        setInputEnabled(true);
        startTimer(msg.remaining);
      } else if (state === "voting") {
        stopTimer();
        timerEl.textContent = "00:00";
        showScreen("vote");
      } else if (state === "finished") {
        // El resultado llega por mensaje "result"
      } else {
        // waiting
        showScreen("waiting");
        waitingMsg.textContent = "Esperando que el sistema esté listo…";
      }
      // Actualizar tiempo si ya estamos en chat
      if (state === "active" && currentScreen === "chat") {
        remainingSeconds = msg.remaining;
      }
      break;
    }

    case "message": {
      if (currentScreen === "chat") {
        appendMessage(msg.role, msg.content);
        // Re-habilitar input cuando llega respuesta del interlocutor
        if (msg.role === "interlocutor") {
          setInputEnabled(true);
        }
      }
      break;
    }

    case "typing": {
      typingEl.classList.toggle("hidden", !msg.state);
      if (msg.state) {
        messagesEl.scrollTop = messagesEl.scrollHeight;
      }
      break;
    }

    case "result": {
      showResult(msg);
      break;
    }

    case "error": {
      console.warn("[turing] Error del servidor:", msg.message);
      break;
    }

    case "pong":
      break;
  }
}

// --- WebSocket ---
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url   = `${proto}://${location.host}/ws`;
  ws = new WebSocket(url);

  ws.addEventListener("open", () => {
    reconnectDelay = RECONNECT_BASE_MS;
    waitingMsg.textContent = "Conectado. Esperando inicio de sesión…";
    // Ping periódico para mantener la conexión viva
    startPing();
  });

  ws.addEventListener("message", evt => {
    try {
      handleMessage(JSON.parse(evt.data));
    } catch (e) {
      console.error("[turing] JSON inválido:", evt.data);
    }
  });

  ws.addEventListener("close", () => {
    stopPing();
    scheduleReconnect();
  });

  ws.addEventListener("error", () => {
    ws.close();
  });
}

let pingInterval = null;
function startPing() {
  clearInterval(pingInterval);
  pingInterval = setInterval(() => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "ping" }));
    }
  }, 25000);
}
function stopPing() {
  clearInterval(pingInterval);
}

function scheduleReconnect() {
  clearTimeout(reconnectTimer);
  waitingMsg.textContent = `Reconectando en ${Math.round(reconnectDelay / 1000)} s…`;
  reconnectTimer = setTimeout(() => {
    connect();
    reconnectDelay = Math.min(reconnectDelay * RECONNECT_FACTOR, RECONNECT_MAX_MS);
  }, reconnectDelay);
}

// --- Eventos del input ---
inputEl.addEventListener("input", autoResize);
inputEl.addEventListener("keydown", evt => {
  if (evt.key === "Enter" && !evt.shiftKey) {
    evt.preventDefault();
    sendMessage();
  }
});
sendBtn.addEventListener("click", sendMessage);

// --- Arrancar ---
setInputEnabled(false);
connect();
