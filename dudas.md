# Dudas

Preguntas que quedaron abiertas durante el desarrollo del TP. Algunas se
resolvieron, otras no. Las dejo escritas porque la consigna dice que las dudas
existentes y abordadas son bienvenidas, y porque volver a leerlas me sirve para
saber qué tengo flojo.

> **Estado del archivo:** las secciones 1 y 2 son dudas que se resolvieron
> durante el desarrollo, con cómo se resolvieron. La 3 son las que siguen
> abiertas. La 4 son cosas que entiendo "de palabra" pero no estoy seguro de
> poder defender a fondo.

---

## 1. Dudas que se resolvieron probando

### ¿Leer `/proc` es I/O-bound o CPU-bound?

Arranqué convencido de que era I/O-bound, y usé eso como argumento para elegir
procesos sobre threads. **El argumento estaba al revés:** si fuera I/O-bound,
los threads andarían bien, porque un thread suelta el GIL mientras espera en una
syscall.

Lo que lo resolvió: `/proc` no está en disco, el kernel genera el texto en el
momento. El `read()` vuelve enseguida. El costo real está en el parseo —
`split()`, `startswith()`, `int()` — que es bytecode y corre con el GIL agarrado.
Se ve con `dis.dis()`: ~35 instrucciones de VM para extraer un solo campo.

Es CPU-bound disfrazado de I/O.

### ¿Por qué el agregador no puede ser el proceso servidor del `Manager`?

Porque el `Manager` no recuerda nada: solo guarda el último valor escrito. El
`%CPU` no existe en `/proc` —lo que hay es un contador acumulado de jiffies—, así
que hay que restar dos lecturas. Alguien tiene que sostener la lectura anterior
en memoria privada.

### ¿Por qué `snapshot["resumen"][pid] = x` no funciona?

Porque `snapshot` es un `DictProxy`, no un `dict`. Solo el primer nivel es un
proxy: leer `snapshot["resumen"]` devuelve una **copia local** del dict anidado.
Modificarla y no reasignarla tira el cambio.

Lo peor es que **no da error**. Escribís, no explota, y el dato nunca aparece.
Por eso el agregador siempre reasigna la clave de primer nivel entera.

### ¿A quién le manda `SIGINT` el `Ctrl+C`?

No al proceso: al **grupo de procesos en foreground**. Los 10 hijos la reciben
igual que el padre, porque heredaron el PGID.

Comprobado con un `fork()` chico donde padre e hijo imprimen al recibir la
señal: aparecen los dos mensajes.

### ¿Cuáles son los campos de PGID y SID en `/proc/<pid>/stat`?

La consigna dice que son los campos 6-7. Según `man 5 proc` son el **5**
(`pgrp`) y el **6** (`session`); el 7 es `tty_nr`. Verificado:

```
ps -o pid,pgid,sid  ->  13305  13305  13305
stat campo 5 = 13305   campo 6 = 13305   campo 7 = 0
```

Se implementó siguiendo el man. Preguntar en clase si la consigna está corrida o
si hay alguna convención distinta que no conozco.

---

## 2. Bugs que costaron encontrar

### El `comm` de `stat` puede tener espacios

`/proc/<pid>/stat` es posicional, pero el campo 2 viene entre paréntesis y puede
contener espacios y paréntesis:

```
1624 (mt76-tx phy0) S 2 0 0 ...
```

Un `split()` ingenuo devuelve `estado = 'phy0)'` y corre todos los campos de
lugar. **No falla: devuelve números equivocados en silencio.** Se resuelve
buscando el último `)` con `rfind()`.

Hay un test que documenta el bug además del fix, para que nadie "simplifique" el
parseo más adelante.

### El teclado no respondía ninguna tecla en la TUI

La TUI dibujaba perfecto pero ninguna tecla hacía nada. La clase `Teclado`
probada sola bajo una pty funcionaba (`['2','3','q','ARRIBA',...]`), así que el
problema no era el parseo de teclas.

La causa: **`multiprocessing.Process` cierra `stdin` en todos sus hijos.** En
`BaseProcess._bootstrap` llama a `util._close_stdin()`, que cierra `sys.stdin` y
lo reabre apuntando a `/dev/null`. El display es un proceso hijo, así que
`sys.stdin.fileno()` devolvía el fd de `/dev/null`.

Comprobado:

```python
def hijo(q): q.put(repr(sys.stdin))
# el hijo reporta un objeto nuevo con fd 5 -> /dev/null
```

