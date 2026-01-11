"""AppDaemon integration for a physical chessboard talking to Lichess via MQTT.

This module wires together:
- MQTT topic subscriptions/publishing (via :class:`lichess_components.MqttGateway`)
- Lichess API / event / board streaming workers (via the worker classes in ``lichess_components``)
- Token handling (tokens are received via MQTT encrypted payloads)

No business logic should live on the MQTT callbacks beyond:
- decrypt / validate inputs
- update state
- start/stop the appropriate worker threads and sessions
"""

import appdaemon.plugins.hass.hassapi as hass
import json

import lichess_helpers as lh
from lichess_components import (
    LichessClientPool,
    LichessWorkerApi,
    LichessWorkerBoard,
    LichessWorkerEvent,
    MqttGateway,
)

# ---------------------------------------------------------------------------
# Constants / defaults
# ---------------------------------------------------------------------------

IDLE_GAME_ID = "idle"
IDLE_LICHESS_TOKEN = "idle"
IDLE_MQTT_STATE = "idle"

STATUS_OFFLINE = "offline"
STATUS_ONLINE = "online"

# MQTT topics
MQTT_API_CALL_TOPIC = "chessboard/api_call"
MQTT_GAME_ID_TOPIC = "chessboard/game_id"
MQTT_TOKEN_MAIN_TOPIC = "chessboard/token_main"
MQTT_TOKEN_OPP_TOPIC = "chessboard/token_opponent"
MQTT_STATUS_TOPIC = "chessboard/status"
MQTT_RESPONSE_TOPIC = "chessboard/response"

# Secrets location on the HA host
SECRET_PATH = "/config/secrets.yaml"

# Client/session identifiers for :class:`lichess_components.LichessClientPool`
CLIENT_NAME_API_MAIN = "client_api_main"
CLIENT_NAME_API_OPPONENT = "client_api_opponent"
CLIENT_NAME_BOARD_MAIN = "client_board_main"
CLIENT_NAME_BOARD_OPPONENT = "client_board_opponent"


