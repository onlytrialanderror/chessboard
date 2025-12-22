import berserk
import threading
from typing import Callable, Dict, Optional

def _default_log(msg: str, level: str = "INFO") -> None:
    # Minimal fallback logger (AppDaemon will inject its own)
    print(f"[{level}] {msg}")

class LichessClientPool:
    """
    Manages berserk TokenSessions and Clients, keyed by a string.

    This eliminates the duplicated _init_session/_close_session methods.
    """

    def __init__(self, log: Callable[[str, str], None] = _default_log) -> None:
        self._log = log
        self._lock = threading.Lock()
        self._sessions: Dict[str, berserk.TokenSession] = {}
        self._clients: Dict[str, berserk.Client] = {}

    def set_token(self, key: str, token: str, *, idle_token: str) -> None:
        """
        (Re)create session+client under `key`.
        If token is idle_token/empty -> closes existing.
        """
        with self._lock:
            self._close_nolock(key)
            if not token or token == idle_token:
                return
            sess = berserk.TokenSession(token)
            self._sessions[key] = sess
            self._clients[key] = berserk.Client(sess)

    def get(self, key: str) -> Optional[berserk.Client]:
        with self._lock:
            return self._clients.get(key)

    def close(self, key: str) -> None:
        with self._lock:
            self._close_nolock(key)

    def close_all(self) -> None:
        with self._lock:
            keys = list(self._sessions.keys())
        for k in keys:
            self.close(k)

    def _close_nolock(self, key: str) -> None:
        sess = self._sessions.pop(key, None)
        self._clients.pop(key, None)
        if sess:
            try:
                sess.close()
            except Exception:
                self._log(f"Failed to close the client session {key}")
                pass