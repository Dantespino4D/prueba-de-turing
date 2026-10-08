#!/usr/bin/env bash
# Espera hasta ~6 minutos a que el servidor RKLLM (puerto 8085) acepte conexiones.
# flask_server.py solo abre el puerto DESPUES de cargar el modelo.
# Si se agota el tiempo, sale con 0 para que la app arranque igual.
for i in $(seq 1 180); do
  if (exec 3<>/dev/tcp/127.0.0.1/8085) 2>/dev/null; then
    exit 0
  fi
  sleep 2
done
exit 0
