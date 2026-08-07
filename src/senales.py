"""
senales.py — Manejo de las señales que recibe EL MONITOR.

Ojo con el nombre: este archivo NO tiene nada que ver con
analizadores/senales.py, que lee las máscaras SigBlk/SigIgn/SigCgt de los
procesos observados. Este es sobre las señales que le llegan a nuestro propio
proceso padre.

===========================================================================
EL PROBLEMA: por qué un handler no puede hacer trabajo real
===========================================================================

Un handler de señal no se ejecuta "cuando el programa está libre". El kernel
interrumpe al proceso en un punto ARBITRARIO —puede ser en medio de un
malloc(), de un print(), o de un put() en una Queue— ejecuta el handler, y
después devuelve el control a donde estaba.

Eso genera un peligro concreto: si el código interrumpido tenía tomado un lock
interno y el handler intenta tomar el mismo lock, el proceso se bloquea a sí
mismo para siempre. No hay otro thread que lo libere: es el mismo thread
esperándose.

Ejemplo de deadlock real:
    - el loop principal está haciendo cola.put(dato) y tiene el lock interno
    - llega SIGINT
    - el handler hace print("saliendo...")  ← print también toma locks
    - deadlock

Las funciones que se pueden llamar con seguridad desde un handler se llaman
ASYNC-SIGNAL-SAFE. La lista es corta: write(), read(), _exit(), kill()...
Casi nada de Python lo es.

===========================================================================
LA SOLUCIÓN: self-pipe
===========================================================================

Se crea un pipe anónimo (clase 5). Cuando llega una señal, lo único que pasa
es que se escribe UN BYTE en la punta de escritura — write() es
async-signal-safe. El loop principal está esperando en select() sobre la punta
de lectura, se despierta, lee el byte, y hace el trabajo real FUERA del
contexto de señal, cuando el proceso está en un estado consistente.

Acá se usa signal.set_wakeup_fd(), que es la versión que hace el propio
intérprete de CPython en C: escribe el NÚMERO de la señal en el fd, sin
ejecutar código Python. Es aún más seguro que escribir a mano en el handler.
Los handlers de Python quedan como no-op: solo hacen falta para que CPython
considere la señal "manejada" y dispare el wakeup_fd.

Detalle importante: las dos puntas del pipe se ponen en NO BLOQUEANTE. Si
llegaran miles de señales y nadie leyera, un write() bloqueante dentro del
kernel sobre un pipe lleno colgaría el proceso. En no bloqueante, el byte
sobrante simplemente se descarta.
"""

import os
import select
import signal

# Señales que el monitor maneja, según la consigna.
SENALES_MANEJADAS = [
    signal.SIGINT,    # Ctrl+C          -> shutdown limpio
    signal.SIGTERM,   # kill            -> shutdown limpio
    signal.SIGHUP,    # kill -HUP       -> recargar config.json
    signal.SIGUSR1,   # kill -USR1      -> dump del snapshot a JSON
    signal.SIGUSR2,   # kill -USR2      -> toggle verbose
    signal.SIGWINCH,  # terminal redimensionada -> repintar
]

# Señales que los HIJOS deben bloquear. Ctrl+C se manda al grupo de procesos
# entero, así que sin esto los 10 hijos se morirían por su cuenta y sería
# imposible ordenar el shutdown.
SENALES_A_BLOQUEAR_EN_HIJOS = [
    signal.SIGINT,
    signal.SIGHUP,
    signal.SIGUSR1,
    signal.SIGUSR2,
    signal.SIGWINCH,
]


