"""
fds.py — Analizador de la vista 3 (File descriptors). Default: 5 s.

Lista los FDs abiertos de cada proceso y su destino.

Por qué 5 s: cada FD requiere un readlink() aparte. Un proceso con 200 FDs
son 200 syscalls, y hay procesos con miles. Es el analizador con más
syscalls por muestra (aunque memoria.py mueve más bytes).

Permisos: /proc/<pid>/fd/ es modo 0500 y pertenece al dueño del proceso. Solo
vas a ver los FDs de TUS procesos, salvo que corras como root. Con procesos
ajenos, procfs.listar_fds() devuelve None y esa entrada queda marcada como
'sin_permiso' para poder distinguirla en la TUI de "no tiene FDs".
"""

import procfs

# Cuántos FDs se guardan por proceso. Sin tope, un proceso con 4000 FDs
# abiertos haría que el snapshot pese megabytes y que cada round-trip del
# Manager (pickle + socket) se vuelva lento.
#
# El modo verbose (SIGUSR2) sube el tope: es exactamente lo que pide la
# consigna con "más detalle en cada proceso, ej: más FDs visibles". Se paga
# con un snapshot más pesado, por eso no es el default.
TOPE_NORMAL = 32
TOPE_VERBOSE = 256


def extraer(pid, verbose=False):
    """FDs de un proceso, o None si murió."""
    tope = TOPE_VERBOSE if verbose else TOPE_NORMAL
    lista = procfs.listar_fds(pid)

    if lista is None:
        # Puede ser que el proceso murió, o que es de otro usuario. Se
        # distingue chequeando si el proceso sigue existiendo.
        if procfs.leer_stat(pid) is None:
            return None  # murió: no aparece en esta muestra
        return {"pid": pid, "sin_permiso": True, "total": None, "fds": [],
                "por_tipo": {}}

    por_tipo = {}
    for f in lista:
        por_tipo[f["tipo"]] = por_tipo.get(f["tipo"], 0) + 1

    return {
        "pid": pid,
        "sin_permiso": False,
        "total": len(lista),
        "truncado": len(lista) > tope,
        "verbose": verbose,
        "fds": lista[:tope],
        # Conteo por tipo: sirve para la vista resumida y no depende del tope.
        # 'pipe' y 'socket' son los interesantes para la materia: un pipe es
        # literalmente lo de clase 5, y el número entre corchetes es el inode
        # del pipe — dos procesos con el MISMO inode están conectados.
        "por_tipo": por_tipo,
    }