class LichessApiHandler(hass.Hass):
    """AppDaemon app coordinating MQTT input with Lichess worker threads.

    Lifecycle:
      - ``initialize`` creates the MQTT gateway, loads secrets, and instantiates workers.
      - MQTT callbacks update tokens / game id and start/stop workers accordingly.
      - ``terminate`` stops workers and closes sessions on shutdown/reload.

    Notes:
      - Tokens are expected to arrive encrypted; decryption happens via
        :func:`lichess_helpers.decrypt_message` using ``chessboard_secret_key``.
      - The "idle" token/game id values are used to represent a disconnected state.
    """

    # Store the encryption secret key (loaded from secrets.yaml).
    _current_secret_key = None

    def initialize(self) -> None:
        """AppDaemon entry point; called once when the app starts."""
        self.log("AppDaemon LichessApiHandler script initialized!")

        # ---- Load secrets (local file) ----
        self.secrets = lh.load_secrets(SECRET_PATH)

        # ---- Chess secret key ----
        self._current_secret_key = self.secrets.get("chessboard_secret_key", None)
        if not self._current_secret_key:
            raise RuntimeError(
                "Missing 'chessboard_secret_key' in {}.".format(SECRET_PATH)
            )

        # ---- MQTT config (from secrets.yaml) ----
        self._mqtt = MqttGateway(self.secrets, self.log)

        # ---- MQTT topic routing ----
        # Each handler is a small adapter that forwards work to the correct worker.
        self._mqtt.register_handler(MQTT_API_CALL_TOPIC, self._on_mqtt_api_call)
        self._mqtt.register_handler(MQTT_GAME_ID_TOPIC, self._on_mqtt_game_id)
        self._mqtt.register_handler(MQTT_TOKEN_MAIN_TOPIC, self._on_mqtt_token_main)
        self._mqtt.register_handler(MQTT_TOKEN_OPP_TOPIC, self._on_mqtt_token_opponent)
        self._mqtt.register_handler(MQTT_STATUS_TOPIC, self._on_mqtt_chessboard_status)

        # Current runtime state (protected by worker-internal locks where needed).
        self._current_game_id = IDLE_GAME_ID
        self._token_main = IDLE_LICHESS_TOKEN
        self._token_opponent = IDLE_LICHESS_TOKEN

        # Keep references to sessions so we can close them on token change.
        # Lichess (berserk) clients/sessions are managed by this pool.
        self._clients = LichessClientPool(log=self.log)

        # Worker instances:
        # - Board workers stream board/game state per player token
        # - Event worker streams account events for the main token
        # - API worker performs on-demand API calls and pushes responses
        self._worker_board_main = LichessWorkerBoard(
            main_worker=True,
            idle_token=IDLE_LICHESS_TOKEN,
            idle_game_id=IDLE_GAME_ID,
            log=self.log,
        )
        self._worker_board_opponent = LichessWorkerBoard(
            main_worker=False,
            idle_token=IDLE_LICHESS_TOKEN,
            idle_game_id=IDLE_GAME_ID,
            log=self.log,
        )
        self._worker_event = LichessWorkerEvent(idle_token=IDLE_LICHESS_TOKEN, log=self.log)
        self._worker_api = LichessWorkerApi(
            idle_token=IDLE_LICHESS_TOKEN,
            idle_game_id=IDLE_GAME_ID,
            log=self.log,
        )

        # Register a shared publisher for all workers so they can send results back over MQTT.
        self._worker_board_main.register_publish_function(self.publish_response)
        self._worker_board_opponent.register_publish_function(self.publish_response)
        self._worker_event.register_publish_function(self.publish_response)
        self._worker_api.register_publish_function(self.publish_response)

        # ---- Initialize MQTT client ----
        # Subscribing to all relevant topics; handlers above will dispatch work.
        self._mqtt.connect_and_loop(
            (
                (MQTT_API_CALL_TOPIC, 0),
                (MQTT_GAME_ID_TOPIC, 0),
                (MQTT_TOKEN_MAIN_TOPIC, 0),
                (MQTT_TOKEN_OPP_TOPIC, 0),
                (MQTT_STATUS_TOPIC, 0),
            )
        )

        self.log("Initialization complete for LichessApiHandler")

    def terminate(self) -> None:
        """Called by AppDaemon on shutdown/reload; best-effort cleanup."""
        # Stop workers first (threads) to avoid them using closing sessions.
        try:
            self._worker_api.stop_worker()
            self._worker_event.stop_worker()
            self._worker_board_main.stop_worker()
            self._worker_board_opponent.stop_worker()
        except Exception:
            # Keep shutdown resilient; AppDaemon may call terminate during partial init.
            pass

        # Close any lingering HTTP sessions in the client pool.
        try:
            self._clients.close_all()
        except Exception:
            pass

        # Stop the MQTT loop to prevent callbacks after shutdown.
        try:
            if hasattr(self, "_mqtt") and self._mqtt:
                self._mqtt.stop()
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # MQTT handlers
    # -------------------------------------------------------------------------

    def publish_response(self, payload: str) -> None:
        """Publish a worker response on the shared response topic."""
        self._mqtt.publish(MQTT_RESPONSE_TOPIC, payload)

    def clear_topics(self) -> None:
        """Reset all MQTT topics to an 'idle' state (used when the board goes offline)."""
        self._mqtt.publish(MQTT_API_CALL_TOPIC, IDLE_MQTT_STATE)
        self._mqtt.publish(MQTT_GAME_ID_TOPIC, IDLE_MQTT_STATE)
        self._mqtt.publish(MQTT_TOKEN_MAIN_TOPIC, IDLE_MQTT_STATE)
        self._mqtt.publish(MQTT_TOKEN_OPP_TOPIC, IDLE_MQTT_STATE)
        self._mqtt.publish(MQTT_STATUS_TOPIC, IDLE_MQTT_STATE)
        self._mqtt.publish(MQTT_RESPONSE_TOPIC, IDLE_MQTT_STATE)

    def _on_mqtt_api_call(self, payload: str) -> None:
        """Handle an API call request coming from MQTT.

        The payload format is interpreted by :class:`lichess_components.LichessWorkerApi`.
        """
        self._worker_api.perform_api_call(payload)

    def _on_mqtt_token_main(self, payload: str) -> None:
        """Handle (encrypted) main account token updates.

        When the token changes we:
          - close all sessions/clients tied to that token
          - stop workers that depend on it
          - update the token and recreate clients/workers if the token is not idle
        """
        if payload and payload != IDLE_LICHESS_TOKEN:
            data = json.loads(payload)                # convert JSON string -> Python dict
            payload = data["token"]
        # Decrypt token from MQTT payload (board sends encrypted token).
        new_token = lh.decrypt_message(self._current_secret_key, payload)

        # Short-circuit if token did not change.
        if new_token == self._token_main:
            return

        self.log(
            f"Token changed (main) in LichessApiHandler: {self._token_main} -> {new_token}"
        )

        # Close sessions/clients dependent on the main token.
        self._clients.close(CLIENT_NAME_API_MAIN)
        self._clients.close(CLIENT_NAME_BOARD_MAIN)

        # Stop running workers that were using the previous token.
        self._worker_event.stop_worker()
        self._worker_board_main.stop_worker()

        # Update stored token.
        self._token_main = new_token

        # Start new sessions/clients and restart workers if we have a real token.
        if new_token != IDLE_LICHESS_TOKEN:
            self._clients.set_token(CLIENT_NAME_API_MAIN, new_token, idle_token=IDLE_LICHESS_TOKEN)
            self._clients.set_token(CLIENT_NAME_BOARD_MAIN, new_token, idle_token=IDLE_LICHESS_TOKEN)

            # Update worker clients so subsequent calls/streams use the new sessions.            
            self._worker_api.update_lichess_client_main(self._clients.get(CLIENT_NAME_API_MAIN), new_token)
            self._worker_event.update_lichess_token(new_token)

            # Event stream should be running for main token.
            self._worker_event.run_worker()

            self._worker_board_main.update_lichess_client(self._clients.get(CLIENT_NAME_BOARD_MAIN), new_token)

    def _on_mqtt_token_opponent(self, payload: str) -> None:
        """Handle (encrypted) opponent token updates.

        Similar to the main token handler, but only affects opponent API/board clients.
        """
        if payload and payload != IDLE_LICHESS_TOKEN:
            data = json.loads(payload)                # convert JSON string -> Python dict
            payload = data["token"]
        # Decrypt token from MQTT payload.
        new_token = lh.decrypt_message(self._current_secret_key, payload)

        # Short-circuit if token did not change.
        if new_token == self._token_opponent:
            return

        self.log(
            f"Token changed (opponent) in LichessApiHandler: {self._token_opponent} -> {new_token}"
        )

        # Stop/close resources tied to the previous opponent token.
        self._clients.close(CLIENT_NAME_API_OPPONENT)
        self._clients.close(CLIENT_NAME_BOARD_OPPONENT)
        self._worker_board_opponent.stop_worker()

        # Update stored token.
        self._token_opponent = new_token

        # Start new sessions/clients if we have a real token.
        if new_token != IDLE_LICHESS_TOKEN:
            self._clients.set_token(CLIENT_NAME_API_OPPONENT, new_token, idle_token=IDLE_LICHESS_TOKEN)
            self._clients.set_token(CLIENT_NAME_BOARD_OPPONENT, new_token, idle_token=IDLE_LICHESS_TOKEN)

            self._worker_api.update_lichess_client_opponent(self._clients.get(CLIENT_NAME_API_OPPONENT), new_token)
            self._worker_board_opponent.update_lichess_client(self._clients.get(CLIENT_NAME_BOARD_OPPONENT), new_token)

    def _on_mqtt_game_id(self, payload: str) -> None:
        """Handle game-id updates.

        A game-id change triggers (re)starting the board stream workers so they subscribe to
        the new game stream.
        """
        if payload and payload != IDLE_GAME_ID:
            data = json.loads(payload)                # convert JSON string -> Python dict
            payload = data["game_id"]

        # Short-circuit if game id did not change.
        if payload == self._current_game_id:
            return

        self.log(f"Game ID changed in LichessApiHandler: {self._current_game_id} -> {payload}")

        # Update game id and inform all workers that depend on it.
        self._current_game_id = payload

        # Update API worker and board workers with the new game id.
        self._worker_api.update_game_id(payload)
        self._worker_board_main.update_game_id(payload)
        self._worker_board_opponent.update_game_id(payload)

        # Start board streams for the new game.
        self._worker_board_main.run_worker()
        self._worker_board_opponent.run_worker()

    def _on_mqtt_chessboard_status(self, payload: str) -> None:
        """Handle online/offline status of the physical chessboard."""
        # Chessboard is online: (re)initialize sessions and start background streams.
        if payload == STATUS_ONLINE:
            self.log("Chessboard is online")
            self._init_sessions_all()
            # if the board is online, we run the api worker (even without berserk clients set)
            self._worker_api.run_worker()
            self._worker_event.run_worker()

        # Chessboard is offline: stop all worker threads and clear MQTT topics.
        elif payload == STATUS_OFFLINE:
            self.log("Chessboard is offline in LichessApiHandler")
            if self.is_any_thread_alive():
                self.log("At least one thread is alive, stopping ...")
                self._clients.close_all()
                self._worker_api.stop_worker()
                self._worker_event.stop_worker()
                self._worker_board_main.stop_worker()
                self._worker_board_opponent.stop_worker()
            else:
                self.log("No threads running")

            self.clear_topics()

    # -------------------------------------------------------------------------
    # Session initialization helpers
    # -------------------------------------------------------------------------

    def _init_sessions_all(self) -> None:
        """Ensure all Lichess clients exist for the currently stored tokens."""
        self._clients.set_token(CLIENT_NAME_API_MAIN, self._token_main, idle_token=IDLE_LICHESS_TOKEN)
        self._clients.set_token(CLIENT_NAME_API_OPPONENT, self._token_opponent, idle_token=IDLE_LICHESS_TOKEN)
        self._clients.set_token(CLIENT_NAME_BOARD_MAIN, self._token_main, idle_token=IDLE_LICHESS_TOKEN)
        self._clients.set_token(CLIENT_NAME_BOARD_OPPONENT, self._token_opponent, idle_token=IDLE_LICHESS_TOKEN)

    # -------------------------------------------------------------------------
    # Helper / introspection
    # -------------------------------------------------------------------------

    def is_any_thread_alive(self) -> bool:
        """Return True if any worker thread is currently alive."""
        return bool(
            self._worker_api.is_any_thread_alive()
            or self._worker_board_main.is_any_thread_alive()
            or self._worker_board_opponent.is_any_thread_alive()
            or self._worker_event.is_any_thread_alive()
        )