class CanalSenales:
    """
    Encapsula el self-pipe. Se instancia UNA sola vez, en el proceso padre,
    antes de forkear.
    """

    def __init__(self):
        self.fd_lectura = None
        self.fd_escritura = None
        self._anterior = {}

    def instalar(self):
        """
        Crea el pipe, lo pone en no bloqueante y registra los handlers.
        Devuelve el fd de lectura para meterlo en el select() del loop.
        """
        self.fd_lectura, self.fd_escritura = os.pipe()
        os.set_blocking(self.fd_lectura, False)
        os.set_blocking(self.fd_escritura, False)

        # A partir de acá, cada señal manejada hace que CPython escriba su
        # número (1 byte) en fd_escritura, desde código C.
        signal.set_wakeup_fd(self.fd_escritura)

        for sig in SENALES_MANEJADAS:
            try:
                # El handler es un no-op A PROPÓSITO: el trabajo real lo hace
                # el loop principal al leer del pipe. Pero hay que registrar
                # algo, porque set_wakeup_fd solo dispara para señales que
                # Python considera manejadas.
                self._anterior[sig] = signal.signal(sig, self._noop)
            except (OSError, ValueError):
                pass  # SIGWINCH puede no existir en algunas plataformas

        return self.fd_lectura

    @staticmethod
    def _noop(signum, frame):
        """
        Handler vacío. NO agregar nada acá: cualquier cosa que se haga en este
        punto corre el riesgo de deadlock (ver el comentario de arriba).
        """
        return

    def esperar(self, timeout=0.5):
        """
        Espera hasta `timeout` segundos a que llegue alguna señal.
        Devuelve la lista de números de señal recibidos (puede venir más de
        una si llegaron juntas), o [] si venció el timeout.

        El timeout existe para que el loop principal despierte periódicamente
        aunque no llegue ninguna señal: así puede chequear si algún hijo se
        murió solo.
        """
        try:
            listos, _, _ = select.select([self.fd_lectura], [], [], timeout)
        except (InterruptedError, OSError):
            # EINTR: llegó una señal mientras estábamos en select(). Es normal.
            return []
        if not listos:
            return []

        try:
            datos = os.read(self.fd_lectura, 1024)
        except (BlockingIOError, OSError):
            return []
        return list(datos)  # cada byte es un número de señal

    def cerrar(self):
        """Desregistra el wakeup_fd y cierra el pipe."""
        try:
            signal.set_wakeup_fd(-1)
        except (OSError, ValueError):
            pass
        for sig, anterior in self._anterior.items():
            try:
                signal.signal(sig, anterior)
            except (OSError, ValueError, TypeError):
                pass
        for fd in (self.fd_lectura, self.fd_escritura):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.fd_lectura = self.fd_escritura = None


def preparar_hijo():
    """
    Se llama como PRIMERA línea de cada proceso hijo.

    Bloquea las señales que maneja el padre. "Bloquear" no es lo mismo que
    "ignorar":

      - IGNORAR (SIG_IGN): la señal llega y se descarta. Se pierde.
      - BLOQUEAR (sigmask): la señal llega y queda PENDIENTE. No se actúa
        sobre ella, pero sigue ahí; si después se desbloquea, se entrega.
        Se puede ver en el campo SigPnd de /proc/<pid>/status.
      - MANEJAR: se ejecuta un handler.

    Se bloquea (y no se ignora) por dos razones: es reversible, y deja rastro
    observable en /proc, que es justamente lo que este TP enseña a mirar.
    Probalo mientras corre el monitor:

        grep -E 'SigBlk|SigPnd' /proc/<pid de un analizador>/status

    SIGTERM NO se bloquea: es el canal por el que el padre ordena la salida.
    Y SIGKILL no aparece en la lista porque no se puede bloquear ni manejar,
    por diseño del kernel: siempre tiene que haber una forma de matar un
    proceso.
    """
    try:
        signal.pthread_sigmask(signal.SIG_BLOCK, SENALES_A_BLOQUEAR_EN_HIJOS)
    except (OSError, ValueError):
        pass
    # El hijo hereda el wakeup_fd del padre por el fork(): hay que sacarlo o
    # escribiría en el pipe del padre desde otro proceso.
    try:
        signal.set_wakeup_fd(-1)
    except (OSError, ValueError):
        pass


def nombre(signum):
    """Número de señal -> nombre legible, para logs."""
    try:
        return signal.Signals(signum).name
    except ValueError:
        return f"SIG{signum}"
