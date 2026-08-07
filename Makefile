# Makefile — atajos para correr y probar el monitor.
#
# Existe sobre todo por un motivo: `docker compose up` NO reenvía el teclado
# al contenedor (attachea la salida, no la entrada), así que una TUI queda
# muerta. El comando correcto es `docker compose run`, que es más largo y
# fácil de olvidar. `make run` lo encapsula.

.DEFAULT_GOAL := help
.PHONY: help run up local debug test capturas build down clean

SERVICIO := monitor
IMAGEN_TEST := python:3.13-slim

help:  ## Muestra esta ayuda
	@echo "monitor_linux_01 — Computación II"
	@echo
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'
	@echo

run:  ## Corre el monitor en Docker CON teclado (forma recomendada)
	docker compose run --rm --build $(SERVICIO)

up:  ## Igual que `run` pero con `up`: la TUI se ve pero NO responde teclas
	@echo "AVISO: 'docker compose up' no reenvía stdin al contenedor."
	@echo "       La TUI se va a dibujar pero el teclado no va a responder."
	@echo "       Usá 'make run' para la versión interactiva."
	@echo
	docker compose up --build

local:  ## Corre el monitor fuera de Docker (necesita rich instalado)
	python3 src/main.py

debug:  ## Corre sin TUI, imprimiendo el snapshot por stdout
	python3 src/main.py --debug

test:  ## Corre los 51 tests en un contenedor descartable
	@# PYTHONDONTWRITEBYTECODE y -p no:cacheprovider evitan que el contenedor
	@# —que corre como root— deje __pycache__ y .pytest_cache de root en el
	@# volumen montado, que después el usuario no puede borrar.
	docker run --rm --pid=host -e PYTHONDONTWRITEBYTECODE=1 \
		-v "$(CURDIR)":/w -w /w $(IMAGEN_TEST) \
		sh -c "pip install -q pytest rich && \
		       python -m pytest tests -q -p no:cacheprovider"

capturas:  ## Regenera las capturas SVG de docs/img/
	python3 docs/generar_capturas.py

build:  ## Construye la imagen sin correr nada
	docker compose build

down:  ## Baja los contenedores y limpia
	docker compose down --remove-orphans

clean:  ## Borra archivos generados en runtime
	@rm -f dump_*.json monitor.log
	@# El `|| true` tolera archivos de root que pudo dejar un contenedor.
	@find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
	@find . -name '.pytest_cache' -type d -prune -exec rm -rf {} + 2>/dev/null || true
	@echo "limpio"
