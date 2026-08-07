"""
agregador.py — El único proceso con MEMORIA entre iteraciones.

Consume las muestras crudas de las 7 colas, calcula todo lo que requiere
comparar dos instantes, y escribe el resultado al snapshot.

===========================================================================
POR QUÉ ESTE PROCESO EXISTE
===========================================================================
El proceso servidor del Manager ya guarda estado global, así que podría
parecer que los analizadores podrían escribir directo al snapshot y este
proceso sobraría.

No alcanza, y la razón es concreta: EL %CPU NO EXISTE EN /proc. Lo que hay es
un contador acumulado de jiffies desde que el proceso arrancó. Para sacar el
porcentaje actual hay que hacer:

    %CPU = (jiffies₂ - jiffies₁) / HZ / (t₂ - t₁) × 100

El Manager no recuerda nada: solo guarda el último valor escrito. Alguien
tiene que sostener jiffies₁ en memoria PRIVADA. Ese alguien es este proceso.

Lo mismo vale para el resto de los derivados: top 3 por CPU y por memoria,
totales por estado, conteo de zombies. Todo eso son cálculos SOBRE las
muestras, no muestras.

Consecuencia de diseño: el agregador es el ÚNICO escritor del snapshot (salvo
la clave "pids", que escribe el recolector). Eso elimina por construcción
cualquier race condition entre escritores.
"""

import queue as queue_mod
import time

import procfs
import senales

# Cada cuánto revisar las colas cuando no hay nada. Ver la nota sobre polling
# en `correr()`.
POLL = 0.05


