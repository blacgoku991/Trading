"""Verrou d'instance unique : deux bots sur le même compte enverraient des ordres en double.

Verrou du système d'exploitation sur un fichier : libéré automatiquement si le processus s'arrête,
même brutalement (pas de verrou « oublié » après un plantage).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: IO[str] | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        handle.truncate(0)
        handle.write(str(os.getpid()))
        handle.flush()
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is not None:
            self._handle.close()  # le verrou est libéré avec le descripteur
            self._handle = None
