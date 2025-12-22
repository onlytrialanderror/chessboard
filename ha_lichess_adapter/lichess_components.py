"""Lichess + MQTT components.

This module contains small building blocks used by an AppDaemon integration:
- A pool for berserk clients/sessions
- An MQTT gateway (paho) with optional TLS
- Worker threads to stream board state/events and to serialize API calls

Only formatting, comments, and docstrings have been added; runtime behaviour is unchanged.
"""

import berserk
import threading
import queue
from typing import Callable, Dict, Optional, Tuple
import json
import paho.mqtt.client as paho
import ssl
import time
import lichess_helpers as lh

CA_CERT_PATH = "/config/hivemq.pem"


def _default_log(msg: str, level: str = "INFO") -> None:
    """
    Fallback logger used when AppDaemon (or another framework) does not inject a logger.

    Args:
        msg: Message to log.
        level: Log level label.
    """
    # Minimal fallback logger (AppDaemon will inject its own)
    print(f"[{level}] {msg}")


class LichessClientPool:
    """
    Manages berserk TokenSessions and Clients, keyed by a string.

    This eliminates the duplicated _init_session/_close_session methods.
    """

    def __init__(self, log: Callable[[str, str], None] = _default_log) -> None:
        """
        Initialize the client pool.

        Args:
            log: Logger function accepting (message, level).
        """
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
        """
        Return the cached berserk Client for `key` (or None if not configured).
        """
        with self._lock:
            return self._clients.get(key)

    def close(self, key: str) -> None:
        """
        Close and remove the session/client registered under `key`.
        """
        with self._lock:
            self._close_nolock(key)

    def close_all(self) -> None:
        """
        Close all sessions/clients managed by this pool.
        """
        with self._lock:
            keys = list(self._sessions.keys())
        for k in keys:
            self.close(k)

    def _close_nolock(self, key: str) -> None:
        """
        Internal helper: close and remove a session/client without acquiring the lock.

        Callers must hold `self._lock`.
        """
        sess = self._sessions.pop(key, None)
        self._clients.pop(key, None)
        if sess:
            try:
                sess.close()
            except Exception:
                self._log(f"Failed to close the client session {key}")
                pass


