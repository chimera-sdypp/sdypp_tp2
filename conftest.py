import sys
from pathlib import Path

def pytest_pycollect_makemodule(module_path, parent):
    test_dir = str(module_path.parent)
    if test_dir not in sys.path:
        sys.path.insert(0, test_dir)

    for ancestor in module_path.parents:
        if (ancestor / "app").is_dir() or (ancestor / "tarea.py").is_file():
            p = str(ancestor)
            if p not in sys.path:
                sys.path.insert(0, p)
            # Clear cached app/tarea/dobles modules to avoid cross-hit collision
            for mod in list(sys.modules.keys()):
                if mod in ("app", "tarea", "dobles") or mod.startswith(("app.", "tarea.", "dobles.")):
                    sys.modules.pop(mod, None)
            break
