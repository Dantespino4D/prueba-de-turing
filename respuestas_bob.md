# Respuestas a tus preguntas

1. WebSocket fallback: solo WebSocket con reconexion automatica, y una nota en el README de como agregar long polling. No implementes long polling todavia.
2. Recuperacion al recargar: debe funcionar para el profesor Y para el complice. Si cualquiera recarga a media sesion, recupera historial, rol y tiempo restante.
3. Password del panel: una sola contrasena compartida, sin usuario. Formulario simple que genere una cookie de sesion httpOnly (no localStorage).
4. Modo forzado: se fija una vez por sesion al iniciarla (forzado o aleatorio) y solo cambia al reiniciar. La excepcion es "tomar el control", que actua por ronda.
5. Mock de RKLLM: si, un stub simple con respuestas de una lista y latencia simulada configurable (por ejemplo 3-10 s). Debe imitar el formato real del endpoint.
6. Retrasos: si el complice responde mas rapido que el minimo, se retiene hasta cumplirlo. Si responde mas lento, se entrega en cuanto termina, sin retraso extra. Pon un tope maximo de espera de unos 30 s.
7. "Modelo listo": basta una respuesta HTTP del servidor RKLLM o una conexion TCP exitosa, NO una inferencia real (ocuparia la NPU). Agrega un boton separado "probar modelo" en el panel que lance una inferencia corta.

# Instrucciones extra

- Antes de escribir el cliente de RKLLM, lee /home/dante/rknn-llm/examples/rkllm_server_demo/rkllm_server/flask_server.py y chat_api_flask.py para usar el endpoint y formato reales, sin inventarlos.
- Trabaja por fases y detente al final de cada una para que probemos:
  1. Backend y frontends con el mock de RKLLM.
  2. Integracion con el servidor RKLLM real.
  3. Contenedor Podman y valores para Nginx Proxy Manager.
- El servidor RKLLM del demo carga un solo modelo al arrancar. Cambiar de modelo implica reiniciar ese servicio. Documentalo asi en el README.
- Crea tambien un .gitignore (config con password, historiales, venv, __pycache__, .env, archivos .rkllm) y un config.example con valores falsos.

Empieza por la fase 1.
