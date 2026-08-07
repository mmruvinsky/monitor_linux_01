"""
memoria.py — Analizador de la vista 2 (Memoria). Default: 3 s.

Extrae los campos Vm* de status, los page faults de stat, y los segmentos
agrupados de maps.

Por qué el intervalo default es 3 s y no 2 s: este es el analizador MÁS CARO
de los 7. /proc/<pid>/maps tiene una línea por región de memoria —un Chrome
puede tener 1500— y el kernel las genera todas en cada lectura. Multiplicado
por ~460 procesos es el mayor consumo de CPU del monitor.
"""

import procfs

# Campos Vm* de /proc/<pid>/status y qué significa cada uno.
CAMPOS_VM = {
    "VmSize": "virtual_kb",   # espacio de direcciones TOTAL reservado. Incluye
                              # memoria que nunca se tocó: no es lo que ocupa.
    "VmRSS": "rss_kb",        # Resident Set Size: páginas realmente EN RAM.
                              # Este es el número que importa.
    "VmData": "data_kb",      # segmento de datos + heap
    "VmStk": "stack_kb",      # stack
    "VmExe": "text_kb",       # código del ejecutable
    "VmLib": "lib_kb",        # bibliotecas compartidas mapeadas
    "VmHWM": "hwm_kb",        # High Water Mark: el RSS MÁXIMO histórico.
                              # Sirve para ver picos que ya pasaron.
    "VmSwap": "swap_kb",      # páginas mandadas a swap
    "VmPTE": "pte_kb",        # tablas de páginas (el costo de mapear tanto)
}


def extraer(pid):
    """Datos de memoria de un proceso, o None si murió."""
    status = procfs.leer_status(pid)
    if status is None:
        return None

    datos = {"pid": pid}
    for campo, clave in CAMPOS_VM.items():
        datos[clave] = _kb(status.get(campo))

    # Page faults desde /proc/<pid>/stat, acumulados desde que arrancó.
    campos = procfs.leer_stat(pid)
    if campos:
        # MINOR fault: la página no estaba mapeada en la tabla de páginas del
        # proceso, pero SÍ estaba en RAM (por ejemplo, una página compartida
        # que otro proceso ya cargó, o una página COW al escribirla). Es
        # barato: no toca el disco.
        datos["minflt"] = _int(procfs.campo_stat(campos, procfs.STAT_MINFLT))
        # MAJOR fault: hubo que ir al DISCO a buscarla. Es órdenes de magnitud
        # más caro. Muchos major faults = el proceso está paginando y la
        # máquina está corta de RAM.
        datos["majflt"] = _int(procfs.campo_stat(campos, procfs.STAT_MAJFLT))
        datos["cminflt"] = _int(procfs.campo_stat(campos, procfs.STAT_CMINFLT))
        datos["cmajflt"] = _int(procfs.campo_stat(campos, procfs.STAT_CMAJFLT))

    # Segmentos desde maps. Devuelve None para procesos de otros usuarios:
    # maps es legible solo por el dueño (a diferencia de status y stat, que
    # son públicos). Eso NO es un bug, es aislamiento del kernel.
    regiones = procfs.leer_maps(pid)
    if regiones is not None:
        datos["segmentos"] = procfs.agrupar_segmentos(regiones)
        datos["regiones_totales"] = len(regiones)
    else:
        datos["segmentos"] = None
        datos["regiones_totales"] = None

    return datos


def _kb(valor):
    """'4352 kB' -> 4352"""
    if not valor:
        return None
    try:
        return int(valor.split()[0])
    except (ValueError, IndexError):
        return None


def _int(valor):
    try:
        return int(valor)
    except (TypeError, ValueError):
        return 0
