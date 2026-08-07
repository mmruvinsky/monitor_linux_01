"""
senales.py (analizador) — Vista 5 (Señales). Default: 10 s.

OJO CON EL NOMBRE: este archivo lee las máscaras de señales de los procesos
OBSERVADOS. No tiene nada que ver con src/senales.py, que maneja las señales
que recibe el monitor. No se importan entre sí.

Por qué 10 s: las máscaras de señales de un proceso se definen al arrancar y
casi nunca cambian. Refrescarlas cada 2 s sería gastar CPU para ver el mismo
número.

--------------------------------------------------------------------------
LOS TRES ESTADOS DE UNA SEÑAL
--------------------------------------------------------------------------
  BLOQUEADA (SigBlk) : llega y queda PENDIENTE. No se actúa sobre ella, pero
                       no se pierde: si el proceso la desbloquea, se entrega.
  IGNORADA  (SigIgn) : llega y se descarta. Se pierde para siempre.
  MANEJADA  (SigCgt) : el proceso instaló un handler propio.

Y las pendientes:
  SigPnd : pendientes para ESTE thread
  ShdPnd : pendientes para todo el thread group (shared)

SIGKILL (9) y SIGSTOP (19) nunca aparecen en SigBlk ni SigIgn ni SigCgt: el
kernel no permite bloquearlas, ignorarlas ni manejarlas. Siempre tiene que
haber una forma de matar y de detener un proceso.

Probalo con tu propio monitor corriendo:
    grep -E 'Sig(Blk|Ign|Cgt|Pnd)' /proc/<pid de un analizador>/status
"""

import procfs

CAMPOS = {
    "SigBlk": "bloqueadas",
    "SigIgn": "ignoradas",
    "SigCgt": "manejadas",
    "SigPnd": "pendientes",
    "ShdPnd": "pendientes_grupo",
}


def extraer(pid):
    """Máscaras de señales decodificadas, o None si el proceso murió."""
    status = procfs.leer_status(pid)
    if status is None:
        return None

    datos = {"pid": pid}
    for campo, clave in CAMPOS.items():
        crudo = status.get(campo)
        datos[clave] = procfs.decodificar_mascara(crudo)
        # Se guarda también el hexadecimal original: la TUI lo muestra al
        # lado del nombre y sirve para verificar a mano contra /proc.
        datos[clave + "_hex"] = crudo

    # Señales que este proceso NO puede recibir de forma efectiva: bloqueadas
    # más ignoradas. Útil para explicar por qué un `kill` no hizo nada.
    datos["sordas"] = sorted(set(datos["bloqueadas"]) | set(datos["ignoradas"]))
    return datos
