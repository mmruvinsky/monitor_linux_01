"""
display.py — La TUI. Corre en su propio proceso.

Reglas que este módulo respeta y que conviene tener presentes al leerlo:

  · NO lee /proc jamás. Todo sale del snapshot.
  · NO escribe al snapshot. El único escritor es el agregador.
  · Lo único que escribe son los Value('d') de intervalos, cuando apretás +/-.
  · Si el snapshot está viejo o vacío, dibuja lo que hay y marca la
    antigüedad. Nunca se cuelga esperando datos.

El estado de la interfaz —vista activa, fila seleccionada, PID pineado,
filtros, criterio de orden— vive SOLO acá. Nadie más lo necesita, así que no
se comparte con nadie: es memoria privada de este proceso.
"""

import time

from rich.align import Align
from rich.console import Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

import config
import senales
from teclado import Teclado

# vista -> (numero, tecla alternativa, titulo)
VISTAS = {
    "resumen":    ("1", "r", "Resumen"),
    "memoria":    ("2", "m", "Memoria"),
    "fds":        ("3", "f", "File Descriptors"),
    "threads":    ("4", "t", "Threads (LWPs)"),
    "senales":    ("5", "s", "Señales"),
    "scheduling": ("6", "p", "Scheduling"),
    "sistema":    ("7", "g", "Sistema global"),
}
POR_TECLA = {}
for _dim, (_num, _alt, _tit) in VISTAS.items():
    POR_TECLA[_num] = _dim
    POR_TECLA[_alt] = _dim

ORDENES = ["cpu", "rss_kb", "pid"]
ETIQUETA_ORDEN = {"cpu": "CPU%", "rss_kb": "RSS", "pid": "PID"}

COLOR_ESTADO = {
    "R": "bold green",
    "S": "white",
    "D": "bold yellow",
    "T": "magenta",
    "t": "magenta",
    "Z": "bold red",
    "I": "dim",
}


