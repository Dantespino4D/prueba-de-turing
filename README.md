# Prueba de Turing

Aplicación web para una demostración controlada de la Prueba de Turing en clase.
El profesor chatea ~5 minutos con un interlocutor desconocido y al final vota si habló con un humano o una IA.
El servidor decide al azar (50/50) en cada sesión quién responde.

---

## Estructura del proyecto

```
prueba-turing/
├── app/
│   ├── __init__.py
│   ├── config.py          # Carga y valida config.yaml
│   ├── main.py            # FastAPI: rutas HTTP y WebSocket
│   ├── rkllm_client.py    # Cliente async para el servidor RKLLM
│   ├── session.py         # Estado de sesión en memoria
│   └── timing.py          # Motor de retrasos simulados
├── mock_rkllm/
│   ├── __init__.py
│   └── server.py          # Servidor mock para desarrollo (sin NPU)
├── static/
│   ├── index.html         # Chat del profesor
│   ├── professor.js
│   ├── panel.html         # Panel del cómplice
│   ├── panel.js
│   ├── login.html         # Formulario de acceso al panel
│   └── style.css
├── config.example.yaml    # Plantilla de configuración (sin contraseñas)
├── config.yaml            # ← TÚ LO CREAS (en .gitignore)
├── system_prompt.txt      # System prompt de la IA (editable)
├── requirements.txt
├── Containerfile          # Para podman build
├── rkllm-server.service   # Unidad systemd para el servidor RKLLM en el host
└── README.md
```

---

## Instalación (Fase 1 — con mock, sin contenedor)

### 1. Clonar y preparar el entorno

```bash
cd /home/dante/prueba-turing
python3.12 -m venv venv            # o usa el Python del contenedor
source venv/bin/activate
pip install -r requirements.txt
```

> **Nota:** El host usa Python 3.14. El venv y el contenedor usan Python 3.12.

### 2. Crear la configuración

```bash
cp config.example.yaml config.yaml
```

Edita `config.yaml`:
- Cambia `panel_password` y `secret_key`.
- Ajusta `panel_path` si quieres una URL diferente para el panel.
- Mantén `mock.enabled: true` para Fase 1.

### 3. Iniciar el servidor mock del RKLLM

En una terminal separada:

```bash
source venv/bin/activate
python -m mock_rkllm.server --port 8080 --min-latency 3 --max-latency 8
```

El mock imita exactamente la API del servidor RKLLM real (`POST /v1/chat/completions`).

### 4. Iniciar la aplicación FastAPI

```bash
source venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8100 --reload
```

Abre:
- **Profesor:** http://localhost:8100/
- **Panel:** http://localhost:8100`<panel_path>` (según `config.yaml`)

---

## Flujo de uso

1. El cómplice abre el panel e inicia sesión con la contraseña.
2. Espera a que **"Panel: conectado"** y **"Modelo: disponible"** aparezcan en verde.
3. El cómplice pulsa **"Iniciar sesión"** (elige modo aleatorio o forzado).
4. El profesor escribe su primer mensaje.
5. Dependiendo del modo elegido, la IA o el cómplice responden.
6. Al agotar el tiempo (o si el profesor vota antes), aparece la pregunta de voto.
7. El panel muestra el resultado y la transcripción se guarda en JSON.

### Modo forzado (para ensayos)

Seleccionable en el panel antes de iniciar:
- **Aleatorio** — 50/50 por sesión.
- **Forzar IA** — siempre responde el modelo.
- **Forzar Humano** — siempre responde el cómplice.

El modo se fija al iniciar la sesión. Solo cambia al reiniciar.
La excepción es **"Tomar control"**, que afecta solo la ronda en curso.

### Botón "Tomar control"

Aparece cuando la IA está generando. Al pulsarlo, el cómplice puede escribir
su propia respuesta; la respuesta de la IA se descarta y el profesor recibe
la del cómplice. El profesor no nota diferencia alguna.

---

## Configuración avanzada

