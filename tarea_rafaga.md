# Tarea: una sola respuesta por rafaga de mensajes del profesor

Solo edita dentro de prueba-turing. No toques flask_server.py ni config.production.yaml.
Prueba con el modelo RKLLM real (servicio rkllm-server, puerto 8085 del host), no con mock_rkllm/. Corre la app de desarrollo con config.yaml en otro puerto distinto de 8100 (por ejemplo 8101), con base_url apuntando al 8085 del host (revisa que sea alcanzable fuera del contenedor). No reinicies rkllm-server ni el contenedor turing de produccion.

## Problema
- En app/main.py (ciclo del WS del profesor, ~linea 345) cada mensaje "message" crea una tarea `_handle_professor_message` que llama a `_respond_as_ai` o `_respond_as_human`. Con 3 mensajes seguidos hay 3 respuestas.
- Las tareas corren a la vez y comparten estado: el parche de `session_manager.deliver_human_response`, `_takeover_event`, `_set_model_busy`.
- `get_history_for_rkllm` (app/session.py) no fusiona turnos: dos mensajes seguidos del profesor llegan al modelo como dos turnos `user` consecutivos.

## Antes de editar
Lee completas `_respond_as_ai`, `_respond_as_human`, `_deliver_response` y el ciclo del WS del profesor.

## Cambios
1. Cada mensaje del profesor se sigue guardando en el historial y reenviando al panel al instante, pero ya NO lanza una respuesta por mensaje.
2. Pausa de rafaga: despues del ultimo mensaje se espera `timing.burst_wait_seconds` (nuevo, default 2.5, leerlo en config.py y ponerlo en config.yaml). Cada mensaje nuevo reinicia la espera. Al terminar se lanza UNA sola respuesta (IA o humano segun `sess.responder`). La pausa de lectura actual se mantiene.
3. Solo una tarea de respuesta activa a la vez:
   - Mensaje nuevo durante la espera: cancelar y reprogramar.
   - Mensaje nuevo mientras la IA genera y aun no se entrego: descartar ese resultado sin guardarlo ni enviarlo, apagar el indicador de escritura, restaurar `deliver_human_response` (try/finally) y regenerar con todo el historial.
   - Mensaje nuevo despues de que ya se entrego una respuesta: ciclo nuevo normal.
   - Modo humano: una sola solicitud al complice por rafaga. Los mensajes nuevos ya llegan a su panel; no repetir el aviso ni el temporizador de `human_max_wait`.
4. `get_history_for_rkllm`: fusionar mensajes consecutivos del mismo rol en un solo turno, unidos con "\n".
5. En el mensaje de respuesta del JSON guardar `burst_size` (cuantos mensajes del profesor respondio).
6. No cambiar: tomar control, heartbeat, filtro de salida, timeouts, voto, fin de sesion.

## Pruebas con el modelo real
El modelo tarda de 3 a 25 s por respuesta, asi que las pruebas son lentas. Revisa tambien los logs o el JSON para confirmar el numero de generaciones.
- 3 mensajes en menos de 2 s: 1 sola respuesta, y el historial enviado al modelo lleva un solo turno `user` con los 3 textos.
- Mensaje durante la generacion: se entrega una sola respuesta, la del historial completo.
- Mensaje aislado: se comporta como antes.
- Tomar control funciona antes y durante la generacion.
- Terminar o reiniciar la sesion con una espera pendiente no deja tareas colgadas.

## Entrega
Resumen de 5 lineas con los archivos y funciones cambiados.
