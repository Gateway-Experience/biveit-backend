# Intentions: a SCRATCH pad for poking async service functions against the LIVE server's connections during
# local dev. app.lifespan calls do_test() once at startup (LOCAL ONLY) -- and uvicorn --reload re-runs startup
# on every file save, so the loop is: edit the body of do_test, save, read the output in the server log. You're
# already inside the server's event loop with its live connections (pg/redis/http all connected by lifespan
# before this runs), so just `await` service functions directly -- no asyncio.run, no connect(), no boilerplate.
#
# NOT part of the product. do_test's body is throwaway; leave it a no-op (pass) when you're not actively
# debugging so a normal boot logs nothing. NEVER rely on this in prod (lifespan gates it to env == 'local').
# References: evolix-backend src/debug_scratch.py (same mechanism).

from src.utils.logging import log_error


async def do_test() -> None:
    """The startup ENTRYPOINT -- a one-line POINTER to a `do_test` you define IN the file you're testing (so
    the payload lives next to the logic under test, not here). To debug a different module, change the import
    below to point at ITS do_test. Comment the body out (leave `pass`) when not debugging.

    Runs on the server's loop with live pg/redis/http; wrapped by run_do_test so a raise never aborts startup."""
    # --- point at the module you're testing (define `async def do_test()` in it), then save ---------------
    # from src.services.agent.sessions import do_test as _colocated
    #
    # await _colocated()


async def run_do_test() -> None:
    """Startup entrypoint: run do_test() but SWALLOW any error (a broken scratch body must never take the
    server down on reload). Logs the failure so you see it in the server log. [I/O: whatever do_test does]"""
    try:
        await do_test()
    except Exception as e:
        log_error(e, "do_test (debug scratch) raised -- ignored so startup continues")