| Clave | Descripción | Por defecto |
|-------|-------------|-------------|
| `session.duration_seconds` | Duración de la sesión | 300 |
| `rkllm.base_url` | URL del servidor RKLLM | `http://host.containers.internal:8080` |
| `rkllm.timeout_seconds` | Timeout de inferencia | 40 |
| `rkllm.max_tokens` | Tokens máximos por respuesta | 200 |
| `timing.read_delay_min/max` | Pausa de "lectura" (s) | 1–3 |
| `timing.typing_speed_min/max` | Velocidad simulada (car/s) | 4–6 |
| `timing.human_min_total` | Mínimo de espera para humano (s) | 5 |
| `timing.human_max_wait` | Máximo de espera para humano (s) | 30 |
| `mock.enabled` | Usar mock en vez del RKLLM real | true |
| `webhook.enabled` | Enviar resumen a n8n al terminar | false |

El archivo `system_prompt.txt` define el personaje de la IA. Las líneas
que comienzan con `#` son comentarios y se ignoran.

---

## Fase 2 — Integración con el servidor RKLLM real

### API del servidor RKLLM (flask_server.py)

El servidor expone una API compatible con OpenAI en el puerto **8080** (por defecto):

| Método | Ruta | Descripción |
|--------|------|-------------|
| `GET`  | `/v1/models` | Lista el modelo cargado (usado para ping de disponibilidad) |
| `POST` | `/v1/chat/completions` | Inferencia. Acepta `stream: true/false`. Devuelve **503** si está ocupado. |

Campos del payload relevantes (confirmados en `flask_server.py`):

```json
{
  "model": "rkllm",
  "messages": [{"role": "user", "content": "..."}],
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
```

El servidor **maneja una sola petición a la vez** (lock interno + `threaded=False`).
La app ya tiene su propio `asyncio.Lock` para garantizarlo.

### Preparar el servidor RKLLM en el host

El servidor Flask del demo carga **un solo modelo al arrancar**. Para cambiar
de modelo hay que **reiniciar el servicio** (`sudo systemctl restart rkllm-server`).

```bash
# 1. Construir la librería nativa (si aún no existe lib/librkllmrt.so)
cd /home/dante/rknn-llm/examples/rkllm_server_demo
bash build_rkllm_server_flask.sh
# o copiar manualmente:
# cp /home/dante/rknn-llm/rkllm-runtime/Linux/librkllm_api/aarch64/librkllmrt.so \
#    rkllm_server/lib/

# 2. Crear venv para el servidor RKLLM (el host usa Python 3.14, necesita 3.12)
cd /home/dante/rknn-llm/examples/rkllm_server_demo
python3.12 -m venv venv
source venv/bin/activate
pip install flask

# 3. Instalar y arrancar la unidad systemd
sudo cp /home/dante/prueba-turing/rkllm-server.service /etc/systemd/system/
# Edita la unidad: ajusta MODEL_PATH al modelo que quieras
sudo systemctl daemon-reload
sudo systemctl enable --now rkllm-server
sudo systemctl status rkllm-server
journalctl -u rkllm-server -f
```

Modelos disponibles en `/home/dante/jarvis/`:
| Archivo | Notas |
|---------|-------|
| `llama3.1-supernova-instruct-merge-ab.rkllm` | ~8B, candidato principal |
| `qwen2.5-3b-distill.rkllm` | 3B, más rápido |
| `qwen2.5-3b-josiefied.rkllm` | 3B alternativo |
| `qwen2.5-1.5B-agentic-trace.rkllm` | 1.5B, puede emitir `<think>` |
| `qwen2.5-7b-coder-rk3588-UC.rkllm` | Especializado en código |

Los modelos Qwen2.5 pueden emitir bloques `<think>...</think>`.
La app los filtra automáticamente antes de entregar nada al profesor.

### Configurar la app para usar el RKLLM real

En `config.yaml`:

```yaml
mock:
  enabled: false

rkllm:
  base_url: "http://host.containers.internal:8080"  # desde el contenedor
  # o "http://localhost:8080" si corres la app directo en el host
  timeout_seconds: 40
```

---

## Fase 3 — Contenedor Podman

### Construir la imagen

```bash
cd /home/dante/prueba-turing
podman build -t prueba-turing:latest -f Containerfile .
```

### Ejecutar (modo rootless)

