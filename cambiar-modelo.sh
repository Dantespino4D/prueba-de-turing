#!/usr/bin/env bash
# Selecciona un modelo RKLLM, actualiza el servicio y lo reinicia.
set -euo pipefail

MODELOS=(
  "/home/dante/jarvis/llama3.1-supernova-instruct-merge-ab.rkllm"
  "/home/dante/jarvis/qwen2.5-7b-coder-rk3588-UC.rkllm"
  "/home/dante/jarvis/qwen2.5-3b-distill.rkllm"
  "/home/dante/jarvis/qwen2.5-3b-josiefied.rkllm"
  "/home/dante/jarvis/qwen2.5-1.5B-agentic-trace.rkllm"
)

echo "=== Cambiar modelo RKLLM ==="
for i in "${!MODELOS[@]}"; do
  echo "  $((i+1))) $(basename "${MODELOS[$i]}")"
done
echo ""
read -rp "Elige [1-${#MODELOS[@]}]: " eleccion

if ! [[ "$eleccion" =~ ^[0-9]+$ ]] || (( eleccion < 1 || eleccion > ${#MODELOS[@]} )); then
  echo "Opción inválida." >&2
  exit 1
fi

MODELO="${MODELOS[$((eleccion-1))]}"
echo ""
echo "Modelo seleccionado: $(basename "$MODELO")"
read -rp "¿Confirmar? [s/N]: " ok
[[ "$ok" =~ ^[sS]$ ]] || { echo "Cancelado."; exit 0; }

# Actualizar MODEL_PATH en el servicio
sudo sed -i "s|^Environment=\"MODEL_PATH=.*\"|Environment=\"MODEL_PATH=$MODELO\"|" \
  /etc/systemd/system/rkllm-server.service

echo ""
echo "Recargando systemd y reiniciando rkllm-server (el modelo tarda varios minutos en cargar)..."
sudo systemctl daemon-reload
sudo systemctl restart rkllm-server

echo ""
echo "Logs en vivo (Ctrl+C para salir):"
journalctl -u rkllm-server -f
