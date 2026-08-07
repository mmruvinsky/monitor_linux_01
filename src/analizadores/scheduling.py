"""
scheduling.py — Analizador de la vista 6 (Scheduling). Default: 10 s.

Nice, prioridad, política, afinidad de CPU, context switches, y sesión/grupo
de procesos.

Por qué 10 s: nice y policy se setean al arrancar el proceso y casi nunca
cambian. Los context switches sí cambian todo el tiempo, pero como se
muestran acumulados (no como tasa), no hace falta refrescarlos rápido.
"""

import procfs

# Políticas de scheduling: /proc/<pid>/stat campo 41.
POLITICAS = {
    0: "SCHED_OTHER",     # el default. Round-robin con prioridad dinámica
                          # (CFS en Linux moderno). Todos los procesos
                          # normales usan este.
    1: "SCHED_FIFO",      # tiempo real: corre hasta que se bloquea o cede.
                          # NO lo interrumpe otro de la misma prioridad.
    2: "SCHED_RR",        # tiempo real con quantum: como FIFO pero rota
                          # entre los de igual prioridad.
    3: "SCHED_BATCH",     # para trabajos batch: el scheduler asume que es
                          # CPU-bound y lo penaliza en interactividad.
    5: "SCHED_IDLE",      # prioridad mínima: solo corre si no hay nada más.
    6: "SCHED_DEADLINE",  # tiempo real con deadline explícito.
}


def extraer(pid):
    """Datos de scheduling de un proceso, o None si murió."""
    campos = procfs.leer_stat(pid)
    if campos is None:
        return None

    status = procfs.leer_status(pid) or {}
    politica = _int(procfs.campo_stat(campos, procfs.STAT_POLICY))

    return {
        "pid": pid,
        # NICE: de -20 (más prioritario) a 19 (menos). Solo root puede bajarlo.
        # Es una SUGERENCIA al scheduler, no una garantía.
        "nice": _int(procfs.campo_stat(campos, procfs.STAT_NICE)),
        # PRIORITY: el valor interno del kernel. Para SCHED_OTHER va de 100 a
        # 139 y se muestra como prio-100... pero /proc lo da como nice+20.
        # Para tiempo real es negativo. Es confuso a propósito: el número que
        # importa es nice.
        "priority": _int(procfs.campo_stat(campos, procfs.STAT_PRIORITY)),
        "policy": politica,
        "policy_nombre": POLITICAS.get(politica, f"?({politica})"),
        "rt_priority": _int(procfs.campo_stat(campos, procfs.STAT_RT_PRIORITY)),
        # Afinidad: en qué cores PUEDE correr este proceso. Se setea con
        # taskset. '0-15' significa cualquiera de los 16.
        "affinity": status.get("Cpus_allowed_list"),

        # ------------------------------------------------------------------
        # CONTEXT SWITCHES — el par de números más informativo de esta vista
        # ------------------------------------------------------------------
        # VOLUNTARIO: el proceso MISMO cedió la CPU porque necesitaba esperar
        # algo (sleep, I/O, un lock ocupado). Le dijo al kernel "sacame".
        "vol_ctxt": _int(status.get("voluntary_ctxt_switches")),
        # INVOLUNTARIO: el kernel se la ARREBATÓ, porque se acabó el quantum
        # o apareció algo más prioritario. El proceso no pidió nada.
        "invol_ctxt": _int(status.get("nonvoluntary_ctxt_switches")),
        # Regla práctica: muchos voluntarios -> I/O-bound.
        #                 muchos involuntarios -> CPU-bound.
        # Un `while True: pass` tiene 0 voluntarios y miles de involuntarios.

        "utime": _int(procfs.campo_stat(campos, procfs.STAT_UTIME)),
        "stime": _int(procfs.campo_stat(campos, procfs.STAT_STIME)),

        # SESIÓN y GRUPO DE PROCESOS. Verificado contra man 5 proc: pgrp es el
        # campo 5 y session el 6 (la consigna dice 6-7, está corrida en uno;
        # comprobable con `ps -o pid,pgid,sid`).
        #
        # Estos dos números explican por qué Ctrl+C mata a todo un pipeline:
        # la terminal manda SIGINT al GRUPO DE PROCESOS en foreground, no a
        # un proceso. Todos los que comparten PGID la reciben.
        "pgid": _int(procfs.campo_stat(campos, procfs.STAT_PGRP)),
        "sid": _int(procfs.campo_stat(campos, procfs.STAT_SESSION)),
    }


def _int(valor):
    try:
        return int(valor)
    except (TypeError, ValueError):
        return 0
