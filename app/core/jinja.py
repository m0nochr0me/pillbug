"""
Jinja environment setup for rendering application templates.
"""

from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.util.base_dir import get_module_root

__all__ = ("jinja_env", "render_template")

jinja_env = Environment(
    loader=FileSystemLoader(get_module_root("app")),
    autoescape=select_autoescape(enabled_extensions=("html", "xml")),
    keep_trailing_newline=True,
)


def render_template(template_name: str, /, **context: Any) -> str:
    return jinja_env.get_template(template_name).render(**context)
