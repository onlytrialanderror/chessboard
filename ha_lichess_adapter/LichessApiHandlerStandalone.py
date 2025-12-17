#!/usr/bin/env python3
"""
Standalone Lichess API handler for Termux/Android.

- MQTT: uses Eclipse Paho (paho-mqtt)
- No Home Assistant / AppDaemon dependencies
- Connects to local Mosquitto broker (default: 127.0.0.1:1883)
- Reads MQTT host/port/username/password and chessboard_secret_key from ./secrets.yaml
- Subscribes and publishes on the same topics as the original AppDaemon app.
"""

from __future__ import annotations

import json
import logging
import queue
import signal
import threading
import time
from typing import Optional

import berserk
import paho.mqtt.client as mqtt
import yaml

import lichess_helpers as lh

# ---- Constants (kept from original app) ----
IDLE_GAME_ID = "idle"
UNAVAILABLE_STATE = "unavailable"
UNKNOWN_STATE = "unknown"
EMPTY_STRING = ""
IDLE_LICHESS_TOKEN = "idle"
STATUS_OFFLINE = "offline"
STATUS_ONLINE = "online"

# MQTT topics (kept from original app)
MQTT_API_CALL_TOPIC = "chessboard/api_call"
MQTT_RESPONSE_TOPIC = "chessboard/response"

MQTT_GAME_ID_TOPIC = "chessboard/game_id"
MQTT_TOKEN_MAIN_TOPIC = "chessboard/token_main"
MQTT_TOKEN_OPP_TOPIC = "chessboard/token_opponent"

MQTT_STATUS_TOPIC = "chessboard/status"

CLASS_NAME = "LichessApiHandlerStandalone"

SECRETS_PATH = "C:/work/python/secrets.yaml"

