FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TURING_CONFIG=/config/config.yaml

# Usuario no-root con UID 1000, compatible con UserNS=keep-id (Podman rootless).
# Con keep-id el host mapea su propio UID al mismo UID dentro del contenedor,
# por lo que UID 1000 aqui == UID 1000 de "dante" en el host == dueno de /data.
RUN groupadd --gid 1000 turing && \
    useradd --uid 1000 --gid 1000 --no-create-home --shell /sbin/nologin turing

WORKDIR /app

COPY requirements.txt .
# tzdata: por si la imagen no trae zonas horarias (el historial usa America/Mexico_City)
RUN pip install --no-cache-dir -r requirements.txt tzdata

COPY app/ ./app/
COPY static/ ./static/
COPY mock_rkllm/ ./mock_rkllm/
COPY system_prompt.txt ./system_prompt.txt

# Ceder /app al usuario no-root antes de cambiar de usuario
RUN chown -R turing:turing /app

USER turing

EXPOSE 8100

# Sin --reload: esto es producción.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100"]
