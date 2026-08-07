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
        Name:\tthread
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


# ---------------------------------------------------------------------------
# /proc/<pid>/fd/  — file descriptors
# ---------------------------------------------------------------------------

def clasificar_fd(destino):
    """
    Infiere el tipo de un FD a partir del destino de su symlink.

    Linux representa TODO como archivo, pero el destino del link te dice qué
    es realmente. Formatos que devuelve el kernel:

        /dev/pts/3          -> tty      (terminal)
        socket:[95130]      -> socket   (el número es el inode del socket)
        pipe:[12345]        -> pipe     (pipe anónimo, ej: el de un `cmd | cmd`)
        anon_inode:[eventfd]-> anon     (eventfd, epoll, timerfd, signalfd...)
        /dev/null           -> dev
        /home/user/x.log    -> file     (archivo regular en disco)
        /memfd:foo (deleted)-> memfd    (memoria anónima con nombre, mmap)

    Los tres primeros son los que importan para la materia: 'pipe:' es
    literalmente lo de clase 5, y 'socket:' te deja ver qué procesos hablan
    por red o por unix sockets sin usar la red vos.
    """
    if destino is None:
        return "?"
    if destino.startswith("socket:"):
        return "socket"
    if destino.startswith("pipe:"):
        return "pipe"
    if destino.startswith("anon_inode:"):
        return "anon"
    if destino.startswith("/memfd:"):
        return "memfd"
    if destino.startswith("/dev/pts/") or destino == "/dev/tty":
        return "tty"
    if destino.startswith("/dev/"):
        return "dev"
    return "file"


# Los tres FDs que todo proceso hereda de su padre (clase 5).
FDS_ESTANDAR = {0: "stdin", 1: "stdout", 2: "stderr"}


def listar_fds(pid):
    """
    Devuelve [{'fd': int, 'destino': str, 'tipo': str, 'estandar': str|None}]
    ordenado por número de FD, o None si no se puede leer el directorio.

    Por qué os.readlink() y no os.path.realpath():
    /proc/<pid>/fd/N es un symlink MÁGICO. No apunta a una ruta del filesystem
    en el sentido normal: el kernel lo genera describiendo el objeto abierto.
    Por eso puede "apuntar" a cosas que no son archivos, como 'socket:[95130]',
    que no existe en ningún directorio. realpath() intentaría resolverlo y
    fallaría; readlink() devuelve el texto crudo, que es lo que queremos.

    Permisos: este directorio es modo 0500 y pertenece al dueño del proceso.
    Solo podés listarlo si sos ese usuario o root. Con procesos ajenos vas a
    recibir None constantemente, y eso NO es un bug — es aislamiento del
    kernel. Verificalo:  ls -l /proc/1/fd/   (falla salvo que seas root)

    Race condition inherente: entre listdir() y readlink() el proceso puede
    haber cerrado el FD. Por eso el readlink va dentro de su propio try.
    """
    carpeta = f"{PROC}/{pid}/fd"
    try:
        nombres = os.listdir(carpeta)
    except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
        return None

    fds = []
    for nombre in nombres:
        if not nombre.isdigit():
            continue
        numero = int(nombre)
        try:
            destino = os.readlink(f"{carpeta}/{nombre}")
        except OSError:
            continue  # se cerró entre el listdir y el readlink
        fds.append({
            "fd": numero,
            "destino": destino,
            "tipo": clasificar_fd(destino),
            "estandar": FDS_ESTANDAR.get(numero),
        })
    fds.sort(key=lambda d: d["fd"])
    return fds


# ---------------------------------------------------------------------------
# /proc/<pid>/maps  — mapa de memoria virtual
# ---------------------------------------------------------------------------

