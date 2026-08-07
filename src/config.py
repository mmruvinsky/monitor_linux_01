"""
config.py — Carga y validación de config.json.

Este módulo existe por SIGHUP: la consigna pide poder recargar la
configuración en caliente. Por eso `cargar()` tiene que poder llamarse muchas
veces durante la vida del proceso y NUNCA lanzar una excepción: si el usuario
edita el JSON y lo deja mal formado mientras el monitor corre, recargar tiene
que degradar a los valores actuales, no matar el monitor.
"""

import json
import os

# Orden canónico de los 7 analizadores. Este orden define el índice de cada
# uno en las listas de Queues y de Values compartidos, así que TIENE que ser
# estable: el analizador i usa colas[i] e intervalos[i].
DIMENSIONES = [
    "resumen",
    "memoria",
    "fds",
    "threads",
    "senales",
    "scheduling",
    "sistema",
]

# Valores por defecto según la tabla de la consigna. Si config.json no existe
# o está roto, el monitor arranca igual con esto.
DEFAULTS = {
    "intervalos": {
        "recolector": 1.0,
        "resumen": 2.0,
        "memoria": 3.0,
        "fds": 5.0,
        "threads": 2.0,
        "senales": 10.0,
        "scheduling": 10.0,
        "sistema": 2.0,
    },
    "intervalos_minimos": {
        "resumen": 0.5,
        "memoria": 1.0,
        "fds": 2.0,
        "threads": 0.5,
        "senales": 5.0,
        "scheduling": 5.0,
        "sistema": 1.0,
    },
    "cola_maxsize": 4,
    "vista_inicial": "resumen",
    "filtro_comando": "",
    "filtro_usuario": "",
    "orden": "cpu",
    "verbose": False,
}

RUTA_DEFAULT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json"
)


def _fusionar(base, encima):
    """
    Fusión superficial por dimensión: si el JSON del usuario define solo
    algunos intervalos, los demás quedan con el default en vez de
    desaparecer. Sin esto, un config.json parcial dejaría analizadores sin
    intervalo definido.
    """
    resultado = {}
    for clave, valor in base.items():
        if isinstance(valor, dict):
            copia = dict(valor)
            copia.update(encima.get(clave, {}) or {})
            resultado[clave] = copia
        else:
            resultado[clave] = encima.get(clave, valor)
    return resultado


def cargar(ruta=None):
    """
    Lee config.json y devuelve un dict validado. Nunca lanza excepción.

    Devuelve siempre una configuración usable: si el archivo no existe, está
    mal formado, o tiene tipos incorrectos, se cae a DEFAULTS para lo que
    falle y se conserva lo que sí es válido.
    """
    ruta = ruta or RUTA_DEFAULT
    crudo = {}
    try:
        with open(ruta, "r", encoding="utf-8") as f:
            leido = json.load(f)
        if isinstance(leido, dict):
            crudo = leido
    except (FileNotFoundError, PermissionError, json.JSONDecodeError, OSError):
        # Silencio deliberado: recargar con SIGHUP un JSON roto no debe
        # tumbar el monitor. Se sigue con los defaults.
        pass

    cfg = _fusionar(DEFAULTS, crudo)

    # Validación de tipos y rangos. Un intervalo negativo o cero haría que el
    # analizador entre en busy-loop y queme un core entero.
    for nombre in ["recolector"] + DIMENSIONES:
        cfg["intervalos"][nombre] = _positivo(
            cfg["intervalos"].get(nombre), DEFAULTS["intervalos"][nombre]
        )
    for nombre in DIMENSIONES:
        cfg["intervalos_minimos"][nombre] = _positivo(
            cfg["intervalos_minimos"].get(nombre),
            DEFAULTS["intervalos_minimos"][nombre],
        )
        # Un intervalo por debajo del mínimo de la consigna se sube al mínimo.
        if cfg["intervalos"][nombre] < cfg["intervalos_minimos"][nombre]:
            cfg["intervalos"][nombre] = cfg["intervalos_minimos"][nombre]

    try:
        cfg["cola_maxsize"] = max(1, int(cfg["cola_maxsize"]))
    except (TypeError, ValueError):
        cfg["cola_maxsize"] = DEFAULTS["cola_maxsize"]

    if cfg.get("vista_inicial") not in DIMENSIONES:
        cfg["vista_inicial"] = DEFAULTS["vista_inicial"]

    cfg["verbose"] = bool(cfg.get("verbose", False))
    return cfg


def _positivo(valor, defecto):
    """Convierte a float positivo; si no se puede, devuelve el default."""
    try:
        v = float(valor)
    except (TypeError, ValueError):
        return defecto
    return v if v > 0 else defecto


def intervalos_en_orden(cfg):
    """
    Devuelve los 7 intervalos en el orden de DIMENSIONES, listo para crear
    los Value compartidos con el mismo índice que las colas.
    """
    return [cfg["intervalos"][d] for d in DIMENSIONES]
