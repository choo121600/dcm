"""Single dedicated-thread SQLite serializer (R1).

A sqlite3 connection has thread-affinity to its creating thread — sharing it across a thread pool
(to_thread) or multiple threads risks ProgrammingError/corruption. So the connection is created
'inside' a single worker thread, and every op (write/read) is passed via a thread-safe
`queue.Queue` for serial execution → lock contention / `database is locked` are structurally
absent.

`asyncio.Queue` is not used because it is bound to the event loop and .get()/.put() from an
external thread is not safe. Reads that need a result are handed back via a
`concurrent.futures.Future` (the calling thread blocks briefly on .result() — the same pattern as
an ordinary synchronous sqlite read). Writes return immediately as fire-and-forget (non-blocking
on the hot path).
"""
from __future__ import annotations

import logging
import queue
import sqlite3
import threading
from concurrent.futures import Future
from typing import Callable

log = logging.getLogger(__name__)

_STOP = object()  # stop sentinel (queue FIFO processes all preceding ops, then stops → drain)


class SqliteWriter:
    def __init__(
        self, db_path: str, *, schema: str = "", pragmas: tuple[str, ...] = ()
    ) -> None:
        self._db_path = db_path
        self._schema = schema
        self._pragmas = tuple(pragmas)
        self._q: "queue.Queue[object]" = queue.Queue()
        self._thread = threading.Thread(
            target=self._run, name="leveling-writer", daemon=True
        )
        self._stopped = False

    def start(self) -> None:
        self._thread.start()

    def submit(self, fn: Callable[[sqlite3.Connection], object], *, wait: bool):
        """Submit an op to the worker thread. If wait=True, block until the result is available."""
        if self._stopped:
            raise RuntimeError("SqliteWriter is stopped")
        if wait:
            fut: "Future[object]" = Future()
            self._q.put((fn, fut))
            return fut.result()
        self._q.put((fn, None))
        return None

    def _run(self) -> None:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            for pragma in self._pragmas:
                conn.execute(pragma)
            if self._schema:
                conn.executescript(self._schema)
            conn.commit()
        except Exception:  # noqa: BLE001
            log.exception("leveling writer: connection init failed")
        while True:
            item = self._q.get()
            try:
                if item is _STOP:
                    break
                fn, fut = item  # type: ignore[misc]
                try:
                    result = fn(conn)
                    if fut is not None:
                        fut.set_result(result)
                except Exception as exc:  # noqa: BLE001
                    if fut is not None:
                        fut.set_exception(exc)
                    else:
                        log.exception("leveling writer: op failed (fire-and-forget)")
            finally:
                self._q.task_done()
        conn.close()

    def stop(self, *, timeout: float = 5.0) -> None:
        """Graceful shutdown: enqueue the sentinel at the tail to drain all preceding ops before stopping (R2)."""
        if self._stopped:
            return
        self._stopped = True
        self._q.put(_STOP)
        self._thread.join(timeout=timeout)
