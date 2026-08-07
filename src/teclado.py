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

    def __init__(self, fd_heredado=None):
        """
        fd_heredado: descriptor de la terminal que el proceso PADRE duplicó
        con os.dup(0) antes de forkear. Es la vía más confiable, porque no
        depende de que /dev/tty sea accesible ni de qué le hizo
        multiprocessing a sys.stdin.
        """
        self.cola = queue.Queue()
        self._parar = threading.Event()
        self._hilo = None
        self._fd = None
        self._propio = False        # ¿el fd lo abrimos nosotros?
        self._settings = None
        self._fd_heredado = fd_heredado
        self.diagnostico = "sin inicializar"

    def __enter__(self):
        self.iniciar()
        return self

    def __exit__(self, *exc):
        self.detener()
        return False

    def _abrir_terminal(self):
        """
        Consigue un fd de lectura sobre la terminal REAL.

        ---------------------------------------------------------------
        POR QUÉ NO SE PUEDE USAR sys.stdin ACÁ
        ---------------------------------------------------------------
        El display corre en un proceso hijo creado con
        multiprocessing.Process, y multiprocessing CIERRA stdin en todos
        sus hijos: en BaseProcess._bootstrap llama a util._close_stdin(),
        que hace sys.stdin.close() y lo reabre apuntando a /dev/null.

        Comprobable:

            def hijo(q): q.put(os.readlink('/proc/self/fd/0'))
            # el hijo reporta /dev/null, no la terminal

        Consecuencia si se usa sys.stdin igual: select() sobre /dev/null
        SIEMPRE dice que hay datos listos, read() devuelve b'' (EOF), y el
        thread entra en busy-loop quemando un core entero sin recibir
        jamás una tecla.

        La solución es abrir /dev/tty, que es la terminal de control del
        proceso: existe independientemente de qué le hayan hecho a los fds
        estándar, y sobrevive al fork.
        """
        # 1. El fd que el padre duplicó antes del fork. Es el más confiable.
        if self._fd_heredado is not None:
            try:
                if os.isatty(self._fd_heredado):
                    self.diagnostico = f"fd heredado del padre ({self._fd_heredado})"
                    return self._fd_heredado
            except OSError:
                pass

        # 2. La terminal de control del proceso.
        try:
            fd = os.open("/dev/tty", os.O_RDONLY | os.O_NOCTTY)
            self._propio = True
            self.diagnostico = "/dev/tty"
            return fd
        except OSError as e:
            self.diagnostico = f"/dev/tty falló ({e.strerror})"
        # 2. Fallback: el DESCRIPTOR 0 crudo, no sys.stdin.
        #    _close_stdin() reemplaza el objeto sys.stdin por uno nuevo que
        #    apunta a /dev/null, pero el fd 0 del proceso puede seguir siendo
        #    la terminal. Por eso se chequea os.isatty(0) y no
        #    sys.stdin.fileno(): usar sys.stdin acá es exactamente el bug que
        #    hacía que el teclado no respondiera.
        try:
            if os.isatty(0):
                self._propio = True
                self.diagnostico = "dup del fd 0"
                return os.dup(0)
        except OSError:
            pass
        self.diagnostico = (self.diagnostico + " y el fd 0 no es una tty"
                            if "falló" in self.diagnostico
                            else "no hay terminal disponible")
        return None

    def iniciar(self):
        self._fd = self._abrir_terminal()
        if self._fd is None:
            # Sin terminal (salida redirigida, docker sin -t). El monitor
            # sigue funcionando, solo que sin teclado.
            return
        try:
            self._settings = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        except (termios.error, ValueError, OSError):
            os.close(self._fd)
            self._fd = None
            return

        # daemon=True acá SÍ está bien, al revés que con los procesos hijos:
        # este thread solo lee stdin, no tiene nada que limpiar, y no
        # queremos que impida salir si quedó bloqueado en un read().
        self._hilo = threading.Thread(target=self._loop, daemon=True)
        self._hilo.start()

    def detener(self):
        self._parar.set()
        if self._fd is None:
            return
        if self._settings is not None:
            # Restaurar SIEMPRE, o la terminal queda sin echo y hay que
            # escribir `reset` a ciegas.
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._settings)
            except (termios.error, ValueError, OSError):
                pass
        # Solo se cierra si lo abrimos nosotros. El fd heredado es del padre.
        if self._propio:
            try:
                os.close(self._fd)
            except OSError:
                pass
        self._fd = None

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
                crudo = os.read(self._fd, 1)
            except (OSError, ValueError):
                return
            if not crudo:
                # Lectura vacía = EOF. Si acá hiciéramos `continue`, el loop
                # giraría a máxima velocidad quemando un core, porque select()
                # sobre un fd en EOF devuelve "listo" para siempre.
                return
            ch = crudo.decode("utf-8", errors="ignore")
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
