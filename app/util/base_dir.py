import importlib.resources
from functools import cache
from pathlib import Path

__all__ = ("get_module_root",)


@cache
def get_module_root(module_name: str) -> Path:
    """
    Get the root directory of a given module.

    Args:
        module_name (str): The name of the module.

    Returns:
        Module root directory as a pathlib.Path object.
    """

    with importlib.resources.path(module_name, "pyproject.toml") as path_to_file:
        module_root = path_to_file.parent
        return module_root
