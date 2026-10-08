# Instrucciones para Bob: mejoras pendientes + Fase 3

Contexto: lee tambien prompt_bob_turing.md y respuestas_bob.md. Fases 1 y 2 estan probadas. Ignora la carpeta venv.
Regla permanente: el profesor nunca debe recibir pistas de quien responde. No toques lo que ya funciona (modelo, tomar control, heartbeat, recuperacion de historial).
No ejecutes sudo, systemctl ni podman por tu cuenta: genera los archivos y dame los comandos para correrlos yo.

Trabaja en DOS bloques y detente al terminar el bloque A para que yo pruebe.

---

## BLOQUE A: mejoras antes de empaquetar

### A1. JSON del historial mas util (para mi reporte)
No cambies los campos existentes. Agrega:
- Por mensaje: "source" con valor ia, humano o takeover (takeover = el complice intercepta una respuesta de la IA), y "model_latency_s" y "delay_applied_s" cuando aplique.
- "events": lista con hora y tipo (timeout, error_modelo, reintento, takeover, reset, vote).
- Nivel sesion: nombre del .rkllm usado (el id que devuelve /v1/models), temperatura, nombre del system_prompt, "end_time" y "end_reason" (vote, timeout, reset).
- Todas las horas tambien en ISO 8601 con zona America/Mexico_City.

### A2. Complice sin respuesta
Hoy, si el complice no responde en human_max_wait (30 s), solo aparece el error en el panel. Quiero:
- Opcion de config "human_timeout_fallback": "none" | "ia". Con "ia", el modelo responde esa ronda automaticamente y el mensaje queda con source "ia_fallback" y un evento en "events". Valor por defecto: "none".
- Con "none", el profesor igual debe recibir algo creible (por ejemplo seguir mostrando "escribiendo..." hasta un maximo configurable y luego una respuesta corta generica configurable), nunca silencio indefinido ni un mensaje de error visible para el profesor.
- Siempre avisar al complice en el panel.

### A3. Aviso al complice
En el panel, cuando llegue un mensaje del profesor en una ronda que le toca al complice: sonido corto (con boton para silenciar) y titulo de la pestana parpadeando. Configurable y solo en el panel.

### A4. Lista de verificacion previa a la demo
Agrega al README una checklist (modo de sesion en Aleatorio y no Forzado, modelo correcto cargado, "modelo listo" en verde, Frigate/Immich ML detenidos, contrasena del panel cambiada, historial escribiendo en el NAS, prueba desde la red de la escuela).

DETENTE aqui y espera mi confirmacion.

---

## BLOQUE B: Fase 3, despliegue

### Entorno (datos reales)
- Orange Pi 5 Plus, Arch Linux aarch64, usuario dante, Podman rootless (tambien hay Docker corriendo otros servicios; no lo uses para esta app).
- Servidor RKLLM: corre en el HOST, no en contenedor. Puerto 8085, escucha en 0.0.0.0, argumentos reales de flask_server.py: --rkllm_model_path y --target_platform rk3588 (NO existe --port; el puerto esta fijo en app.run()).
- Rutas: servidor en /home/dante/rknn-llm/examples/rkllm_server_demo/rkllm_server/ (con lib/librkllmrt.so dentro; el script debe ejecutarse DESDE esa carpeta), venv en /home/dante/rknn-llm/examples/rkllm_server_demo/venv, modelos en /home/dante/jarvis/, modelo por defecto llama3.1-supernova-instruct-merge-ab.rkllm.
- App FastAPI: puerto 8100. Proyecto en /home/dante/prueba-turing. Historial: /mnt/disco_8TB/historial-turing en el host.
- Nginx Proxy Manager corre en Docker en esta misma Orange (puertos 80, 81, 443). Dominio: turing.dantespino4d.me.
- Puertos ocupados en el host (no usar): 22, 53, 3001, 3012, 5000, 5678, 6167, 8000, 8080 (Dozzle), 8082, 8090, 1984, 2283, 8554, 8555, 8971.

