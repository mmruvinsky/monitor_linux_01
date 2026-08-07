#!/usr/bin/env python3
"""
diagnostico_teclado.py — Averigua por qué el teclado de la TUI no responde.

Reproduce exactamente la situación del display —un proceso hijo de
multiprocessing intentando leer la terminal— e informa qué ve en cada paso.

Uso (desde una terminal de verdad, NO redirigido a un archivo):

    python3 docs/diagnostico_teclado.py

Te va a pedir que aprietes teclas. Pegá toda la salida.
"""

import multiprocessing as mp
import os
import select
import sys
import termios
import time
import tty


def describir_fd(fd):
    try:
        destino = os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        destino = "(no existe)"
    try:
        tty_ = os.isatty(fd)
    except OSError:
        tty_ = "?"
    return f"fd {fd} -> {destino}   isatty={tty_}"


def hijo(cola):
    """Corre como proceso hijo de multiprocessing, igual que el display."""
    info = {}
    info["pid"] = os.getpid()
    info["pgid"] = os.getpgrp()
    info["sid"] = os.getsid(0)
    info["pgid de la terminal (foreground)"] = _pgid_terminal()
    info["fd 0"] = describir_fd(0)
    info["sys.stdin"] = repr(sys.stdin)
    try:
        info["sys.stdin.fileno()"] = sys.stdin.fileno()
    except Exception as e:
        info["sys.stdin.fileno()"] = f"error: {e}"

    # ¿se puede abrir /dev/tty?
    try:
        fd = os.open("/dev/tty", os.O_RDONLY | os.O_NOCTTY)
        info["/dev/tty"] = "ABRE OK -> " + describir_fd(fd)
        os.close(fd)
    except OSError as e:
        info["/dev/tty"] = f"FALLA: {e}"

    cola.put(info)


def _pgid_terminal():
    """Qué grupo de procesos tiene el control de la terminal ahora mismo."""
    for fd in (0, 1, 2):
        try:
            return os.tcgetpgrp(fd)
        except OSError:
            continue
    return "no se pudo averiguar"


def leer_teclas_en_hijo(cola, fd_heredado):
    """
    Igual que el display: proceso hijo, modo cbreak, leer teclas.
    Prueba las dos vías (fd heredado y /dev/tty) y reporta cuál funciona.
    """
    resultados = {}

    for nombre, obtener in [
        ("fd heredado del padre", lambda: fd_heredado),
        ("/dev/tty", lambda: os.open("/dev/tty", os.O_RDONLY | os.O_NOCTTY)),
    ]:
        try:
            fd = obtener()
            if fd is None:
                resultados[nombre] = "no disponible"
                continue
            previo = termios.tcgetattr(fd)
            tty.setcbreak(fd)
            recibidas = []
            fin = time.time() + 4
            while time.time() < fin:
                r, _, _ = select.select([fd], [], [], 0.2)
                if r:
                    b = os.read(fd, 1)
                    if not b:
                        resultados[nombre] = "EOF (fd cerrado o /dev/null)"
                        break
                    recibidas.append(b.decode("utf-8", "replace"))
            else:
                resultados[nombre] = (f"{len(recibidas)} teclas: {recibidas!r}"
                                      if recibidas else "0 teclas en 4 s")
            termios.tcsetattr(fd, termios.TCSADRAIN, previo)
        except Exception as e:
            resultados[nombre] = f"error: {type(e).__name__}: {e}"

    cola.put(resultados)


def main():
    mp.set_start_method("fork", force=True)

    print("=" * 68)
    print("DIAGNÓSTICO DEL TECLADO")
    print("=" * 68)
    print(f"\nTERM = {os.environ.get('TERM')!r}")
    print(f"Python {sys.version.split()[0]}")

    print("\n--- PROCESO PADRE ---")
    print(f"  pid={os.getpid()}  pgid={os.getpgrp()}  sid={os.getsid(0)}")
    print(f"  pgid con control de la terminal: {_pgid_terminal()}")
    for fd in (0, 1, 2):
        print(f"  {describir_fd(fd)}")

    print("\n--- PROCESO HIJO (multiprocessing, igual que el display) ---")
    cola = mp.Queue()
    p = mp.Process(target=hijo, args=(cola,))
    p.start()
    for clave, valor in cola.get().items():
        print(f"  {clave}: {valor}")
    p.join()

    if not os.isatty(0):
        print("\n  !! El fd 0 NO es una terminal. Estás corriendo esto")
        print("     redirigido o dentro de algo que no da tty. Corrélo")
        print("     directamente en una terminal.")
        return

    print("\n--- PRUEBA REAL DE LECTURA ---")
    print("  Voy a lanzar un proceso hijo que intenta leer teclas por dos vías.")
    print("  APRETÁ VARIAS TECLAS (por ejemplo 2 3 4 5) durante los próximos")
    print("  8 segundos, en esta misma ventana.\n")
    time.sleep(1)

    fd_dup = os.dup(0)
    cola2 = mp.Queue()
    p2 = mp.Process(target=leer_teclas_en_hijo, args=(cola2, fd_dup))
    p2.start()
    resultados = cola2.get()
    p2.join()

    print("\n  RESULTADOS:")
    for via, r in resultados.items():
        marca = "OK   " if "teclas:" in str(r) else "FALLA"
        print(f"    {marca}  {via}: {r}")

    print("\n" + "=" * 68)
    print("Pegá TODA esta salida.")
    print("=" * 68)


if __name__ == "__main__":
    main()
