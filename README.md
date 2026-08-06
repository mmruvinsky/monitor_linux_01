# Monitor de Procesos y Threads

**Trabajo Práctico Nº 1 — Computación II — Universidad de Mendoza — 2026**

Monitor del sistema en tiempo real para Linux, con arquitectura multiproceso.
Toda la información se extrae leyendo `/proc` directamente (sin `psutil` ni
equivalentes).

---

## 1. Descripción general

> TODO: qué hace el monitor y cómo se usa.

---

## 2. Diagrama de arquitectura

```
                    ┌─────────────────────────┐
                    │  MAIN (padre)           │
                    │  handlers de señales    │
                    │  self-pipe              │
                    └───────────┬─────────────┘
                                │ fork()
     ┌──────────────┬───────────┼───────────────┬──────────────┐
     │              │           │               │              │
┌────▼─────┐  ┌─────▼──────┐   ...        ┌────▼─────┐  ┌─────▼────┐
│Recolector│  │Analizador 1│  (×7)        │Agregador │  │ Display  │
│ lista    │  │  resumen   │              │          │  │  TUI     │
│ /proc    │  │            │              │          │  │          │
└────┬─────┘  └──┬──────▲──┘              └────▲──┬──┘  └──┬────▲──┘
     │           │      │                      │  │        │    │
     │ escribe   │      │ lee lista            │  │        │    │
     │ "pids"    │      │ de pids              │  │        │    │
     │           │  [Queue]                    │  │ escribe│    │ lee
     │           └──────┼──────────────────────┘  │ dimens.│    │ snapshot
     │                  │   resultados crudos     │        │    │
     │                  │                         │        │    │
     └──────────────────▼─────────────────────────▼────────▼────┘
              ┌──────────────────────────────────────────┐
              │      Manager().dict()  (proceso propio)  │
              │  "pids", "resumen", "memoria", "fds",    │
              │  "threads", "senales", "scheduling",     │
              │  "sistema"     + timestamp cada uno      │
              └──────────────────────────────────────────┘

        Display --[Value(double) ×7]--> Analizadores   (intervalos, +/-)
```

### Procesos del sistema

| Proceso | Cantidad | Responsabilidad |
|---|---|---|
| Main (padre) | 1 | Lanza los hijos, maneja señales, orquesta el shutdown |
| Recolector | 1 | Lista los PIDs vivos en `/proc` y los publica como estado |
| Analizadores | 7 | Cada uno extrae una dimensión, con su propio intervalo |
| Agregador | 1 | Consume las muestras crudas, calcula derivados, escribe el snapshot |
| Display | 1 | Renderiza la vista activa (TUI) y lee el teclado |
| Servidor del `Manager` | 1 | Creado implícitamente por `multiprocessing.Manager()` |

---

## 3. Decisiones de diseño

### 3.1 Por qué multiproceso y no multithread

La intuición inicial fue que leer `/proc` es una carga **I/O-bound** y que por
eso el GIL sería un problema. Ese razonamiento es incorrecto en las dos mitades,
y vale la pena dejarlo escrito porque la conclusión correcta llega por otro
camino.

**Primero:** si la carga fuera realmente I/O-bound, los *threads* funcionarían
bien. Un thread de Python **suelta el GIL** mientras espera en una syscall
bloqueante, así que varios threads pueden esperar I/O simultáneamente sin
estorbarse. "Es I/O-bound" es un argumento *a favor* de threads, no en contra.

**Segundo:** esta carga no es I/O-bound. `/proc` es un sistema de archivos
virtual: no hay disco, el kernel **genera el texto en el momento** en que se
lee. El `open()` + `read()` devuelve casi instantáneamente. El costo real está
en lo que viene después: `split()`, `startswith()`, `strip()`, `int()` sobre
~50 campos × ~300 procesos × 7 dimensiones.

Esas operaciones de string son **bytecode de Python**, y el bytecode se ejecuta
con el GIL agarrado. Desensamblando un parser mínimo con `dis.dis()` se ven
~35 instrucciones de VM para extraer *un solo campo* de *un solo proceso*:

```
LOAD_FAST     0 (texto)
LOAD_ATTR     1 (split)
LOAD_CONST    1 ('\n')
CALL          1
GET_ITER
FOR_ITER      87
...
```

O sea: **es una carga CPU-bound disfrazada de I/O**, porque involucra `open()`
y `read()`. Con threads, los 7 analizadores se turnarían para usar un solo
core. Con procesos, cada uno tiene su propio intérprete y su propio GIL, y el
scheduler de Linux los reparte entre los cores disponibles.

**Un tercer motivo, que no tiene que ver con performance:** aislamiento de
fallas. Si un analizador crashea parseando un `/proc/<pid>/maps` raro, muere
solo ese proceso y el resto del monitor sigue funcionando. Con threads, una
excepción no atrapada o un segfault en una extensión C se lleva puesto todo el
proceso.

### 3.2 Elección de primitiva de IPC por flujo

El criterio que ordena todas estas decisiones es una sola pregunta:
**¿esto es un mensaje o es estado?**

- **Mensaje**: algo que pasó, se consume una vez y desaparece → `Queue` / `Pipe`
- **Estado**: el valor actual de algo, se lee N veces → `Manager` / `Value`

| Flujo | Primitiva | Por qué |
|---|---|---|
| recolector → analizadores (PIDs) | `Manager().dict()` | Es **estado**, se lee N veces con ritmos distintos. Una `Queue` lo consumiría una sola vez |
| analizadores → agregador | `Queue` (`maxsize`, descarta el viejo) | Es un **evento** con un solo consumidor. El agregador necesita cada muestra para calcular deltas de CPU |
| agregador → snapshot | `Manager().dict()` | **Estado** leído por el display a otro ritmo |
| display → analizadores (intervalo) | `Value('d')` | Un solo número. `mmap` sin pickle ni socket |