### B1. Servicio systemd del servidor RKLLM (archivo rkllm-server.service, de sistema)
- User=dante, WorkingDirectory= la carpeta rkllm_server (importante por la ruta relativa de la libreria).
- ExecStart con el python del venv directamente (sin "source activate" ni ExecStartPre inutil), --rkllm_model_path y --target_platform rk3588.
- Restart=on-failure con espera razonable, Environment con el modelo configurable, un comentario claro de que cambiar de modelo implica editar el archivo y reiniciar el servicio, y otro de que el puerto se cambia en flask_server.py.
- Tiempo de arranque largo (el modelo de 8B tarda en cargar): TimeoutStartSec generoso.
- Opcional, documentado y comentado (no activo): script scripts/fix_freq_rk3588.sh del repo rknn-llm para fijar frecuencias.

### B2. Contenedor de la app
- Containerfile con python:3.12-slim, usuario no root, dependencias desde requirements.txt, y archivo .containerignore (excluir venv, __pycache__, historiales, config.yaml con secretos, .git).
- La app debe aceptar la ruta del config por variable de entorno (por ejemplo TURING_CONFIG) y la ruta del historial debe poder fijarse en /data.
- config.production.example.yaml: rkllm.base_url http://host.containers.internal:8085, history.output_dir /data, contrasena y ruta oculta del panel como valores de ejemplo (yo los cambio), mock sin usar.
- El config real se monta como volumen de solo lectura; nunca se mete dentro de la imagen.
- Volumen del historial: preferir --userns=keep-id para que el contenedor escriba como dante sin cambiar el dueno de la carpeta del NAS (NO uses :U sobre esa carpeta). Verifica que el archivo creado quede propiedad de dante.
- Si host.containers.internal no resuelve o no conecta, documenta la alternativa --network=host.

### B3. Arranque automatico con Quadlet
- Archivo turing.container para ~/.config/containers/systemd/ (modo rootless), Restart=always, publicar 8100, volumenes y variables, y healthcheck simple (HTTP a la app).
- Documenta: loginctl enable-linger dante (obligatorio para que arranque al reiniciar la Orange sin sesion abierta), systemctl --user daemon-reload, systemctl --user enable --now, y como ver logs (journalctl --user -u turing).
- Como el servicio RKLLM es de sistema y la app de usuario, no se puede ordenar con After=: la app debe tolerar que el modelo aun no este listo (ya existe el indicador "modelo listo") y reintentar el ping periodicamente hasta que responda.

### B4. Valores para Nginx Proxy Manager (solo documentar, yo lo configuro en la interfaz)
- Domain: turing.dantespino4d.me. Forward Hostname/IP: la IP LAN de la Orange (NO localhost, porque NPM esta en un contenedor), puerto 8100, esquema http.
- Activar Websockets Support, Block Common Exploits, SSL con Let's Encrypt, Force SSL y HTTP/2.
- Bloque de configuracion avanzada: proxy_read_timeout y proxy_send_timeout de 300s, proxy_buffering off, y cabecera X-Robots-Tag "noindex, nofollow".
- Recordatorio: no abrir en el router los puertos 8085 ni 8100, solo el 443 (y 80 para Let's Encrypt).

### B5. Verificacion
Deja en el README una lista corta de comandos de comprobacion: systemctl status del servicio RKLLM, ss -tlnp para 8085 y 8100, podman ps, journalctl --user, curl al health de la app desde el host y desde fuera, y una prueba de reinicio completo de la Orange (todo debe levantar solo; la app muestra "modelo listo" cuando RKLLM termina de cargar).

### Entregables del bloque B
rkllm-server.service corregido, Containerfile, .containerignore, turing.container (Quadlet), config.production.example.yaml, README actualizado (seccion Despliegue y checklist), y la lista de comandos exactos que yo debo ejecutar, en orden.
