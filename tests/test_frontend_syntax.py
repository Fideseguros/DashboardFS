"""Los <script> inline de las plantillas deben ser JavaScript válido.

El 6-oct-2026 un salto de línea dentro de una cadena dejó el tablero entero
en "Cargando cartera…" en producción. Este test lo habría atajado: extrae
cada bloque <script> y lo valida con `node --check` (si Node está instalado;
si no, usa un chequeo mínimo de cadenas sin cerrar).
"""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "templates"
SCRIPT_RE = re.compile(r"<script>(.*?)</script>", re.S)


def _bloques():
    for html in sorted(TEMPLATES.glob("*.html")):
        texto = html.read_text(encoding="utf-8")
        for i, m in enumerate(SCRIPT_RE.finditer(texto)):
            yield f"{html.name}#{i}", m.group(1)


_BLOQUES = dict(_bloques())


@pytest.mark.parametrize("nombre", list(_BLOQUES), ids=list(_BLOQUES))
def test_script_inline_es_js_valido(nombre):
    codigo = _BLOQUES[nombre]
    node = shutil.which("node")
    if node:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(codigo)
            ruta = f.name
        res = subprocess.run([node, "--check", ruta], capture_output=True, text=True, timeout=60)
        assert res.returncode == 0, f"{nombre}: {res.stderr[-800:]}"
    else:
        # Sin Node: al menos que no haya un salto de línea dentro de una
        # cadena de comillas simples (el error que tumbó producción).
        for n, linea in enumerate(codigo.splitlines(), 1):
            sin_esc = re.sub(r"\.", "", linea)
            sin_tpl = re.sub(r"`[^`]*`", "", sin_esc)
            assert sin_tpl.count("'") % 2 == 0 or "//" in sin_tpl, f"{nombre} línea {n}: comilla simple sin cerrar"
