import appdaemon.plugins.hass.hassapi as hass
import paho.mqtt.client as paho

import ssl
import lichess_helpers as lh

import berserk
import json
import threading
import queue
from typing import Optional

IDLE_GAME_ID = "idle"
IDLE_LICHESS_TOKEN = "idle"
IDLE_MQTT_STATE = "idle"
STATUS_OFFLINE = "offline"
STATUS_ONLINE = "online"

# MQTT topics
MQTT_GAME_ID_TOPIC = "chessboard/game_id"
MQTT_TOKEN_MAIN_TOPIC = "chessboard/token_main"
MQTT_TOKEN_OPP_TOPIC = "chessboard/token_opponent"
MQTT_STATUS_TOPIC = "chessboard/status"
MQTT_RESPONSE_TOPIC = "chessboard/response"

SECRET_PATH = "/config/secrets.yaml"
CA_CERT_PATH = "/config/hivemq.pem"

CLASS_NAME = "LichessStreamBoard"

class LichessStreamBoard(hass.Hass):
     
    # store the encryption secret key
    _current_secret_key = None

    def initialize(self):
        self.log(f"AppDaemon {CLASS_NAME} script initialized!")
                       
        # ---- Load secrets (local file) ----
        self.secrets = lh.load_secrets(SECRET_PATH)

        # ---- Chess secret key ----
        self._current_secret_key = self.secrets.get("chessboard_secret_key", None)
        if not self._current_secret_key:
            raise RuntimeError("Missing 'chessboard_secret_key' in {}.".format(SECRET_PATH))

        # ---- MQTT config (from secrets.yaml) ----
        mqtt_cfg = self.secrets.get("mqtt", {}) or {}
        self.mqtt_host = mqtt_cfg.get("host", "127.0.0.1")
        self.mqtt_port = int(mqtt_cfg.get("port", 1883))        
        self.mqtt_username = mqtt_cfg.get("username") or None
        self.mqtt_password = mqtt_cfg.get("password") or None
        self.mqtt_keepalive = int(mqtt_cfg.get("keepalive", 60))
        #self.mqtt_client_id = mqtt_cfg.get("client_id", "lichess_api_handler")
        self.mqtt_client_id = f"{CLASS_NAME}"
        self.mqtt_tls_enabled = bool(mqtt_cfg.get("mqtt_tls", False))
        self.mqtt_ca_cert_path = mqtt_cfg.get("mqtt_ca_cert", CA_CERT_PATH) 

        # current runtime state (protected by self._lock where needed)
        self._current_game_id = IDLE_GAME_ID
        self._token_main = IDLE_LICHESS_TOKEN
        self._token_opponent = IDLE_LICHESS_TOKEN

        # Keep references to sessions so we can close them on token change
        # lichess (berserk) clients/sessions
        self._session_lichess_board_main: Optional[berserk.TokenSession] = None 
        self._session_lichess_board_opponent: Optional[berserk.TokenSession] = None

        self._client_lichess_board_main: Optional[berserk.Client] = None 
        self._client_lichess_board_opponent: Optional[berserk.Client] = None

        self._lichess_stream_board_main_init_value = lh.concat_values(IDLE_LICHESS_TOKEN, IDLE_GAME_ID)
        self._lichess_stream_board_opponent_init_value = lh.concat_values(IDLE_LICHESS_TOKEN, IDLE_GAME_ID)


        # single-threaded API worker to serialize Lichess calls
        self._board_worker_main: Optional[threading.Thread] = None    
        self._board_worker_opponent: Optional[threading.Thread] = None 
        self._lock = threading.Lock()   
        self._stop_event_board_main = threading.Event()
        self._stop_event_board_opponent = threading.Event()

        
        # ---- Initialize MQTT client ----
        self._client_mqtt = paho.Client(client_id=self.mqtt_client_id, protocol=paho.MQTTv311, clean_session=True)
        if self.mqtt_username is not None:
            self._client_mqtt.username_pw_set(self.mqtt_username, password=self.mqtt_password)

        # TLS/SSL setup
        if self.mqtt_tls_enabled:
            if not self.mqtt_ca_cert_path:
                raise ValueError("mqtt_tls is true but mqtt_ca_cert is not set (path to CA certificate).")
            self._client_mqtt.tls_set(
                ca_certs=self.mqtt_ca_cert_path,
                certfile=None,
                keyfile=None,
                cert_reqs=ssl.CERT_REQUIRED,
                tls_version=ssl.PROTOCOL_TLS_CLIENT,
            )

        self._client_mqtt.on_connect = self._mqtt_on_connect
        self._client_mqtt.on_message = self._mqtt_on_message
        self._client_mqtt.on_disconnect = self._mqtt_on_disconnect

        # connect + start network loop
        self._client_mqtt.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client_mqtt.connect(self.mqtt_host, self.mqtt_port, keepalive=self.mqtt_keepalive)
        self._client_mqtt.loop_start()

        self.log(
            f"MQTT ready in {CLASS_NAME} via paho. host={self.mqtt_host} port={self.mqtt_port} "
            f"tls={'on' if self.mqtt_tls_enabled else 'off'} response='{MQTT_RESPONSE_TOPIC}'"
        )
        self.log(
            f"MQTT inputs for {CLASS_NAME}: game_id='{MQTT_GAME_ID_TOPIC}', "
            f"token_main='{MQTT_TOKEN_MAIN_TOPIC}', token_opponent='{MQTT_TOKEN_OPP_TOPIC}'"
        )
        self.log(f"Initialization complete for {CLASS_NAME}")

    def terminate(self):
        """Called by AppDaemon on shutdown/reload."""
        try:
            self._stop_all_workers()
        except Exception:
            pass

        try:
            if self._client_mqtt is not None:
                self._client_mqtt.loop_stop()
                self._client_mqtt.disconnect()
        except Exception:
            pass

    #####################################################################
    ############## ---------- MQTT callbacks ---------- #################
    #####################################################################

    def _mqtt_on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            self.log(f"MQTT connection failed in {CLASS_NAME}, rc={rc}", level="ERROR")
            return

        self.log(f"MQTT connected in {CLASS_NAME}, subscribing to topics...")
        client.subscribe([
            (MQTT_GAME_ID_TOPIC, 0),
            (MQTT_TOKEN_MAIN_TOPIC, 0),
            (MQTT_TOKEN_OPP_TOPIC, 0),
            (MQTT_STATUS_TOPIC, 0),
        ])

    def _mqtt_on_disconnect(self, client, userdata, rc):
        # rc==0 means clean disconnect
        self.log(f"MQTT disconnected in {CLASS_NAME}, rc={rc}")

    def _mqtt_on_message(self, client, userdata, msg):
        topic = msg.topic
        payload = lh.payload_to_str(msg.payload)
        if payload is None:
            return

        elif topic == MQTT_GAME_ID_TOPIC:
            self._on_mqtt_game_id(payload)
        elif topic == MQTT_TOKEN_MAIN_TOPIC:
            self._on_mqtt_token_main(payload)
        elif topic == MQTT_TOKEN_OPP_TOPIC:
            self._on_mqtt_token_opponent(payload)
        elif topic == MQTT_STATUS_TOPIC:
            self._on_mqtt_chessboard_status(payload)

    #####################################################################
    ############## ---------- MQTT handlers ----------  #################
    #####################################################################

    def publish_response(self, payload: str):
        """Publish response JSON string to chessboard/response."""
        try:
            if self._client_mqtt is None:
                raise RuntimeError("MQTT client not initialized")
            self._client_mqtt.publish(MQTT_RESPONSE_TOPIC, payload=payload, qos=0, retain=False)
        except Exception as e:
            self.log(f"Failed to publish MQTT response in {CLASS_NAME}: {e}")

    def clear_topics(self):
        """Publish response JSON string to chessboard/response."""
        try:
            if self._client_mqtt is None:
                raise RuntimeError("MQTT client not initialized")
            self._client_mqtt.publish(MQTT_RESPONSE_TOPIC, payload=IDLE_MQTT_STATE, qos=0, retain=False)
            self._client_mqtt.publish(MQTT_TOKEN_OPP_TOPIC, payload=IDLE_LICHESS_TOKEN, qos=0, retain=False)
            self._client_mqtt.publish(MQTT_TOKEN_MAIN_TOPIC, payload=IDLE_LICHESS_TOKEN, qos=0, retain=False)
            self._client_mqtt.publish(MQTT_GAME_ID_TOPIC, payload=IDLE_GAME_ID, qos=0, retain=False)
            self._client_mqtt.publish(MQTT_STATUS_TOPIC, payload=IDLE_MQTT_STATE, qos=0, retain=False)
        except Exception as e:
            self.log(f"Failed to publish empty MQTT response in {CLASS_NAME}: {e}")
     
    def _on_mqtt_token_main(self, payload: str) -> None:

        # get decrypted token
        new_decrypted = lh.decrypt_message(self._current_secret_key, payload)

        # check if token really changed
        if new_decrypted == self._token_main:
            return

        self.log(f"Token changed (main) in {CLASS_NAME}: {self._token_main} -> {new_decrypted}")

        # trying to stop all running workers with previous token
        self._close_session_lichess_board_main()
        self._stop_board_worker_main()

        # update token
        self._token_main = new_decrypted    

        # start new session/client if not idle
        if new_decrypted != IDLE_LICHESS_TOKEN:
            self._init_session_lichess_board_main()

    def _on_mqtt_token_opponent(self, payload: str) -> None:
        # get decrypted token
        new_decrypted = lh.decrypt_message(self._current_secret_key, payload)

        # check if token really changed
        if new_decrypted == self._token_opponent:
            return

        self.log(f"Token changed (opponent) in {CLASS_NAME}: {self._token_opponent} -> {new_decrypted}")

        # trying to stop all running workers with previous token
        self._close_session_lichess_board_opponent()
        self._stop_board_worker_opponent()

        # update token
        self._token_opponent = new_decrypted

        # start new session/client if not idle
        if new_decrypted != IDLE_LICHESS_TOKEN:
            self._init_session_lichess_board_opponent()

    def _on_mqtt_game_id(self, payload: str) -> None:

        # check if game id really changed
        if payload == self._current_game_id:
            return
        
        self.log(f"Game ID changed in {CLASS_NAME}: {self._current_game_id} -> {payload}")

        # reset game_id to assist stopping of workers
        self._current_game_id = IDLE_GAME_ID

        # overwrites current game id to stop existing stream
        self._stop_board_workers()
        
        # update game id
        self._current_game_id = payload
        # start new board stream        
        self._run_board_workers()

    def _on_mqtt_chessboard_status(self, payload: str) -> None:
        # chessboard is online
        if payload == STATUS_ONLINE:
            self.log(f"Chessboard is online")
            self._init_sessions_all()
            self._run_board_workers()
        # chessboard is offline
        elif payload == STATUS_OFFLINE:
            self.log(f"Chessboard is offline in {CLASS_NAME}")
            if self.is_any_thread_alive():
                self.log(f"At least one thread is alive, stopping ...")
                self._close_sessions_all()           
                self._stop_board_workers()
            else:
                self.log(f"No threads running")            
            self.clear_topics()

    #####################################################################
    ######### ---------- INIT AND CLOSE BERSERK -------------  ##########
    ##################################################################### 


    def _init_session_lichess_board_main(self):
        if self._token_main != IDLE_LICHESS_TOKEN and self._session_lichess_board_main is None and self._current_game_id != IDLE_GAME_ID:
            # open session with new token and create client
            self._session_lichess_board_main = berserk.TokenSession(self._token_main)
            self._client_lichess_board_main = berserk.Client(self._session_lichess_board_main)
    
    def _init_session_lichess_board_opponent(self):
        if self._token_opponent != IDLE_LICHESS_TOKEN and self._session_lichess_board_opponent is None and self._current_game_id != IDLE_GAME_ID:
            # open session with new token and create client
            self._session_lichess_board_opponent = berserk.TokenSession(self._token_opponent)
            self._client_lichess_board_opponent = berserk.Client(self._session_lichess_board_opponent)

    def _init_session_lichess_boards(self):
        self._init_session_lichess_board_main()
        self._init_session_lichess_board_opponent()

    def _init_sessions_all(self):
        self._init_session_lichess_boards()

    def _close_session_lichess_board_main(self):
        try:
            if self._session_lichess_board_main is not None:
                self._session_lichess_board_main.close()
                self._session_lichess_board_main: Optional[berserk.TokenSession] = None 
                self._client_lichess_board_main: Optional[berserk.Client] = None 
        except Exception:
            pass

   
    def _close_session_lichess_board_opponent(self):
        try:
            if self._session_lichess_board_opponent is not None:
                self._session_lichess_board_opponent.close()
                self._session_lichess_board_opponent: Optional[berserk.TokenSession] = None 
                self._client_lichess_board_opponent: Optional[berserk.Client] = None 
        except Exception:
            pass
    
    def _close_session_lichess_boards(self):
        self._close_session_lichess_board_main()
        self._close_session_lichess_board_opponent()

    def _close_sessions_all(self):
        self._close_session_lichess_boards()


    #####################################################################
    ############## ---------- RUN WOKERS -------------  #################
    #####################################################################

    def _run_board_worker_main(self):   

        token_plus_game_id = lh.concat_values(self._token_main, self._current_game_id)

        # check if api thread is already running or token still not set, we skip starting
        if (self._lichess_stream_board_main_init_value == token_plus_game_id and self._board_worker_main and self._board_worker_main.is_alive()) or self._token_main == IDLE_LICHESS_TOKEN or self._current_game_id == IDLE_GAME_ID:
            return    
        
        self._stop_event_board_main.clear()

        self.log(f"Board {token_plus_game_id}: (main) worker starting") 

        # start board worker thread (main)
        self._board_worker_main = threading.Thread(
            target=self.handle_board_stream_main,
            args=(token_plus_game_id, ),
            daemon=True
        )
        self._board_worker_main.start()

    def _run_board_worker_opponent(self):   

        token_plus_game_id = lh.concat_values(self._token_opponent, self._current_game_id)

        # check if api thread is already running or token still not set, we skip starting
        if (self._lichess_stream_board_opponent_init_value == token_plus_game_id and self._board_worker_opponent and self._board_worker_opponent.is_alive()) or self._token_opponent == IDLE_LICHESS_TOKEN or self._current_game_id == IDLE_GAME_ID:
            return    
        
        self._stop_event_board_opponent.clear()

        self.log(f"Board {token_plus_game_id}: (opponent) worker starting") 

        # start board worker thread (opponent)
        self._board_worker_opponent = threading.Thread(
            target=self.handle_board_stream_opponent,
            args=(token_plus_game_id, ),
            daemon=True
        )
        self._board_worker_opponent.start()

    def _run_board_workers(self): 
        self._run_board_worker_main()
        self._run_board_worker_opponent()

    def _run_all_workers(self):
        self._run_board_workers()
   
    #####################################################################
    ############## ---------- STOP WOKERS ------------  #################
    #####################################################################

    def _stop_board_worker_main(self):
               
        # stop main board worker
        if  self._board_worker_main is not None and self._board_worker_main.is_alive():

            self._stop_event_board_main.set()

            self.log(f"Main board worker stopping")
            # wait main board thread is finished
            self._board_worker_main.join(timeout=1)

            # check if main thread is stopped
            if self._board_worker_main and self._board_worker_main.is_alive():
                self.log(f"Stopping board worker (main) failed")
                # we call dummy function (here abort) to provoke an event in the stream to call the loop-abort checks
                lh.write_into_chat(self._client_lichess_board_main, self._current_game_id, {"text", f"Event on main board-stream: {self._current_game_id}"})
            else:
                self.log(f"Board worker (main) stopped")

        # check if init variable is reseted
        if self._lichess_stream_board_main_init_value != lh.concat_values(IDLE_LICHESS_TOKEN, IDLE_GAME_ID):
            self.log(f"Thread init-variable {self._lichess_stream_board_main_init_value} for stream board main worker is still set", LEVEL=Warning)

    def _stop_board_worker_opponent(self):        
        
        if self._board_worker_opponent is not None and self._board_worker_opponent.is_alive():

            self._stop_event_board_main.set()
           
            self.log(f"Opponent board worker stopping")
            # wait opponent board thread is finished
            self._board_worker_opponent.join(timeout=1)
        
            # check if opponent thread is stopped
            if self._board_worker_opponent and self._board_worker_opponent.is_alive():
                self.log(f"Stopping board worker (opponent) failed")
                # we call dummy function (here abort) to provoke an event in the stream to call the loop-abort checks
                lh.write_into_chat(self._client_lichess_board_opponent, self._current_game_id, {"text", f"Event on opponent board-stream: {self._current_game_id}"})
            else:
                self.log(f"Board worker (opponent) stopped")

                # check if init variable is reseted
        if self._lichess_stream_board_opponent_init_value != lh.concat_values(IDLE_LICHESS_TOKEN, IDLE_GAME_ID):
            self.log(f"Thread init-variable {self._lichess_stream_board_opponent_init_value} for stream board opponent worker is still set", LEVEL=Warning)

    def _stop_board_workers(self):
        self._stop_board_worker_main()
        self._stop_board_worker_opponent()

    def _stop_all_workers(self):
        self._stop_board_workers()


    #####################################################################
    ############## ---------- HANDLER FUNCTIONS ---------  ##############
    #####################################################################

    def handle_board_stream_main(self, init_value):
        # init_value is concat(token main + game_id)
        splitted_init_value = lh.split_concated_values(init_value)
        game_id = "idle"
        if len(splitted_init_value) > 1:
            game_id = splitted_init_value[1]

        # init_value is token main
        with  self._lock:
            self._lichess_stream_board_main_init_value = init_value
          
        self.log(f"Starting the board stream (main): {init_value}")

        for line in self._client_lichess_api_main.board.stream_game_state(game_id):
            if line: # valid dic 
                reduced_data = json.dumps(lh.reduce_response_board(game_id, line))
                # let ha know about the move
                self.publish_response(reduced_data)
                self.log(f"Board (main): {reduced_data}")
  
                # check if we have to abort the game                    
                if lh.check_game_over(line, init_value, self._token_main, self._current_game_id):
                    self.log(f"Terminating the board stream (main): {init_value}")
                    # close the stream
                    break
                
            if self._stop_event_board_main.is_set():
                self.log(f"Board stream stop event set, terminating the board stream (main): {init_value}")
                break
            with self._lock:
                    if (lh.concat_values(self._token_main, self._current_game_id)  != init_value):
                        self.log(f"Board stream stopping: game_id or token changed, terminating the board stream (main): {init_value}")
                        break
        # we are ready to go
        self.log(f"Terminated board stream (main): {init_value}")
        with  self._lock:
            self._lichess_stream_board_main_init_value = lh.concat_values(IDLE_LICHESS_TOKEN, IDLE_GAME_ID)

    def handle_board_stream_opponent(self, init_value):
        # init_value is concat(token main + game_id)
        splitted_init_value = lh.split_concated_values(init_value)
        game_id = "idle"
        if len(splitted_init_value) > 1:
            game_id = splitted_init_value[1]

        with  self._lock:
            self._lichess_stream_board_opponent_init_value = init_value

        self.log(f"Starting the board stream (opponent): {init_value}")

        # do nothing, just keep stream alive
        for line in self._client_lichess_api_opponent.board.stream_game_state(game_id):
            if line: # valid dic
                self.log(f"Board (opponent): {line}")

                # check if we have to abort the game
                if lh.check_game_over(line):
                    self.log(f"Terminating the board stream (opponent): {init_value}")
                    # close the stream
                    break
            if self._stop_event_board_opponent.is_set():
                self.log(f"Board stream stop event set, terminating the board stream (opponent): {init_value}")
                break
            with self._lock:
                if (lh.concat_values(self._token_opponent, self._current_game_id)  != init_value):
                    self.log(f"Board stream stopping: game_id or token changed, terminating the board stream (opponent): {init_value}")
                    break
        with  self._lock:
            self._lichess_stream_board_opponent_init_value = lh.concat_values(IDLE_LICHESS_TOKEN, IDLE_GAME_ID)

        # we are ready to go
        self.log(f"Terminated board stream (opponent): {init_value}")

    #####################################################################
    ############## ---------- HELPER FUNCTIONS ----------  ##############
    #####################################################################

    def is_any_thread_alive(self):
        return  bool((self._api_worker and self._api_worker.is_alive()) 
                     or (self._stream_worker and self._stream_worker.is_alive()) 
                     or (self._board_worker_main and self._board_worker_main.is_alive()) 
                     or (self._board_worker_opponent and self._board_worker_opponent.is_alive()))

