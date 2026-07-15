"""Identificador del build, para invalidar cachés HTTP cuando cambia el código.

POR QUÉ EXISTE
--------------
Los endpoints de lista (cartera, recaudo, solicitudes, jurídico) sirven un
ETag construido a partir del último sync + el rol. El problema: buena parte
de la lógica de presentación se aplica AL LEER, no al guardar. Ejemplos:
`_normalize_estado` (NEGADA→ANULADA, TRATAMIENTO DE DATOS→EN ESTUDIO),
el enmascarado de PII, el netting de columnas.

Cuando corregimos una de esas reglas, los datos en BD no cambian → el ETag
no cambia → el navegador manda If-None-Match, recibe 304 Not Modified y
sigue mostrando el JSON viejo. El fix queda invisible para el usuario hasta
que suba un archivo nuevo o limpie la caché a mano.

Caso real (jul-2026): la gerente reportó que las solicitudes ANULADAS no
aparecían. El backend ya las devolvía bien (369 anuladas); su navegador
estaba sirviendo una respuesta cacheada previa al fix de estados.

CÓMO SE RESUELVE
----------------
Metemos esta versión en cada ETag. Cambia en cada deploy, así que un deploy
invalida las cachés de todos los clientes — que es exactamente lo que
queremos cuando cambia la lógica de lectura. Entre requests del mismo
deploy la caché sigue funcionando normal (no perdemos el beneficio del 304).

Preferimos el commit SHA que Railway inyecta: es estable entre reinicios del
mismo deploy (un restart NO invalida cachés innecesariamente). Si no está,
hasheamos el código de app/ como aproximación.
"""
import hashlib
import logging
import os
from pathlib import Path

_log = logging.getLogger("fide.build")


def _compute() -> str:
    sha = (os.getenv("RAILWAY_GIT_COMMIT_SHA")
           or os.getenv("GIT_COMMIT_SHA")
           or os.getenv("SOURCE_VERSION") or "").strip()
    if sha:
        return sha[:12]
    # Fallback local/dev: hash del código fuente de app/. Cambia cuando
    # editamos cualquier .py, que es justo el disparador que nos interesa.
    try:
        h = hashlib.md5()
        for p in sorted(Path(__file__).parent.rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            h.update(p.name.encode("utf-8"))
            h.update(p.read_bytes())
        return h.hexdigest()[:12]
    except Exception:
        _log.exception("no se pudo derivar BUILD_VERSION; usando 'dev'")
        return "dev"


# Se calcula una sola vez al importar (arranque del proceso).
BUILD_VERSION = _compute()
