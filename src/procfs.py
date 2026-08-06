"""
procfs.py — Helpers de bajo nivel para leer y parsear /proc.

Este módulo es deliberadamente "tonto": no sabe nada de multiprocessing, de
analizadores ni de la TUI. Solo sabe abrir archivos de /proc, parsearlos y
devolver estructuras de Python. Eso lo hace testeable en aislamiento y hace que
el resto del sistema no tenga que repetir el manejo de errores.

Regla de oro de este módulo: NUNCA lanzar una excepción por un proceso que
desapareció. En un monitor, los procesos nacen y mueren mientras lo leés; eso
es el caso normal, no un error.
"""

import os

PROC = "/proc"


# ---------------------------------------------------------------------------
# Lectura cruda con manejo de errores
# ---------------------------------------------------------------------------

def leer_texto(ruta):
    """
    Lee un archivo de /proc completo y lo devuelve como str.
    Devuelve None si el archivo no se puede leer.

    Los tres modos de falla que importan, y por qué NO son bugs:

    - FileNotFoundError  : el proceso murió entre que listamos /proc y que
                           abrimos el archivo. Es una race condition inherente
                           a monitorear un sistema vivo. No se puede evitar,
                           solo se puede manejar.
    - PermissionError    : /proc/<pid>/ de otro usuario. Podemos leer 'stat' y
                           'status' (son públicos) pero no 'fd/' ni 'maps' de
                           procesos ajenos, salvo que corramos como root.
    - ProcessLookupError : el PID dejó de existir durante la lectura misma.
    - OSError (ESRCH/EIO): casos raros del kernel al leer entradas volátiles.

    Nota: se lee con UN solo read() (open().read()) a propósito. El kernel
    genera el contenido de estos archivos de forma atómica por lectura, así que
    leer todo de una evita quedarse con un snapshot inconsistente a la mitad.
    """
    try:
        with open(ruta, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
        return None


def leer_bytes(ruta):
    """
    Igual que leer_texto pero en binario. Necesario para 'cmdline' y
    'environ', donde los argumentos vienen separados por bytes nulos (\\x00)
    y no por espacios.
    """
    try:
        with open(ruta, "rb") as f:
            return f.read()
    except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
        return None


# ---------------------------------------------------------------------------
# Listado de procesos
# ---------------------------------------------------------------------------

def listar_pids():
    """
    Devuelve la lista de PIDs vivos, leyendo los nombres de carpeta de /proc.

    /proc contiene carpetas numéricas (una por proceso) mezcladas con entradas
    que no son procesos: 'meminfo', 'stat', 'uptime', 'self', 'thread-self',
    'net', 'sys'... Por eso el filtro isdigit().

    Ojo: esta lista queda vieja apenas la devolvemos. Cualquier PID de acá
    puede estar muerto para cuando el analizador lo lea. Por eso todas las
    funciones de abajo devuelven None en vez de explotar.
    """
    pids = []
    try:
        for nombre in os.listdir(PROC):
            if nombre.isdigit():
                pids.append(int(nombre))
    except OSError:
        return []
    return pids


def listar_tids(pid):
    """
    Devuelve la lista de TIDs (threads / LWPs) de un proceso.

    Cada thread de Linux es un "task" con su propia entrada en
    /proc/<pid>/task/<tid>/, con los mismos archivos que un proceso
    (stat, status, comm...). Un proceso monothread tiene UN solo task,
    cuyo TID coincide con el PID.

    Esta es la evidencia concreta de que en Linux "proceso" y "thread" son la
    misma abstracción del kernel (task_struct); lo que cambia es qué recursos
    comparten. El PID que ves con ps es en realidad el TGID (thread group id).
    """
    try:
        return [int(t) for t in os.listdir(f"{PROC}/{pid}/task") if t.isdigit()]
    except OSError:
        return []


# ---------------------------------------------------------------------------
# /proc/<pid>/status  — formato "Campo:\tvalor"
# ---------------------------------------------------------------------------

def leer_status(pid, tid=None):
    """
    Parsea /proc/<pid>/status (o /proc/<pid>/task/<tid>/status) y devuelve
    un dict {campo: valor} con los valores SIN convertir (todos str).

    Formato real:
        Name:\thead
        State:\tR (running)
        PPid:\t11726
        Uid:\t1000\t1000\t1000\t1000
        VmRSS:\t   4352 kB

    Decisiones de diseño:

    1. Se parte solo en el PRIMER ':' (split(':', 1)). Si partiéramos en todos,
       campos como 'Cpus_allowed_list:\t0-15' andarían igual, pero cualquier
       valor que contenga ':' se rompería.

    2. Los valores se dejan como str crudos, sin convertir a int. 'Uid' tiene
       CUATRO números (real, efectivo, saved, filesystem) y 'VmRSS' tiene
       unidad ('4352 kB'). No hay una conversión única que sirva para todos,
       así que convertir es responsabilidad de quien consume.

    3. Devuelve None (no {}) si el proceso no existe, para poder distinguir
       "murió" de "existe pero no tiene campos".
    """
    ruta = f"{PROC}/{pid}/status" if tid is None else f"{PROC}/{pid}/task/{tid}/status"
    texto = leer_texto(ruta)
    if texto is None:
        return None

    campos = {}
    for linea in texto.splitlines():
        if ":" not in linea:
            continue
        clave, valor = linea.split(":", 1)
        campos[clave.strip()] = valor.strip()
    return campos


# ---------------------------------------------------------------------------
# /proc/<pid>/stat  — formato posicional, con una trampa
# ---------------------------------------------------------------------------

# Índices de los campos de /proc/<pid>/stat, numerados como en proc(5),
# es decir EMPEZANDO EN 1. La función leer_stat() devuelve una lista 0-indexada,
# así que se accede con  campos[STAT_STATE - 1].
#
# Verificado contra man 5 proc. Cuidado: la consigna dice que SID y PGID son
# los campos 6-7, pero según el man son 5 (pgrp) y 6 (session). Confiar en el
# man y verificarlo con `ps -o pid,pgid,sid`.
STAT_PID = 1
STAT_COMM = 2
STAT_STATE = 3
STAT_PPID = 4
STAT_PGRP = 5
STAT_SESSION = 6
STAT_MINFLT = 10
STAT_CMINFLT = 11
STAT_MAJFLT = 12
STAT_CMAJFLT = 13
STAT_UTIME = 14
STAT_STIME = 15
STAT_PRIORITY = 18
STAT_NICE = 19
STAT_NUM_THREADS = 20
STAT_STARTTIME = 22
STAT_RT_PRIORITY = 40
STAT_POLICY = 41


def leer_stat(pid, tid=None):
    """
    Parsea /proc/<pid>/stat y devuelve la lista de campos como strings,
    0-indexada (usar las constantes STAT_* menos 1).

    ---------------------------------------------------------------
    LA TRAMPA (esto es lo importante de esta función)
    ---------------------------------------------------------------
    El campo 2 es el 'comm' (nombre del ejecutable) y viene entre paréntesis.
    El problema es que ese nombre PUEDE CONTENER ESPACIOS Y PARÉNTESIS.
    Ejemplos reales de este mismo sistema:

        1624 (mt76-tx phy0) S 2 0 0 ...
        1961 (UVM global queue) S 2 0 0 ...

    Entonces esto está MAL y es el bug clásico del TP:

        campos = texto.split()          # ← ROTO
        estado = campos[2]              # devuelve 'phy0)' en vez de 'S'

    Todo se corre de lugar y a partir de ahí los datos son basura silenciosa:
    no crashea, simplemente muestra números equivocados.

    La solución: el comm es lo único entre el PRIMER '(' y el ÚLTIMO ')'.
    Se usa rfind(')') —buscar desde la derecha— porque el nombre puede tener
    paréntesis adentro, pero nunca después del cierre. Todo lo que sigue al
    último ')' son campos numéricos separados por espacios simples, así que
    ahí sí se puede hacer split() tranquilo.
    """
    ruta = f"{PROC}/{pid}/stat" if tid is None else f"{PROC}/{pid}/task/{tid}/stat"
    texto = leer_texto(ruta)
    if texto is None:
        return None

    abre = texto.find("(")
    cierra = texto.rfind(")")
    if abre == -1 or cierra == -1 or cierra < abre:
        return None  # formato inesperado; mejor descartar que devolver basura

    pid_txt = texto[:abre].strip()          # campo 1
    comm = texto[abre + 1:cierra]           # campo 2, con espacios y todo
    resto = texto[cierra + 1:].split()      # campos 3 en adelante

    return [pid_txt, comm] + resto


def campo_stat(campos, numero):
    """
    Acceso seguro a un campo de stat por su número de proc(5) (1-indexado).
    Devuelve None si el campo no existe (kernels viejos tienen menos campos
    que los nuevos: el campo 41 'policy' no está en todos lados).
    """
    if campos is None:
        return None
    idx = numero - 1
    if 0 <= idx < len(campos):
        return campos[idx]
    return None


# ---------------------------------------------------------------------------
# /proc/<pid>/cmdline
# ---------------------------------------------------------------------------

def leer_cmdline(pid):
    """
    Devuelve la línea de comando completa como str, o None.

    cmdline guarda los argv separados por BYTES NULOS ('\\x00'), no por
    espacios. Es así porque un argumento puede contener espacios:
        ['python3', '-c', 'import time; time.sleep(60)']
    Si el kernel los separara con espacios, sería imposible saber dónde
    termina un argumento.

    Casos especiales:
    - Los KERNEL THREADS (kthreadd y sus hijos: kworker, ksoftirqd, migration)
      tienen cmdline VACÍO, porque no vienen de un exec() de un binario en
      disco: los crea el kernel directamente. Para esos hay que caer al 'comm'
      de stat, y por convención se muestran entre corchetes: [kworker/0:1].
      Probalo:  cat /proc/2/cmdline    (kthreadd, sale vacío)
    - Suele terminar con un \\x00 sobrante, por eso el strip final.
    """
    crudo = leer_bytes(f"{PROC}/{pid}/cmdline")
    if crudo is None:
        return None
    if not crudo.strip(b"\x00"):
        return ""  # kernel thread: existe pero no tiene cmdline
    partes = crudo.rstrip(b"\x00").split(b"\x00")
    return " ".join(p.decode("utf-8", errors="replace") for p in partes)


def nombre_proceso(pid):
    """
    Nombre "lindo" para mostrar: cmdline si existe, si no el comm entre
    corchetes (convención de ps/htop para kernel threads).
    """
    cmd = leer_cmdline(pid)
    if cmd:
        return cmd
    campos = leer_stat(pid)
    comm = campo_stat(campos, STAT_COMM)
    return f"[{comm}]" if comm else "?"


# ---------------------------------------------------------------------------
# Máscaras de señales
# ---------------------------------------------------------------------------

# Nombres de señal indexados por número. Se construye desde el módulo signal
# en vez de hardcodear la tabla, porque los números cambian entre
# arquitecturas (SIGUSR1 es 10 en x86-64 pero 16 en MIPS).
def _tabla_senales():
    import signal as _sig
    tabla = {}
    for s in _sig.Signals:
        tabla.setdefault(s.value, s.name)
    return tabla


NOMBRES_SENAL = _tabla_senales()


def decodificar_mascara(hexa):
    """
    Convierte una máscara de señales de /proc/<pid>/status a lista de nombres.

    Los campos SigBlk, SigIgn, SigCgt, SigPnd y ShdPnd vienen como un entero
    hexadecimal de 64 bits, donde cada bit representa una señal:

        SigCgt: 0000000180000002

    El BIT i (contando desde 0) corresponde a la SEÑAL i+1. El desfasaje es
    porque no existe la señal 0 (el kernel usa el 0 para 'probar si el proceso
    existe' sin mandar nada: kill(pid, 0)).

    Entonces:  bit 0 -> SIGHUP (1),  bit 1 -> SIGINT (2),  bit 14 -> SIGTERM (15)

    Verificalo a mano:
        grep Sig /proc/$$/status
        python3 -c "print(hex(1 << (15-1)))"   # máscara de solo SIGTERM
    """
    try:
        valor = int(hexa, 16)
    except (TypeError, ValueError):
        return []

    nombres = []
    for bit in range(64):
        if valor & (1 << bit):
            numero = bit + 1
            nombres.append(NOMBRES_SENAL.get(numero, f"SIG{numero}"))
    return nombres


# ---------------------------------------------------------------------------
# Utilidades del sistema
# ---------------------------------------------------------------------------

def hz():
    """
    Jiffies por segundo (USER_HZ). Los tiempos de /proc/<pid>/stat (utime,
    stime) están en jiffies, no en segundos: hay que dividir por esto.

    Casi siempre es 100, pero NO hay que hardcodearlo: depende de cómo se
    compiló el kernel. os.sysconf lo pregunta en runtime.
    """
    return os.sysconf("SC_CLK_TCK")


def tamano_pagina():
    """
    Bytes por página de memoria (típicamente 4096). Necesario porque algunos
    campos de /proc están en páginas y no en kB.
    """
    return os.sysconf("SC_PAGE_SIZE")
