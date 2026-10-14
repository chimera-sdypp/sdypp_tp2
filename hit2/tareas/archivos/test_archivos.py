import importlib.util
from pathlib import Path
import pytest

_spec = importlib.util.spec_from_file_location("tarea_archivos", Path(__file__).parent / "tarea.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

ejecutarTarea = _mod.ejecutarTarea
ParametrosInvalidos = _mod.ParametrosInvalidos


def test_archivos_valido():
    parametros = {
        "archivos": [
            {"id": "doc1.txt", "contenido": "Hola mundo de sistemas distribuidos."},
            {"id": "doc2.txt", "contenido": "Prueba con varias acciones concurrentes."},
        ],
        "acciones": ["hash_sha256", "comprimir", "contar_palabras"],
    }
    res = ejecutarTarea("procesar_archivos", parametros)
    assert res["archivos_procesados"] == 2
    assert res["acciones_por_archivo"] == 3
    assert res["total_operaciones"] == 6
    assert len(res["resumen"]) == 2
    assert "hash" in res["resumen"][0]["acciones"]["hash_sha256"]


def test_archivos_limite_100_archivos():
    archivos = [{"id": f"f_{i:03d}.txt", "tamano_kb": 1} for i in range(100)]
    acciones = ["hash_sha256", "comprimir", "contar_palabras", "rotar_lineas", "analisis_frecuencia"]
    res = ejecutarTarea("procesar_archivos", {"archivos": archivos, "acciones": acciones})
    assert res["archivos_procesados"] == 100
    assert res["acciones_por_archivo"] == 5
    assert res["total_operaciones"] == 500


def test_archivos_invalido_accion_desconocida():
    with pytest.raises(ParametrosInvalidos):
        ejecutarTarea("procesar_archivos", {
            "archivos": [{"id": "a.txt", "contenido": "abc"}],
            "acciones": ["accion_que_no_existe"]
        })


def test_archivos_invalido_lista_vacia():
    with pytest.raises(ParametrosInvalidos):
        ejecutarTarea("procesar_archivos", {"archivos": [], "acciones": ["hash_sha256"]})

    with pytest.raises(ParametrosInvalidos):
        ejecutarTarea("procesar_archivos", {"archivos": [{"id": "a.txt"}], "acciones": []})
