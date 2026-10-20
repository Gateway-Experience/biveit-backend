# Programs that run on their own, next to the backend rather than inside it -- each started as
# `python -m src.runners.<name>.main`. They must NOT import the backend's settings (src.config) or anything
# that loads them: they're handed what they need through their env, so they can move into a container
# without dragging the backend's secrets along. src.constants and generic src.utils are fine.
