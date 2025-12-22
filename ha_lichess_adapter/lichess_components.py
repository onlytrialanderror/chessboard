import berserk
import threading
from typing import Callable, Dict, Optional, Tuple


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

import paho.mqtt.client as paho
import ssl
import lichess_helpers as lh

CA_CERT_PATH = "/config/hivemq.pem"
class MqttGateway:
    """
    Thin wrapper around paho MQTT client.
    - handles TLS setup
    - subscribes to topics
    - routes messages to registered topic handlers
    """

    def __init__(
        self,
        secrets: Dict[str, Dict[str, str]],
        log: Callable[[str, str], None] = _default_log
    ) -> None:
        
        # ---- MQTT config ----
        mqtt_cfg = secrets.get("mqtt", {}) or {}
        username = mqtt_cfg.get("username") or None
        password = mqtt_cfg.get("password") or None
        self._tls_enabled = bool(mqtt_cfg.get("mqtt_tls", False))
        ca_cert_path = mqtt_cfg.get("mqtt_ca_cert", CA_CERT_PATH)

        self._host = mqtt_cfg.get("host", "127.0.0.1")
        self._port = int(mqtt_cfg.get("port", 1883))
        self._client_id = mqtt_cfg.get("client_id", "appdaemon-lichess")
        self._log = log

        self._client = paho.Client(client_id=self._client_id, protocol=paho.MQTTv311, clean_session=True)

        if username is not None:
            self._client.username_pw_set(username, password=password)

        if self._tls_enabled:
            if not ca_cert_path:
                raise ValueError("TLS enabled but ca_cert_path not provided.")
            self._client.tls_set(
                ca_certs=ca_cert_path,
                certfile=None,
                keyfile=None,
                cert_reqs=ssl.CERT_REQUIRED,
                tls_version=ssl.PROTOCOL_TLS_CLIENT,
            )

        self._handlers: Dict[str, Callable[[str], None]] = {}
        self._subscriptions: Tuple[Tuple[str, int], ...] = tuple()

        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    def register_handler(self, topic: str, handler: Callable[[str], None]) -> None:
        self._handlers[topic] = handler

    def connect_and_loop(self, subscriptions: Tuple[Tuple[str, int], ...]) -> None:
        self._subscriptions = subscriptions
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client.connect(self._host, self._port, keepalive=60)
        self._client.loop_start()

    def publish(self, topic: str, payload: str, qos: int = 0, retain: bool = False) -> None:
        self._client.publish(topic, payload=payload, qos=qos, retain=retain)

    def stop(self) -> None:
        try:
            self._client.loop_stop()
        finally:
            try:
                self._client.disconnect()
            except Exception:
                pass

    # ---- callbacks ----

    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            self._log(f"MQTT connection failed rc={rc}", LEVEL = "ERROR")
            return
        self._log("MQTT connected, subscribing to topics...", LEVEL = "INFO")
        if self._subscriptions:
            client.subscribe(list(self._subscriptions))
        self._log(
            f"MQTT ready in LichessApiHandler via paho. host={self._host}, port={self._port}, tls={'on' if self._tls_enabled else 'off'}"
        )

    def _on_disconnect(self, client, userdata, rc):
        self._log(f"MQTT disconnected rc={rc}", LEVEL = "INFO")

    def _on_message(self, client, userdata, msg):
        payload = lh.payload_to_str(msg.payload)
        if payload is None:
            return
        handler = self._handlers.get(msg.topic)
        if handler:
            handler(payload)