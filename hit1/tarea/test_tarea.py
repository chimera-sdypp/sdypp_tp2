import pytest

from tarea import ParametrosInvalidos, ejecutarTarea


@pytest.mark.parametrize("calculo, esperado", [
    ("suma", 9), ("resta", 3), ("multiplicacion", 18), ("division", 2),
])
def test_calculos(calculo, esperado):
    assert ejecutarTarea(calculo, {"a": 6, "b": 3}) == esperado


@pytest.mark.parametrize("calculo, parametros, mensaje", [
    ("raiz", {"a": 1, "b": 1}, "cálculo desconocido"),
    ("suma", {"a": 1}, "tienen que ser números"),
    ("suma", {"a": "1", "b": 2}, "tienen que ser números"),
    ("suma", {"a": True, "b": 2}, "tienen que ser números"),
    ("division", {"a": 1, "b": 0}, "división por cero"),
    ("multiplicacion", {"a": 1e308, "b": 10}, "se va de rango"),
    ("division", {"a": 10**400, "b": 1}, "se va de rango"),
])
def test_rechazos(calculo, parametros, mensaje):
    with pytest.raises(ParametrosInvalidos, match=mensaje):
        ejecutarTarea(calculo, parametros)
