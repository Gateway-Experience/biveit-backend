import asyncio
import logging
import sys

from src.utils.logging import _downgrade_shutdown_cancels


def _make_record(msg: str, exc: BaseException | None = None) -> logging.LogRecord:
    exc_info = (type(exc), exc, None) if exc else None
    return logging.LogRecord("uvicorn.error", logging.ERROR, __file__, 1, msg, (3,), exc_info)


def test_shutdown_task_cancel_becomes_warning():
    record = _make_record("Cancel %s running task(s), timeout graceful shutdown exceeded")
    downgraded = _downgrade_shutdown_cancels(record)
    assert (downgraded.levelno, downgraded.levelname) == (logging.WARNING, "WARNING")
    assert downgraded.getMessage() == "Cancel 3 running task(s), timeout graceful shutdown exceeded"
    assert record.levelno == logging.ERROR  # the original isn't touched


def test_cancelled_asgi_app_becomes_warning():
    record = _make_record("Exception in ASGI application\n", asyncio.CancelledError("timeout graceful shutdown"))
    assert _downgrade_shutdown_cancels(record).levelno == logging.WARNING


def test_real_asgi_error_stays_error():
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            "uvicorn.error", logging.ERROR, __file__, 1, "Exception in ASGI application\n", None, sys.exc_info()
        )
    assert _downgrade_shutdown_cancels(record) is record
