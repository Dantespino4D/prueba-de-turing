"""
app/timing.py — Motor de tiempos para simular escritura humana.

Reglas (de la especificación):
  1. Pausa de "lectura": 1–3 s aleatorios → luego indicador "escribiendo…"
  2. Retraso de "escritura": proporcional a len(respuesta) / velocidad_chars_por_seg
  3. Para IA: descuenta el tiempo que tardó el modelo en generar.
  4. Para humano:
     - Si el cómplice tardó más que el mínimo → entregar de inmediato (ya esperamos).
     - Si el cómplice tardó menos que el mínimo → retener hasta cumplir el mínimo.
  5. Tope máximo: 30 s de espera del humano (ya controlado en session.py).

El motor NO hace await por sí mismo; calcula los valores y quien llama decide.
"""
import random

from .config import config


def _tc() -> "TimingConfig":
    return config.timing


def reading_delay() -> float:
    """Pausa de 'lectura' antes de mostrar 'escribiendo...' (segundos)."""
    return random.uniform(_tc().read_delay_min, _tc().read_delay_max)


def typing_delay(text: str) -> float:
    """
    Tiempo de 'escritura' proporcional a la longitud del texto (segundos).
    Velocidad aleatoria entre typing_speed_min y typing_speed_max chars/s.
    """
    if not text:
        return 0.0
    speed = random.uniform(_tc().typing_speed_min, _tc().typing_speed_max)
    return len(text) / speed


def ai_response_delay(text: str, model_elapsed: float) -> float:
    """
    Calcula cuánto tiempo adicional esperar DESPUÉS de que el modelo respondió.

    total_esperado = reading_delay + typing_delay(text)
    adicional      = max(0, total_esperado - model_elapsed)

    Nota: reading_delay ya pasó mientras el modelo generaba si model_elapsed
    fue mayor, de lo contrario se aplica la diferencia.
    """
    total = reading_delay() + typing_delay(text)
    additional = max(0.0, total - model_elapsed)
    return additional


def human_response_delay(text: str, human_elapsed: float) -> float:
    """
    Calcula cuánto tiempo adicional retener la respuesta del humano.

    Reglas:
    - Si el cómplice tardó >= human_min_total → no retener más (entregar ya).
    - Si tardó < human_min_total → retener la diferencia.
    - Además, el retraso de escritura proporcional al texto debe aplicarse
      en cualquier caso (para que parezca que está escribiendo).

    Devuelve los segundos a esperar ANTES de entregar al profesor.
    """
    # Mínimo de la sesión (reemplaza cualquier espera que ya ocurrió)
    min_total = _tc().human_min_total
    typing = reading_delay() + typing_delay(text)

    # Tiempo que debería tomar como mínimo
    target = max(min_total, typing)

    # Cuánto falta todavía
    remaining = max(0.0, target - human_elapsed)
    return remaining


def typing_speed_chars_per_sec() -> float:
    """Velocidad de referencia para mostrar en el panel del cómplice."""
    return random.uniform(_tc().typing_speed_min, _tc().typing_speed_max)
