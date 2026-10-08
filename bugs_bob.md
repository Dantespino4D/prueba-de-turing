# Bugs encontrados en pruebas (modo Forzar IA)

Contexto: lee prompt_bob_turing.md y fase3_bob.md si necesitas recordar el proyecto. Ignora la carpeta venv. No toques nada que no este en este documento. No ejecutes sudo, systemctl ni podman.
PRIMERO DIAGNOSTICA leyendo el codigo y dime que encontraste; luego corrige.

## Sintomas
1. El modelo tardo mas que el timeout (40 s) con una pregunta abierta ("ok, que sabes sobre c++") y salio "El modelo no respondio. Puedes tomar el control".
2. Durante esa espera, el panel mostro "Profesor: desconectado" aunque la pagina del profesor seguia abierta, y en la pagina del profesor quedaron los puntos de "escribiendo" y el boton de enviar deshabilitado para siempre.
3. Primer "Tomar control" (21:28:40): quedo registrado en events como takeover, pero su mensaje NO llego al historial ni al profesor.
4. Segundo "Tomar control" (21:29:01): funciono (quedo en el historial con source takeover y el profesor lo recibio al recargar su pagina).
5. Al reconectar, la pagina del profesor mostro el historial DUPLICADO (repinta sin limpiar el chat).
6. Las respuestas cortas del modelo funcionan bien (2.4 s), el problema es con respuestas largas.

## Hipotesis a verificar en el codigo (no las des por ciertas)
a) La llamada al modelo bloquea el event loop (cliente HTTP sincrono dentro de codigo async). Mientras espera, el servidor no atiende el ping/pong del heartbeat, el pong no llega a tiempo y se cierra el WebSocket del profesor.
b) El takeover fue aceptado y registrado, pero la entrega fallo despues, probablemente al enviar a un WebSocket del profesor ya cerrado, y la excepcion corto el flujo ANTES de agregar el mensaje al historial.
c) El servidor RKLLM es de un solo hilo (threaded=False): tras un timeout sigue generando, y la peticion siguiente espera a que termine.

## Correcciones pedidas
1. La llamada al modelo no debe bloquear el event loop (cliente async o run_in_executor).
2. El heartbeat no debe cerrar el socket por un solo pong tardio: tolera varios fallos seguidos antes de considerar la conexion muerta.
3. En toda entrega de mensaje al profesor: primero agregar el mensaje al historial y DESPUES intentar enviarlo. Cada envio por WebSocket va dentro de try/except para que un socket muerto nunca deje un mensaje perdido.
4. Si el envio falla porque el socket esta cerrado, no abortes el flujo: registra un evento "entrega_fallida" con el motivo y reenvia el mensaje cuando el profesor reconecte. Muestra en el panel si un mensaje no pudo entregarse.
5. "Tomar control" debe funcionar en cualquier momento de una ronda de IA, incluso con el modelo ocupado o despues de un timeout: cancela la espera, entrega el texto al profesor con source takeover, desbloquea la entrada del profesor y DESCARTA cualquier respuesta tardia del modelo.
6. Al reconectar, la pagina del profesor debe limpiar el chat antes de pintar el historial, y restaurar el estado: si no hay respuesta pendiente, habilitar la entrada.
7. Agrega un limite de espera en el cliente del profesor para que la interfaz nunca quede bloqueada indefinidamente (configurable).
8. Si el modelo sigue ocupado con una peticion anterior, indicalo en el panel y no envies otra peticion hasta que termine o expire.
9. Confirma si max_tokens del config realmente llega al modelo. Si flask_server.py lo fija al cargar el modelo y no por peticion, dimelo en vez de modificar ese archivo (esta fuera del workspace).

## Reglas
- El profesor nunca debe recibir pistas de quien responde ni mensajes de error visibles.
- No cambies lo que ya funciona: recuperacion de historial (ambos lados), guardado en el NAS, heartbeat del estado del profesor, JSON enriquecido.
- Al terminar, dame los pasos exactos para repetir la prueba: pregunta larga con el modelo lento, takeover durante la espera y despues del timeout, recarga de la pagina del profesor sin mensajes duplicados.