**Sobre el flujo recolector → analizadores.** Una `Queue` no sirve acá porque
lo que se saca de una `Queue` desaparece: si el recolector publicara la lista
de PIDs en una sola cola, el primer analizador que hiciera `get()` se la
llevaría y los otros seis se quedarían sin nada. Se necesita *fan-out* (uno a
muchos), y una `Queue` es reparto de trabajo. Publicando la lista como una
dimensión más del `Manager().dict()`, los 7 analizadores la leen a su propio
ritmo y siempre ven la última versión.

Esto además elimina por diseño el problema de acumulación: si el recolector
produjera cada 1 s en una cola y el analizador de señales consumiera cada 10 s,
esa cola crecería sin límite.

**Sobre el flujo analizadores → agregador.** Acá sí hay una cola, y sí puede
llenarse. Se usa `maxsize` acotado y política de **descartar la muestra más
vieja**: en un monitor en tiempo real, un snapshot de hace 30 segundos no sirve
para nada, así que es preferible perderlo antes que bloquear al analizador.
`multiprocessing.Queue` no trae esa política, hay que implementarla:

```python
try:
    q.put_nowait(dato)
except queue.Full:
    try:
        q.get_nowait()      # descarto el más viejo
    except queue.Empty:
        pass
    q.put_nowait(dato)
```

**Sobre el flujo display → analizadores.** El dato que viaja es un único
`float` (el intervalo en segundos). Con un `Manager`, cada lectura del
analizador sería un round-trip por socket con serialización pickle de los dos
lados. Con `Value('d')` es una lectura de una página de memoria compartida por
`mmap`: sin syscall, sin serialización. Es exactamente el caso de uso de
`Value` frente a `Manager`.

### 3.3 Por qué `Manager` y no `Value`/`Array` para el snapshot

`Value` y `Array` sólo manejan tipos C simples (enteros, floats, arreglos de
bytes de tamaño fijo). El snapshot es un diccionario anidado de tamaño variable
—cambia con cada proceso que nace o muere—, y eso no entra en un bloque de
memoria de tamaño fijo. `Manager` paga el costo de serialización a cambio de
soportar estructuras arbitrarias de Python.

La contrapartida es que `Manager().dict()` no devuelve un `dict`, sino un
`DictProxy`. Cada acceso es una llamada remota al proceso servidor que
`Manager()` levanta silenciosamente al instanciarse (verificable con
`ps --ppid <pid>` antes y después de la línea `m = Manager()`).

### 3.4 Por qué el agregador existe como proceso propio

El proceso servidor del `Manager` ya almacena estado global, así que podría
parecer que el agregador sobra y que los analizadores podrían escribir directo
al snapshot.

No alcanza, por una razón concreta: **el `%CPU` no existe en `/proc`**. Hay que
calcularlo como delta de jiffies entre dos lecturas
(`(jiffies_ahora - jiffies_antes) / Δt`). El servidor del `Manager` no recuerda
nada: sólo guarda el último valor escrito. Alguien tiene que mantener la
lectura anterior en memoria propia.

Lo mismo vale para el resto de los derivados que pide la consigna: Top 3 por
CPU, Top 3 por memoria, totales por estado, conteo de zombies. Todo eso son
cálculos *sobre* las muestras crudas, no muestras en sí.

### 3.5 Race conditions

> TODO: completar cuando esté implementada la sincronización.

### 3.6 Intervalos por defecto

> TODO: justificar los intervalos elegidos.

---

## 4. Manejo de señales

> TODO: completar.

Nota de diseño ya establecida: cuando se aprieta `Ctrl+C`, la terminal **no**
manda `SIGINT` al proceso padre solamente, sino a todo el **grupo de procesos
en foreground**. Como los hijos heredan el PGID del padre, los 10 procesos
reciben `SIGINT` simultáneamente. Sin tratamiento explícito, cada hijo moriría
por su cuenta y sería imposible garantizar un shutdown ordenado (vaciar
buffers, persistir el log).

---

## 5. Conceptos del curso aplicados

> TODO: relacionar partes del código con las clases.

**Context switches voluntarios e involuntarios** (clase 10). Un switch
**voluntario** ocurre cuando el proceso mismo cede la CPU porque necesita
esperar algo (`sleep`, I/O, un lock ocupado). Un switch **involuntario** ocurre
cuando el kernel se la arrebata (se acabó el quantum, o apareció algo más
prioritario). Contraste medido en vivo, con dos procesos y 2 segundos de
diferencia:

| Proceso | `voluntary` | `nonvoluntary` (t=1s) | `nonvoluntary` (t=3s) |
|---|---|---|---|
| `time.sleep(8)` | 1 | 3 | 3 (no se movió) |
| `while True: pass` | 0 | 48 | 58 (+10) |

El que duerme tiene exactamente **1** switch voluntario: el momento en que
llamó a `sleep()` y cedió la CPU. Después no volvió a ejecutar nada. El que
quema CPU tiene **0** voluntarios —nunca pidió esperar— y acumula ~5
involuntarios por segundo, porque el scheduler tiene que sacarlo a la fuerza
cada vez que se le termina el quantum.

Regla práctica: muchos `voluntary` → proceso I/O-bound; muchos `nonvoluntary` →
proceso CPU-bound.

---

## 6. Limitaciones conocidas

> TODO.

---

## 7. Cómo correr y testear

> TODO: `docker compose up --build`.
