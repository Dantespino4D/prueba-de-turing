# Prompt para Bob IBM: Prueba de Turing

## Objetivo
Construye una aplicación web para una prueba de Turing controlada, para una demostración en clase. El profesor chatea con un interlocutor desconocido durante unos 5 minutos y al final debe votar si habló con un humano o con una IA. En realidad, el "humano" es un miembro del equipo (cómplice) que responde desde un panel oculto y simula ser una IA experta en un tema. El servidor decide al azar (50/50) en cada sesión quién responde, y desde el lado del profesor ambos casos deben verse idénticos.

## Stack
- **Backend:** Python con FastAPI, uvicorn y WebSockets nativos. Código asíncrono.
- **Frontend:** HTML, CSS y JavaScript puro (sin frameworks), servido por FastAPI como archivos estáticos.
- **Modelo de IA:** servidor RKLLM (Orange Pi 5 Plus, NPU RK3588) basado en `rkllm_server_demo` (Flask, `flask_server.py`) del repo de Rockchip. Corre **directo en el host** (no en contenedor) como servicio systemd. El endpoint, puerto y formato de petición/respuesta deben ser **configurables** (revisa `flask_server.py` y `chat_api_flask.py` para el formato real). Maneja una petición a la vez.
- **App FastAPI:** corre en un contenedor **Podman rootless** (mismo estilo que mis otros servicios) y se publica con `-p 8100:8100`. Debe llegar al servidor RKLLM del host (por ejemplo `host.containers.internal:<puerto>` o `--network=host`). Incluye `Containerfile` (imagen base `python:3.12-slim`, no 3.14) y un ejemplo de `podman run` o unidad systemd/Quadlet.
- **Proxy y dominio:** Nginx Proxy Manager con un Proxy Host para `turing.dantespino4d.me`, HTTPS con Let's Encrypt, Websockets Support y Force SSL activados. NO generes un nginx.conf propio: entrega solo los valores para configurar el Proxy Host y el bloque de "Advanced" (timeouts largos, `proxy_buffering off`).
- **Persistencia:** sin base de datos. Historial en memoria durante la sesión y archivo JSON al terminar, en `/mnt/disco_8TB/historial-turing/` (montado como volumen en el contenedor).

## Entorno real (no inventar otros valores)
- Máquina: Orange Pi 5 Plus, 16 GB RAM, Arch Linux (aarch64), hostname `dante-server`, usuario `dante`.
- Python del host: 3.14.7 (muy reciente; por eso la app va en contenedor con Python 3.12; el servidor RKLLM del host debe usar un `venv`).
- Proyecto: `/home/dante/prueba-turing`.
- RKLLM: repo en `/home/dante/rknn-llm`, servidor en `/home/dante/rknn-llm/examples/rkllm_server_demo/` (carpeta `rkllm_server/lib` vacía: hay que correr `build_rkllm_server_flask.sh` o copiar `librkllmrt.so` desde `rkllm-runtime/Linux/librkllm_api/aarch64/`). Driver NPU: v0.9.7.
- Modelos `.rkllm` en `/home/dante/jarvis/`: `llama3.1-supernova-instruct-merge-ab.rkllm`, `qwen2.5-3b-distill.rkllm`, `qwen2.5-3b-josiefied.rkllm`, `qwen2.5-1.5B-agentic-trace.rkllm`, `qwen2.5-7b-coder-rk3588-UC.rkllm`. El modelo debe ser **seleccionable por config**; candidatos principales: el Llama 3.1 supernova (~8B) y un Qwen2.5 3B. Algunos pueden emitir bloques de razonamiento (`<think>...</think>`): la app debe **filtrarlos** antes de entregar nada al profesor.
- Puertos ya ocupados en el host: 22, 53, 5000, 5355, 6167, 8000, 8090, 1984, 8554, 8555, 8971, 38423, 64390. Usar: **FastAPI 8100**, **RKLLM 8080** (default del demo). Ambos configurables.
- Dominio: `turing.dantespino4d.me`. Equipo: un solo cómplice a la vez, una sola sesión activa. Idioma de toda la interfaz: español (México).
- Contraseña del panel y ruta oculta del panel: en el archivo de config (valores de ejemplo, yo los cambio).
- Opcional: llamada a un webhook (n8n) al terminar la sesión, desactivada por defecto. No es parte del camino crítico.

## Rutas
- `/` : chat del profesor.
- `/ws` : WebSocket del profesor.
- `/panel` : panel del cómplice (protegido con contraseña de la app y URL no enlazada desde ningún lado).
- `/ws/panel` : WebSocket del cómplice.

