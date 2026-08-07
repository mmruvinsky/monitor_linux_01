# Imagen slim: no hace falta compilar nada. La única dependencia externa es
# rich, que es Python puro. Todo lo demás sale de la stdlib, porque el TP
# prohíbe psutil y cualquier cosa que lea /proc por nosotros.
FROM python:3.13-slim

# PYTHONUNBUFFERED: sin esto, stdout se bufferea cuando no es un tty y los
# logs del monitor aparecen recién al salir.
# PYTHONDONTWRITEBYTECODE: no ensuciar el bind mount con __pycache__.
# TERM: rich necesita saber que la terminal soporta color; sin esto dibuja
# todo en blanco y negro.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TERM=xterm-256color \
    PYTHONPATH=/app/src

WORKDIR /app

# Las dependencias van en una capa aparte y ANTES del código: así, cuando
# cambiás un .py, Docker reusa la capa cacheada del pip install en vez de
# reinstalar todo en cada build.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY config.json ./
COPY src/ ./src/

# Se corre como root a propósito. No es descuido: /proc/<pid>/fd/ y
# /proc/<pid>/maps son legibles SOLO por el dueño del proceso. Sin root, las
# vistas de FDs y de segmentos de memoria quedarían vacías para casi todos
# los procesos del sistema. Es el mismo motivo por el que htop muestra más
# información cuando lo corrés con sudo.
CMD ["python3", "src/main.py"]
