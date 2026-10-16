import importlib.util
from pathlib import Path
import pytest

_spec = importlib.util.spec_from_file_location("tarea_cpu", Path(__file__).parent / "tarea.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

ejecutarTarea = _mod.ejecutarTarea
ParametrosInvalidos = _mod.ParametrosInvalidos


def test_primos_valido():
    res = ejecutarTarea("primos", {"limite": 30})
    assert res["total_primos"] == 10
    assert res["max_primo"] == 29
    assert res["tiempo_cpu_ms"] >= 0


def test_primos_limite_invalido():
    with pytest.raises(ParametrosInvalidos):
        ejecutarTarea("primos", {"limite": 1})

    with pytest.raises(ParametrosInvalidos):
        ejecutarTarea("primos", {"limite": -5})

    with pytest.raises(ParametrosInvalidos):
        ejecutarTarea("primos", {"limite": "treinta"})


def test_pow_valido():
    res = ejecutarTarea("pow", {"dificultad": 2, "prefijo": "test"})
    assert res["hash"].startswith("00")
    assert res["nonce"] >= 0


def test_pow_invalido():
    with pytest.raises(ParametrosInvalidos):
        ejecutarTarea("pow", {"dificultad": 0})

    with pytest.raises(ParametrosInvalidos):
        ejecutarTarea("pow", {"dificultad": 10})
