"""
base.py — Loop común de los 7 analizadores.

Los 7 hacen exactamente lo mismo salvo por una función: qué extraen de cada
proceso. Este módulo tiene el loop; cada analizador concreto aporta su
`extraer`.

Existe para no repetir 7 veces los mismos tres detalles sutiles:
  1. releer el intervalo en CADA vuelta (si no, +/- no tiene efecto)
  2. dormir de forma interrumpible (si no, el shutdown tarda hasta 10 s)
  3. descartar la muestra vieja cuando la cola está llena (si no, se bloquea)
"""

import queue as queue_mod
import time

import senales


def enviar_descartando_viejo(cola, item):
    """
    Mete `item` en la cola. Si está llena, tira la muestra MÁS VIEJA y mete
    la nueva.

    Por qué descartar y no bloquear: en un monitor en tiempo real, un snapshot
    de hace 30 segundos no sirve para nada. Si el agregador se atrasa,
    preferimos perder muestras intermedias antes que frenar al analizador —
    frenarlo haría que además se atrase la lectura de /proc y los datos
    quedarían viejos igual, pero encima sin que nadie se entere.

    Sobre la race condition: entre el get_nowait() y el put_nowait() podría
    meterse otro proceso. Acá NO pasa, porque cada analizador tiene su PROPIA
    cola: hay un solo escritor. Si las 7 compartieran una cola, dos podrían
    encontrarla llena a la vez, sacar dos y meter dos, y el segundo put
    volvería a fallar. La elección de una cola por analizador elimina el
    problema por diseño en vez de resolverlo con un lock.
    """
    try:
        cola.put_nowait(item)
        return True
    except queue_mod.Full:
        try:
            cola.get_nowait()          # descarto la más vieja
        except queue_mod.Empty:
            pass                        # el agregador la sacó justo antes
        try:
            cola.put_nowait(item)
            return True
        except queue_mod.Full:
            return False                # no debería pasar con un solo escritor


def dormir_interrumpible(parar, segundos):
    """
    Duerme hasta `segundos`, pero devuelve apenas alguien setea el Event.

    Con time.sleep(10) pelado, el analizador de señales tardaría hasta 10
    segundos en enterarse de que hay que parar, y el shutdown se sentiría
    colgado. Event.wait() se despierta inmediatamente.
    """
    if segundos > 0:
        parar.wait(timeout=segundos)


def correr(nombre, extraer, snapshot, cola, intervalo, parar, por_pid=True,
           verbose=None):
    """
    Loop principal de un analizador. Corre en su propio proceso.

    Parámetros:
      nombre   : str, la dimensión ("resumen", "memoria", ...)
      extraer  : callable con firma extraer(pid, verbose) si por_pid=True, o
                 extraer(verbose) si por_pid=False. Devuelve dict|None.
      snapshot : DictProxy. SOLO SE LEE de acá ("pids"). Nunca se escribe:
                 el único escritor del snapshot es el agregador.
      cola     : Queue propia de este analizador.
      intervalo: Value('d') con su período, ajustable con +/- desde el display.
      parar    : Event de shutdown.
      por_pid  : False para el analizador "sistema", que lee archivos globales
                 y no itera sobre procesos.
      verbose  : Value('b') compartido, toggleado por SIGUSR2 desde el padre.
                 Todos los analizadores lo reciben aunque solo 'fds' y
                 'threads' lo usen: mantener la firma uniforme evita tener
                 que ramificar el cableado en main.py.
    """
    senales.preparar_hijo()

    while not parar.is_set():
        t0 = time.monotonic()

        # Se lee UNA vez por vuelta, no una vez por proceso: son ~490
        # lecturas de memoria compartida que no aportan nada, y además así
        # toda la muestra queda coherente (no puede quedar mitad verbose y
        # mitad no si el usuario aprieta SIGUSR2 a mitad del recorrido).
        detallado = bool(verbose.value) if verbose is not None else False

        if por_pid:
            # Lectura del canal 2. Devuelve una COPIA de la lista: el
            # DictProxy serializa el valor y lo manda por el socket. Por eso
            # se lee una sola vez por vuelta y no dentro del for.
            pids = snapshot.get("pids", [])
            datos = {}
            for pid in pids:
                d = extraer(pid, detallado)
                # None = el proceso murió entre que el recolector lo listó y
                # que llegamos acá, o no tenemos permisos. Es el caso normal,
                # no un error: simplemente no aparece en esta muestra.
                if d is not None:
                    datos[pid] = d
        else:
            datos = extraer(detallado)

        # Canal 3: muestra CRUDA al agregador. Ojo con lo que NO va acá:
        # no hay ningún porcentaje calculado. Los porcentajes necesitan la
        # lectura anterior, y este proceso no la guarda.
        enviar_descartando_viejo(cola, {
            "dimension": nombre,
            "ts": time.time(),
            "duracion": time.monotonic() - t0,   # cuánto costó la muestra
            "datos": datos,
        })

        # Canal 6: se relee en CADA vuelta. Si se leyera una sola vez antes
        # del while, apretar +/- no haría nada hasta reiniciar el proceso.
        with intervalo.get_lock():
            periodo = intervalo.value

        dormir_interrumpible(parar, periodo - (time.monotonic() - t0))
