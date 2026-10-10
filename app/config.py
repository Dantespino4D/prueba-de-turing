"""
app/config.py — carga y valida la configuración desde config.yaml.
"""
import os
import yaml
from dataclasses import dataclass, field
from pathlib import Path


# Ruta base del proyecto (un nivel arriba de este archivo)
BASE_DIR = Path(__file__).parent.parent


@dataclass
class AppConfig:
    host: str = "0.0.0.0"
    port: int = 8100
    panel_path: str = "/panel-secreto"
    panel_password: str = "changeme"
    secret_key: str = "changeme-secret-key"


@dataclass
class SessionConfig:
    duration_seconds: int = 300


@dataclass
class RkllmConfig:
    base_url: str = "http://host.containers.internal:8085"
    timeout_seconds: int = 40
    max_tokens: int = 200
    temperature: float = 0.8
    top_p: float = 0.9
    top_k: int = 1
    repeat_penalty: float = 1.1


@dataclass
class MockConfig:
    port: int = 8081
    min_latency_seconds: float = 3.0
    max_latency_seconds: float = 10.0


@dataclass
class TimingConfig:
    read_delay_min: float = 1.0
    read_delay_max: float = 3.0
    typing_speed_min: float = 4.0
    typing_speed_max: float = 6.0
    human_min_total: float = 5.0
    human_max_wait: float = 30.0
    # A2: qué hace el servidor cuando el cómplice no responde a tiempo
    # "none" → muestra "escribiendo..." hasta generic_typing_extra_seconds y luego
    #           envía generic_timeout_message (el profesor nunca ve silencio ni error).
    # "ia"   → el modelo responde automáticamente (source = "ia_fallback").
    human_timeout_fallback: str = "none"
    # Segundos adicionales de "escribiendo..." antes de entregar el mensaje genérico (modo none)
    generic_typing_extra_seconds: float = 5.0
    # Mensaje genérico que recibe el profesor cuando se agota el tiempo y fallback=none
    generic_timeout_message: str = "Interesante pregunta, déjame pensar un momento más..."
    # Filtro de respuesta IA: recortar a max_words palabras y eliminar oraciones incompletas
    filter_enabled: bool = True
    filter_max_words: int = 18


@dataclass
class HistoryConfig:
    output_dir: str = "/mnt/disco_8TB/historial-turing"


@dataclass
class WebhookConfig:
    enabled: bool = False
    url: str = ""


@dataclass
class Config:
    app: AppConfig = field(default_factory=AppConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    rkllm: RkllmConfig = field(default_factory=RkllmConfig)
    mock: MockConfig = field(default_factory=MockConfig)
    timing: TimingConfig = field(default_factory=TimingConfig)
    history: HistoryConfig = field(default_factory=HistoryConfig)
    webhook: WebhookConfig = field(default_factory=WebhookConfig)
    system_prompt: str = ""


def _merge(dataclass_obj, data: dict):
    """Aplica los valores del dict a los campos del dataclass, ignorando claves desconocidas."""
    if not data:
        return
    for key, val in data.items():
        if hasattr(dataclass_obj, key):
            setattr(dataclass_obj, key, val)


def load_config(path: str | None = None) -> Config:
    """
    Carga config.yaml (o el archivo indicado).
    La ruta se resuelve en este orden:
      1. El argumento ``path`` si se pasa explícitamente.
      2. La variable de entorno ``TURING_CONFIG`` si está definida.
      3. ``config.yaml`` en el directorio de trabajo actual como respaldo.
    Si el archivo no existe, usa valores por defecto (útil en desarrollo).
    Siempre carga system_prompt.txt si existe.
    """
    config = Config()

    # Buscar archivo de configuración
    if path is None:
        path = os.environ.get("TURING_CONFIG", str(BASE_DIR / "config.yaml"))

    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        _merge(config.app, raw.get("app"))
        _merge(config.session, raw.get("session"))
        _merge(config.rkllm, raw.get("rkllm"))
        _merge(config.mock, raw.get("mock"))
        _merge(config.timing, raw.get("timing"))
        _merge(config.history, raw.get("history"))
        _merge(config.webhook, raw.get("webhook"))
    else:
        print(f"[config] Aviso: no se encontró {path}, usando valores por defecto.")

    # Cargar system prompt
    prompt_path = BASE_DIR / "system_prompt.txt"
    if prompt_path.exists():
        raw_prompt = prompt_path.read_text(encoding="utf-8")
        # Ignora líneas de comentario (#) al inicio del archivo
        lines = [l for l in raw_prompt.splitlines() if not l.startswith("#")]
        config.system_prompt = "\n".join(lines).strip()

    return config


# Instancia global, cargada una sola vez al importar
config: Config = load_config()
