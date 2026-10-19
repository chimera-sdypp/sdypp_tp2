"""Configuración Global de Pytest y Aislamiento de Módulos Inter-Hit.

¿Por qué es necesario este archivo de configuración en la raíz del repositorio?
El proyecto está estructurado como un monorepo donde `hit1/`, `hit2/` y `hit3/` poseen
módulos con nombres idénticos (`app`, `tarea`, `dobles`).

En Python, el diccionario `sys.modules` es global para todo el proceso del intérprete.
Cuando pytest ejecuta consecutivamente los tests de `hit1`, `hit2` y `hit3` en una misma sesión:
1. Sin aislamiento, `import app.nodo` en `hit3` terminaría resolviendo el módulo cargado previamente
   por los tests de `hit2` o fallando por colisiones de paquetes en caché.
2. El hook `pytest_pycollect_makemodule` intercepta dinámicamente cada archivo de test antes
   de que pytest lo importe, ajusta `sys.path` al directorio raíz del hit correspondiente
   y purga del caché `sys.modules` cualquier módulo `app`, `tarea` o `dobles` cargado previamente.
"""

import sys
from pathlib import Path


def pytest_pycollect_makemodule(module_path: Path, parent) -> None:
    """Hook de pytest invocado previo a la recolección e importación de cada módulo de test.
    
    Acciones realizadas:
    1. Inyecta el directorio del test en `sys.path` para permitir importaciones relativas locales.
    2. Recorre los directorios ancestros buscando la raíz del componente (`app/` o `tarea.py`).
    3. Purga selectivamente `sys.modules` para garantizar aislamiento estricto entre hit1, hit2 y hit3.
    """
    # Inyectar el directorio que contiene el archivo de prueba actual
    test_dir = str(module_path.parent)
    if test_dir not in sys.path:
        sys.path.insert(0, test_dir)

    # Identificar la raíz del hit que aloja la aplicación o tarea evaluada
    for ancestor in module_path.parents:
        if (ancestor / "app").is_dir() or (ancestor / "tarea.py").is_file():
            p = str(ancestor)
            if p not in sys.path:
                sys.path.insert(0, p)

            # Purgar módulos en caché que podrían colisionar entre diferentes hits
            for mod in list(sys.modules.keys()):
                if mod in ("app", "tarea", "dobles") or mod.startswith(("app.", "tarea.", "dobles.")):
                    sys.modules.pop(mod, None)
            break