class MqttGateway:
    """
    Thin wrapper around paho MQTT client.
    - handles TLS setup
    - subscribes to topics
    - routes messages to registered topic handlers
    """

    """
        Initialize an MQTT gateway using values from secrets.

        Args:
            secrets: Nested dict containing an mqtt section.
            log: Logger function accepting (message, level).
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
        """
        Register a handler callback for an MQTT topic.

        The handler is called with the decoded payload (string) whenever a message on `topic` arrives.
        """
        self._handlers[topic] = handler

    def connect_and_loop(self, subscriptions: Tuple[Tuple[str, int], ...]) -> None:
        """
        Connect to the broker and start the network loop in a background thread.

        Args:
            subscriptions: Tuple of (topic, qos) pairs to subscribe to after connecting.
        """
        self._subscriptions = subscriptions
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client.connect(self._host, self._port, keepalive=60)
        self._client.loop_start()

    def publish(self, topic: str, payload: str, qos: int = 0, retain: bool = False) -> None:
        """
        Publish an MQTT message.
        """
        self._client.publish(topic, payload=payload, qos=qos, retain=retain)

    def stop(self) -> None:
        """
        Stop the MQTT loop and disconnect (best-effort).
        """
        try:
            self._client.loop_stop()
        finally:
            try:
                self._client.disconnect()
            except Exception:
                pass

    # ---- callbacks ----

    def _on_connect(self, client, userdata, flags, rc):
        """
        paho callback: invoked on successful/failed connection.
        """
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
        """
        paho callback: invoked when the client disconnects.
        """
        self._log(f"MQTT disconnected rc={rc}", LEVEL = "INFO")

    def _on_message(self, client, userdata, msg):
        """
        paho callback: invoked on every incoming message; routes to registered handlers.
        """
        payload = lh.payload_to_str(msg.payload)
        if payload is None:
            return
        handler = self._handlers.get(msg.topic)
        if handler:
            handler(payload)


class LichessWorkerBoard:


    def __init__(self, main_worker: bool = False, idle_token: str = "idle", idle_game_id: str = "idle", log: Callable[[str, str], None] = lh.default_log):

        """
        Create a worker that streams board state for a single game.

        Args:
            main_worker: If True, publishes reduced board state updates.
            idle_token: Sentinel token indicating an inactive client.
            idle_game_id: Sentinel game id indicating no active game.
            log: Logger function accepting (message, level).
        """
        self._idle_token = idle_token
        self._idle_game_id = idle_game_id
        self._lichess_stream_board_init_value = lh.concat_values(self._idle_token, self._idle_game_id)
        self._current_game_id = self._idle_game_id
        self._current_token = self._idle_token

        # single-threaded API worker to serialize Lichess calls
        self._board_worker: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._stop_event_board = threading.Event()

        self._client_lichess: Optional[berserk.Client] = None

        self._main_worker = main_worker
        self._mqtt_publish_function  = None

        self.worker_type = "opponent"
        if self._main_worker == True:
            self.worker_type = "main"

        self._log = log

        self._log(f"Board ({self.worker_type}) worker is initialized", LEVEL = "INFO")

    def update_game_id(self, game_id:str) -> None:
        """
        Update the active game id.

        If the game id changes, the corresponding worker stream is stopped so it can be restarted for the new game.
        """
        # check if game id really changed
        if game_id == self._current_game_id:
            return

        self.stop_worker()

        self._log(f"Board ({self.worker_type}) worker: game_id is updateded from {self._current_game_id} to {game_id}", LEVEL = "INFO")

        # reset game_id to assist stopping of workers
        self._current_game_id = game_id

    def update_lichess_client(self, lichess_client: berserk.Client, new_token: str) -> None:
        """
        Update the Lichess client instance and (optionally) restart the worker if the token changed.
        """
        self._client_lichess = lichess_client
        # check if token really changed
        if new_token == self._current_token:
            return

        self.stop_worker()

        # update token
        self._current_token = new_token

    def register_publish_function(self, publish_function: Callable[[str], None]):
        """
        Register a callback used to publish reduced JSON payloads (e.g., to MQTT).
        """
        self._mqtt_publish_function  = publish_function

    def is_any_thread_alive(self):
        """
        Return True if the worker thread is currently alive.
        """
        return  bool(self._board_worker and self._board_worker.is_alive())


    #####################################################################
    ############## ---------- RUN WOKERS -------------  #################
    #####################################################################


    def run_worker(self):

        """
        Start the worker thread if prerequisites are met and no worker is running.
        """
        token_plus_game_id = lh.concat_values(self._current_token, self._current_game_id)

        # check if api thread is already running or token still not set, we skip starting
        if (self._lichess_stream_board_init_value == token_plus_game_id and self._board_worker and self._board_worker.is_alive()) or self._current_token == self._idle_token or self._current_game_id == self._idle_game_id:
            return

        self._stop_event_board.clear()

        self._log(f"Board {token_plus_game_id}: ({self.worker_type}) worker starting")

        # start board worker thread ({self.worker_type})
        self._board_worker = threading.Thread(
            target=self._handle_board_stream,
            args=(token_plus_game_id, ),
            daemon=True
        )
        self._board_worker.start()


    #####################################################################
    ############## ---------- STOP WOKERS ------------  #################
    #####################################################################


    def stop_worker(self):

        """
        Request the worker thread to stop and wait briefly for shutdown (best-effort).
        """
        # stop main board worker
        if  self._board_worker is not None and self._board_worker.is_alive():

            self._stop_event_board.set()

            self._log(f"Board ({self.worker_type}) worker stopping")
            # wait main board thread is finished
            self._board_worker.join(timeout=1)

            # check if main thread is stopped
            if self._board_worker and self._board_worker.is_alive():
                self._log(f"Stopping board worker ({self.worker_type}) failed")
                # we call dummy function (here abort) to provoke an event in the stream to call the loop-abort checks
                lh.write_into_chat(self._client_lichess, self._current_game_id, {"text": f"Event on {self.worker_type} board-stream: {self._current_game_id}"})
            else:
                self._log(f"Board worker ({self.worker_type}) stopped")

        # check if init variable is reseted
        if self._lichess_stream_board_init_value != lh.concat_values(self._idle_token, self._idle_game_id):
            self._log(f"Thread init-variable {self._lichess_stream_board_init_value} for stream board {self.worker_type} worker is still set", level="WARNING")


    #####################################################################
    ############## ---------- HANDLER FUNCTIONS ---------  ##############
    #####################################################################

    def _handle_board_stream(self, init_value):
        """
        Stream board/game state updates from Lichess and optionally publish reduced payloads.

        This method runs inside the board worker thread.
        """
        # init_value is concat(token main + game_id)
        splitted_init_value = lh.split_concated_values(init_value)
        game_id = "idle"
        if len(splitted_init_value) > 1:
            game_id = splitted_init_value[1]

        # init_value is token main
        with  self._lock:
            self._lichess_stream_board_init_value = init_value

        self._log(f"Starting the board stream ({self.worker_type}): {init_value}")

        for line in self._client_lichess.board.stream_game_state(game_id):
            if line: # valid dic

                if self._main_worker == True and self._mqtt_publish_function is not None:

                    reduced_data = json.dumps(lh.reduce_response_board(game_id, line))
                    # let ha know about the move
                    self._mqtt_publish_function(reduced_data)
                    self._log(f"Board ({self.worker_type}): {reduced_data}")

                # check if we have to abort the game
                if lh.check_game_over(line):
                    self._log(f"Terminating the board stream ({self.worker_type}): {init_value}")
                    # close the stream
                    break

            if self._stop_event_board.is_set():
                self._log(f"Board stream stop event set, terminating the board stream ({self.worker_type}): {init_value}")
                break
            with self._lock:
                if (lh.concat_values(self._current_token, self._current_game_id)  != init_value):
                    self._log(f"Board stream stopping: game_id or token changed, terminating the board stream ({self.worker_type}): {init_value}")
                    break
        # we are ready to go
        self._log(f"Terminated board stream ({self.worker_type}): {init_value}")
        with  self._lock:
            self._lichess_stream_board_init_value = lh.concat_values(self._idle_token, self._idle_game_id)


class LichessWorkerEvent:


    def __init__(self, idle_token: str = "idle", log: Callable[[str, str], None] = lh.default_log):

        """
        Create a worker that streams incoming Lichess events.

        Args:
            idle_token: Sentinel token indicating an inactive client.
            log: Logger function accepting (message, level).
        """
        self._idle_token = idle_token
        self._lichess_stream_event_init_value = self._idle_token
        self._current_token = self._idle_token

        # single-threaded API worker to serialize Lichess calls
        self._stream_worker: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._stop_event_stream = threading.Event()

        self._client_lichess: Optional[berserk.Client] = None

        self._mqtt_publish_function  = None
        self._log = log

        self._log(f"Event worker is initialized", LEVEL = "INFO")


    def update_lichess_client(self, lichess_client: berserk.Client, new_token: str) -> None:
        """
        Update the Lichess client instance and (optionally) restart the worker if the token changed.
        """
        self._client_lichess = lichess_client
        # check if token really changed
        if new_token == self._current_token:
            return

        self.stop_worker()

        # update token
        self._current_token = new_token

    def register_publish_function(self, publish_function: Callable[[str], None]):
        """
        Register a callback used to publish reduced JSON payloads (e.g., to MQTT).
        """
        self._mqtt_publish_function  = publish_function

    def is_any_thread_alive(self):
        """
        Return True if the worker thread is currently alive.
        """
        return  bool(self._stream_worker and self._stream_worker.is_alive())


    #####################################################################
    ############## ---------- RUN WOKERS -------------  #################
    #####################################################################


    def run_worker(self):

        """
        Start the worker thread if prerequisites are met and no worker is running.
        """
        # check if api thread is already running or token still not set, we skip starting
        if (self._lichess_stream_event_init_value == self._current_token and self._lichess_stream_event_init_value != self._idle_token and self._stream_worker and self._stream_worker.is_alive()) or self._current_token == self._idle_token:
            return

        self._stop_event_stream.clear()

        self._log(f"Stream worker started")
        # start stream worker thread
        self._stream_worker = threading.Thread(
            target=self._handle_incoming_events,
            args=(self._current_token,),
            daemon=True
        )
        self._stream_worker.start()


    #####################################################################
    ############## ---------- STOP WOKERS ------------  #################
    #####################################################################


    def stop_worker(self):

        """
        Request the worker thread to stop and wait briefly for shutdown (best-effort).
        """
        if self._stream_worker and self._stream_worker.is_alive():
            self._log(f"Stream event worker is running, trying to stop")
            # thread shutdown
            self._stop_event_stream.set()
            # we reset the token to idle to stop the stream
            self._current_token =  self._idle_token

            if self._stream_worker and self._stream_worker.is_alive():
                self._stream_worker.join(timeout=2)

            # check if stream thread is stopped
            if self._stream_worker and self._stream_worker.is_alive():
                self._log(f"Stopping Stream event worker failed")
            else:
                self._log(f"Stream worker stopped")

        # check if init variable is reseted
        if self._lichess_stream_event_init_value != self._idle_token:
            self._log(f"Thread init-variable for stream event worker is still set", level="WARNING")


    #####################################################################
    ############## ---------- HANDLER FUNCTIONS ---------  ##############
    #####################################################################

    def _handle_incoming_events(self, token_init):

        """
        Stream incoming board events from Lichess and optionally publish reduced payloads.

        This method runs inside the event worker thread.
        """
        self._lichess_stream_event_init_value = token_init

        self._log(f"Starting the stream (event): {token_init}")

        # open the stream for whole chess game
        for event in self._client_lichess.board.stream_incoming_events():
            if event:
                reduced_data = json.dumps(lh.reduce_response_event(event))
                self._log(f"Event: {reduced_data}")
                if self._mqtt_publish_function is not None:
                    self._mqtt_publish_function(reduced_data)
            if self._stop_event_stream.is_set():
                self._log(f"Stream stop event set, terminating the stream (event): {self._lichess_stream_event_init_value}")
                break
            with  self._lock:
                if self._lichess_stream_event_init_value != self._current_token:
                    self._log(f"Terminating the event stream (token changed): {self._lichess_stream_event_init_value}->{self._current_token}")
                    # close the stream
                    break

        self._log(f"Terminated the stream (event): {self._lichess_stream_event_init_value}")
        self._lichess_stream_event_init_value = self._idle_token




class LichessWorkerApi:

    def __init__(self, idle_token: str = "idle", idle_game_id: str = "idle", log: Callable[[str, str], None] = lh.default_log):

        """
        Create a worker that serializes API calls to Lichess via a single thread.

        Args:
            idle_token: Sentinel token indicating an inactive client.
            idle_game_id: Sentinel game id indicating no active game.
            log: Logger function accepting (message, level).
        """
        self._idle_token = idle_token
        self._idle_game_id = idle_game_id
        self._lichess_api_init_value = self._idle_token
        self._current_game_id = self._idle_game_id
        self._current_token_main = self._idle_token
        self._current_token_opponent = self._idle_token

        # single-threaded API worker to serialize Lichess calls
        self._api_worker: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._stop_event_api = threading.Event()
        self._api_q = queue.Queue()

        self._client_lichess_main: Optional[berserk.Client] = None
        self._client_lichess_opponent: Optional[berserk.Client] = None

        self._mqtt_publish_function  = None
        self._log = log

        self._log(f"Api worker is initialized", LEVEL = "INFO")

    def update_game_id(self, game_id:str) -> None:
        """
        Update the active game id.

        If the game id changes, the corresponding worker stream is stopped so it can be restarted for the new game.
        """
        # check if game id really changed
        if game_id == self._current_game_id:
            return

        # reset game_id to assist stopping of workers
        self._current_game_id = game_id

    def update_lichess_client_main(self, lichess_client: berserk.Client, new_token: str) -> None:
        """
        Update the main (authenticated) Lichess client and token used for API calls.
        """
        self._client_lichess_main = lichess_client
        # check if token really changed
        if new_token == self._current_token_main:
            return

        # update token
        self._current_token_main = new_token

    def update_lichess_client_opponent(self, lichess_client: berserk.Client, new_token: str) -> None:
        """
        Update the opponent Lichess token/client used for opponent actions (best-effort).

        If the token is set to the idle token, the stored opponent client is cleared.
        """
        self._client_lichess_opponent = lichess_client
        # check if token really changed
        if new_token == self._current_token_opponent:
            return

        # update token
        self._current_token_opponent = new_token

        # we have to reset client if idle token
        if new_token == self._idle_token and self._client_lichess_opponent is not None:
            self._client_lichess_opponent = None

    def register_publish_function(self, publish_function: Callable[[str], None]):
        """
        Register a callback used to publish reduced JSON payloads (e.g., to MQTT).
        """
        self._mqtt_publish_function  = publish_function

    def perform_api_call(self, api_json: str):
        """
        Enqueue an API call (JSON string) for processing by the API worker thread.
        """
        if api_json and api_json != self._idle_game_id:
            self._api_q.put(api_json)

    def is_any_thread_alive(self):
        """
        Return True if the worker thread is currently alive.
        """
        return  bool(self._api_worker and self._api_worker.is_alive())


    #####################################################################
    ############## ---------- RUN WOKERS -------------  #################
    #####################################################################


    def run_worker(self):

        """
        Start the worker thread if prerequisites are met and no worker is running.
        """
        # check if api thread is already running, we skip starting
        if self._api_worker and self._api_worker.is_alive():
            return

        self._log(f"API worker starting")

        self._stop_event_api.clear()

        # start api worker thread
        self._api_worker = threading.Thread(
            target=self._handle_api_calls,
            daemon=True
        )
        self._api_worker.start()


    #####################################################################
    ############## ---------- STOP WOKERS ------------  #################
    #####################################################################


    def stop_worker(self):

        """
        Request the worker thread to stop and wait briefly for shutdown (best-effort).
        """
        if self._api_worker and self._api_worker.is_alive():
            self._log(f"API main worker is running, trying to stop")
            # sentinel shutdown
            self._api_q.put("terminate_api_worker")
            # thread shutdown
            self._stop_event_api.set()

            if self._api_worker and self._api_worker.is_alive():
                self._api_worker.join(timeout=1)

            # check if api thread is stopped
            if self._api_worker and self._api_worker.is_alive():
                self._log(f"Stopping API main worker failed", level="WARNING")
            else:
                self._log(f"API main worker stopped")

        # check if init variable is reseted
        if self._lichess_api_init_value != self._idle_token:
            self._log(f"Thread init-variable for API main worker is still set", level="WARNING")


    #####################################################################
    ############## ---------- HANDLER FUNCTIONS ---------  ##############
    #####################################################################


    def _handle_api_calls(self):
        """
        Main API worker loop: consume queued API requests and execute them sequentially.
        """
        # we set to any value, that is not equal to idle
        self._lichess_api_init_value = "loop_is_running"
        # init_value is token main
        self._log(f"Starting main-loop")
        while True:
            # get next item from the queue
            try:
                item = self._api_q.get(timeout=1)
            except queue.Empty:
                continue

            # check for stop event
            if self._stop_event_api.is_set():
                self._log(f"Terminating main-loop due to stop event")
                break

            # check for sentinel
            if item == "terminate_api_worker":
                self._log(f"Terminating main-loop due to sentinel")
                break

            # handle the API call
            try:
                if item:
                    self._perform_api_call(item)
                else:
                    self._log(f"Empty API call received", level="WARNING")
            except Exception as e:
                self._log(f"API call error: {e}", level="ERROR")
            finally:
                self._api_q.task_done()

            # if a game isn't running, we sleep
            if self._current_game_id == self._idle_game_id:
                time.sleep(1)

        self._log(f"Terminated main-loop")
        self._lichess_api_init_value = self._idle_token


    #####################################################################
    ############## ---------- HELPER FUNCTIONS ----------  ##############
    #####################################################################

    def _perform_api_call(self, new):

        """
        Decode and dispatch a single API request.

        The request is expected to be a JSON string with a `type` field that determines the action.
        """
        try:
            json_data = json.loads(new)
        except json.JSONDecodeError as e:
            self._log(f"Invalid JSON in api_call: {e} payload={new!r}", level="WARNING")
            return

        call_type = json_data.get("type", None)

        self._log(f"API-call: " + json.dumps(json_data))

        if json_data and call_type:

            with self._lock:

                valid_token = self._current_token_main != self._idle_token and self._client_lichess_main is not None
                valid_token_opponent = (self._current_token_opponent == self._idle_token and self._client_lichess_opponent is None) or (self._current_token_opponent != self._idle_token and self._client_lichess_opponent is not None)
                valid_game_id = self._current_game_id != self._idle_game_id

                if valid_token == True:

                    if call_type == "getAccountInfoMain":
                        json_response = lh.getAccountInfoMain(self._client_lichess_main, self_log=self._log)
                        self._mqtt_publish_function(json_response)
                        return

                    if call_type == "abortRunningGames":
                        lh.abortRunningGames(self._client_lichess_main, self_log=self._log)
                        return

                    if call_type == "withdrawTornament":
                        lh.withdrawTornament(json_data, self._client_lichess_main, self_log=self._log)
                        return

                    if call_type == "joinTournamentByName":
                        json_response = lh.joinTournamentByName(json_data, self._client_lichess_main, self_log=self._log)
                        self._mqtt_publish_function(json_response)
                        return

                    if call_type == "joinTournamentById":
                        json_response = lh.joinTournamentById(json_data, self._client_lichess_main, self_log=self._log)
                        self._mqtt_publish_function(json_response)
                        return

                if valid_token == True and valid_token_opponent == True:

                    if call_type == "createGame" :
                        json_response = lh.createGame(json_data, self._client_lichess_main, self._current_token_opponent, self_log=self._log)
                        self._mqtt_publish_function(json_response)
                        return

                if valid_token == True and valid_game_id == True:

                    if call_type == "abort":
                        lh.abort(self._client_lichess_main, self._current_game_id, self_log=self._log)
                        return

                    if call_type == "resign":
                        lh.resign(self._client_lichess_main, self._current_game_id, self_log=self._log)
                        return

                    if call_type == "claim-victory":
                        lh.claimVictory(self._client_lichess_main, self._current_game_id, self_log=self._log)
                        return

                    if call_type == "makeMove":
                        lh.makeMove(json_data, self._client_lichess_main, self._current_game_id, self_log=self._log)
                        return

                    if call_type == "draw":
                        lh.draw(json_data, self._client_lichess_main, self._current_game_id, self_log=self._log)
                        return

                    if call_type == "takeback":
                        lh.takeback(json_data, self._client_lichess_main, self._current_game_id, self_log=self._log)
                        return

                    if call_type == "writeChatMessage":
                        lh.writeChatMessage(json_data, self._client_lichess_main, self._current_game_id, self_log=self._log)
                        return

                if valid_token_opponent == True and valid_game_id == True:

                    if call_type == "makeMoveOpponent":
                        lh.makeMoveOpponent(json_data, self._current_token_opponent, self._current_game_id, self_log=self._log)
                        return

                    if call_type == "resignOpponent":
                        lh.resignOpponent(json_data, self._current_token_opponent, self._current_game_id, self_log=self._log)
                        return

                    if call_type == "drawOpponent":
                        lh.drawOpponent(json_data, self._current_token_opponent, self._current_game_id, self_log=self._log)
                        return

                # we should return till that point
                self._log(f"API-Call " + json.dumps(json_data) + " could not be performed!", level="WARNING")
