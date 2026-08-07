#!/usr/bin/env python3
"""
generar_capturas.py — Genera las capturas de las 7 vistas para el README.

No son screenshots sacadas a mano: se toma un dump real del monitor
(el mismo que produce SIGUSR1) y se renderiza cada vista con el código de
display.py, exportando a SVG con rich. Así las capturas se pueden regenerar
y siempre corresponden al código actual.

Uso:
    python3 docs/generar_capturas.py

Requiere: rich. Corre el monitor unos segundos, le manda SIGUSR1 y usa ese
dump.
"""

import glob
import json
import os
import signal
import subprocess
import sys
import time

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(RAIZ, "src")
SALIDA = os.path.join(RAIZ, "docs", "img")

sys.path.insert(0, SRC)


def tomar_dump(segundos=16):
    """Levanta el monitor en modo debug, le manda SIGUSR1 y devuelve el dump."""
    for viejo in glob.glob(os.path.join(RAIZ, "dump_*.json")):
        os.remove(viejo)

    print(f"  levantando el monitor {segundos}s para que se llenen las 7 dimensiones...")
    proc = subprocess.Popen(
        [sys.executable, "src/main.py", "--debug"],
        cwd=RAIZ, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(segundos)
        os.kill(proc.pid, signal.SIGUSR1)
        time.sleep(2)
    finally:
        os.kill(proc.pid, signal.SIGTERM)
        proc.wait(timeout=15)

    dumps = sorted(glob.glob(os.path.join(RAIZ, "dump_*.json")))
    if not dumps:
        sys.exit("  ERROR: no se generó ningún dump")
    return dumps[-1]


def normalizar(dump):
    """
    JSON convierte las claves de dict a string. El display espera PIDs int,
    así que se revierte.
    """
    con_pid = ("resumen", "memoria", "fds", "threads", "senales", "scheduling")
    return {
        k: ({int(kk): vv for kk, vv in v.items()}
            if k in con_pid and isinstance(v, dict) else v)
        for k, v in dump.items()
    }


class ValueFalso:
    """Reemplaza multiprocessing.Value: acá no hay procesos, solo render."""

    def __init__(self, valor):
        self.value = valor

    def get_lock(self):
        import contextlib
        return contextlib.nullcontext()


class EventFalso:
    def set(self):
        pass

    def is_set(self):
        return False


def main():
    from rich.console import Console
    import config
    import display as disp

    os.makedirs(SALIDA, exist_ok=True)

    ruta_dump = tomar_dump()
    print(f"  dump: {os.path.basename(ruta_dump)} "
          f"({os.path.getsize(ruta_dump)/1024/1024:.1f} MB)")

    snapshot = normalizar(json.load(open(ruta_dump, encoding="utf-8")))
    cfg = config.cargar(os.path.join(RAIZ, "config.json"))
    intervalos = {n: ValueFalso(cfg["intervalos"][n]) for n in config.DIMENSIONES}

    ui = disp.Display(snapshot, intervalos, ValueFalso(0), EventFalso(), cfg)

    # Se elige un proceso propio con FDs y threads suficientes para que las
    # vistas 3 y 4 muestren algo interesante.
    filas = ui._procesos()
    ui.pin = next(
        (f["pid"] for f in filas
         if (snapshot.get("fds", {}).get(f["pid"]) or {}).get("total", 0) > 5
         and (snapshot.get("threads", {}).get(f["pid"]) or {}).get("total", 0) > 3),
        filas[0]["pid"] if filas else None,
    )
    print(f"  proceso destacado: pid {ui.pin}")

    vistas = list(disp.VISTAS) + ["ayuda"]
    for i, vista in enumerate(vistas, start=1):
        if vista == "ayuda":
            ui.mostrar_ayuda = True
            nombre = "8-ayuda"
        else:
            ui.vista = vista
            nombre = f"{i}-{vista}"

        consola = Console(width=155, height=46, record=True,
                          file=open(os.devnull, "w", encoding="utf-8"))
        consola.print(ui.render())

        destino = os.path.join(SALIDA, f"{nombre}.svg")
        consola.save_svg(destino, title=f"monitor_linux_01 — {nombre}")
        print(f"  {os.path.relpath(destino, RAIZ)}")

    os.remove(ruta_dump)
    print(f"\n  {len(vistas)} capturas en docs/img/")


if __name__ == "__main__":
    main()
