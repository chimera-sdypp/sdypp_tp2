import importlib.util
from pathlib import Path
import pytest

_spec = importlib.util.spec_from_file_location("tarea_falla", Path(__file__).parent / "tarea.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

ejecutarTarea = _mod.ejecutarTarea
ParametrosInvalidos = _mod.ParametrosInvalidos
ErrorSimulado = _mod.ErrorSimulado


def test_dormir_valido():
    res = ejecutarTarea("dormir", {"segundos": 0.05})
    assert res["segundos_dormidos"] == 0.05
    assert res["estado"] == "desperté"


def test_dormir_invalido():
    with pytest.raises(ParametrosInvalidos):
        ejecutarTarea("dormir", {"segundos": -1})

    with pytest.raises(ParametrosInvalidos):
        ejecutarTarea("dormir", {"segundos": "un segundo"})


def test_division_valida_e_invalida():
    res = ejecutarTarea("division", {"a": 10, "b": 2})
    assert res["resultado"] == 5.0

    with pytest.raises(ParametrosInvalidos):
        ejecutarTarea("division", {"a": 10, "b": 0})


def test_explotar():
    with pytest.raises(ErrorSimulado):
        ejecutarTarea("explotar", {})
