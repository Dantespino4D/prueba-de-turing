Hola Bob. Hay un bug en la función "Tomar el control" del panel. Cuando el cómplice escribe una respuesta y la envía, no se intercepta la IA y termina respondiendo el modelo.

Problema identificado:
El mecanismo actual usa un "parche temporal" de deliver_human_response en session.py, pero parece haber un problema de timing o de conexión del evento.

Solución solicitada:
1. En app/session.py, modifica el método take_control para que dispare un evento directo de takeover:
   - Agrega self._takeover_event = asyncio.Event() y self._takeover_text = None en __init__
   - En take_control, setea ese evento y guarda el texto directamente

2. En app/main.py, dentro de _respond_as_ai, agrega una tarea adicional que espere el evento directo de takeover:
   - Crea async def wait_takeover_direct() que espere session_manager._takeover_event
   - Agrégala al asyncio.wait junto con ai_task y take_control_event.wait()

3. Asegúrate de que cuando se dispare el takeover (por cualquier vía), se cancele ai_task y se use el texto del cómplice.

4. Agrega logs para debug: print("[take_control] Recibido:", text) en session.py y print("[take_control] Evento disparado") cuando se setee el evento.

Restricciones:
- NO cambies la lógica de rondas humanas normales
- NO toques el frontend (ya envía el mensaje correctamente)
- Mantén el parche de deliver_human_response como respaldo

Al terminar, detente y avísame para probar.