def leer_maps(pid):
    """
    Parsea /proc/<pid>/maps y devuelve la lista de regiones:
        [{'inicio', 'fin', 'tam_kb', 'perms', 'offset', 'dev', 'inode', 'ruta'}]

    Formato de cada línea (el último campo puede faltar):

        5bf9c4e25000-5bf9c4e2c000 r-xp 00002000 fc:01 15352654 /usr/bin/head
        └──── rango ─────────────┘ └──┘ └─offset┘ └dev┘ └inode┘ └── ruta ──┘

    Los permisos son 4 caracteres: r/w/x y el cuarto es 'p' (private) o
    's' (shared). Ese cuarto es el que revela Copy-on-Write: una región 'p'
    mapeada desde un archivo se copia recién cuando la escribís.

    Se usa split(maxsplit=5) porque la RUTA PUEDE CONTENER ESPACIOS
    (ej: /home/user/mi carpeta/lib.so). Mismo tipo de trampa que el comm.
    """
    texto = leer_texto(f"{PROC}/{pid}/maps")
    if texto is None:
        return None

    regiones = []
    for linea in texto.splitlines():
        partes = linea.split(maxsplit=5)
        if len(partes) < 5:
            continue
        rango, perms, offset, dev, inode = partes[:5]
        ruta = partes[5].strip() if len(partes) > 5 else ""
        if "-" not in rango:
            continue
        ini_txt, fin_txt = rango.split("-", 1)
        try:
            inicio, fin = int(ini_txt, 16), int(fin_txt, 16)
        except ValueError:
            continue
        regiones.append({
            "inicio": inicio,
            "fin": fin,
            "tam_kb": (fin - inicio) // 1024,
            "perms": perms,
            "offset": offset,
            "dev": dev,
            "inode": inode,
            "ruta": ruta,
        })
    return regiones


def agrupar_segmentos(regiones):
    """
    Agrupa las regiones de maps en los segmentos clásicos de la teoría
    (clase 3: text / data / BSS / heap / stack) y devuelve
    {segmento: {'kb': int, 'regiones': int}}.

    Cómo se clasifica cada región, y por qué:

    - '[heap]'  -> heap    : el kernel lo etiqueta explícitamente. Crece con
                             brk()/sbrk(). Acá vive lo que pedís con malloc.
    - '[stack]' -> stack   : también etiquetado. Crece hacia abajo.
    - '[vdso]', '[vsyscall]', '[vvar]' -> kernel : páginas que el kernel mapea
                             en todo proceso para acelerar syscalls como
                             gettimeofday() sin cambiar a modo kernel.
    - perms[3] == 's' -> shared : memoria compartida (mmap MAP_SHARED). ACÁ es
                             donde van a aparecer tus propios Value/Array
                             cuando corras el monitor. Es literalmente clase 7.
    - 'r-x' con ruta -> text : código ejecutable del binario o de una .so.
                             Read + execute, NO write: por eso el binario se
                             puede compartir entre procesos sin copiarlo (COW).
    - 'r--' con ruta -> rodata : constantes, strings literales.
    - 'rw-' con ruta -> data : variables globales inicializadas.
    - 'rw-' sin ruta -> anon : mapeos anónimos. Acá cae el BSS (globales sin
                             inicializar) y lo que pide malloc para bloques
                             grandes (glibc usa mmap en vez de brk arriba de
                             cierto tamaño). Por eso NO se puede separar BSS
                             de heap-por-mmap mirando solo maps: son
                             indistinguibles desde afuera. (Limitación conocida.)
    """
    grupos = {}

    def sumar(clave, kb):
        g = grupos.setdefault(clave, {"kb": 0, "regiones": 0})
        g["kb"] += kb
        g["regiones"] += 1

    for r in regiones or []:
        ruta, perms, kb = r["ruta"], r["perms"], r["tam_kb"]

        if ruta == "[heap]":
            sumar("heap", kb)
        elif ruta == "[stack]" or ruta.startswith("[stack:"):
            sumar("stack", kb)
        elif ruta in ("[vdso]", "[vsyscall]", "[vvar]"):
            sumar("kernel", kb)
        elif len(perms) > 3 and perms[3] == "s":
            sumar("shared", kb)
        elif "x" in perms and ruta:
            sumar("text", kb)
        elif ruta and perms.startswith("r--"):
            sumar("rodata", kb)
        elif ruta and "w" in perms:
            sumar("data", kb)
        elif ruta:
            sumar("otros_file", kb)
        else:
            sumar("anon", kb)

    return grupos


# ---------------------------------------------------------------------------
# Estadísticas globales del sistema
# ---------------------------------------------------------------------------

# Columnas de la línea 'cpu' de /proc/stat, en orden. Todas en jiffies
# acumulados desde el boot.
COLUMNAS_CPU = [
    "user",       # tiempo en modo usuario
    "nice",       # modo usuario con nice > 0
    "system",     # modo kernel
    "idle",       # sin hacer nada
    "iowait",     # idle pero esperando I/O (poco confiable, ver nota abajo)
    "irq",        # atendiendo interrupciones de hardware
    "softirq",    # atendiendo interrupciones de software
    "steal",      # tiempo robado por el hypervisor (solo en VMs)
    "guest",      # corriendo una VM invitada
    "guest_nice",
]