class Display:
    def __init__(self, snapshot, intervalos, verbose, parar, cfg):
        self.snapshot = snapshot
        self.intervalos = intervalos
        self.verbose = verbose
        self.parar = parar
        self.cfg = cfg

        # --- estado privado de la UI ---
        self.vista = cfg.get("vista_inicial", "resumen")
        self.seleccion = 0            # índice en la lista filtrada
        self.pin = None               # PID pineado, o None
        self.filtro_cmd = cfg.get("filtro_comando", "")
        self.filtro_usr = cfg.get("filtro_usuario", "")
        self.orden = cfg.get("orden", "cpu")
        self.mostrar_ayuda = False
        self.modo_entrada = None      # None | "cmd" | "usr"
        self.buffer_entrada = ""
        self.mensaje = ""
        self.filas = []               # última lista renderizada

    # ==================================================================
    # Entrada de teclado
    # ==================================================================

    def procesar(self, tecla):
        # Si estamos capturando texto para un filtro, todas las teclas van al
        # buffer salvo Enter (confirmar) y Escape (cancelar).
        if self.modo_entrada:
            return self._procesar_entrada(tecla)

        if tecla in ("q", "\x03"):        # q o Ctrl+C
            self.parar.set()
        elif tecla in ("h", "?"):
            self.mostrar_ayuda = not self.mostrar_ayuda
        elif tecla in POR_TECLA:
            self.vista = POR_TECLA[tecla]
            self.mensaje = f"vista: {VISTAS[self.vista][2]}"
        elif tecla == "ARRIBA":
            self.seleccion = max(0, self.seleccion - 1)
        elif tecla == "ABAJO":
            self.seleccion = min(max(0, len(self.filas) - 1), self.seleccion + 1)
        elif tecla == "PGUP":
            self.seleccion = max(0, self.seleccion - 10)
        elif tecla == "PGDN":
            self.seleccion = min(max(0, len(self.filas) - 1), self.seleccion + 10)
        elif tecla in ("\r", "\n"):
            self._toggle_pin()
        elif tecla == "/":
            self.modo_entrada = "cmd"
            self.buffer_entrada = self.filtro_cmd
        elif tecla == "u":
            self.modo_entrada = "usr"
            self.buffer_entrada = self.filtro_usr
        elif tecla == "c":
            self.orden = ORDENES[(ORDENES.index(self.orden) + 1) % len(ORDENES)]
            self.mensaje = f"orden: {ETIQUETA_ORDEN[self.orden]}"
        elif tecla in ("+", "="):
            self._ajustar_intervalo(+0.5)
        elif tecla in ("-", "_"):
            self._ajustar_intervalo(-0.5)
        elif tecla == "ESC":
            self.pin = None
            self.mensaje = "pin liberado"

    def _procesar_entrada(self, tecla):
        if tecla in ("\r", "\n"):
            if self.modo_entrada == "cmd":
                self.filtro_cmd = self.buffer_entrada
            else:
                self.filtro_usr = self.buffer_entrada
            self.modo_entrada = None
            self.buffer_entrada = ""
            self.seleccion = 0
        elif tecla == "ESC":
            self.modo_entrada = None
            self.buffer_entrada = ""
        elif tecla in ("\x7f", "\b"):
            self.buffer_entrada = self.buffer_entrada[:-1]
        elif len(tecla) == 1 and tecla.isprintable():
            self.buffer_entrada += tecla

    def _toggle_pin(self):
        """
        Pin: fija un PID para que el panel de detalle lo siga mostrando aunque
        cambie el orden de la lista. Sin esto, mirar un proceso que sube y
        baja en el ranking de CPU es imposible.
        """
        pid = self._pid_seleccionado()
        if pid is None:
            return
        if self.pin == pid:
            self.pin = None
            self.mensaje = "pin liberado"
        else:
            self.pin = pid
            self.mensaje = f"pin en {pid}"

    def _ajustar_intervalo(self, delta):
        """
        Canal 6: escribe el Value compartido del analizador de la vista
        activa. El analizador lo relee en su siguiente vuelta, así que el
        cambio se aplica sin reiniciar nada.

        Se respeta el mínimo de la consigna por vista: bajar demasiado haría
        que el analizador consuma más CPU que los procesos que observa.
        """
        compartido = self.intervalos.get(self.vista)
        if compartido is None:
            return
        minimo = self.cfg["intervalos_minimos"].get(self.vista, 0.5)
        with compartido.get_lock():
            nuevo = max(minimo, min(60.0, compartido.value + delta))
            compartido.value = nuevo
        self.mensaje = f"{VISTAS[self.vista][2]}: {nuevo:.1f}s (min {minimo}s)"

    # ==================================================================
    # Datos
    # ==================================================================

    def _pid_seleccionado(self):
        if self.pin is not None:
            return self.pin
        if 0 <= self.seleccion < len(self.filas):
            return self.filas[self.seleccion]["pid"]
        return None

    def _procesos(self):
        """Lista de procesos filtrada y ordenada, desde la dimensión resumen."""
        datos = self.snapshot.get("resumen", {}) or {}
        filas = list(datos.values())

        if self.filtro_cmd:
            aguja = self.filtro_cmd.lower()
            filas = [f for f in filas
                     if aguja in (f.get("cmdline") or "").lower()
                     or aguja in (f.get("comm") or "").lower()]
        if self.filtro_usr:
            aguja = self.filtro_usr.lower()
            filas = [f for f in filas if aguja in (f.get("usuario") or "").lower()]

        inverso = self.orden != "pid"
        filas.sort(key=lambda f: f.get(self.orden) or 0, reverse=inverso)
        return filas

    def _edad(self, dimension):
        ts = self.snapshot.get(f"{dimension}_ts")
        return (time.time() - ts) if ts else None

    def _stale(self, dimension):
        """
        Un analizador muerto no vacía el snapshot: el Manager conserva su
        último valor para siempre. Sin este chequeo, la TUI mostraría datos
        congelados como si fueran frescos. Se considera viejo a partir de
        3 intervalos sin actualizar.
        """
        edad = self._edad(dimension)
        if edad is None:
            return True
        compartido = self.intervalos.get(dimension)
        periodo = compartido.value if compartido else 2.0
        return edad > 3 * periodo

    # ==================================================================
    # Render
    # ==================================================================

    def render(self):
        layout = Layout()
        layout.split_column(
            Layout(self.cabecera(), size=5),
            Layout(self.tabla_procesos(), name="lista"),
            Layout(self.detalle(), name="detalle", size=16),
            Layout(self.pie(), size=4),
        )
        if self.mostrar_ayuda:
            return self.ayuda()
        return layout

    # ------------------------------------------------------------------

    def cabecera(self):
        sis = self.snapshot.get("sistema", {}) or {}
        der = self.snapshot.get("derivados", {}) or {}
        cpu = sis.get("cpu_pct", {}) or {}
        mem = sis.get("mem", {}) or {}
        load = sis.get("load", {}) or {}
        up = sis.get("uptime", {}) or {}

        uso = cpu.get("uso", 0.0)
        t = Table.grid(padding=(0, 2))
        t.add_column(justify="left")
        t.add_column(justify="left")
        t.add_column(justify="left")

        t.add_row(
            self._barra("CPU", uso, 100.0),
            f"user [cyan]{cpu.get('user', 0)}%[/]  sys [magenta]{cpu.get('system', 0)}%[/]  "
            f"iowait [yellow]{cpu.get('iowait', 0)}%[/]  idle [dim]{cpu.get('idle', 0)}%[/]",
            f"load [bold]{load.get('1min', 0)}[/] {load.get('5min', 0)} {load.get('15min', 0)}"
            f"  sobre {load.get('cpus', '?')} cores",
        )
        usada = mem.get("usada_kb", 0) / 1024
        total = max(1, mem.get("total_kb", 1)) / 1024
        swap_u = (mem.get("swap_total_kb", 0) - mem.get("swap_libre_kb", 0)) / 1024
        t.add_row(
            self._barra("MEM", usada, total),
            f"{usada:,.0f} / {total:,.0f} MB   cache [dim]{mem.get('cached_kb', 0)/1024:,.0f} MB[/]"
            f"   swap {swap_u:,.0f} MB",
            f"procs [bold]{der.get('procesos_totales', 0)}[/]  "
            f"threads [bold]{der.get('threads_totales', 0)}[/]  "
            f"zombies [{'bold red' if der.get('zombies') else 'dim'}]{der.get('zombies', 0)}[/]"
            f"  up {up.get('uptime_s', 0)/3600:.1f}h",
        )
        estados = der.get("por_estado", {}) or {}
        t.add_row(
            Text("estados", style="dim"),
            "  ".join(f"[{COLOR_ESTADO.get(k, 'white')}]{k}[/]:{v}"
                      for k, v in sorted(estados.items(), key=lambda x: -x[1])),
            "",
        )
        return Panel(t, title="[bold]monitor_linux_01[/]", border_style="blue")

    @staticmethod
    def _barra(etiqueta, valor, maximo, ancho=22):
        frac = 0.0 if maximo <= 0 else max(0.0, min(1.0, valor / maximo))
        llenos = int(frac * ancho)
        color = "green" if frac < 0.6 else ("yellow" if frac < 0.85 else "red")
        barra = f"[{color}]{'█' * llenos}[/][dim]{'░' * (ancho - llenos)}[/]"
        return Text.from_markup(f"{etiqueta} [{barra}] {frac*100:5.1f}%")

    # ------------------------------------------------------------------

    def tabla_procesos(self):
        self.filas = self._procesos()
        if self.seleccion >= len(self.filas):
            self.seleccion = max(0, len(self.filas) - 1)

        t = Table(expand=True, box=None, pad_edge=False, header_style="bold blue")
        t.add_column("PID", justify="right", width=8)
        t.add_column("USUARIO", width=12, overflow="ellipsis")
        t.add_column("S", width=1)
        t.add_column("CPU%", justify="right", width=6)
        t.add_column("RSS", justify="right", width=10)
        t.add_column("THR", justify="right", width=4)
        # ratio=1 hace que COMANDO se quede con el espacio sobrante en vez de
        # pelearlo con las columnas de ancho fijo. Sin esto, rich mide el
        # cmdline completo (Chrome pasa los 300 caracteres), decide que la
        # tabla no entra, y encoge TODAS las columnas hasta dejarlas en "…".
        t.add_column("COMANDO", overflow="ellipsis", no_wrap=True, ratio=1)

        # Ventana visible alrededor de la selección: no tiene sentido mandarle
        # 460 filas a rich cuando entran 20 en pantalla.
        alto = 20
        inicio = max(0, self.seleccion - alto // 2)
        visibles = self.filas[inicio:inicio + alto]

        for i, f in enumerate(visibles, start=inicio):
            estado = f.get("estado") or "?"
            estilo = "reverse" if i == self.seleccion else ""
            marca = "[bold cyan]▸[/]" if f["pid"] == self.pin else " "
            t.add_row(
                f"{marca}{f['pid']}",
                f.get("usuario") or "?",
                Text(estado, style=COLOR_ESTADO.get(estado, "white")),
                self._cpu_coloreado(f.get("cpu") or 0.0),
                self._kb(f.get("rss_kb")),
                str(f.get("threads") or "-"),
                # Se corta acá y no solo con overflow="ellipsis" porque rich
                # usa el largo del texto para repartir el ancho de la tabla.
                (f.get("cmdline") or f.get("comm") or "")[:120],
                style=estilo,
            )

        edad = self._edad("resumen")
        aviso = " [red](sin actualizar)[/]" if self._stale("resumen") else ""
        titulo = (f"Procesos: [bold]{len(self.filas)}[/]"
                  f"  orden [bold]{ETIQUETA_ORDEN[self.orden]}[/]"
                  f"  actualizado hace {edad:.1f}s" if edad is not None else "Procesos")
        return Panel(t, title=titulo + aviso, border_style="blue", title_align="left")

    @staticmethod
    def _cpu_coloreado(v):
        color = "dim" if v < 1 else ("white" if v < 25 else ("yellow" if v < 75 else "bold red"))
        return Text(f"{v:.1f}", style=color)

    @staticmethod
    def _kb(v):
        if v is None:
            return "-"
        if v >= 1024 * 1024:
            return f"{v/1024/1024:.1f}G"
        if v >= 1024:
            return f"{v/1024:.1f}M"
        return f"{v}K"

    # ------------------------------------------------------------------

    def detalle(self):
        pid = self._pid_seleccionado()
        titulo = VISTAS[self.vista][2]
        if self.pin is not None:
            titulo += f"  [cyan](pin en {self.pin})[/]"
        if self._stale(self.vista):
            titulo += "  [red]· datos viejos o analizador caído[/]"
        else:
            edad = self._edad(self.vista)
            if edad is not None:
                intervalo = self.intervalos[self.vista].value
                titulo += f"  [dim]· hace {edad:.1f}s · cada {intervalo:.1f}s[/]"

        cuerpo = getattr(self, f"vista_{self.vista}")(pid)
        return Panel(cuerpo, title=titulo, border_style="green", title_align="left")

    def _dim(self, dimension, pid):
        datos = self.snapshot.get(dimension, {}) or {}
        # Las claves del snapshot son int, pero después de pasar por JSON o
        # por algunos proxies pueden llegar como str. Se prueban las dos.
        return datos.get(pid) or datos.get(str(pid))

    # --- vista 1 -------------------------------------------------------

    def vista_resumen(self, pid):
        d = self._dim("resumen", pid)
        if not d:
            return Align.center(Text("seleccioná un proceso", style="dim"))
        t = Table.grid(padding=(0, 3))
        t.add_column(style="bold blue", justify="right")
        t.add_column()
        t.add_row("PID / PPID", f"{d['pid']}  /  {d.get('ppid')}")
        t.add_row("Usuario", f"{d.get('usuario')} (uid {d.get('uid')})")
        t.add_row("Estado", f"[{COLOR_ESTADO.get(d.get('estado'),'white')}]"
                            f"{d.get('estado')} — {d.get('estado_desc')}[/]")
        t.add_row("CPU", f"{d.get('cpu')}%   (utime {d.get('utime')} + stime {d.get('stime')} jiffies)")
        t.add_row("RSS", self._kb(d.get("rss_kb")))
        t.add_row("Threads", str(d.get("threads")))
        t.add_row("comm", d.get("comm") or "")
        t.add_row("cmdline", (d.get("cmdline") or "")[:160])
        return t

    # --- vista 2 -------------------------------------------------------

    def vista_memoria(self, pid):
        d = self._dim("memoria", pid)
        if not d:
            return Align.center(Text("sin datos de memoria para este proceso", style="dim"))

        izq = Table.grid(padding=(0, 3))
        izq.add_column(style="bold blue", justify="right")
        izq.add_column(justify="right")
        for etiqueta, clave in [("VmSize", "virtual_kb"), ("VmRSS", "rss_kb"),
                                ("VmHWM", "hwm_kb"), ("VmData", "data_kb"),
                                ("VmStk", "stack_kb"), ("VmExe", "text_kb"),
                                ("VmLib", "lib_kb"), ("VmSwap", "swap_kb")]:
            izq.add_row(etiqueta, self._kb(d.get(clave)))
        izq.add_row("", "")
        izq.add_row("minor faults", f"{d.get('minflt', 0):,}")
        izq.add_row("major faults",
                    f"[{'bold red' if d.get('majflt', 0) > 1000 else 'white'}]"
                    f"{d.get('majflt', 0):,}[/]")

        segs = d.get("segmentos")
        if segs is None:
            der = Text("\n/proc/<pid>/maps no legible\n(proceso de otro usuario)",
                       style="dim yellow")
        else:
            der = Table(box=None, header_style="bold blue")
            der.add_column("segmento")
            der.add_column("tamaño", justify="right")
            der.add_column("regiones", justify="right")
            for k, v in sorted(segs.items(), key=lambda x: -x[1]["kb"]):
                der.add_row(k, self._kb(v["kb"]), str(v["regiones"]))

        fila = Table.grid(padding=(0, 6))
        fila.add_column()
        fila.add_column()
        fila.add_row(izq, der)
        return fila

    # --- vista 3 -------------------------------------------------------

    def vista_fds(self, pid):
        d = self._dim("fds", pid)
        if not d:
            return Align.center(Text("sin datos de FDs", style="dim"))
        if d.get("sin_permiso"):
            return Align.center(Text(
                "/proc/<pid>/fd/ no legible: el directorio es del dueño del proceso.\n"
                "Solo se ven los FDs de tus propios procesos (o todos, si corrés como root).",
                style="yellow", justify="center"))

        t = Table(box=None, header_style="bold blue", expand=True)
        t.add_column("FD", justify="right", width=5)
        t.add_column("", width=7)
        t.add_column("tipo", width=8)
        # ratio=1: mismo motivo que en la lista de procesos. Sin esto, un
        # destino largo hace que rich encoja las columnas fijas a "…".
        t.add_column("destino", overflow="ellipsis", no_wrap=True, ratio=1)
        for f in d.get("fds", []):
            t.add_row(str(f["fd"]),
                      Text(f.get("estandar") or "", style="cyan"),
                      f["tipo"], f["destino"][:110])
        resumen = Text.from_markup(
            f"total [bold]{d.get('total')}[/]   "
            + "  ".join(f"{k}:[bold]{v}[/]" for k, v in sorted(d.get("por_tipo", {}).items()))
            + ("   [yellow](lista truncada)[/]" if d.get("truncado") else ""))
        return Group(resumen, t)

    # --- vista 4 -------------------------------------------------------

    def vista_threads(self, pid):
        d = self._dim("threads", pid)
        if not d:
            return Align.center(Text("sin datos de threads", style="dim"))
        t = Table(box=None, header_style="bold blue", expand=True)
        t.add_column("TID", justify="right", width=9)
        t.add_column("nombre", width=18, overflow="ellipsis")
        t.add_column("S", width=1)
        t.add_column("CPU%", justify="right", width=6)
        t.add_column("ctx vol", justify="right", width=11)
        t.add_column("ctx invol", justify="right", width=11)
        # Columna elástica al final: absorbe el ancho sobrante para que las
        # anteriores no queden separadas por metros de espacio.
        t.add_column("", ratio=1)
        for h in d.get("threads", []):
            t.add_row(str(h["tid"]), h.get("nombre") or "",
                      Text(h.get("estado") or "?",
                           style=COLOR_ESTADO.get(h.get("estado"), "white")),
                      self._cpu_coloreado(h.get("cpu") or 0.0),
                      f"{h.get('vol_ctxt', 0):,}", f"{h.get('invol_ctxt', 0):,}",
                      Text("PRINCIPAL", style="bold cyan") if h.get("principal") else "")
        cab = Text.from_markup(
            f"threads [bold]{d.get('total')}[/]   CPU sumado [bold]{d.get('cpu_total')}%[/]"
            f"   [dim]TID == PID identifica al thread principal[/]")
        return Group(cab, t)

    # --- vista 5 -------------------------------------------------------

    def vista_senales(self, pid):
        d = self._dim("senales", pid)
        if not d:
            return Align.center(Text("sin datos de señales", style="dim"))
        t = Table.grid(padding=(0, 3))
        t.add_column(style="bold blue", justify="right", width=18)
        t.add_column(width=18, style="dim")
        t.add_column()
        for etiqueta, clave, color in [
            ("Bloqueadas", "bloqueadas", "yellow"),
            ("Ignoradas", "ignoradas", "dim"),
            ("Con handler", "manejadas", "green"),
            ("Pendientes", "pendientes", "bold red"),
            ("Pend. grupo", "pendientes_grupo", "bold red"),
        ]:
            nombres = d.get(clave) or []
            t.add_row(etiqueta, d.get(clave + "_hex") or "",
                      Text(", ".join(nombres) if nombres else "—", style=color))
        t.add_row("", "", "")
        t.add_row("", "", Text(
            "SIGKILL(9) y SIGSTOP(19) nunca aparecen: el kernel no permite "
            "bloquearlas,\nignorarlas ni manejarlas.", style="dim italic"))
        return t

    # --- vista 6 -------------------------------------------------------

    def vista_scheduling(self, pid):
        d = self._dim("scheduling", pid)
        if not d:
            return Align.center(Text("sin datos de scheduling", style="dim"))
        izq = Table.grid(padding=(0, 3))
        izq.add_column(style="bold blue", justify="right")
        izq.add_column()
        izq.add_row("Nice", str(d.get("nice")))
        izq.add_row("Priority", str(d.get("priority")))
        izq.add_row("Policy", d.get("policy_nombre") or "")
        izq.add_row("RT priority", str(d.get("rt_priority")))
        izq.add_row("Afinidad CPU", d.get("affinity") or "?")
        izq.add_row("utime / stime", f"{d.get('utime')} / {d.get('stime')} jiffies")
        izq.add_row("PGID / SID", f"{d.get('pgid')}  /  {d.get('sid')}")

        vol = d.get("vol_ctxt", 0)
        invol = d.get("invol_ctxt", 0)
        perfil = "—"
        if vol + invol > 20:
            perfil = ("[cyan]I/O-bound[/] (cede la CPU por su cuenta)" if vol > invol
                      else "[red]CPU-bound[/] (el kernel se la arrebata)")
        der = Table.grid(padding=(0, 3))
        der.add_column(style="bold blue", justify="right")
        der.add_column()
        der.add_row("ctx voluntarios", f"{vol:,}")
        der.add_row("ctx involuntarios", f"{invol:,}")
        der.add_row("perfil", Text.from_markup(perfil))
        der.add_row("", "")
        der.add_row("", Text("voluntario: el proceso pidió esperar algo\n"
                             "involuntario: se le acabó el quantum",
                             style="dim italic"))

        fila = Table.grid(padding=(0, 8))
        fila.add_column()
        fila.add_column()
        fila.add_row(izq, der)
        return fila

    # --- vista 7 -------------------------------------------------------

    def vista_sistema(self, _pid):
        sis = self.snapshot.get("sistema", {}) or {}
        der = self.snapshot.get("derivados", {}) or {}
        if not sis:
            return Align.center(Text("esperando datos del sistema...", style="dim"))
        mem = sis.get("mem", {}) or {}
        load = sis.get("load", {}) or {}
        up = sis.get("uptime", {}) or {}

        izq = Table.grid(padding=(0, 3))
        izq.add_column(style="bold blue", justify="right")
        izq.add_column()
        izq.add_row("Boot", time.strftime("%Y-%m-%d %H:%M:%S",
                                          time.localtime(sis.get("btime", 0))))
        izq.add_row("Uptime", f"{up.get('uptime_s', 0)/3600:.2f} h")
        izq.add_row("Idle sumado", f"{up.get('idle_s', 0)/3600:.2f} h "
                                   f"[dim](suma de {load.get('cpus','?')} cores)[/]")
        izq.add_row("Forks desde boot", f"{sis.get('forks_desde_boot', 0):,} "
                                        "[dim](acumulado, no procesos vivos)[/]")
        izq.add_row("Corriendo / Bloqueados",
                    f"{sis.get('procs_running')} / {sis.get('procs_blocked')}")
        izq.add_row("Load", f"{load.get('1min')} {load.get('5min')} {load.get('15min')}")
        izq.add_row("Memoria", f"{mem.get('usada_kb',0)/1024:,.0f} MB usados de "
                               f"{mem.get('total_kb',0)/1024:,.0f} MB")
        izq.add_row("", Text("usada = total − MemAvailable, no − MemFree: el kernel usa\n"
                             "la RAM libre como cache y la suelta cuando hace falta",
                             style="dim italic"))

        top = Table(box=None, header_style="bold blue")
        top.add_column("top CPU", width=26, overflow="ellipsis")
        top.add_column("%", justify="right", width=7)
        for p in der.get("top_cpu", []):
            top.add_row(f"{p['pid']} {p['comm']}", f"{p.get('cpu')}")
        top.add_row("", "")
        top.add_column
        for p in der.get("top_rss", []):
            top.add_row(f"[dim]RSS[/] {p['pid']} {p['comm']}", self._kb(p.get("rss_kb")))

        fila = Table.grid(padding=(0, 8))
        fila.add_column()
        fila.add_column()
        fila.add_row(izq, top)
        return fila

    # ------------------------------------------------------------------

    def pie(self):
        if self.modo_entrada:
            etiqueta = "filtrar comando" if self.modo_entrada == "cmd" else "filtrar usuario"
            cuerpo = Text.from_markup(
                f"[bold yellow]{etiqueta}:[/] {self.buffer_entrada}[blink]▏[/]"
                "   [dim]Enter confirma · Esc cancela[/]")
        else:
            tabs = []
            for dim, (num, alt, tit) in VISTAS.items():
                estilo = "bold reverse" if dim == self.vista else "dim"
                tabs.append(f"[{estilo}] {num}:{tit} [/]")
            filtros = []
            if self.filtro_cmd:
                filtros.append(f"cmd~[bold]{self.filtro_cmd}[/]")
            if self.filtro_usr:
                filtros.append(f"user~[bold]{self.filtro_usr}[/]")
            # El estado de verbose se lee del Value compartido, no de una
            # variable local: lo cambia el PADRE al recibir SIGUSR2, no la TUI.
            if self.verbose is not None and self.verbose.value:
                filtros.append("[bold yellow]VERBOSE[/]")
            extra = ("   " + "  ".join(filtros)) if filtros else ""
            cuerpo = Text.from_markup(
                "".join(tabs) + extra
                + f"\n[dim]↑↓ navegar · Enter pin · / cmd · u user · c orden · "
                  f"+/- intervalo · h ayuda · q salir[/]"
                + (f"   [green]{self.mensaje}[/]" if self.mensaje else ""))
        return Panel(cuerpo, border_style="blue", padding=(0, 1))

    def ayuda(self):
        t = Table(box=None, header_style="bold blue", padding=(0, 3))
        t.add_column("tecla", style="bold cyan", width=22, no_wrap=True)
        t.add_column("acción")
        for tecla, accion in [
            ("1-7 / r m f t s p g", "cambiar de vista"),
            ("↑ ↓ / PgUp PgDn", "navegar por la lista"),
            ("Enter", "pin del proceso seleccionado"),
            ("Esc", "liberar el pin"),
            ("/", "filtrar por comando"),
            ("u", "filtrar por usuario"),
            ("c", "alternar orden (CPU% / RSS / PID)"),
            ("+ / -", "ajustar el intervalo de la vista activa"),
            ("h / ?", "esta ayuda"),
            ("q", "salir limpiamente"),
        ]:
            t.add_row(tecla, accion)
        señales = Table(box=None, header_style="bold blue", padding=(0, 3))
        señales.add_column("señal", style="bold cyan", width=22, no_wrap=True)
        señales.add_column("acción")
        for s, a in [("SIGINT / SIGTERM", "shutdown limpio"),
                     ("SIGHUP", "recargar config.json"),
                     ("SIGUSR1", "volcar snapshot a dump_<ts>.json"),
                     ("SIGUSR2", "toggle modo verbose"),
                     ("SIGWINCH", "repintar")]:
            señales.add_row(s, a)
        return Panel(
            Group(t, Text(""), Text("Señales (mandarlas al PID del padre):",
                                    style="bold blue"), señales),
            title="[bold]Ayuda[/] — cualquier tecla para volver",
            border_style="cyan")


def correr(snapshot, intervalos, verbose, parar, cfg):
    """Punto de entrada del proceso display."""
    senales.preparar_hijo()
    ui = Display(snapshot, intervalos, verbose, parar, cfg)

    # screen=True usa la PANTALLA ALTERNATIVA de la terminal (el mismo truco
    # de vim o less): al salir, el contenido previo de la terminal vuelve
    # intacto en vez de quedar el monitor pegado en el scrollback.
    with Teclado() as teclado, Live(ui.render(), screen=True,
                                    refresh_per_second=8,
                                    transient=False) as live:
        while not parar.is_set():
            teclas = teclado.leer_todas()
            if teclas and ui.mostrar_ayuda:
                ui.mostrar_ayuda = False
                teclas = teclas[1:]
            for tecla in teclas:
                ui.procesar(tecla)

            try:
                live.update(ui.render())
            except Exception as e:
                # Un error de render (terminal muy chica, dato inesperado) no
                # debe matar la TUI: se muestra y se sigue.
                live.update(Panel(Text(f"error de render: {e!r}", style="red")))

            # ~8 fps. El display es independiente de los analizadores: dibuja
            # lo último que haya en el snapshot, sin esperar a nadie.
            parar.wait(timeout=0.125)