class LichessApiHandlerStandalone:
    def __init__(self, log_level: str = "INFO") -> None:
        # ---- Logging ----
        self.log = logging.getLogger(CLASS_NAME)
        self.log.setLevel(getattr(logging, log_level.upper(), logging.INFO))

        # ---- Load secrets (local file) ----
        self.secrets = lh.load_secrets(SECRETS_PATH)

        # ---- Chess secret key ----
        self._current_secret_key = self.secrets.get("chessboard_secret_key")
        if not self._current_secret_key:
            raise RuntimeError("Missing 'chessboard_secret_key' in ./secrets.yaml")

        # ---- MQTT config (from secrets.yaml) ----
        mqtt_cfg = self.secrets.get("mqtt", {}) or {}
        self.mqtt_host = mqtt_cfg.get("host", "127.0.0.1")
        self.mqtt_port = int(mqtt_cfg.get("port", 1883))
        self.mqtt_keepalive = int(mqtt_cfg.get("keepalive", 60))
        self.mqtt_username = mqtt_cfg.get("username") or None
        self.mqtt_password = mqtt_cfg.get("password") or None
        self.mqtt_client_id = mqtt_cfg.get("client_id", "lichess_api_handler")

        # ---- Runtime state ----
        self._current_game_id = IDLE_GAME_ID
        self._token_main = IDLE_LICHESS_TOKEN
        self._token_opponent = IDLE_LICHESS_TOKEN

        self._session_main: Optional[berserk.TokenSession] = None
        self._session_opponent: Optional[berserk.TokenSession] = None
        self._client_main = EMPTY_STRING
        self._client_opponent = EMPTY_STRING

        # Workers / concurrency
        self._api_q: queue.Queue[Optional[str]] = queue.Queue()
        self._api_worker: Optional[threading.Thread] = None
        self._stream_worker: Optional[threading.Thread] = None
        self._board_worker_main: Optional[threading.Thread] = None
        self._board_worker_opponent: Optional[threading.Thread] = None

        self._lock = threading.Lock()
        self._stop_event = threading.Event()

        # ---- MQTT client ----
        self.mqtt = mqtt.Client(client_id=self.mqtt_client_id, protocol=mqtt.MQTTv311)
        if self.mqtt_username:
            self.mqtt.username_pw_set(self.mqtt_username, self.mqtt_password)

        self.mqtt.on_connect = self._on_connect
        self.mqtt.on_message = self._on_message
        self.mqtt.on_disconnect = self._on_disconnect

    # ---------- Public lifecycle ----------

    def start(self) -> None:
        self.log.info("Starting %s", CLASS_NAME)

        # Connect & start network loop in background thread
        self.mqtt.connect(self.mqtt_host, self.mqtt_port, self.mqtt_keepalive)
        self.mqtt.loop_start()

        # Wait until stopped (Ctrl+C or SIGTERM)
        while not self._stop_event.is_set():
            time.sleep(0.2)

        self.stop()

    def stop(self) -> None:
        # idempotent stop
        self._stop_event.set()

        self.log.info("Stopping %s ...", CLASS_NAME)

        # Stop workers
        self._stop_api_worker()
        self._stop_stream_worker()
        self._stop_board_worker()

        # MQTT teardown
        try:
            self.mqtt.loop_stop()
        except Exception:
            pass
        try:
            self.mqtt.disconnect()
        except Exception:
            pass

        # Close sessions
        try:
            if self._session_main is not None:
                self._session_main.close()
        except Exception:
            pass
        try:
            if self._session_opponent is not None:
                self._session_opponent.close()
        except Exception:
            pass

        self.log.info("%s stopped.", CLASS_NAME)

    # ---------- MQTT callbacks ----------

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.log.info("MQTT connected to %s:%s", self.mqtt_host, self.mqtt_port)

            # Subscribe to inputs
            client.subscribe(MQTT_API_CALL_TOPIC, qos=0)
            client.subscribe(MQTT_GAME_ID_TOPIC, qos=0)
            client.subscribe(MQTT_TOKEN_MAIN_TOPIC, qos=0)
            client.subscribe(MQTT_TOKEN_OPP_TOPIC, qos=0)
            client.subscribe(MQTT_STATUS_TOPIC, qos=0)

            self.log.info(
                "MQTT ready. api_call='%s', response='%s'",
                MQTT_API_CALL_TOPIC,
                MQTT_RESPONSE_TOPIC,
            )
        else:
            self.log.error("MQTT connect failed rc=%s", rc)

    def _on_disconnect(self, client, userdata, rc):
        self.log.warning("MQTT disconnected rc=%s", rc)

    def _on_message(self, client, userdata, msg):
        topic = msg.topic
        payload = self._payload_to_str(msg.payload)
        if payload is None:
            return

        if topic == MQTT_API_CALL_TOPIC:
            self._on_mqtt_api_call(payload)
        elif topic == MQTT_GAME_ID_TOPIC:
            self._on_mqtt_game_id(payload)
        elif topic == MQTT_TOKEN_MAIN_TOPIC:
            self._on_mqtt_token_main(payload)
        elif topic == MQTT_TOKEN_OPP_TOPIC:
            self._on_mqtt_token_opponent(payload)
        elif topic == MQTT_STATUS_TOPIC:
            self._on_mqtt_status(payload)

    # ---------- Payload helpers ----------

    @staticmethod
    def _payload_to_str(payload_bytes: bytes) -> Optional[str]:
        try:
            return payload_bytes.decode("utf-8", errors="strict")
        except Exception:
            try:
                return payload_bytes.decode("utf-8", errors="replace")
            except Exception:
                return None

    # ---------- Publish helpers ----------

    def publish_response(self, payload: str) -> None:
        try:
            self.mqtt.publish(MQTT_RESPONSE_TOPIC, payload=payload, qos=0, retain=False)
        except Exception as e:
            self.log.exception("Failed to publish MQTT response: %s", e)

    # ---------- MQTT topic handlers ----------

    def _on_mqtt_api_call(self, payload: str) -> None:
        if payload in {IDLE_GAME_ID, UNAVAILABLE_STATE, UNKNOWN_STATE, EMPTY_STRING}:
            return
        self._api_q.put(payload)

    def _on_mqtt_game_id(self, payload: str) -> None:
        if payload in {UNAVAILABLE_STATE, UNKNOWN_STATE, EMPTY_STRING}:
            return
        self.game_id_changed(payload)

    def _on_mqtt_token_main(self, payload: str) -> None:
        self.token_changed_main(payload)

    def _on_mqtt_token_opponent(self, payload: str) -> None:
        self.token_changed_opponent(payload)

    def _on_mqtt_status(self, payload: str) -> None:
        if payload in {EMPTY_STRING, STATUS_OFFLINE}:
            self.log.info("Chessboard is offline, stopping workers")
            self._stop_api_worker()
            self._stop_stream_worker()
            self._stop_board_worker()
        else:
            self.log.info("Chessboard is online, starting workers")
            self._run_api_worker()
            self._run_stream_worker()
            self._run_board_worker()

    # ---------- Worker control ----------

    def _run_api_worker(self) -> None:
        if self._api_worker and self._api_worker.is_alive():
            return
        self.log.info("API worker started")
        self._api_worker = threading.Thread(target=self._api_loop, daemon=True)
        self._api_worker.start()

    def _stop_api_worker(self) -> None:
        # sentinel shutdown
        self._api_q.put(None)

    def _run_stream_worker(self) -> None:
        if self._token_main != IDLE_LICHESS_TOKEN:
            if self._stream_worker and self._stream_worker.is_alive():
                return
            self.log.info("Stream worker started")
            self._stream_worker = threading.Thread(
                target=self.handle_incoming_events,
                args=(self._token_main,),
                daemon=True,
            )
            self._stream_worker.start()

    def _stop_stream_worker(self) -> None:
        if self._stream_worker and self._stream_worker.is_alive():
            self.log.info("Stopping Stream worker")
            self._token_main = IDLE_LICHESS_TOKEN
            self._stream_worker.join(timeout=2)
            self._stream_worker = None
            self.log.info("Stream worker stopped")

    def _run_board_worker(self) -> None:
        current_game_id = self._current_game_id
        self.log.info("Board %s: (main) worker starting", current_game_id)
        self._board_worker_main = threading.Thread(
            target=self.handle_game_state_main,
            args=(current_game_id,),
            daemon=True,
        )
        self.log.info("Board %s: (opponent) worker starting", current_game_id)
        self._board_worker_opponent = threading.Thread(
            target=self.handle_game_state_opponent,
            args=(current_game_id,),
            daemon=True,
        )
        self._board_worker_main.start()
        self._board_worker_opponent.start()

    def _stop_board_worker(self) -> None:
        self._current_game_id = IDLE_GAME_ID
        self.log.info("Board worker stopping")
        if self._board_worker_main is not None:
            self._board_worker_main.join(timeout=1)
            self._board_worker_main = None
        if self._board_worker_opponent is not None:
            self._board_worker_opponent.join(timeout=1)
            self._board_worker_opponent = None
        self.log.info("Board worker stopped")

    # ---------- State changes ----------

    def game_id_changed(self, new: str) -> None:
        if not new or new in {UNAVAILABLE_STATE, UNKNOWN_STATE, EMPTY_STRING}:
            return
        if new == self._current_game_id:
            return

        old = self._current_game_id
        self._stop_board_worker()
        self._current_game_id = new
        self.log.info("Game ID changed: %s -> %s", old, new)
        self._run_board_worker()

    def token_changed_main(self, new: str) -> None:
        if not new or new in {UNAVAILABLE_STATE, UNKNOWN_STATE, EMPTY_STRING}:
            if new in {UNAVAILABLE_STATE, UNKNOWN_STATE}:
                self.log.warning("Not allowed token (main): %s", new)
            return

        new_decrypted = lh.decrypt_message(self._current_secret_key, new)
        if new_decrypted == self._token_main:
            return

        old = self._token_main
        self._token_main = new_decrypted
        self.log.info("Token changed (main): %s -> %s", old, new_decrypted)

        try:
            if self._session_main is not None:
                self._session_main.close()
        except Exception:
            pass
        self._session_main = None

        if new_decrypted != IDLE_LICHESS_TOKEN:
            self._session_main = berserk.TokenSession(new_decrypted)
            self._client_main = berserk.Client(self._session_main)
            self._run_stream_worker()
        else:
            self._client_main = EMPTY_STRING
            self._stop_stream_worker()

    def token_changed_opponent(self, new: str) -> None:
        if not new or new in {UNAVAILABLE_STATE, UNKNOWN_STATE, EMPTY_STRING}:
            if new in {UNAVAILABLE_STATE, UNKNOWN_STATE}:
                self.log.warning("Not allowed token (opponent): %s", new)
            return

        new_decrypted = lh.decrypt_message(self._current_secret_key, new)
        if new_decrypted == self._token_opponent:
            return

        old = self._token_opponent
        self._token_opponent = new_decrypted
        self.log.info("Token changed (opponent): %s -> %s", old, new_decrypted)

        try:
            if self._session_opponent is not None:
                self._session_opponent.close()
        except Exception:
            pass
        self._session_opponent = None

        if new_decrypted != IDLE_LICHESS_TOKEN:
            self._session_opponent = berserk.TokenSession(new_decrypted)
            self._client_opponent = berserk.Client(self._session_opponent)
        else:
            self._client_opponent = EMPTY_STRING

    # ---------- Lichess logic ----------

    def check_game_over(self, dat: dict, game_id: str) -> bool:
        break_game = False
        if self._current_game_id == IDLE_GAME_ID:
            break_game = True
        if self._current_game_id != game_id:
            break_game = True
        if dat.get("type") == "gameState" and dat.get("status") != "started":
            break_game = True
        if dat.get("type") == "gameFull" and dat.get("state", {}).get("status") != "started":
            break_game = True
        if dat.get("type") == "opponentGone" and dat.get("gone") is True and dat.get("claimWinInSeconds") == 0:
            break_game = True
        return break_game

    def handle_call_trigger(self, new: str) -> None:
        try:
            json_data = json.loads(new)
        except json.JSONDecodeError as e:
            self.log.warning("Invalid JSON in api_call: %s payload=%r", e, new)
            return

        call_type = json_data.get("type")
        self.log.info("API-call type=%s data=%s", call_type, json.dumps(json_data))

        if not json_data or not call_type:
            return

        with self._lock:
            valid_token = (
                self._token_main not in {IDLE_LICHESS_TOKEN, EMPTY_STRING, UNAVAILABLE_STATE, UNKNOWN_STATE}
                and self._token_main is not None
            )
            valid_game_id = (
                self._current_game_id not in {IDLE_GAME_ID, UNAVAILABLE_STATE, UNKNOWN_STATE, EMPTY_STRING}
                and self._current_game_id is not None
            )

            if valid_token:
                if call_type == "getAccountInfoMain":
                    json_response = lh.getAccountInfoMain(self._client_main, self_log=self.log.info)
                    self.publish_response(json_response)
                    return

                if call_type == "abortRunningGames":
                    lh.abortRunningGames(self._client_main, self_log=self.log.info)
                    return

                if call_type == "createGame":
                    json_response = lh.createGame(json_data, self._client_main, self._client_opponent, self_log=self.log.info)
                    self.publish_response(json_response)
                    return

                if call_type == "withdrawTornament":
                    lh.withdrawTornament(json_data, self._client_main, self_log=self.log.info)
                    return

                if call_type == "joinTournamentByName":
                    json_response = lh.joinTournamentByName(json_data, self._client_main, self_log=self.log.info)
                    self.publish_response(json_response)
                    return

                if call_type == "joinTournamentById":
                    json_response = lh.joinTournamentById(json_data, self._client_main, self_log=self.log.info)
                    self.publish_response(json_response)
                    return

            if valid_token and valid_game_id:
                if call_type == "abort":
                    lh.abort(self._client_main, self._current_game_id, self_log=self.log.info)
                    return
                if call_type == "resign":
                    lh.resign(self._client_main, self._current_game_id, self_log=self.log.info)
                    return
                if call_type == "claim-victory":
                    lh.claimVictory(self._client_main, self._current_game_id, self_log=self.log.info)
                    return
                if call_type == "makeMove":
                    lh.makeMove(json_data, self._client_main, self._current_game_id, self_log=self.log.info)
                    return
                if call_type == "draw":
                    lh.draw(json_data, self._client_main, self._current_game_id, self_log=self.log.info)
                    return
                if call_type == "takeback":
                    lh.takeback(json_data, self._client_main, self._current_game_id, self_log=self.log.info)
                    return
                if call_type == "writeChatMessage":
                    lh.writeChatMessage(json_data, self._client_main, self._current_game_id, self_log=self.log.info)
                    return
                if call_type == "makeMoveOpponent":
                    lh.makeMoveOpponent(json_data, self._client_opponent, self._current_game_id, self_log=self.log.info)
                    return
                if call_type == "resignOpponent":
                    lh.resignOpponent(self._client_opponent, self._current_game_id, self_log=self.log.info)
                    return
                if call_type == "drawOpponent":
                    lh.drawOpponent(json_data, self._client_opponent, self._current_game_id, self_log=self.log.info)
                    return

    def handle_incoming_events(self, token_init: str = IDLE_LICHESS_TOKEN) -> None:
        if token_init and token_init not in (IDLE_LICHESS_TOKEN, UNAVAILABLE_STATE, UNKNOWN_STATE):
            self.log.info("Starting the stream (event): %s", token_init)
            for event in self._client_main.board.stream_incoming_events():
                if self._stop_event.is_set():
                    break
                if event:
                    reduced_data = json.dumps(lh.reduce_response_event(event))
                    self.publish_response(reduced_data)
                    self.log.info("Event: %s", reduced_data)

                    with self._lock:
                        if token_init != self._token_main:
                            self.log.info("Terminating the stream (event): %s", token_init)
                            break
                else:
                    with self._lock:
                        if token_init != self._token_main:
                            self.log.info("Terminating the stream (no event): %s", token_init)
                            break
        else:
            self.log.info("Waiting for new stream (event)")

    def _api_loop(self) -> None:
        self.log.info("Starting main-loop")
        while not self._stop_event.is_set():
            try:
                item = self._api_q.get(timeout=1)
            except queue.Empty:
                continue

            if item is None:
                break

            try:
                self.handle_call_trigger(item)
            except Exception as e:
                self.log.exception("API call error: %s", e)
            finally:
                try:
                    self._api_q.task_done()
                except Exception:
                    pass

        self.log.info("Terminating main-loop")

    def handle_game_state_main(self, game_id: str) -> None:
        with self._lock:
            valid_game_id = game_id not in {IDLE_GAME_ID, UNAVAILABLE_STATE, UNKNOWN_STATE}
            valid_token = self._token_main not in {IDLE_LICHESS_TOKEN, UNAVAILABLE_STATE, UNKNOWN_STATE}

        if valid_game_id and valid_token:
            self.log.info("Starting the board stream (main): %s", game_id)
            for line in self._client_main.board.stream_game_state(game_id):
                if self._stop_event.is_set():
                    break
                if line:
                    reduced_data = json.dumps(lh.reduce_response_board(game_id, line))
                    self.publish_response(reduced_data)
                    self.log.info("Board (main): %s", reduced_data)

                    with self._lock:
                        if self.check_game_over(line, game_id):
                            self.log.info("Terminating the board stream (main): %s", game_id)
                            break

            off_json_str = json.dumps({"type": "streamBoardResponse", "state": IDLE_GAME_ID})
            self.publish_response(off_json_str)
            self.log.info("Waiting for new board stream (main)")

    def handle_game_state_opponent(self, game_id: str) -> None:
        with self._lock:
            valid_game_id = game_id not in {IDLE_GAME_ID, UNAVAILABLE_STATE, UNKNOWN_STATE}
            valid_token = self._token_opponent not in {IDLE_LICHESS_TOKEN, UNAVAILABLE_STATE, UNKNOWN_STATE}

        if valid_game_id and valid_token:
            for line in self._client_opponent.board.stream_game_state(game_id):
                if self._stop_event.is_set():
                    break
                if line:
                    self.log.info("Board (opponent): %s", line)
                    with self._lock:
                        if self.check_game_over(line, game_id):
                            self.log.info("Terminating the board stream (opponent): %s", game_id)
                            break

            off_json_str = json.dumps({"type": "streamBoardResponseOpponent", "state": IDLE_GAME_ID})
            self.publish_response(off_json_str)
            self.log.info("Waiting for new board stream (opponent)")


def _configure_root_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main() -> None:
    _configure_root_logging("INFO")
    handler = LichessApiHandlerStandalone(log_level="INFO")

    def _sig_stop(*_args):
        handler.stop()

    signal.signal(signal.SIGINT, _sig_stop)
    signal.signal(signal.SIGTERM, _sig_stop)

    handler.start()


if __name__ == "__main__":
    main()