def leer_stat_global():
    """
    Parsea /proc/stat y devuelve:
        {'cpu': {user, nice, system, ...},          # agregado de todos los cores
         'cpus': {'cpu0': {...}, 'cpu1': {...}},    # por core
         'btime': int,          # timestamp Unix del boot
         'processes': int,      # procesos creados desde el boot (acumulado)
         'procs_running': int,  # tasks en estado R AHORA MISMO
         'procs_blocked': int}  # tasks en estado D (I/O ininterrumpible) AHORA

    Igual que utime/stime de un proceso, los valores de 'cpu' son JIFFIES
    ACUMULADOS DESDE EL BOOT. Para sacar el %CPU del sistema hay que hacer
    delta entre dos lecturas:

        %uso = (total₂-total₁ - (idle₂-idle₁)) / (total₂-total₁) × 100

    donde total = suma de todas las columnas.

    Nota sobre 'iowait': el propio kernel documenta que este valor no es
    confiable en máquinas multi-core, porque un core puede quedar idle y
    contabilizar iowait de un I/O que en realidad está esperando otro core.
    Mostralo, pero no lo uses para decidir nada.

    Ojo con 'processes': es un CONTADOR ACUMULADO de forks desde el boot
    (15558 en este sistema), NO la cantidad de procesos vivos. Para eso hay
    que contar las carpetas con listar_pids().
    """
    texto = leer_texto(f"{PROC}/stat")
    if texto is None:
        return None

    datos = {"cpu": {}, "cpus": {}}
    for linea in texto.splitlines():
        partes = linea.split()
        if not partes:
            continue
        clave = partes[0]

        if clave == "cpu" or (clave.startswith("cpu") and clave[3:].isdigit()):
            valores = {}
            for nombre, txt in zip(COLUMNAS_CPU, partes[1:]):
                try:
                    valores[nombre] = int(txt)
                except ValueError:
                    valores[nombre] = 0
            if clave == "cpu":
                datos["cpu"] = valores
            else:
                datos["cpus"][clave] = valores

        elif clave in ("btime", "processes", "procs_running", "procs_blocked",
                       "ctxt", "intr"):
            try:
                datos[clave] = int(partes[1])
            except (IndexError, ValueError):
                pass

    return datos


def total_jiffies(cpu):
    """Suma de todas las columnas de una línea de CPU. Denominador del %."""
    return sum(cpu.values()) if cpu else 0


def porcentaje_cpu_global(antes, ahora):
    """
    Calcula el % de uso de CPU entre dos lecturas de leer_stat_global()['cpu'].
    Devuelve {'user': %, 'system': %, 'idle': %, 'iowait': %, 'uso': %}.

    Esta es LA función que demuestra por qué el agregador debe ser un proceso
    con memoria propia: necesita 'antes' y 'ahora'. Un Manager solo guarda
    'ahora'.

    Devuelve todo en 0 si el delta es 0 (dos lecturas demasiado juntas, o
    reloj sin avanzar). Nunca divide por cero.
    """
    if not antes or not ahora:
        return {}
    delta_total = total_jiffies(ahora) - total_jiffies(antes)
    if delta_total <= 0:
        return {}

    resultado = {}
    for col in ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal"):
        delta = ahora.get(col, 0) - antes.get(col, 0)
        resultado[col] = round(100.0 * delta / delta_total, 1)
    resultado["uso"] = round(100.0 - resultado.get("idle", 0.0), 1)
    return resultado


def leer_meminfo():
    """
    Parsea /proc/meminfo y devuelve {campo: kilobytes(int)}.

    Formato:  'MemTotal:       15505860 kB'
    Casi todos los valores están en kB, salvo HugePages_* que son conteos.
    Se descarta la unidad y se guarda el número.

    Campos que importan para la vista Sistema:
      MemTotal, MemFree, MemAvailable, Buffers, Cached, SwapTotal, SwapFree

    Trampa clásica: 'memoria usada' NO es MemTotal - MemFree. Eso te va a dar
    un número altísimo y falso, porque el kernel usa toda la RAM libre como
    cache de disco (Cached/Buffers) y la libera al instante si alguien la
    necesita. Lo correcto es MemTotal - MemAvailable, que es el número que el
    propio kernel calcula teniendo en cuenta qué cache es reclamable.
    Compará los dos y vas a ver la diferencia.
    """
    texto = leer_texto(f"{PROC}/meminfo")
    if texto is None:
        return None

    datos = {}
    for linea in texto.splitlines():
        if ":" not in linea:
            continue
        clave, valor = linea.split(":", 1)
        partes = valor.split()
        if not partes:
            continue
        try:
            datos[clave.strip()] = int(partes[0])
        except ValueError:
            continue
    return datos


