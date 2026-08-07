"""
recolector.py — Descubre qué procesos existen y publica la lista.

Es el componente más simple del sistema y a propósito: su única
responsabilidad es enumerar. No lee el contenido de ningún /proc/<pid>/, no
sabe qué es el RSS ni el nice.

¿Por qué es un proceso aparte, si un os.listdir() es baratísimo?

No es por performance. Es porque los 7 analizadores tienen que trabajar sobre
LA MISMA lista de PIDs. Si cada uno listara /proc por su cuenta, con ritmos
distintos (2s, 3s, 5s, 10s), las 7 vistas mostrarían conjuntos ligeramente
diferentes de procesos y sería imposible cruzar los datos entre vistas: la
vista Memoria tendría un PID que la vista Resumen no, y viceversa.

Tener un único punto de descubrimiento da una fuente de verdad consistente.
"""

import time

import procfs
import senales


def correr(snapshot, intervalo, parar):
    """
    Loop principal del recolector. Corre en su propio proceso.

    Parámetros (todos creados por main.py ANTES del fork, y heredados):
      snapshot : DictProxy del Manager. Acá se escribe la clave "pids".
      intervalo: Value('d') con el período en segundos (recargable por SIGHUP).
      parar    : Event compartido. Se setea para pedir el shutdown.
    """
    senales.preparar_hijo()

    while not parar.is_set():
        t0 = time.monotonic()

        pids = procfs.listar_pids()

        # ------------------------------------------------------------------
        # SE REASIGNA LA CLAVE ENTERA. Esto no es estilo, es obligatorio.
        #
        # snapshot es un DictProxy, no un dict. Cada operación viaja por un
        # socket al proceso servidor del Manager. Solo el PRIMER NIVEL es un
        # proxy: lo que devuelve snapshot["pids"] es una COPIA local.
        #
        # Por eso esto NO funcionaría (y lo peor: no da error):
        #     snapshot["pids"].append(nuevo)   # modifica una copia y la tira
        #
        # Al reasignar la clave completa, la escritura viaja entera al
        # servidor y además es atómica desde el punto de vista de los
        # lectores: nunca ven media lista.
        # ------------------------------------------------------------------
        snapshot["pids"] = pids
        snapshot["pids_ts"] = time.time()

        # Dormir de forma INTERRUMPIBLE. Con time.sleep() pelado, el
        # recolector tardaría hasta un intervalo entero en enterarse de que
        # hay que parar. Event.wait() devuelve apenas alguien hace set().
        with intervalo.get_lock():
            periodo = intervalo.value
        restante = periodo - (time.monotonic() - t0)
        if restante > 0:
            parar.wait(timeout=restante)
