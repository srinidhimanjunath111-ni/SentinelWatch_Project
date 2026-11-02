"""Killable wall-clock boundary around remote imagery I/O, including provider failover.

GDAL's socket timeouts apply per request, not to an entire window. A spawned process
lets the caller stop a stalled fetch and its band threads without leaving them running.
Only complete arrays return to the parent, which alone writes the scene cache.
"""

from __future__ import annotations

import multiprocessing
import time
from collections.abc import Callable

from app.satellite.provider import ProviderError


class FetchDeadlineExceeded(ProviderError):
    """The remote operation used its entire wall-clock allowance."""


def _invoke(connection, operation, args):
    try:
        connection.send((True, operation(*args)))
    except Exception as error:
        connection.send((False, str(error)))
    finally:
        connection.close()


def bounded_call(operation: Callable, args: tuple, seconds: float):
    """Run remote I/O in a disposable process; never leave a timed-out worker behind."""

    context = multiprocessing.get_context("spawn")
    receiving, sending = context.Pipe(duplex=False)
    process = context.Process(target=_invoke, args=(sending, operation, args), daemon=True)
    deadline = time.monotonic() + seconds
    process.start()
    sending.close()
    try:
        if not receiving.poll(max(0.0, deadline - time.monotonic())):
            raise FetchDeadlineExceeded(
                f"Imagery fetch exceeded its {seconds:g} second wall-clock budget."
            )
        try:
            succeeded, value = receiving.recv()
        except EOFError as error:
            raise ProviderError("Imagery fetch worker stopped without a complete response.") from error
        if not succeeded:
            raise ProviderError(value)
        return value
    finally:
        receiving.close()
        if process.is_alive():
            process.terminate()
        process.join(timeout=1)
        if process.is_alive():
            process.kill()
            process.join(timeout=1)
        process.close()