```bash
podman run -d \
  --name prueba-turing \
  --restart unless-stopped \
  -p 8100:8100 \
  -v /home/dante/prueba-turing/config.yaml:/app/config.yaml:ro,Z \
  -v /home/dante/prueba-turing/system_prompt.txt:/app/system_prompt.txt:ro,Z \
  -v /mnt/disco_8TB/historial-turing:/mnt/disco_8TB/historial-turing:Z \
  --add-host=host.containers.internal:host-gateway \
  prueba-turing:latest
```

> Si prefieres `--network=host` (más simple, menos aislamiento):
> ```bash
> podman run -d \
>   --name prueba-turing \
>   --network=host \
>   -v /home/dante/prueba-turing/config.yaml:/app/config.yaml:ro,Z \
>   -v /home/dante/prueba-turing/system_prompt.txt:/app/system_prompt.txt:ro,Z \
>   -v /mnt/disco_8TB/historial-turing:/mnt/disco_8TB/historial-turing:Z \
>   prueba-turing:latest
> ```
> Con `--network=host` usa `rkllm.base_url: "http://localhost:8080"` en config.yaml.

### Unidad systemd/Quadlet (opcional, para arranque automático)

Crea `~/.config/containers/systemd/prueba-turing.container`:

```ini
[Unit]
Description=Prueba de Turing — FastAPI
After=network-online.target

[Container]
Image=prueba-turing:latest
ContainerName=prueba-turing
PublishPort=8100:8100
Volume=/home/dante/prueba-turing/config.yaml:/app/config.yaml:ro,Z
Volume=/home/dante/prueba-turing/system_prompt.txt:/app/system_prompt.txt:ro,Z
Volume=/mnt/disco_8TB/historial-turing:/mnt/disco_8TB/historial-turing:Z
AddHost=host.containers.internal:host-gateway

[Service]
Restart=always

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now prueba-turing
```

---

## Nginx Proxy Manager — Proxy Host

**Domain Names:** `turing.dantespino4d.me`  
**Scheme:** `http`  
**Forward Hostname / IP:** `localhost` (o la IP del host si NPM corre en otro contenedor)  
**Forward Port:** `8100`  
**Cache Assets:** OFF  
**Block Common Exploits:** ON  
**Websockets Support:** ✅ ON  
**Force SSL:** ✅ ON  
**SSL Certificate:** Let's Encrypt (pide uno nuevo para este dominio)  
**HTTP/2 Support:** ON  
**HSTS Enabled:** ON  

### Bloque "Advanced" (Custom Nginx Configuration)

```nginx
# Timeouts largos para WebSocket y respuestas lentas del modelo
proxy_read_timeout    300s;
proxy_send_timeout    300s;
proxy_connect_timeout  10s;

# Sin buffering: los mensajes WebSocket llegan en tiempo real
proxy_buffering off;

# Cabeceras necesarias para WebSocket
proxy_http_version 1.1;
proxy_set_header Upgrade $http_upgrade;
proxy_set_header Connection "upgrade";
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
```

> **Nota:** Nginx Proxy Manager ya agrega algunas de estas cabeceras automáticamente
> al activar "Websockets Support". El bloque Advanced las refuerza y añade los timeouts.

---

## Agregar long polling (si WebSocket falla)

La arquitectura está preparada para añadir long polling como fallback si la red
de la escuela bloquea WebSocket. Los pasos serían:

1. Agregar en `app/main.py` endpoints HTTP:
   - `POST /api/message` — el profesor envía un mensaje.
   - `GET /api/poll` — el profesor hace polling de respuestas pendientes.
2. En el frontend, detectar si WS falla y cambiar a peticiones `fetch`.

No está implementado en esta versión. Si es necesario, abrir un issue o branch.

---

## Privacidad

El profesor **nunca** recibe en el navegador:
- El rol de quién responde (`ia` / `humano`).
- Mensajes internos del panel.
- Diferencias de formato entre respuestas de la IA y el humano.

Verificar con las DevTools del navegador en la pestaña Network/WS
que los mensajes de tipo `role`, `panel_status` y `your_turn` no aparecen
en el WebSocket del profesor.
