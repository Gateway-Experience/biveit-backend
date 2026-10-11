# Intentions: the codebase's logging utilities -- two related concerns in one place:
#   1. Console logging SETUP (configure_logging + the formatters): install the root handler so our
#      logging.getLogger(...) lines actually print. Called once at process startup (main).
#   2. ERROR logging (log_error / log_http_error / extend_error): one choke point so the whole codebase
#      reports failures the same way. From inside an `except` block it appends the active traceback,
#      preserving what logging.exception() used to give us. AI agents can rely on this normalization.
#
# TWO FORMATS, chosen by env (the observability seam):
#   - LOCAL (env == "local"): human TEXT -- "LEVEL [ts]: message  {json meta}" with the level colour-coded
#     on a TTY. Convenient to eyeball while developing.
#   - DEPLOYED (prod / canary / dev / anything not "local"): pure single-line JSON -- ONE object per line
#     carrying msg + level + env/service + every per-record field, with any traceback folded into a
#     "stacktrace" field. This is the STANDARD structured-logging shape: the collector (Grafana Alloy)
#     parses the WHOLE line with a plain json stage -- NO regex, NO tail-splitting, NO multiline folding.
#     Immune to whatever the message text contains (braces/tabs/newlines are escaped inside the JSON
#     string). Low-cardinality fields (env/service/level) become Loki labels; the rest stay in the body,
#     queryable via LogQL.
# Nothing pushes to a sink from here (the collector tails stdout), so the app never makes a log network
# call. Per-record fields ride on the LogRecord via logging's `extra=` (log_error uses this for
# type/meta). Keep label cardinality low: env/service/level are bounded; identifiers (tenant_id, url,
# error_type, ...) stay in the body, not as labels. References: docs/logging-design.md.
#
# Error metadata convention: raise exceptions with a dict as the 2nd arg -- e.g.
#   raise SomeError("bad base64", {"request_id": rid})
# That dict (exc.args[1]) travels with the exception. Intermediate layers extend it with
# extend_error(exc, {...}) as it bubbles, and log_error() lifts it automatically.

import functools
import inspect
import json
import logging
import sys
import time

from src.config import settings

# --- Console logging setup ------------------------------------------------------------------------

# ANSI colour per level, applied to the LEVEL token only. Only used when the stream is a TTY (piping to a
# file/pipe gets plain text, no escape-code garbage).
_LEVEL_COLORS = {
    logging.DEBUG: "\033[36m",  # cyan
    logging.INFO: "\033[32m",  # green
    logging.WARNING: "\033[33m",  # yellow
    logging.ERROR: "\033[31m",  # red
    logging.CRITICAL: "\033[1;31m",  # bold red
}
_RESET = "\033[0m"

