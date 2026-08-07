"""
threads.py — Analizador de la vista 4 (Threads / LWPs). Default: 2 s.

Lista los threads de cada proceso leyendo /proc/<pid>/task/<tid>/.

--------------------------------------------------------------------------
EL CONCEPTO QUE ESTA VISTA HACE VISIBLE
--------------------------------------------------------------------------
Para Linux no existen "procesos" y "threads" como cosas distintas: existen
TASKS, y cada task tiene su propio TID. Lo que ps muestra como "PID" es en
realidad el TGID (thread group id), que es el TID del PRIMER task del grupo.

Por eso el thread principal siempre tiene TID == PID: no es que herede el
PID, es que el PID ES el TID del thread principal.

Verificalo en vivo:
    ls /proc/$$/task/          # tu shell: un solo task
    grep -E '^(Pid|Tgid)' /proc/<pid>/task/<tid>/status

Igual que resumen.py, este analizador entrega jiffies CRUDOS por thread. El
%CPU por thread lo calcula el agregador, que guarda la muestra anterior.
"""

import procfs

# Tope de threads guardados por proceso. Un navegador puede tener 300+ y el
# snapshot viaja entero por el socket del Manager en cada actualización.
TOPE_THREADS = 64


def extraer(pid):
    """Threads de un proceso, o None si murió."""
    tids = procfs.listar_tids(pid)
    if not tids:
        return None

    threads = []
    for tid in sorted(tids)[:TOPE_THREADS]:
        campos = procfs.leer_stat(pid, tid=tid)
        if campos is None:
            continue  # el thread terminó entre el listdir y la lectura

        utime = _int(procfs.campo_stat(campos, procfs.STAT_UTIME))
        stime = _int(procfs.campo_stat(campos, procfs.STAT_STIME))
        status = procfs.leer_status(pid, tid=tid)

        threads.append({
            "tid": tid,
            # El nombre del thread es propio de cada task: un programa puede
            # renombrarlos con prctl(PR_SET_NAME) para que aparezcan como
            # 'Chrome_IOThread' en vez de 'chrome'. El comm de stat ya lo trae.
            "nombre": procfs.campo_stat(campos, procfs.STAT_COMM),
            "estado": procfs.campo_stat(campos, procfs.STAT_STATE),
            "jiffies": utime + stime,
            "utime": utime,
            "stime": stime,
            # TID == PID identifica al thread principal.
            "principal": tid == pid,
            "vol_ctxt": _int(status.get("voluntary_ctxt_switches")) if status else 0,
            "invol_ctxt": _int(status.get("nonvoluntary_ctxt_switches")) if status else 0,
        })

    if not threads:
        return None

    return {
        "pid": pid,
        "total": len(tids),
        "truncado": len(tids) > TOPE_THREADS,
        "threads": threads,
    }


def _int(valor):
    try:
        return int(valor)
    except (TypeError, ValueError):
        return 0