Y encima había un segundo bug encadenado: `select()` sobre un fd en EOF devuelve
"listo" para siempre, así que el thread del teclado giraba a máxima velocidad
quemando un core sin recibir nunca nada.

Se resolvió abriendo `/dev/tty` —la terminal de control del proceso, que
sobrevive al `fork()` y no depende de qué le hicieron a los fds estándar— y
tratando la lectura vacía como EOF en vez de `continue`.

> Duda que me quedó: ¿por qué `multiprocessing` hace eso? Supongo que para
> evitar que varios hijos peleen por leer la misma terminal, pero no encontré
> la justificación escrita en la documentación.

### `docker compose up` no manda el teclado al contenedor

Después de arreglar lo de `multiprocessing` y stdin, el teclado seguía sin
responder — pero solo en Docker. En la terminal directa andaba.

Corrí el diagnóstico en mi terminal (Tilix) y las dos vías leían teclas sin
problema: 45 teclas por `/dev/tty`. O sea, el mecanismo estaba bien.

La causa: **`docker compose up` attachea la salida de los contenedores pero no
la entrada**, aunque `tty: true` y `stdin_open: true` estén puestos. No hay
flag para cambiarlo. Medido lanzando ambos bajo una pty y mandando teclas:

| comando | dibuja | responde teclas |
|---|---|---|
| `docker compose up` | sí | **no** |
| `docker compose run --rm --build monitor` | sí | **sí** |

Es especialmente confuso porque la TUI se ve perfecta: parece un bug del
programa cuando en realidad es el transporte de stdin.

> **Duda que me queda:** la consigna pide explícitamente
> `docker compose up --build` como comando único. ¿Se puede hacer que `up`
> sea interactivo de alguna forma que no encontré, o corresponde documentar
> que para una TUI hay que usar `run`? Lo dejé documentado y además la TUI
> avisa sola si detecta que no llegan teclas.

### La TUI titilaba constantemente

Dos causas sumadas:

1. **Doble repintado.** `Live` levanta su propio thread que refresca N veces por
   segundo, y nosotros además llamábamos a `live.update()` en el loop. Dos
   escritores sobre la misma pantalla. Se arregla con `auto_refresh=False` y
   refrescando solo desde el loop.
2. **Repintar sin cambios.** Se repintaba la pantalla completa 8 veces por
   segundo aunque el snapshot no se hubiera movido. Ahora se compara una firma
   —que incluye un contador `version` que el agregador incrementa en cada
   escritura— y solo se repinta si hubo tecla, cambió el dato, o pasó un
   segundo.

Medido bajo una pty de 150×44: pasó de ~24 repintados completos cada 3 s a ~3.

Un tercer factor que también contribuía: las tres franjas del layout tenían
tamaño fijo (5 + 16 + 4 = 25 líneas más la lista). En una terminal de menos de
~30 filas el contenido desbordaba y la pantalla alternativa scrolleaba, lo que
se ve igual que titileo. Ahora el alto se reparte según `console.size.height`.

### Rich aplastaba las columnas de la tabla

La lista de procesos salía como `… USU… … COMANDO`. Rich mide el texto más largo
de cada columna para repartir el ancho, y el `cmdline` de Chrome tiene 300
caracteres, así que decidía que la tabla no entraba y encogía todo.

Se arregla con `ratio=1` en la columna elástica **y** truncando el string antes
de pasarlo. `overflow="ellipsis"` solo no alcanza, porque el truncado ocurre
después del cálculo del ancho.

---

## 3. Dudas que siguen abiertas

### 3.1 El proceso servidor del `Manager` no bloquea señales

Los 10 hijos bloquean `SIGINT` con `pthread_sigmask` para que solo el padre
decida cuándo morir. Pero el proceso servidor del `Manager` lo crea
`multiprocessing`, no nosotros, así que no le podemos aplicar `preparar_hijo()`.

**Duda:** si ese proceso recibe `SIGINT` por el grupo de procesos y se muere
antes que el padre, ¿qué pasa con las lecturas del snapshot que el padre haga
durante el shutdown? En la práctica no lo vi fallar, probablemente porque el
shutdown tarda ~0.2 s, pero no sé si es correcto o si tengo suerte.

¿Hay forma de que el `Manager` levante su servidor con una máscara de señales
distinta? ¿O conviene no usar `Manager` y armar el servidor a mano?

