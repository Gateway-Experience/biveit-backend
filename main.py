# Intentions: the process entry point. Configures logging before anything else, then hands off to
# uvicorn. The app itself is src/app.py.

import uvicorn

from src.config import settings
from src.utils.logging import configure_logging


def main() -> None:
    configure_logging()
    # The app is passed as an IMPORT STRING (not the object) so uvicorn's reloader can re-import it on
    # a code change. reload is a dev convenience -- it watches the filesystem and spawns a subprocess,
    # so it must stay false in prod (API_RELOAD).
    uvicorn.run(
        "src.app:app",
        host="0.0.0.0",
        port=settings.api_port,
        reload=settings.api_reload,
        reload_dirs=["src"] if settings.api_reload else None,
    )


if __name__ == "__main__":
    main()
