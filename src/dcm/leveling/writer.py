"""단일 전용-스레드 SQLite serializer (R1).

sqlite3 connection 은 생성 스레드에 귀속(thread-affinity)된다 — 스레드 풀(to_thread)이나
여러 스레드에서 공유하면 ProgrammingError/손상 위험이 있다. 따라서 connection 을 단일 워커
스레드 '안에서' 생성하고, 모든 op(쓰기·읽기)를 thread-safe `queue.Queue` 로 전달해 직렬
실행한다 → 락 경합/`database is locked` 구조적 부재.

`asyncio.Queue` 는 이벤트루프에 귀속돼 외부 스레드에서 .get()/.put() 이 안전하지 않으므로
사용하지 않는다. 결과가 필요한 read 는 `concurrent.futures.Future` 로 교차한다(호출 스레드가
.result() 로 잠깐 블록 — 기존 동기 sqlite read 와 동일 패턴). 쓰기는 fire-and-forget 으로
즉시 반환(핫패스 비블로킹).
"""
from __future__ import annotations

import logging
import queue
import sqlite3
import threading
from concurrent.futures import Future
from typing import Callable

log = logging.getLogger(__name__)

_STOP = object()  # 종료 sentinel (큐 FIFO 로 선행 op 모두 처리 후 종료 → drain)


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
        """op 를 워커 스레드에 제출. wait=True 면 결과를 받을 때까지 블록."""
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
        """그레이스풀 종료: sentinel 을 큐 끝에 넣어 선행 op 를 모두 drain 후 종료(R2)."""
        if self._stopped:
            return
        self._stopped = True
        self._q.put(_STOP)
        self._thread.join(timeout=timeout)
