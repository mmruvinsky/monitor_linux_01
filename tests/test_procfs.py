"""
Tests de procfs.py.

Por qué se testea con STRINGS DE MUESTRA y no contra /proc real:

/proc cambia entre ejecuciones —los procesos nacen y mueren, los contadores
suben— así que un test contra /proc real no es determinista: pasaría o
fallaría según el momento. Con un string fijo se puede afirmar exactamente qué
tiene que salir.

Los casos elegidos no son aleatorios: cada uno corresponde a una trampa real
que rompió el parseo durante el desarrollo, o a un caso límite que aparece en
un sistema vivo.

    python3 -m pytest tests/ -v
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import procfs  # noqa: E402


# ===========================================================================
# leer_stat: la trampa del comm
# ===========================================================================

class TestLeerStat:
    """
    /proc/<pid>/stat es posicional, pero el campo 2 (comm) viene entre
    paréntesis y PUEDE CONTENER ESPACIOS Y PARÉNTESIS. Un split() ingenuo
    corre todos los campos de lugar y devuelve basura SIN dar error.
    """

    def _parsear(self, texto, tmp_path, monkeypatch):
        """Escribe `texto` a un archivo y hace que procfs lo lea como stat."""
        archivo = tmp_path / "stat"
        archivo.write_text(texto)
        monkeypatch.setattr(procfs, "PROC", str(tmp_path.parent))
        return procfs.leer_stat(tmp_path.name)

    def test_comm_simple(self, tmp_path, monkeypatch):
        campos = self._parsear("1234 (bash) S 1200 1234 1234 34816 1300 4194304 "
                               "500 0 0 0 10 20 0 0 20 0 1 0 999",
                               tmp_path, monkeypatch)
        assert campos[procfs.STAT_PID - 1] == "1234"
        assert campos[procfs.STAT_COMM - 1] == "bash"
        assert campos[procfs.STAT_STATE - 1] == "S"
        assert campos[procfs.STAT_PPID - 1] == "1200"

    def test_comm_con_espacios(self, tmp_path, monkeypatch):
        """Caso real de este sistema: el kernel thread 'mt76-tx phy0'."""
        campos = self._parsear("1624 (mt76-tx phy0) S 2 0 0 0 -1 2129984 "
                               "0 0 0 0 0 731 0 0 -20",
                               tmp_path, monkeypatch)
        assert campos[procfs.STAT_COMM - 1] == "mt76-tx phy0"
        assert campos[procfs.STAT_STATE - 1] == "S"
        assert campos[procfs.STAT_PPID - 1] == "2"

    def test_comm_con_parentesis_adentro(self, tmp_path, monkeypatch):
        """
        Por esto se usa rfind(')') y no find(')'): el nombre puede tener
        paréntesis, pero el cierre real siempre es el ÚLTIMO.
        """
        campos = self._parsear("99 (raro (con) parentesis) R 1 99 99 0 -1 0 "
                               "0 0 0 0 5 6 0 0 20 0 1 0 100",
                               tmp_path, monkeypatch)
        assert campos[procfs.STAT_COMM - 1] == "raro (con) parentesis"
        assert campos[procfs.STAT_STATE - 1] == "R"

    def test_split_ingenuo_daria_basura(self, tmp_path, monkeypatch):
        """
        Documenta POR QUÉ existe el parseo especial: el bug silencioso que
        se evita. No falla, devuelve datos equivocados.
        """
        texto = "1624 (mt76-tx phy0) S 2 0 0"
        malo = texto.split()
        assert malo[2] == "phy0)"        # creería que el estado es 'phy0)'
        campos = self._parsear(texto, tmp_path, monkeypatch)
        assert campos[procfs.STAT_STATE - 1] == "S"   # el correcto

    def test_proceso_inexistente_devuelve_none(self):
        """Un proceso que murió NO es un error: es el caso normal."""
        assert procfs.leer_stat(999999) is None

    def test_formato_corrupto_devuelve_none(self, tmp_path, monkeypatch):
        """Sin paréntesis: mejor descartar que devolver campos corridos."""
        assert self._parsear("basura sin parentesis", tmp_path, monkeypatch) is None


# ===========================================================================
# leer_status
# ===========================================================================

STATUS_MUESTRA = """\
Name:\tbash
Umask:\t0022
State:\tS (sleeping)
Tgid:\t1234
Pid:\t1234
PPid:\t1200
Uid:\t1000\t1000\t1000\t1000
Gid:\t1000\t1000\t1000\t1000
Threads:\t1
SigBlk:\t0000000000010000
SigIgn:\t0000000000384004
SigCgt:\t000000004b817efb
VmRSS:\t    4352 kB
Cpus_allowed_list:\t0-11
voluntary_ctxt_switches:\t741
nonvoluntary_ctxt_switches:\t78
"""


class TestLeerStatus:

    @pytest.fixture
    def status(self, tmp_path, monkeypatch):
        (tmp_path / "status").write_text(STATUS_MUESTRA)
        monkeypatch.setattr(procfs, "PROC", str(tmp_path.parent))
        return procfs.leer_status(tmp_path.name)

    def test_campos_simples(self, status):
        assert status["Name"] == "bash"
        assert status["PPid"] == "1200"
        assert status["Threads"] == "1"

    def test_uid_tiene_cuatro_valores(self, status):
        """
        real, efectivo, saved, filesystem. Por eso leer_status NO convierte a
        int: no hay una conversión única que sirva para todos los campos.
        """
        assert status["Uid"] == "1000\t1000\t1000\t1000"
        assert procfs.uid_de_status(status) == 1000

    def test_valor_con_unidad_se_deja_crudo(self, status):
        assert status["VmRSS"] == "4352 kB"

    def test_valor_con_guion_no_se_parte(self, status):
        """split(':', 1) y no split(':'): el valor puede contener ':'."""
        assert status["Cpus_allowed_list"] == "0-11"

    def test_inexistente_devuelve_none(self):
        assert procfs.leer_status(999999) is None


# ===========================================================================
# Máscaras de señales
# ===========================================================================

class TestDecodificarMascara:
    """
    El bit i corresponde a la señal i+1, porque no existe la señal 0: el
    número 0 está reservado para kill(pid, 0), que solo prueba existencia.
    """

    def test_mascara_vacia(self):
        assert procfs.decodificar_mascara("0000000000000000") == []

    def test_bit_0_es_sighup(self):
        assert "SIGHUP" in procfs.decodificar_mascara("0000000000000001")

    def test_bit_1_es_sigint(self):
        assert procfs.decodificar_mascara("0000000000000002") == ["SIGINT"]

    def test_sigterm_es_el_bit_14(self):
        """SIGTERM es la señal 15, así que vive en el bit 14."""
        mascara = hex(1 << (15 - 1))
        assert "SIGTERM" in procfs.decodificar_mascara(mascara)

    def test_varias_a_la_vez(self):
        # bits 0 y 1 -> SIGHUP (1) y SIGINT (2)
        nombres = procfs.decodificar_mascara("0000000000000003")
        assert "SIGHUP" in nombres and "SIGINT" in nombres

    def test_entrada_invalida_no_explota(self):
        assert procfs.decodificar_mascara("no es hexa") == []
        assert procfs.decodificar_mascara(None) == []


# ===========================================================================
# maps y agrupación de segmentos
# ===========================================================================

MAPS_MUESTRA = """\
5bf9c4e25000-5bf9c4e2c000 r-xp 00002000 fc:01 15352654 /usr/bin/head
5bf9c4e2c000-5bf9c4e2e000 r--p 00009000 fc:01 15352654 /usr/bin/head
5bf9c4e2f000-5bf9c4e30000 rw-p 0000b000 fc:01 15352654 /usr/bin/head
5bf9ff4d0000-5bf9ff4f1000 rw-p 00000000 00:00 0 [heap]
721d31310000-721d31311000 rw-p 00000000 00:00 0
7f0000000000-7f0000001000 rw-s 00000000 00:05 12345 /dev/shm/algo
7ffd6e84a000-7ffd6e86c000 rw-p 00000000 00:00 0 [stack]
ffffffffff600000-ffffffffff601000 --xp 00000000 00:00 0 [vsyscall]
"""


class TestMaps:

    @pytest.fixture
    def regiones(self, tmp_path, monkeypatch):
        (tmp_path / "maps").write_text(MAPS_MUESTRA)
        monkeypatch.setattr(procfs, "PROC", str(tmp_path.parent))
        return procfs.leer_maps(tmp_path.name)

    def test_cantidad_de_regiones(self, regiones):
        assert len(regiones) == 8

    def test_calcula_el_tamano(self, regiones):
        # 5bf9c4e2c000 - 5bf9c4e25000 = 0x7000 = 28672 bytes = 28 kB
        assert regiones[0]["tam_kb"] == 28

    def test_region_anonima_sin_ruta(self, regiones):
        assert regiones[4]["ruta"] == ""

    def test_agrupacion(self, regiones):
        g = procfs.agrupar_segmentos(regiones)
        assert "text" in g       # r-xp con ruta
        assert "heap" in g       # [heap]
        assert "stack" in g      # [stack]
        assert "shared" in g     # rw-s  <- memoria compartida (clase 7)
        assert "kernel" in g     # [vsyscall]
        assert "anon" in g       # rw-p sin ruta
        assert g["heap"]["regiones"] == 1

    def test_ruta_con_espacios(self, tmp_path, monkeypatch):
        """
        Misma familia de trampa que el comm: se usa split(maxsplit=5) porque
        la ruta puede contener espacios.
        """
        (tmp_path / "maps").write_text(
            "7f00-7f01 r-xp 00000000 fc:01 123 /home/user/mi carpeta/lib.so\n")
        monkeypatch.setattr(procfs, "PROC", str(tmp_path.parent))
        regiones = procfs.leer_maps(tmp_path.name)
        assert regiones[0]["ruta"] == "/home/user/mi carpeta/lib.so"

    def test_inexistente_devuelve_none(self):
        assert procfs.leer_maps(999999) is None


# ===========================================================================
# cmdline
# ===========================================================================

class TestCmdline:
    """
    cmdline separa los argv con BYTES NULOS, no con espacios, porque un
    argumento puede contener espacios.
    """

    def _leer(self, contenido, tmp_path, monkeypatch):
        (tmp_path / "cmdline").write_bytes(contenido)
        monkeypatch.setattr(procfs, "PROC", str(tmp_path.parent))
        return procfs.leer_cmdline(tmp_path.name)

    def test_argumentos_separados_por_nulos(self, tmp_path, monkeypatch):
        cmd = self._leer(b"python3\x00-c\x00import time; time.sleep(60)\x00",
                         tmp_path, monkeypatch)
        assert cmd == "python3 -c import time; time.sleep(60)"

    def test_kernel_thread_tiene_cmdline_vacio(self, tmp_path, monkeypatch):
        """
        Los kernel threads no vienen de un exec(), así que no tienen argv.
        Se devuelve "" (existe pero está vacío), NO None (que significa
        "no se pudo leer").
        """
        assert self._leer(b"", tmp_path, monkeypatch) == ""
        assert self._leer(b"\x00", tmp_path, monkeypatch) == ""


# ===========================================================================
# Clasificación de file descriptors
# ===========================================================================

class TestClasificarFd:

    @pytest.mark.parametrize("destino,esperado", [
        ("socket:[95130]", "socket"),
        ("pipe:[12345]", "pipe"),
        ("anon_inode:[eventfd]", "anon"),
        ("/dev/pts/3", "tty"),
        ("/dev/null", "dev"),
        ("/dev/urandom", "dev"),
        ("/home/user/archivo.log", "file"),
        ("/memfd:algo (deleted)", "memfd"),
        (None, "?"),
    ])
    def test_tipos(self, destino, esperado):
        assert procfs.clasificar_fd(destino) == esperado


# ===========================================================================
# Cálculo de porcentaje de CPU global
# ===========================================================================

class TestPorcentajeCpu:
    """
    El %CPU NO existe en /proc: hay que calcularlo con dos lecturas. Esta es
    la razón de que el agregador exista como proceso con memoria propia.
    """

    def test_cien_por_ciento_ocupado(self):
        antes = {"user": 0, "system": 0, "idle": 0}
        ahora = {"user": 100, "system": 0, "idle": 0}
        assert procfs.porcentaje_cpu_global(antes, ahora)["uso"] == 100.0

    def test_cien_por_ciento_ocioso(self):
        antes = {"user": 0, "system": 0, "idle": 0}
        ahora = {"user": 0, "system": 0, "idle": 100}
        r = procfs.porcentaje_cpu_global(antes, ahora)
        assert r["idle"] == 100.0
        assert r["uso"] == 0.0

    def test_mitad_y_mitad(self):
        antes = {"user": 0, "system": 0, "idle": 0}
        ahora = {"user": 25, "system": 25, "idle": 50}
        r = procfs.porcentaje_cpu_global(antes, ahora)
        assert r["user"] == 25.0
        assert r["uso"] == 50.0

    def test_sin_delta_no_divide_por_cero(self):
        """Dos lecturas idénticas: no hay tiempo transcurrido que repartir."""
        igual = {"user": 10, "idle": 90}
        assert procfs.porcentaje_cpu_global(igual, igual) == {}

    def test_sin_muestra_previa(self):
        assert procfs.porcentaje_cpu_global(None, {"user": 1}) == {}


# ===========================================================================
# meminfo
# ===========================================================================

MEMINFO_MUESTRA = """\
MemTotal:       15505860 kB
MemFree:          786400 kB
MemAvailable:    5560568 kB
Buffers:          307868 kB
Cached:          4900552 kB
SwapTotal:       4194300 kB
SwapFree:        3116028 kB
"""


class TestMeminfo:

    @pytest.fixture
    def meminfo(self, tmp_path, monkeypatch):
        (tmp_path / "meminfo").write_text(MEMINFO_MUESTRA)
        monkeypatch.setattr(procfs, "PROC", str(tmp_path))
        return procfs.leer_meminfo()

    def test_descarta_la_unidad(self, meminfo):
        assert meminfo["MemTotal"] == 15505860

    def test_usada_usa_available_y_no_free(self, meminfo):
        """
        La trampa clásica: MemTotal - MemFree cuenta el cache de disco como
        memoria usada y reporta la máquina casi llena cuando no lo está.
        """
        usada = procfs.memoria_usada_kb(meminfo)
        assert usada == 15505860 - 5560568          # total - available
        assert usada != 15505860 - 786400           # total - free (incorrecto)
        # la diferencia entre las dos formas son ~4.5 GB de cache reclamable
        assert (15505860 - 786400) - usada > 4_000_000

    def test_sin_memavailable_usa_aproximacion(self):
        """Kernels < 3.14 no tienen MemAvailable."""
        viejo = {"MemTotal": 1000, "MemFree": 100, "Buffers": 50, "Cached": 200}
        assert procfs.memoria_usada_kb(viejo) == 1000 - 350


# ===========================================================================
# Constantes del sistema
# ===========================================================================

class TestConstantes:

    def test_hz_es_razonable(self):
        """No se hardcodea 100: depende de cómo se compiló el kernel."""
        assert procfs.hz() in (100, 250, 300, 1000)

    def test_tamano_pagina(self):
        assert procfs.tamano_pagina() >= 4096

    def test_usuario_root(self):
        assert procfs.usuario_de_uid(0) == "root"

    def test_uid_inexistente_devuelve_el_numero(self):
        """Caso normal en Docker: el UID del host no está en el passwd."""
        assert procfs.usuario_de_uid(999999) == "999999"


# ===========================================================================
# Contra el sistema real: propiedades, no valores exactos
# ===========================================================================

class TestSistemaReal:
    """
    Estos sí tocan /proc real, pero solo afirman PROPIEDADES que tienen que
    valer siempre, nunca valores concretos.
    """

    def test_yo_estoy_en_la_lista(self):
        assert os.getpid() in procfs.listar_pids()

    def test_el_pid_1_existe(self):
        assert 1 in procfs.listar_pids()

    def test_tid_principal_igual_al_pid(self):
        """
        En Linux el PID es el TID del primer task del grupo. Por eso el
        thread principal siempre tiene TID == PID.
        """
        mio = os.getpid()
        assert mio in procfs.listar_tids(mio)

    def test_puedo_leer_mi_propio_status(self):
        status = procfs.leer_status(os.getpid())
        assert status is not None
        assert int(status["Pid"]) == os.getpid()

    def test_mis_fds_estandar_existen(self):
        fds = procfs.listar_fds(os.getpid())
        assert fds is not None
        numeros = {f["fd"] for f in fds}
        assert {0, 1, 2}.issubset(numeros)