# LogRecord attributes we must NOT copy into the JSON tail as "custom" fields -- they're logging's own
# built-ins (or already rendered in the prefix). Anything a caller attaches via `extra=` is NOT in here,
# so it flows into the tail. References: logging.LogRecord attribute list.
_RESERVED_RECORD_ATTRS = frozenset(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


def _record_fields(record: logging.LogRecord) -> dict:
    # [Pure] The per-record custom fields to merge into the JSON tail: everything a caller passed via
    # `extra=` (i.e. record attributes that aren't logging built-ins). log_error rides type/meta here.
    return {k: v for k, v in record.__dict__.items() if k not in _RESERVED_RECORD_ATTRS}


class _TextFormatter(logging.Formatter):
    """LOCAL format: "LEVEL [YYYY-MM-DD HH:MM:SS.mmm]: message  {meta json}", level colour-coded on a TTY.
    For human eyes while developing -- the trailing meta is a convenience, NOT parsed by any collector
    locally (Alloy only runs against deployed envs, which use JSON). datefmt has no sub-second directive,
    so the ms come from record.msecs."""

    def __init__(self, use_color: bool) -> None:
        super().__init__(datefmt="%Y-%m-%d %H:%M:%S")
        self._use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        ts = f"{self.formatTime(record, self.datefmt)}.{int(record.msecs):03d}"
        level = record.levelname
        if self._use_color and record.levelno in _LEVEL_COLORS:
            level = f"{_LEVEL_COLORS[record.levelno]}{level}{_RESET}"
        line = f"{level} [{ts}]: {record.getMessage()}"
        fields = _record_fields(record)
        if fields:  # show the caller's extra= as a compact meta blob (local convenience only)
            line += "  " + json.dumps(fields, default=str)
        # Append the traceback for eyeballing (stock Formatter does this; our override must do it too).
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


class _JsonFormatter(logging.Formatter):
    """DEPLOYED format: ONE JSON object per line -- the standard structured-logging shape a collector
    parses whole (no regex/splitting; immune to message content). Fields: ts, level, env, service, msg,
    every per-record `extra=` field, and (on an exception) a `stacktrace` string. Because it's a single
    JSON object, a message containing braces/tabs/newlines is safely escaped inside the "msg" string and
    a traceback is one escaped "stacktrace" field -- so the whole entry stays ONE physical line, ONE Loki
    entry. default=str so an odd value (Decimal/datetime/exception) can't make a line unserializable."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "env": settings.env,
            "service": settings.app_name,
            "msg": record.getMessage(),
            **_record_fields(record),
        }
        if record.exc_info:
            entry["stacktrace"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging() -> None:
    """Install a root StreamHandler at INFO so our own `logging.getLogger(...)` lines actually print.
    Without any config, Python's last-resort handler is WARNING-only, so every log.info (the worker's
    startup line, the consumer's, and the amz.request access log) was silently dropped. FORMAT is chosen
    by env: `local` -> human text (colour on a TTY); any deployed env (prod/canary/dev/...) -> pure JSON
    lines for the Loki collector. In the api role uvicorn also configures its own uvicorn* loggers; those
    don't propagate to root, so there's no double-print. Call once at process startup (all roles, via
    main)."""
    formatter = _TextFormatter(use_color=sys.stderr.isatty()) if settings.env == "local" else _JsonFormatter()
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers = [handler]  # replace any last-resort/default handler; we own root formatting


# --- Profiling (dev/diagnostic) -------------------------------------------------------------------
# A @profile decorator that logs a function's wall-clock. Async-AWARE: for `async def` it must await the
# coroutine and time the await (a sync wrapper would only time coroutine CREATION -- ~0s -- not execution).
# So we branch on iscoroutinefunction and return the matching wrapper. A DIAGNOSTIC (not a hot-path logger):
# drop it onto a suspect call, read the [PROFILING] lines, then remove it. Bare `@profile` logs under the
# function's __qualname__; `@profile("label")` (or `label=...`) logs under a custom label.

_profiling_log = logging.getLogger("profiling")


# ci:trivial
def log_profiling(item: str, seconds: float) -> None:
    """[Pure-ish] emit one profiling line."""
    _profiling_log.info("[PROFILING] %s took %.4fs", item, seconds)


# ci:trivial
def profile(fn_or_label=None, *, label: str | None = None):
    """Wrap `fn` to log its wall-clock. Works on sync + async functions. Usage:
    @profile              -> logs "<qualname>"
    @profile("page X")    -> logs "<qualname> page X"  -- label shown ALONGSIDE the function name
    @profile(label="X")   -> same, keyword form."""
    # First positional arg is EITHER the decorated function (bare @profile) or a label string (@profile("x")).
    if isinstance(fn_or_label, str):
        label, fn_or_label = fn_or_label, None

    def decorate(f):
        name = f"{f.__qualname__} {label}" if label else f.__qualname__
        if inspect.iscoroutinefunction(f):

            @functools.wraps(f)
            async def async_wrapper(*args, **kwargs):
                start = time.perf_counter()
                try:
                    return await f(*args, **kwargs)
                finally:
                    log_profiling(name, time.perf_counter() - start)

            return async_wrapper

        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            start = time.perf_counter()
            try:
                return f(*args, **kwargs)
            finally:
                log_profiling(name, time.perf_counter() - start)

        return wrapper

    return decorate(fn_or_label) if fn_or_label is not None else decorate  # bare @profile vs @profile("label")


# --- Error logging --------------------------------------------------------------------------------

_error_log = logging.getLogger("error")


# ci:trivial
def extend_error(exc: BaseException, meta: dict) -> None:
    """Extend an in-flight exception's metadata (its 2nd arg) so context accumulates as it bubbles up."""
    if len(exc.args) < 2 or not isinstance(exc.args[1], dict):
        exc.args = ((exc.args[0] if exc.args else ""), {})
    exc.args[1].update(meta)


# Use this in inner (e.g. services) layer, generally when the error is swallowed there or the bubble chain breaks.
# Otherwise it is adviced to just raise the error and let the controller layer take care of logging.
# Exception-safe by design: this is the error choke point and is called from except blocks / resilient
# loops, so it must NEVER raise -- a failure here (a bad __str__/__repr__, a closed stdout, etc.) would
# mask the real error or break the caller's recovery. Any internal failure is swallowed best-effort.
# ci:trivial
def log_error(type: str | BaseException, message: str = "", meta: dict | None = None) -> None:
    """Convenience: pass an Exception and we lift its class name as the type, args[0] as the message,
    and args[1] (if a dict) as the embedded metadata. Explicit call-site meta wins over embedded."""
    try:
        if isinstance(type, BaseException):
            exc = type
            embedded = exc.args[1] if len(exc.args) > 1 and isinstance(exc.args[1], dict) else {}
            message = message or (str(exc.args[0]) if exc.args else "")
            type = exc.__class__.__name__
            meta = {**embedded, **(meta or {})}

        # Human prefix: the greppable "type | message" (unchanged eyeball format). The STRUCTURED copy
        # rides via extra= so the formatter lifts `type`/`meta` into the JSON tail -- `type` is the field
        # alerts filter on (e.g. err rate by type), so it must be a clean tail field, not just prose.
        # `type` is renamed to `error_type` on the record to avoid colliding with LogRecord internals.
        prose = f"type={type}" + (f" | message={message}" if message else "")
        # Route through logging (level=ERROR) so it gets the shared prefix + colour + JSON tail. exc_info
        # when inside an except block appends the active traceback. logging.error never propagates a
        # handler failure, so this stays non-raising.
        _error_log.error(
            prose,
            exc_info=sys.exc_info()[0] is not None,
            extra={"error_type": type, **({"meta": meta} if meta else {})},
        )
    except Exception:
        # Last-ditch: the logger itself must not propagate. Try a minimal note, but swallow even that.
        try:
            _error_log.error("log_error failed to format/emit an error")
        except Exception:
            pass


# Should generally be used in controller layer. Errors from inner layers should bubble up and get logged there.
# ci:trivial
def log_http_error(type: str | BaseException, request, message: str = "", meta: dict | None = None) -> None:
    """Same as log_error but pulls the full request URL + method into the metadata. `request` is any
    object exposing `.url` and `.method` (e.g. a Starlette/FastAPI Request)."""
    http_meta = {"url": str(request.url), "http_method": request.method}
    log_error(type, message, {**(meta or {}), **http_meta})
