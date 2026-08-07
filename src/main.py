#!/usr/bin/env python3
"""
main.py — Entry point del monitor. Es el proceso PADRE de todo.

Responsabilidad única: ciclo de vida. Crea las primitivas de IPC, lanza los
hijos, atiende señales y orquesta el shutdown. No lee /proc, no dibuja, no
calcula nada; si algo de eso aparece acá, está mal ubicado.

Uso:
    python3 src/main.py            # modo normal (por ahora, sin TUI)
    python3 src/main.py --debug    # imprime el snapshot cada segundo

Señales (mandarlas al PID que imprime al arrancar):
    kill -INT  <pid>   shutdown limpio     (o Ctrl+C)
    kill -TERM <pid>   shutdown limpio
    kill -HUP  <pid>   recarga config.json
    kill -USR1 <pid>   dump a dump_<ts>.json
    kill -USR2 <pid>   toggle verbose
"""

import json
import multiprocessing as mp
import os
import signal
import sys
import time

import config
import recolector
import senales

# Cuánto se espera en cada escalón del shutdown, en segundos.
ESPERA_COOPERATIVA = 2.0   # a que los hijos vean el Event y salgan solos
ESPERA_SIGTERM = 1.0       # a que reaccionen al SIGTERM


class Monitor:
    """Contiene el estado del padre: config, IPC y la lista de hijos."""

    def __init__(self, debug=False):
        self.debug = debug
        self.cfg = config.cargar()
        self.hijos = {}          # nombre -> Process
        self.corriendo = True

        # ------------------------------------------------------------------
        # TODAS las primitivas de IPC se crean ACÁ, antes del primer fork().
        #
        # Con start_method='fork' los hijos heredan el espacio de direcciones
        # del padre por Copy-on-Write, así que reciben estos objetos ya
        # construidos sin tener que serializarlos. Si se crearan después del
        # fork, cada hijo tendría los suyos y no se comunicarían con nadie.
        # ------------------------------------------------------------------
        self.mgr = mp.Manager()          # levanta su propio proceso servidor
        self.snapshot = self.mgr.dict()  # DictProxy: el snapshot global
        self.parar = mp.Event()          # flag cooperativo de shutdown

        # Un Value('d') por analizador, más uno para el recolector. Son el
        # canal display -> analizador para ajustar intervalos con +/-.
        self.intervalos = {
            nombre: mp.Value("d", self.cfg["intervalos"][nombre])
            for nombre in ["recolector"] + config.DIMENSIONES
        }

        # Flag de verbose, toggleable con SIGUSR2. Un solo byte compartido:
        # Value('b') y no Manager, porque es un booleano y no justifica un
        # round-trip por socket.
        self.verbose = mp.Value("b", 1 if self.cfg["verbose"] else 0)

        self.canal = senales.CanalSenales()

    # ----------------------------------------------------------------------
    # Arranque
    # ----------------------------------------------------------------------

    def lanzar(self, nombre, funcion, args):
        """
        Lanza un hijo y lo registra.

        daemon=False a propósito: un proceso daemon de multiprocessing es
        matado abruptamente cuando el padre sale, sin darle chance de limpiar.
        Como acá el shutdown es ordenado y explícito, no queremos ese atajo.
        """
        p = mp.Process(target=funcion, args=args, name=nombre, daemon=False)
        p.start()
        self.hijos[nombre] = p
        self.log(f"lanzado {nombre} (pid {p.pid})")
        return p

    def arrancar(self):
        fd = self.canal.instalar()
        self.log(f"monitor arrancado, pid {os.getpid()}, pgid {os.getpgrp()}")

        self.lanzar(
            "recolector",
            recolector.correr,
            (self.snapshot, self.intervalos["recolector"], self.parar),
        )

        # TODO: los 7 analizadores, el agregador y el display.

        return fd

    # ----------------------------------------------------------------------
    # Loop principal: no hace nada salvo esperar señales
    # ----------------------------------------------------------------------

    def loop(self):
        ultimo_debug = 0.0
        while self.corriendo:
            # esperar() se bloquea en select() sobre el self-pipe. El timeout
            # hace que despertemos igual cada 0.5 s para vigilar a los hijos.
            for signum in self.canal.esperar(timeout=0.5):
                self.despachar(signum)

            self.vigilar_hijos()

            if self.debug and time.time() - ultimo_debug > 1.0:
                ultimo_debug = time.time()
                self.imprimir_debug()

    def despachar(self, signum):
        """
        Acá SÍ se puede hacer trabajo pesado: estamos en el loop principal,
        fuera del contexto de señal, con el proceso en estado consistente.
        Esta es toda la razón de ser del self-pipe.
        """
        nombre = senales.nombre(signum)
        self.log(f"señal recibida: {nombre}")

        if signum in (signal.SIGINT, signal.SIGTERM):
            self.corriendo = False

        elif signum == signal.SIGHUP:
            self.recargar_config()

        elif signum == signal.SIGUSR1:
            self.volcar_snapshot()

        elif signum == signal.SIGUSR2:
            with self.verbose.get_lock():
                self.verbose.value = 0 if self.verbose.value else 1
                estado = "ON" if self.verbose.value else "OFF"
            self.log(f"verbose {estado}")

        elif signum == signal.SIGWINCH:
            self.log("terminal redimensionada (el display repinta)")

    # ----------------------------------------------------------------------
    # Acciones de las señales
    # ----------------------------------------------------------------------

    def recargar_config(self):
        """
        SIGHUP: relee config.json y escribe los intervalos nuevos en los
        Value compartidos. Los hijos los leen en su siguiente vuelta, así que
        el cambio se aplica sin reiniciar nada.
        """
        nueva = config.cargar()
        cambios = []
        for nombre, valor in nueva["intervalos"].items():
            compartido = self.intervalos.get(nombre)
            if compartido is None:
                continue
            with compartido.get_lock():
                if abs(compartido.value - valor) > 1e-9:
                    cambios.append(f"{nombre}: {compartido.value}s -> {valor}s")
                    compartido.value = valor
        self.cfg = nueva
        self.log("config recargada" + (": " + ", ".join(cambios) if cambios else " (sin cambios)"))

    def volcar_snapshot(self):
        """
        SIGUSR1: escribe el snapshot completo a dump_<timestamp>.json.

        dict(self.snapshot) fuerza una copia local completa del DictProxy: sin
        eso, json.dump() recibiría un proxy y no sabría serializarlo.
        """
        ruta = f"dump_{time.strftime('%Y%m%d_%H%M%S')}.json"
        try:
            with open(ruta, "w", encoding="utf-8") as f:
                json.dump(dict(self.snapshot), f, indent=2, default=str)
            self.log(f"snapshot volcado a {ruta}")
        except OSError as e:
            self.log(f"no se pudo volcar el snapshot: {e}")

    # ----------------------------------------------------------------------
    # Supervisión y shutdown
    # ----------------------------------------------------------------------

    def vigilar_hijos(self):
        """
        Detecta hijos que murieron por su cuenta (crash, kill -9 externo).

        is_alive() internamente hace un waitpid no bloqueante, así que además
        de detectar la muerte COSECHA al hijo y evita que quede zombie.
        """
        for nombre, p in list(self.hijos.items()):
            if not p.is_alive():
                self.log(f"¡{nombre} (pid {p.pid}) murió! exitcode={p.exitcode}")
                del self.hijos[nombre]

    def apagar(self):
        """
        Shutdown en escalones, de lo más suave a lo más brutal.

        1. Event: aviso cooperativo. El hijo sale por su cuenta en un punto
           seguro de su loop, después de terminar lo que estaba haciendo.
        2. SIGTERM (terminate): el hijo NO lo bloquea. Muerte inmediata pero
           interceptable.
        3. SIGKILL (kill): último recurso. No se puede bloquear ni manejar;
           el hijo no alcanza a limpiar nada.
        4. join(): OBLIGATORIO. Es el wait() de multiprocessing. Sin esto los
           hijos quedan en estado Z y el propio monitor los mostraría como
           zombies en su vista Sistema.
        """
        self.log("iniciando shutdown")

        # 1. cooperativo
        self.parar.set()
        limite = time.monotonic() + ESPERA_COOPERATIVA
        for p in self.hijos.values():
            p.join(timeout=max(0.0, limite - time.monotonic()))

        # 2. SIGTERM a los que siguen vivos
        rezagados = [(n, p) for n, p in self.hijos.items() if p.is_alive()]
        for nombre, p in rezagados:
            self.log(f"{nombre} no salió solo, mando SIGTERM")
            p.terminate()
        limite = time.monotonic() + ESPERA_SIGTERM
        for _, p in rezagados:
            p.join(timeout=max(0.0, limite - time.monotonic()))

        # 3. SIGKILL a los tercos
        for nombre, p in self.hijos.items():
            if p.is_alive():
                self.log(f"{nombre} ignoró SIGTERM, mando SIGKILL")
                p.kill()

        # 4. cosecha final: sin esto quedan zombies
        for nombre, p in self.hijos.items():
            p.join()
            self.log(f"{nombre} cosechado (exitcode {p.exitcode})")

        self.mgr.shutdown()
        self.canal.cerrar()
        self.log("shutdown completo")

    # ----------------------------------------------------------------------
    # Utilidades
    # ----------------------------------------------------------------------

    def log(self, mensaje):
        print(f"[{time.strftime('%H:%M:%S')}] {mensaje}", flush=True)

    def imprimir_debug(self):
        pids = self.snapshot.get("pids", [])
        ts = self.snapshot.get("pids_ts", 0)
        edad = time.time() - ts if ts else -1
        print(
            f"    snapshot: {len(pids)} pids, "
            f"antigüedad {edad:.1f}s, "
            f"claves={sorted(self.snapshot.keys())}",
            flush=True,
        )


def main():
    # fork explícito: es el default en Linux, pero dejarlo escrito documenta
    # la decisión. Con 'spawn' habría que pickear cada objeto IPC y reimportar
    # todos los módulos en cada hijo; con 'fork' se heredan por Copy-on-Write.
    mp.set_start_method("fork", force=True)

    monitor = Monitor(debug="--debug" in sys.argv)
    monitor.arrancar()
    try:
        monitor.loop()
    finally:
        # finally y no solo al final del loop: si el loop revienta por una
        # excepción, los hijos igual tienen que cosecharse.
        monitor.apagar()
    return 0


if __name__ == "__main__":
    sys.exit(main())
