#!/usr/bin/env python3
"""Parche para flask_server.py: formato de chat segun el modelo (llama3 o chatml).

Uso:  python parche_prompt.py [ruta/a/flask_server.py]
- Guarda copia en flask_server.py.bak3 (solo si no existe).
- Si el resultado no compila, restaura el original.
- El formato se elige por el nombre del modelo ("llama" -> llama3, otro -> chatml)
  o forzando con la variable de entorno PROMPT_FORMAT=llama3|chatml.
"""
import os
import py_compile
import shutil
import sys

DEFAULT = os.path.expanduser(
    "~/rknn-llm/examples/rkllm_server_demo/rkllm_server/flask_server.py")
path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT

HELPER = '''
# --- Formato de chat segun el modelo (agregado por parche_prompt.py) ---
PROMPT_FORMAT = os.environ.get("PROMPT_FORMAT", "chatml")


def detectar_formato(model_path):
    forzado = os.environ.get("PROMPT_FORMAT")
    if forzado in ("llama3", "chatml"):
        return forzado
    return "llama3" if "llama" in os.path.basename(model_path).lower() else "chatml"


def build_chat_prompt(sys_prompt, turns):
    """turns: lista de (rol, texto) sin el mensaje system."""
    if PROMPT_FORMAT == "llama3":
        partes = ["<|begin_of_text|>"]
        if sys_prompt:
            partes.append(f"<|start_header_id|>system<|end_header_id|>\\n\\n{sys_prompt}<|eot_id|>")
        for rol, texto in turns:
            partes.append(f"<|start_header_id|>{rol}<|end_header_id|>\\n\\n{texto}<|eot_id|>")
        partes.append("<|start_header_id|>assistant<|end_header_id|>\\n\\n")
        return "".join(partes)
    # ChatML (Qwen): mismo resultado que el codigo anterior
    partes = []
    if sys_prompt:
        partes.append(f"<|im_start|>system\\n{sys_prompt}<|im_end|>")
    for rol, texto in turns:
        partes.append(f"<|im_start|>{rol}\\n{texto}<|im_end|>")
    partes.append("<|im_start|>assistant")
    return "\\n".join(partes) + "\\n"
# --- fin del bloque agregado ---
'''

NEW_BLOCK = '''chat_turns = []  # (rol, texto), sin el mensaje system
for msg in messages:
    msg_role = msg.get("role", "user")
    if msg_role == "system":
        continue  # ya se manejo arriba
    msg_content = msg.get("content", "")
    if isinstance(msg_content, list):
        msg_content = " ".join(
            p.get("text", "") for p in msg_content if p.get("type") == "text"
        )
    chat_turns.append((msg_role, msg_content))
prompt = build_chat_prompt(sys_prompt, chat_turns)
'''


def fail(msg):
    print("ERROR:", msg)
    print("No se modifico nada.")
    sys.exit(1)


if not os.path.exists(path):
    fail(f"no existe {path}")
src = open(path, encoding="utf-8").read()
if "def build_chat_prompt" in src:
    fail("el archivo ya esta parcheado")

lines = src.splitlines(keepends=True)
strip = [l.strip() for l in lines]

def find_all(pred):
    return [i for i, l in enumerate(strip) if pred(l)]

i_start = find_all(lambda l: l == "chatml_parts = []")
i_end = find_all(lambda l: l.startswith('prompt = "\\n".join(chatml_parts)'))
i_imp = find_all(lambda l: l == "import re")
i_args = find_all(lambda l: l == "args = parser.parse_args()")
for nombre, lst in (("chatml_parts = []", i_start), ("prompt = ...join(chatml_parts)", i_end),
                    ("import re", i_imp), ("args = parser.parse_args()", i_args)):
    if len(lst) != 1:
        fail(f"esperaba exactamente 1 coincidencia de '{nombre}' y hay {len(lst)}")
i_start, i_end, i_imp, i_args = i_start[0], i_end[0], i_imp[0], i_args[0]
if not (i_start < i_end and i_end - i_start < 40):
    fail("el bloque de ChatML no tiene la forma esperada")

indent = lines[i_start][: len(lines[i_start]) - len(lines[i_start].lstrip())]
nuevo = "".join(indent + l if l.strip() else l for l in NEW_BLOCK.splitlines(keepends=True))
args_indent = lines[i_args][: len(lines[i_args]) - len(lines[i_args].lstrip())]
extra_main = (f"{args_indent}PROMPT_FORMAT = detectar_formato(args.rkllm_model_path)\n"
              f"{args_indent}print(f\"[prompt] formato de chat: {{PROMPT_FORMAT}}\", flush=True)\n")

# Orden: de abajo hacia arriba para no mover los indices
out = (lines[: i_imp + 1] + [HELPER] + lines[i_imp + 1: i_start] + [nuevo]
       + lines[i_end + 1: i_args + 1] + [extra_main] + lines[i_args + 1:])

bak = path + ".bak3"
if not os.path.exists(bak):
    shutil.copy2(path, bak)
tmp = path + ".nuevo"
open(tmp, "w", encoding="utf-8").write("".join(out))
try:
    py_compile.compile(tmp, doraise=True)
except py_compile.PyCompileError as e:
    os.remove(tmp)
    fail(f"el resultado no compila ({e}). Original intacto.")
os.replace(tmp, path)
print("OK: flask_server.py parcheado. Copia en", bak)
print("Reinicia el servicio y busca en el log: [prompt] formato de chat: llama3")