### 3.2 El agregador hace polling y no me convence

Con 7 colas no puedo hacer `get()` bloqueante en una sin ignorar las otras, así
que las recorro con `get_nowait()` y duermo 50 ms. Despierta ~20 veces por
segundo sin hacer nada.

Vi que existe `multiprocessing.connection.wait()`, que acepta varios objetos y
se bloquea hasta que alguno tenga datos. **Duda:** ¿es correcto pasarle
`cola._reader`? Es un atributo privado, y no encontré una forma documentada de
hacerlo. ¿Cuál es la manera canónica de esperar en varias `Queue` a la vez?

### 3.3 ¿El patrón "descartar el más viejo" está bien resuelto?

```python
try:
    q.put_nowait(dato)
except queue.Full:
    q.get_nowait()
    q.put_nowait(dato)
```

Argumento que no hay race porque cada analizador tiene su propia cola y hay un
solo escritor. Pero el **agregador** también saca de esa cola, así que hay un
escritor y un lector concurrentes.

**Duda:** ¿puede el `get_nowait()` del agregador meterse justo entre mi
`get_nowait()` y mi `put_nowait()`? Creo que no rompe nada —a lo sumo la cola
queda con un hueco— pero no estoy seguro de haberlo razonado bien. Puse un
`try/except Empty` alrededor del `get_nowait()` por las dudas.

### 3.4 `iowait` en multi-core

El kernel documenta que el valor no es confiable en máquinas con varios cores.
Lo muestro porque la consigna lo pide. **Duda:** ¿hay alguna métrica que sí sirva
para "cuánto se está esperando disco"? ¿`procs_blocked` de `/proc/stat`, o los
tasks en estado `D`?

### 3.5 Reutilización de PID

Comparo `starttime` para no calcular un delta entre dos procesos distintos que
compartieron número de PID.

**Duda:** ¿es `starttime` realmente único? Son jiffies desde el boot, así que dos
procesos que arrancaron en el mismo jiffy tendrían el mismo valor. Con HZ=100 eso
es una ventana de 10 ms. ¿Hace falta combinarlo con algo más, o es suficiente en
la práctica?

### 3.6 El snapshot pesa 2.7 MB

Documentado como limitación conocida (README §6.1). Tengo la idea de cómo
arreglarlo —que los analizadores caros solo recolecten el detalle del PID
seleccionado—, pero no lo implementé.

**Duda:** ¿es aceptable para el TP dejarlo documentado, o se espera que esté
resuelto? Y si lo resuelvo así, ¿no estoy metiendo un acoplamiento feo entre el
display y los analizadores?

### 3.7 Docker y permisos

Dentro del contenedor corro como root, así que puedo leer `/proc/<pid>/fd/` de
todos los procesos del host. Fuera de Docker, como usuario normal, no.

**Duda:** ¿es un problema de seguridad tener un contenedor con `pid: host` y
root? Entiendo que el filesystem y la red siguen aislados, pero no tengo claro
qué más se puede hacer desde ahí.

---

## 4. Cosas que entiendo "de palabra" pero no a fondo

Las anoto para repasar antes del final, no porque me bloqueen.

- **Copy-on-Write.** Entiendo la idea (el `fork()` no copia, marca read-only y
  duplica al escribir) pero no sabría mostrar en `/proc` qué páginas ya se
  copiaron y cuáles no. ¿Se ve en `smaps`?

- **`priority` en `/proc/<pid>/stat`.** Sé que para `SCHED_OTHER` sale como
  `nice + 20` y que para tiempo real es negativo, pero no tengo claro cómo se
  relaciona con la prioridad interna del kernel (100-139) ni por qué se muestra
  así.

- **`fork` vs `forkserver`.** Entiendo `fork` y `spawn`. `forkserver` lo leí pero
  no me queda claro cuándo conviene sobre los otros dos.

- **Async-signal-safe.** Entiendo el problema (el handler interrumpe en un punto
  arbitrario, puede haber un lock tomado) y por qué el self-pipe lo resuelve.
  Lo que no sé es de dónde sale la lista de funciones seguras ni cómo verificar
  si una función de Python lo es.

- **`SCHED_DEADLINE`.** Lo puse en la tabla de políticas porque aparece en el
  man, pero nunca vi un proceso que lo use ni sé cómo se configura.

- **Diferencia entre `VmData` y el heap de `maps`.** Los números no coinciden y
  no tengo claro qué incluye cada uno.
