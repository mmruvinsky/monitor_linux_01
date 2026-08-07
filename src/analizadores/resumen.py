"""
resumen.py — Analizador de la vista 1 (Resumen). Default: 2 s.

Extrae los datos básicos de cada proceso: PID, PPID, usuario, estado,
comando, cantidad de threads, RSS y los jiffies de CPU.

--------------------------------------------------------------------------
LO MÁS IMPORTANTE DE ESTE ARCHIVO: NO CALCULA EL CPU%
--------------------------------------------------------------------------
Devuelve `jiffies` crudos (utime + stime acumulados desde que el proceso
arrancó) y nada más. El porcentaje requiere comparar dos lecturas separadas
en el tiempo:

    %CPU = (jiffies₂ - jiffies₁) / HZ / (t₂ - t₁) × 100

Este proceso no guarda la lectura anterior, así que no puede calcularlo. Lo
hace el agregador, que sí tiene memoria entre iteraciones. Esa división de
responsabilidades es la razón por la que el agregador existe como proceso.
"""

import procfs

# Letras de estado de /proc/<pid>/stat, campo 3 (clase 3 de la materia).
ESTADOS = {
    "R": "running",       # ejecutando o en la cola de listos
    "S": "sleeping",      # dormido esperando algo, INTERRUMPIBLE por señales
    "D": "disk sleep",    # dormido en I/O, ININTERRUMPIBLE: no responde ni a
                          # SIGKILL hasta que el I/O termine. Un proceso pegado
                          # en D suele significar disco o NFS con problemas.
    "T": "stopped",       # detenido por SIGSTOP/SIGTSTP (Ctrl+Z)
    "t": "tracing stop",  # detenido por un debugger (ptrace)
    "Z": "zombie",        # terminó pero el padre no llamó a wait() todavía
    "X": "dead",
    "I": "idle",          # kernel thread ocioso
}


def extraer(pid):
    """
    Devuelve el resumen de un proceso, o None si murió / no se puede leer.

    Se hacen DOS lecturas (stat y status) que no son atómicas entre sí: el
    proceso puede morir entre una y otra. Si falla stat descartamos el
    proceso entero; si falla solo status seguimos con lo que tenemos y
    dejamos los campos faltantes en None. Es una decisión de diseño: mostrar
    una fila incompleta por un frame es preferible a que el proceso
    parpadee fuera de la lista.
    """
    campos = procfs.leer_stat(pid)
    if campos is None:
        return None

    estado = procfs.campo_stat(campos, procfs.STAT_STATE)
    utime = _int(procfs.campo_stat(campos, procfs.STAT_UTIME))
    stime = _int(procfs.campo_stat(campos, procfs.STAT_STIME))

    status = procfs.leer_status(pid)
    uid = procfs.uid_de_status(status)

    return {
        "pid": pid,
        "ppid": _int(procfs.campo_stat(campos, procfs.STAT_PPID)),
        "comm": procfs.campo_stat(campos, procfs.STAT_COMM),
        "cmdline": procfs.nombre_proceso(pid),
        "estado": estado,
        "estado_desc": ESTADOS.get(estado, "?"),
        "uid": uid,
        "usuario": procfs.usuario_de_uid(uid) if uid is not None else "?",
        # utime + stime: tiempo en modo usuario MÁS tiempo en modo kernel.
        # Se suman porque para "cuánta CPU consume este proceso" las dos
        # cuentan; la vista Scheduling los muestra separados.
        "jiffies": utime + stime,
        "utime": utime,
        "stime": stime,
        "threads": _int(status.get("Threads")) if status else None,
        "rss_kb": _kb(status.get("VmRSS")) if status else None,
        # starttime sirve para detectar REUTILIZACIÓN DE PID: si un proceso
        # muere y el kernel le da el mismo PID a uno nuevo, el agregador
        # compararía jiffies de dos procesos distintos y daría un CPU%
        # absurdo. Comparando starttime se detecta que es otro proceso.
        "starttime": _int(procfs.campo_stat(campos, procfs.STAT_STARTTIME)),
    }


def _int(valor):
    try:
        return int(valor)
    except (TypeError, ValueError):
        return 0


def _kb(valor):
    """'4352 kB' -> 4352"""
    if not valor:
        return None
    try:
        return int(valor.split()[0])
    except (ValueError, IndexError):
        return None