## Flujo
1. El cómplice abre `/panel` e inicia sesión. El panel muestra indicadores **"cómplice listo"** y **"modelo listo"** (el servidor comprueba el modelo con un ping). La sesión no puede iniciar si alguno falla.
2. El profesor entra a `/`. Ve un mensaje breve de instrucciones y el chat. **El profesor escribe primero.**
3. Al iniciar la sesión, el servidor elige quién responde: `humano` o `ia`, aleatorio 50/50 (usa `secrets` o `random` sin semilla fija). Solo el panel conoce el resultado. Existe un **modo forzado** (humano / IA / aleatorio) seleccionable en el panel, para ensayos.
4. Por cada mensaje del profesor:
   - Si toca **IA**: el backend envía system prompt + historial al servidor RKLLM, espera la respuesta **completa** (sin streaming al profesor), aplica el retraso humano simulado y la entrega.
   - Si toca **humano**: el backend reenvía el mensaje al panel del cómplice. Cuando el cómplice responde, el backend aplica el mismo esquema de tiempos y entrega la respuesta al profesor.
5. En ambos casos el profesor recibe el mensaje por el mismo canal y con el mismo formato. El **servidor es el único** que entrega mensajes al profesor.
6. Al terminar el tiempo (por defecto 5 minutos, configurable) o si el profesor vota antes, aparece la pregunta: **"¿Con quién estuviste hablando? Humano / IA"**.
7. Pantalla final: resultado (acertó o no), revelación de la verdad y transcripción completa. El historial se guarda en archivo JSON con fecha, modo, voto y resultado.

## Manejo de tiempos (importante)
- Retraso de "lectura" de 1 a 3 s aleatorios, luego indicador "escribiendo…".
- Retraso total de "escritura" proporcional al largo de la respuesta (aprox. 4-6 caracteres por segundo, con variación aleatoria). En IA, **descuenta** el tiempo que ya tardó el modelo y espera solo la diferencia.
- En humano, aplica un **tiempo mínimo** antes de entregar, para que el cómplice no sea más rápido que la IA. En el panel muestra un contador o ritmo objetivo.
- Parámetros (velocidad, mínimos, variación) en un archivo de configuración.
- Cuenta regresiva visible para el profesor.

## Fallos y robustez
- Timeout al modelo configurable (30-40 s). Si falla, avisar en el panel y permitir reintentar.
- Botón **"tomar el control"** en el panel: el cómplice responde esa ronda en lugar de la IA sin que el profesor note nada.
- Reconexión automática del WebSocket en cliente (reintentos con espera creciente). La sesión se conserva en el servidor: si el profesor recarga, recupera el historial y el tiempo restante.
- Alternativa si el WebSocket falla (red de la escuela): fallback a long polling o peticiones HTTP, o al menos dejar la arquitectura lista para ello.
- Un solo hilo de petición al modelo a la vez (RKLLM no maneja concurrencia). Una sola sesión activa.

## Panel del cómplice
- Chat igual al del profesor, más: indicador de rol de la ronda (**"te toca a ti"** o **"responde la IA"**), vista de lo que dice la IA cuando responde ella, selector de modo forzado, botón **reiniciar sesión**, botón **tomar el control**, estado de conexión del profesor, y ritmo objetivo de escritura.
- Debe funcionar bien en celular.

## Configuración externa (no hardcodear)
Archivo `config` (YAML o `.env`) con: duración, puerto, URL del servidor RKLLM, timeout, parámetros de tiempos, contraseña del panel, ruta de historial.
Archivo aparte `system_prompt.txt` con el system prompt de la IA, **vacío por ahora** (se define después: personaje, tema experto, tono, reglas de longitud). Incluye un placeholder funcional razonable ("responde en frases cortas y en español") para poder probar.
Parámetros de generación del modelo (`max_new_tokens` bajo, temperatura) también configurables.

## Interfaz del canal humano (diseño)
Trata al cómplice como una interfaz abstracta (`enviar_mensaje` / `recibir_respuesta`), igual que al modelo de IA, para poder cambiar el canal después (por ejemplo, un bot de Telegram) sin tocar la lógica de sesión.

## Privacidad
El profesor no debe ver nada en el código, la red ni los textos que revele quién responde. No incluir etiquetas, nombres de rol ni diferencias de formato en los mensajes enviados al cliente del profesor.

## Estilo visual (BAJA PRIORIDAD)
Primero funcionalidad y robustez. La estética es mínima: no inviertas esfuerzo en animaciones ni efectos.
- Modo oscuro, fondo azul muy oscuro, acentos en morado azulado y azul.
- Chat limpio tipo plataforma de IA normal (burbujas simples, tipografía del sistema), **responsivo**.
- Pantalla del profesor: neutral y creíble, sin ninguna pista visual que revele el rol.
- Panel del cómplice: puede ser funcional y sobrio, la claridad importa más que el diseño.

## Entregables
1. Estructura de proyecto con código comentado.
2. Frontend del profesor y del panel.
3. `Containerfile` y comando/unidad para Podman (app FastAPI), unidad systemd para el servidor RKLLM del host, y los valores para configurar el Proxy Host en Nginx Proxy Manager.
4. `README` con instalación, configuración, cómo ejecutarlo y cómo probar sin el modelo (modo simulado/mock del servidor RKLLM).
5. Un mock del servidor RKLLM para desarrollo.
