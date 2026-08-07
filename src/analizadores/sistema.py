"""
sistema.py — Analizador de la vista 7 (Sistema global). Default: 2 s.

Es el único de los 7 que NO itera sobre PIDs: lee cuatro archivos globales
(/proc/stat, /proc/meminfo, /proc/loadavg, /proc/uptime). Por eso su costo es
constante y no crece con la cantidad de procesos, y por eso `base.correr()`
lo lanza con por_pid=False.

Igual que resumen.py, entrega los jiffies de CPU CRUDOS. El porcentaje de uso
del sistema también necesita dos lecturas, y también lo calcula el agregador.
"""

import procfs


def extraer(verbose=False):
    """
    Devuelve el estado global del sistema, o {} si /proc no responde.
    No recibe pid: base.correr() lo llama sin argumentos.
    """
    stat = procfs.leer_stat_global()
    meminfo = procfs.leer_meminfo()
    load = procfs.leer_loadavg()
    uptime = procfs.leer_uptime()

    datos = {}

    if stat:
        # Jiffies acumulados desde el boot, sin procesar. El agregador saca
        # el delta contra la muestra anterior.
        datos["cpu_jiffies"] = stat.get("cpu", {})
        datos["cpus_jiffies"] = stat.get("cpus", {})
        datos["btime"] = stat.get("btime")
        # OJO: 'processes' es el contador ACUMULADO de forks desde el boot,
        # no la cantidad de procesos vivos. Se renombra para que nadie lo
        # confunda al leer el snapshot.
        datos["forks_desde_boot"] = stat.get("processes")
        datos["procs_running"] = stat.get("procs_running")
        datos["procs_blocked"] = stat.get("procs_blocked")

    if meminfo:
        datos["mem"] = {
            "total_kb": meminfo.get("MemTotal", 0),
            "libre_kb": meminfo.get("MemFree", 0),
            "disponible_kb": meminfo.get("MemAvailable", 0),
            "buffers_kb": meminfo.get("Buffers", 0),
            "cached_kb": meminfo.get("Cached", 0),
            # usada = total - disponible, NO total - libre. El kernel usa
            # toda la RAM ociosa como cache de disco y la libera al instante
            # si alguien la necesita; contarla como "usada" reporta la
            # máquina al 93% cuando está al 62%.
            "usada_kb": procfs.memoria_usada_kb(meminfo),
            "swap_total_kb": meminfo.get("SwapTotal", 0),
            "swap_libre_kb": meminfo.get("SwapFree", 0),
        }

    if load:
        datos["load"] = load

    if uptime:
        datos["uptime"] = uptime

    return datos
