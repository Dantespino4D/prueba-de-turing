FROM python:3.12-slim

LABEL maintainer="dante"
LABEL description="Prueba de Turing — FastAPI app"

WORKDIR /app

# Instalar dependencias del sistema (necesarias para httpx y uvicorn)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copiar requirements e instalar
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar código fuente
COPY app/        ./app/
COPY static/     ./static/
COPY system_prompt.txt .

# El archivo config.yaml se monta como volumen en producción.
# Si no existe, se usan los valores por defecto (útil para desarrollo).

# El directorio de historial se monta desde el host.
# RUN mkdir -p /mnt/disco_8TB/historial-turing  ← lo crea el host o el volumen

EXPOSE 8100

# Arrancar con uvicorn
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100", "--log-level", "info"]