def memoria_usada_kb(meminfo):
    """
    Memoria realmente usada, en kB. Ver la nota de leer_meminfo() sobre por
    qué se usa MemAvailable y no MemFree.
    """
    if not meminfo:
        return 0
    total = meminfo.get("MemTotal", 0)
    disponible = meminfo.get("MemAvailable")
    if disponible is None:
        # Kernels < 3.14 no tienen MemAvailable; aproximación clásica.
        disponible = (meminfo.get("MemFree", 0)
                      + meminfo.get("Buffers", 0)
                      + meminfo.get("Cached", 0))
    return max(0, total - disponible)


def leer_loadavg():
    """
    Parsea /proc/loadavg:  '0.90 1.37 1.23 1/2130 15549'

    Devuelve {'1min','5min','15min','ejecutables','total_tasks','ultimo_pid'}.

    Qué es el load average, porque casi todo el mundo lo entiende mal: NO es
    "% de CPU". Es el promedio móvil de la cantidad de tasks en estado
    R (ejecutando/listo) MÁS los que están en D (I/O ininterrumpible).

    Por eso, en una máquina de 16 cores un load de 8 es media máquina, pero en
    una de 4 cores el mismo 8 significa que hay el doble de trabajo que
    capacidad. Siempre hay que compararlo contra os.cpu_count().

    Y por eso también un load alto puede venir de disco saturado (tasks en D)
    sin que la CPU esté ocupada en absoluto.

    El campo '1/2130' es ejecutables/total de tasks (no de procesos: cuenta
    threads). El último número es el PID asignado más recientemente.
    """
    texto = leer_texto(f"{PROC}/loadavg")
    if texto is None:
        return None
    partes = texto.split()
    if len(partes) < 5:
        return None
    try:
        ejecutables, total = partes[3].split("/")
        return {
            "1min": float(partes[0]),
            "5min": float(partes[1]),
            "15min": float(partes[2]),
            "ejecutables": int(ejecutables),
            "total_tasks": int(total),
            "ultimo_pid": int(partes[4]),
            "cpus": os.cpu_count() or 1,
        }
    except (ValueError, IndexError):
        return None


def leer_uptime():
    """
    Parsea /proc/uptime: '4074.70 46363.88'
      - primer número: segundos desde el boot
      - segundo: suma del tiempo idle de TODOS los cores

    Por eso el segundo puede ser mayor que el primero: en 4074 s de uptime,
    16 cores acumulan hasta 65184 s de idle sumados. Si dividís
    idle/uptime/cpus tenés la fracción de máquina ociosa desde el boot.
    """
    texto = leer_texto(f"{PROC}/uptime")
    if texto is None:
        return None
    partes = texto.split()
    try:
        return {"uptime_s": float(partes[0]), "idle_s": float(partes[1])}
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# UID -> nombre de usuario
# ---------------------------------------------------------------------------

_cache_usuarios = {}


def usuario_de_uid(uid):
    """
    Traduce un UID numérico a nombre de usuario.

    Se cachea porque /proc/<pid>/status te da el UID en cada lectura, y
    resolverlo para 400 procesos cada 2 segundos significaría releer
    /etc/passwd 400 veces por ciclo. Los UIDs no cambian de nombre en runtime,
    así que cachear es seguro.

    Si el UID no existe en /etc/passwd (típico dentro de un contenedor, donde
    el usuario del host no está en el passwd de la imagen) se devuelve el
    número como string. No es un error: es normal en Docker.
    """
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        return "?"
    if uid in _cache_usuarios:
        return _cache_usuarios[uid]
    try:
        import pwd
        nombre = pwd.getpwuid(uid).pw_name
    except (KeyError, ImportError, OSError):
        nombre = str(uid)
    _cache_usuarios[uid] = nombre
    return nombre


def uid_de_status(status):
    """
    Extrae el UID REAL de un dict de leer_status().

    El campo viene con CUATRO valores: 'Uid:  1000  1000  1000  1000'
    que son, en orden:
        real       - quién lanzó el proceso
        efectivo   - con qué permisos actúa AHORA (cambia con setuid)
        saved      - el efectivo guardado, para poder volver
        filesystem - el que se usa para chequear permisos de archivos (Linux)

    Un binario setuid como 'passwd' tiene real=1000 pero efectivo=0. Por eso
    importa cuál mostrás: el real dice quién lo lanzó, el efectivo dice qué
    puede hacer. Acá devolvemos el real, que es lo que muestra `ps -u`.
    """
    if not status:
        return None
    campo = status.get("Uid", "")
    partes = campo.split()
    if not partes:
        return None
    try:
        return int(partes[0])
    except ValueError:
        return None
