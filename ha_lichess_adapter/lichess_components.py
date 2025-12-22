import berserk
import threading
from typing import Callable, Dict, Optional, Tuple
import json
import paho.mqtt.client as paho
import ssl
import lichess_helpers as lh

CA_CERT_PATH = "/config/hivemq.pem"


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

class LichessWorkerBoard:
     

    def __init__(self, main_worker: bool = False, idle_token: str = "idle", idle_game_id: str = "idle", log: Callable[[str, str], None] = lh.default_log):

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
        self._game_id(game_id)
   
    def update_lichess_client(self, lichess_client: berserk.Client, token: str = "idle") -> None:
        self._client_lichess = lichess_client
        self._update_token_main(token)
       
    def register_publish_function(self, publish_function: Callable[[str], None]):
        self._mqtt_publish_function  = publish_function



    #####################################################################
    ############## ---------- MQTT handlers ----------  #################
    #####################################################################

    # ------------------------------------------------------------------
    # MQTT publisher helpers
    # ------------------------------------------------------------------

    def _publish_response(self, payload: str) -> None:
        self._mqtt_publish_function(payload)


    def _update_token_main(self, new_token: str) -> None:
        # check if token really changed
        if new_token == self._current_token:
            return

        self._stop_board_worker()

        # update token
        self._current_token = new_token    


    def _game_id(self, game_id: str) -> None:

        # check if game id really changed
        if game_id == self._current_game_id:
            return
        
        self._log(f"Board ({self.worker_type}) worker: game_id is updateded from {self._current_game_id} to {game_id}", LEVEL = "INFO")
        
        # reset game_id to assist stopping of workers
        self._current_game_id = self._idle_game_id

        # overwrites current game id to stop existing stream
        self._stop_board_worker()
        
        # update game id
        self._current_game_id = game_id
        # start new board stream        
        self._run_board_worker()


    #####################################################################
    ############## ---------- RUN WOKERS -------------  #################
    #####################################################################


    def _run_board_worker(self):   

        token_plus_game_id = lh.concat_values(self._current_token, self._current_game_id)

        # check if api thread is already running or token still not set, we skip starting
        if (self._lichess_stream_board_init_value == token_plus_game_id and self._board_worker and self._board_worker.is_alive()) or self._current_token == self._idle_token or self._current_game_id == self._idle_game_id:
            return    
        
        self._stop_event_board.clear()

        self._log(f"Board {token_plus_game_id}: ({self.worker_type}) worker starting") 

        # start board worker thread ({self.worker_type})
        self._board_worker = threading.Thread(
            target=self.handle_board_stream,
            args=(token_plus_game_id, ),
            daemon=True
        )
        self._board_worker.start()

   
    #####################################################################
    ############## ---------- STOP WOKERS ------------  #################
    #####################################################################


    def _stop_board_worker(self):
               
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

    def handle_board_stream(self, init_value):
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

                if self._main_worker == True:

                    reduced_data = json.dumps(lh.reduce_response_board(game_id, line))
                    # let ha know about the move
                    self._publish_response(reduced_data)
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