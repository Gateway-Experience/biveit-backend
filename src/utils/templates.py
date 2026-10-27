# Intentions: rendering text templates where `{{{<python expression>}}}` is replaced by its value, evaluated
# against a scope the caller hands in. Generic: what's in the scope (and so what a template can reach) is
# the caller's business. Templates must be OURS and trusted -- expressions run with eval (no builtins).

import os
import re

_PLACEHOLDER = re.compile(r"\{\{\{(.+?)\}\}\}")


class TemplateFailed(Exception):
    """A template is missing, or one of its expressions failed. The message names the template."""


def render_template_file(template_path: str, scope: dict) -> str:
    """[Pure-ish] The template at `template_path`, each placeholder evaluated against `scope`. Raises
    TemplateFailed, naming the template's file."""
    template_name = os.path.basename(template_path)
    try:
        with open(template_path, encoding="utf-8") as template_file:
            template_text = template_file.read()
        return _PLACEHOLDER.sub(lambda match: str(eval(match.group(1), {"__builtins__": {}}, scope)), template_text)
    except Exception as error:
        raise TemplateFailed(f"{template_name}: {type(error).__name__}: {error}") from error
