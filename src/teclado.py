"""
teclado.py — Lectura de teclas sin bloquear el render.

--------------------------------------------------------------------------
POR QUÉ ACÁ SÍ SE USA UN THREAD (y es la única excepción del TP)
--------------------------------------------------------------------------
Toda la arquitectura del monitor es multiproceso porque el trabajo real
—parsear /proc— es CPU-bound: son operaciones de string, o sea bytecode de
Python, o sea GIL agarrado. Con threads los analizadores se turnarían para
usar un solo core.

Leer una tecla es exactamente lo contrario:

  1. Es I/O-BOUND PURO. read() sobre stdin se bloquea esperando al humano.
     Durante esa espera el thread SUELTA EL GIL, así que el thread que
     dibuja sigue corriendo a toda velocidad. El GIL no estorba.

  2. Necesita COMPARTIR MEMORIA con el que dibuja: la vista activa, la fila
     seleccionada, el texto del filtro. Con un proceso aparte habría que
     montar IPC para algo que son tres variables.

Es el caso de libro donde los threads son la herramienta correcta, y por eso
la consigna lo autoriza explícitamente (nota de la línea 80).

--------------------------------------------------------------------------
MODO CRUDO DE LA TERMINAL
--------------------------------------------------------------------------
Por defecto la terminal está en modo CANÓNICO: el driver de tty acumula lo
que escribís en un buffer y no se lo entrega al programa hasta que apretás
Enter (por eso podés borrar con backspace antes de mandar). Para una TUI eso
no sirve: necesitamos cada tecla apenas se aprieta.

tty.setcbreak() desactiva el modo canónico y el echo de línea. Es
OBLIGATORIO restaurar los settings al salir: si el programa muere sin
restaurar, la terminal queda sin echo y hay que hacer `reset` a ciegas. Por
eso el restore va en un finally.
"""

import os
import queue
import select
import sys
import termios
import threading
import tty

# Secuencias ANSI que manda la terminal para las teclas especiales. No son
# un carácter: son varios bytes empezando con ESC (0x1b).
SECUENCIAS = {
    "\x1b[A": "ARRIBA",
    "\x1b[B": "ABAJO",
    "\x1b[C": "DERECHA",
    "\x1b[D": "IZQUIERDA",
    "\x1b[5~": "PGUP",
    "\x1b[6~": "PGDN",
    "\x1b[H": "HOME",
    "\x1b[F": "FIN",
}


class Teclado:
    """
    Lee teclas en un thread y las deja en una cola que el loop de render
    consume sin bloquearse.
    """

    def __init__(self):
        self.cola = queue.Queue()
        self._parar = threading.Event()
        self._hilo = None
        self._fd = None
        self._settings = None

    def __enter__(self):
        self.iniciar()
        return self

    def __exit__(self, *exc):
        self.detener()
        return False

    def iniciar(self):
        try:
            self._fd = sys.stdin.fileno()
            self._settings = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        except (termios.error, ValueError, OSError):
            # Sin tty (por ejemplo, salida redirigida a un archivo o docker
            # sin -t). El monitor sigue funcionando, solo que sin teclado.
            self._fd = None
            return

        # daemon=True acá SÍ está bien, al revés que con los procesos hijos:
        # este thread solo lee stdin, no tiene nada que limpiar, y no
        # queremos que impida salir si quedó bloqueado en un read().
        self._hilo = threading.Thread(target=self._loop, daemon=True)
        self._hilo.start()

    def detener(self):
        self._parar.set()
        if self._fd is not None and self._settings is not None:
            # Restaurar SIEMPRE, o la terminal queda inutilizable.
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._settings)
            except (termios.error, ValueError, OSError):
                pass

    def _loop(self):
        """
        Corre en el thread. select() con timeout en vez de read() pelado para
        poder chequear el flag de parada periódicamente: con un read()
        bloqueante el thread nunca vería que hay que salir.
        """
        while not self._parar.is_set():
            try:
                listos, _, _ = select.select([self._fd], [], [], 0.2)
            except (OSError, ValueError):
                return
            if not listos:
                continue
            try:
                ch = os.read(self._fd, 1).decode("utf-8", errors="ignore")
            except (OSError, ValueError):
                return
            if not ch:
                continue

            if ch == "\x1b":
                ch = self._leer_secuencia()

            self.cola.put(ch)

    def _leer_secuencia(self):
        """
        Llegó un ESC. Puede ser la tecla Escape sola, o el comienzo de una
        secuencia de flecha. Se leen más bytes con un timeout MUY corto: si
        no viene nada en 50 ms, era Escape solo.
        """
        seq = "\x1b"
        for _ in range(4):
            try:
                listos, _, _ = select.select([self._fd], [], [], 0.05)
            except (OSError, ValueError):
                break
            if not listos:
                break
            try:
                seq += os.read(self._fd, 1).decode("utf-8", errors="ignore")
            except (OSError, ValueError):
                break
            if seq in SECUENCIAS:
                return SECUENCIAS[seq]
        return SECUENCIAS.get(seq, "ESC")

    def leer_todas(self):
        """
        Devuelve todas las teclas pendientes sin bloquear. Se llama una vez
        por frame desde el loop de render.
        """
        teclas = []
        while True:
            try:
                teclas.append(self.cola.get_nowait())
            except queue.Empty:
                return teclas