class Agregador:
    """
    Mantiene el estado privado necesario para los cálculos incrementales.
    Este estado NO se comparte con nadie: vive solo en este proceso.
    """

    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.hz = procfs.hz()
        # Muestra anterior de resumen: {pid: (jiffies, starttime, ts)}
        self.prev_procesos = {}
        self.prev_procesos_ts = None
        # Muestra anterior de los jiffies globales de /proc/stat
        self.prev_cpu_global = None
        # %CPU calculado por PID, para poder derivar el top 3 y para que la
        # vista Threads lo reutilice.
        self.cpu_por_pid = {}

    # ------------------------------------------------------------------
    # Despacho
    # ------------------------------------------------------------------

    def procesar(self, muestra):
        """Recibe una muestra cruda de una cola y actualiza el snapshot."""
        dimension = muestra.get("dimension")
        if dimension == "resumen":
            self.procesar_resumen(muestra)
        elif dimension == "sistema":
            self.procesar_sistema(muestra)
        else:
            # Las demás dimensiones no necesitan derivados: pasan tal cual.
            self.escribir(dimension, muestra["datos"], muestra["ts"])

    def escribir(self, dimension, datos, ts):
        """
        Escritura al snapshot. SIEMPRE se reasigna la clave de primer nivel.

        Esto NO funcionaría y lo peor es que no da error:
            self.snapshot["resumen"][pid] = dato

        snapshot es un DictProxy: solo el primer nivel es un proxy. Leer
        snapshot["resumen"] devuelve una COPIA local del dict anidado;
        modificarla y no reasignarla tira el cambio a la basura. El dato
        nunca llega al servidor y nunca aparece en pantalla.
        """
        self.snapshot[dimension] = datos
        self.snapshot[f"{dimension}_ts"] = ts

    # ------------------------------------------------------------------
    # Resumen: acá se calcula el %CPU por proceso
    # ------------------------------------------------------------------

    def procesar_resumen(self, muestra):
        ahora = muestra["ts"]
        datos = muestra["datos"]
        dt = (ahora - self.prev_procesos_ts) if self.prev_procesos_ts else 0.0

        nuevo_prev = {}
        salida = {}

        for pid, p in datos.items():
            jiffies = p.get("jiffies", 0)
            starttime = p.get("starttime", 0)
            cpu = 0.0

            anterior = self.prev_procesos.get(pid)
            if anterior and dt > 0:
                jiffies_ant, starttime_ant, _ = anterior
                # Guarda contra REUTILIZACIÓN DE PID: el kernel recicla PIDs.
                # Si un proceso muere y otro nace con el mismo número,
                # comparar sus jiffies daría un delta negativo o disparatado.
                # starttime (jiffies desde el boot hasta que arrancó) es
                # distinto para cada proceso, así que sirve de identidad.
                if starttime == starttime_ant:
                    delta = jiffies - jiffies_ant
                    if delta >= 0:
                        # / hz -> segundos de CPU;  / dt -> fracción de un core.
                        # Puede pasar de 100 si el proceso usa varios cores:
                        # eso es correcto y es lo que muestra htop.
                        cpu = 100.0 * (delta / self.hz) / dt

            fila = dict(p)
            fila["cpu"] = round(cpu, 1)
            salida[pid] = fila
            nuevo_prev[pid] = (jiffies, starttime, ahora)

        # Los PIDs que no vinieron en esta muestra murieron: se descartan del
        # estado anterior. Sin esto, prev_procesos crecería sin límite durante
        # toda la vida del monitor (leak lento pero real: en una máquina que
        # forkea mucho, miles de entradas por hora).
        self.prev_procesos = nuevo_prev
        self.prev_procesos_ts = ahora
        self.cpu_por_pid = {pid: f["cpu"] for pid, f in salida.items()}

        self.escribir("resumen", salida, ahora)
        self.escribir("derivados", self.derivar(salida), ahora)

    def derivar(self, procesos):
        """
        Cálculos que necesitan ver TODOS los procesos a la vez: totales por
        estado, zombies, top 3. Ningún analizador puede hacer esto porque
        cada uno ve solo su dimensión.
        """
        por_estado = {}
        threads_totales = 0
        for p in procesos.values():
            estado = p.get("estado") or "?"
            por_estado[estado] = por_estado.get(estado, 0) + 1
            threads_totales += p.get("threads") or 0

        def top(clave, n=3):
            ordenados = sorted(
                procesos.values(),
                key=lambda p: p.get(clave) or 0,
                reverse=True,
            )
            return [
                {"pid": p["pid"], "comm": p["comm"], clave: p.get(clave)}
                for p in ordenados[:n]
            ]

        return {
            "procesos_totales": len(procesos),
            "por_estado": por_estado,
            # Un zombie es un proceso que terminó y cuyo padre todavía no
            # llamó a wait(). El kernel conserva su entrada en la tabla solo
            # para que el padre pueda leer el código de salida.
            "zombies": por_estado.get("Z", 0),
            "threads_totales": threads_totales,
            "top_cpu": top("cpu"),
            "top_rss": top("rss_kb"),
        }

    # ------------------------------------------------------------------
    # Sistema: %CPU global, también por delta
    # ------------------------------------------------------------------

    def procesar_sistema(self, muestra):
        datos = dict(muestra["datos"])
        actual = datos.get("cpu_jiffies")

        if actual and self.prev_cpu_global:
            datos["cpu_pct"] = procfs.porcentaje_cpu_global(
                self.prev_cpu_global, actual
            )
        else:
            datos["cpu_pct"] = {}

        if actual:
            self.prev_cpu_global = actual

        # Los jiffies crudos no le sirven a la TUI y ocupan lugar en cada
        # round-trip del Manager: se sacan una vez calculado el porcentaje.
        datos.pop("cpu_jiffies", None)
        datos.pop("cpus_jiffies", None)

        self.escribir("sistema", datos, muestra["ts"])


def correr(snapshot, colas, parar):
    """
    Loop del agregador. Corre en su propio proceso.

    Sobre el polling: con UNA cola se podría hacer cola.get(timeout=...) y
    bloquearse prolijo. Con 7 no se puede bloquear en una sin ignorar las
    otras, así que se recorren las 7 con get_nowait() y se duerme un poco si
    ninguna tenía nada.

    El costo es despertar ~20 veces por segundo sin hacer nada. Es aceptable
    (son 7 llamadas no bloqueantes) y es el precio de haber elegido una cola
    por analizador para evitar la race del descartar-el-viejo. La alternativa
    sería multiprocessing.connection.wait() sobre los readers de las colas,
    que permite esperar en varias a la vez.
    """
    senales.preparar_hijo()
    agregador = Agregador(snapshot)

    while not parar.is_set():
        hubo_algo = False
        for cola in colas:
            try:
                muestra = cola.get_nowait()
            except (queue_mod.Empty, OSError, EOFError):
                continue
            hubo_algo = True
            try:
                agregador.procesar(muestra)
            except Exception as e:
                # Un analizador que devuelve algo inesperado no debe tumbar
                # al agregador: se pierde esa muestra y se sigue.
                print(f"[agregador] error procesando "
                      f"{muestra.get('dimension')}: {e!r}", flush=True)

        if not hubo_algo:
            parar.wait(timeout=POLL)
