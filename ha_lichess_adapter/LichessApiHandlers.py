import appdaemon.plugins.hass.hassapi as hass
import paho.mqtt.client as paho

import ssl
import lichess_helpers as lh

import berserk
import json
import queue
from typing import Optional
import time

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

SECRET_PATH = "/config/secrets.yaml"
CA_CERT_PATH = "/config/hivemq.pem"

CLASS_NAME = "LichessApiHandlers"


class LichessApiHandlers(hass.Hass):
     
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
        self._session_lichess_api_main: Optional[berserk.TokenSession] = None        
        self._session_lichess_api_opponent: Optional[berserk.TokenSession] = None

        self._client_lichess_api_main: Optional[berserk.Client] = None        
        self._client_lichess_api_opponent: Optional[berserk.Client] = None

        self._lichess_api_init_value = IDLE_LICHESS_TOKEN

        # single-threaded API worker to serialize Lichess calls
        self._api_q = queue.Queue()  
  
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
            f"tls={'on' if self.mqtt_tls_enabled else 'off'} api_call='{MQTT_API_CALL_TOPIC}', response='{MQTT_RESPONSE_TOPIC}'"
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
            (MQTT_API_CALL_TOPIC, 0),
            (MQTT_GAME_ID_TOPIC, 0),
            (MQTT_TOKEN_MAIN_TOPIC, 0),
            (MQTT_TOKEN_OPP_TOPIC, 0),
            (MQTT_STATUS_TOPIC, 0),
        ])

    def _mqtt_on_disconnect(self, client, userdata, rc):
        # rc==0 means clean disconnect
        self.log(f"MQTT disconnected in {CLASS_NAME}, rc={rc}")

    def _on_mqtt_game_id(self, payload: str) -> None:

        # check if game id really changed
        if payload == self._current_game_id:
            return
        
        self.log(f"Game ID changed in {CLASS_NAME}: {self._current_game_id} -> {payload}")
       
        # update game id
        self._current_game_id = payload

    def _mqtt_on_message(self, client, userdata, msg):
        topic = msg.topic
        payload = lh.payload_to_str(msg.payload)
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
            self._client_mqtt.publish(MQTT_API_CALL_TOPIC, payload=IDLE_MQTT_STATE, qos=0, retain=False)
            self._client_mqtt.publish(MQTT_STATUS_TOPIC, payload=IDLE_MQTT_STATE, qos=0, retain=False)
        except Exception as e:
            self.log(f"Failed to publish empty MQTT response in {CLASS_NAME}: {e}")
     

    def _on_mqtt_api_call(self, payload: str) -> None:

        if payload != IDLE_GAME_ID:
            """Enqueue an API call payload to be handled by a single worker thread.
            This avoids blocking AppDaemon's event threads and prevents concurrent use of shared clients/state.
            """
            self._api_q.put(payload)

    def _on_mqtt_token_main(self, payload: str) -> None:

        # get decrypted token
        new_decrypted = lh.decrypt_message(self._current_secret_key, payload)

        # check if token really changed
        if new_decrypted == self._token_main:
            return

        self.log(f"Token changed (main) in {CLASS_NAME}: {self._token_main} -> {new_decrypted}")

        # trying to stop all running workers with previous token
        self._close_session_lichess_api_main()
        self._stop_api_worker()

        # update token
        self._token_main = new_decrypted    

        # start new session/client if not idle
        if new_decrypted != IDLE_LICHESS_TOKEN:
            self._init_session_lichess_api_main()
            self._run_api_worker()

    def _on_mqtt_token_opponent(self, payload: str) -> None:
        # get decrypted token
        new_decrypted = lh.decrypt_message(self._current_secret_key, payload)

        # check if token really changed
        if new_decrypted == self._token_opponent:
            return

        self.log(f"Token changed (opponent) in {CLASS_NAME}: {self._token_opponent} -> {new_decrypted}")

        # trying to stop all running workers with previous token
        self._close_session_lichess_api_opponent()

        # update token
        self._token_opponent = new_decrypted

        # start new session/client if not idle
        if new_decrypted != IDLE_LICHESS_TOKEN:
            self._init_session_lichess_api_opponent()

    def _on_mqtt_chessboard_status(self, payload: str) -> None:
        # chessboard is online
        if payload == STATUS_ONLINE:
            self.log(f"Chessboard is online")
            self._init_sessions_all()
            self._run_all_workers()
        # chessboard is offline
        elif payload == STATUS_OFFLINE:
            self.log(f"Chessboard is offline in {CLASS_NAME}")
            self._close_sessions_all()
            self._stop_all_workers()   
            self.clear_topics()

    #####################################################################
    ######### ---------- INIT AND CLOSE BERSERK -------------  ##########
    ##################################################################### 

    def _init_session_lichess_api_main(self):
        if self._token_main != IDLE_LICHESS_TOKEN:
            self._close_session_lichess_api_main()
            # open session with new token and create client
            self._session_lichess_api_main = berserk.TokenSession(self._token_main)
            self._client_lichess_api_main = berserk.Client(self._session_lichess_api_main) 

    def _init_session_lichess_api_opponent(self):
        if self._token_opponent != IDLE_LICHESS_TOKEN:
            self._close_session_lichess_api_opponent()
            # open session with new token and create client
            self._session_lichess_api_opponent = berserk.TokenSession(self._token_opponent)
            self._client_lichess_api_opponent = berserk.Client(self._session_lichess_api_opponent)
    
    def _init_sessions_all(self):
        self._init_session_lichess_api_main()
        self._init_session_lichess_api_opponent()

    def _close_session_lichess_api_main(self):
        try:
            if self._session_lichess_api_main is not None:
                self._session_lichess_api_main.close()
                self._session_lichess_api_main: Optional[berserk.TokenSession] = None 
                self._client_lichess_api_main: Optional[berserk.Client] = None 
        except Exception:
            pass 

    def _close_session_lichess_api_opponent(self):
        try:
            if self._session_lichess_api_opponent is not None:
                self._session_lichess_api_opponent.close()
                self._session_lichess_api_opponent: Optional[berserk.TokenSession] = None 
                self._client_lichess_api_opponent: Optional[berserk.Client] = None 
        except Exception:
            pass

    def _close_sessions_all(self):
        self._close_session_lichess_api_main()
        self._close_session_lichess_api_opponent()


    #####################################################################
    ############## ---------- RUN WOKERS -------------  #################
    #####################################################################

    def _run_api_worker(self):
        # check if api thread is already running or token still not set, we skip starting
        if (self._lichess_api_init_value == self._token_main and self._token_main != IDLE_LICHESS_TOKEN) or self._token_main == IDLE_LICHESS_TOKEN:
            return
        
        self.log(f"API worker starting: {self._token_main}") 

        # start api worker thread
        self.handle_api_calls(self._token_main)

    def _run_all_workers(self):
        self._run_api_worker()
   
    #####################################################################
    ############## ---------- STOP WOKERS ------------  #################
    #####################################################################

    def _stop_api_worker(self):

        if self._lichess_api_init_value != IDLE_LICHESS_TOKEN:
            self.log(f"API main worker is running, trying to stop")
            # sentinel shutdown
            self._api_q.put("terminate_api_worker")

            # let the loop escape
            time.sleep(1)

            # check if init variable is reseted
            if self._lichess_api_init_value != IDLE_LICHESS_TOKEN:
                self.log(f"Stopping API main worker failed", LEVEL=Warning)
            else:
                self.log(f"API main worker stopped")

    def _stop_all_workers(self):
        self._stop_api_worker()


    #####################################################################
    ############## ---------- HANDLER FUNCTIONS ---------  ##############
    #####################################################################
 

    def handle_api_calls(self, init_value):
        # init_value is token main
        self._lichess_api_init_value = init_value
        self.log(f"Starting main-loop {self._lichess_api_init_value}")
        while True:
            # get next item from the queue
            try:
                item = self._api_q.get(timeout=1)
            except queue.Empty:
                continue

            # check for sentinel
            if item == "terminate_api_worker":
                self.log(f"Terminating main-loop {self._lichess_api_init_value} due to sentinel")
                break

            # check if token changed during processing
            if self._lichess_api_init_value != self._token_main:
                self.log(f"Terminating main-loop due to token change: {self._lichess_api_init_value} -> {self._token_main}")
                break

            # handle the API call
            try:
                if item:
                    self.perform_api_call(item)
                else:
                    self.log(f"Empty API call received", level="WARNING")
            except Exception as e:
                self.log(f"API call error: {e}", level="ERROR")
            finally:
                self._api_q.task_done()
        # while loop is running, the init-token is not idle
        self._lichess_api_init_value == IDLE_LICHESS_TOKEN
            
        self.log(f"Terminated main-loop {self._lichess_api_init_value}")

    #####################################################################
    ############## ---------- HELPER FUNCTIONS ----------  ##############
    #####################################################################

    def perform_api_call(self, new):

        try:
            json_data = json.loads(new)
        except json.JSONDecodeError as e:
            self.log(f"Invalid JSON in api_call: {e} payload={new!r}", level="WARNING")
            return

        call_type = json_data.get("type", None)

        self.log(f"API-call: " + json.dumps(json_data))

        if json_data and call_type:

            valid_token = self._token_main != IDLE_LICHESS_TOKEN and self._client_lichess_api_main is not None
            valid_token_opponent = self._token_opponent != IDLE_LICHESS_TOKEN and self._client_lichess_api_opponent is not None
            valid_game_id = self._current_game_id != IDLE_GAME_ID
            
            if valid_token == True:

                if call_type == "getAccountInfoMain":
                    json_response = lh.getAccountInfoMain(self._client_lichess_api_main, self_log=self.log)
                    self.publish_response(json_response)
                    return

                if call_type == "abortRunningGames":
                    lh.abortRunningGames(self._client_lichess_api_main, self_log=self.log)
                    return

                if call_type == "withdrawTornament":
                    lh.withdrawTornament(json_data, self._client_lichess_api_main, self_log=self.log)
                    return

                if call_type == "joinTournamentByName":
                    json_response = lh.joinTournamentByName(json_data, self._client_lichess_api_main, self_log=self.log)
                    self.publish_response(json_response)
                    return

                if call_type == "joinTournamentById":
                    json_response = lh.joinTournamentById(json_data, self._client_lichess_api_main, self_log=self.log)
                    self.publish_response(json_response)
                    return
            
            if valid_token == True and valid_token_opponent == True:

                if call_type == "createGame" :
                    json_response = lh.createGame(json_data, self._client_lichess_api_main, self._client_lichess_api_opponent, self_log=self.log)
                    self.publish_response(json_response)
                    return
            
            if valid_token == True and valid_game_id == True:

                if call_type == "abort":
                    lh.abort(self._client_lichess_api_main, self._current_game_id, self_log=self.log)
                    return
                
                if call_type == "resign":
                    lh.resign(self._client_lichess_api_main, self._current_game_id, self_log=self.log)
                    return
                
                if call_type == "claim-victory":
                    lh.claimVictory(self._client_lichess_api_main, self._current_game_id, self_log=self.log)
                    return  
                                    
                if call_type == "makeMove":
                    lh.makeMove(json_data, self._client_lichess_api_main, self._current_game_id, self_log=self.log)
                    return
                    
                if call_type == "draw":
                    lh.draw(json_data, self._client_lichess_api_main, self._current_game_id, self_log=self.log)
                    return 
                
                if call_type == "takeback":
                    lh.takeback(json_data, self._client_lichess_api_main, self._current_game_id, self_log=self.log)
                    return 
                
                if call_type == "writeChatMessage":
                    lh.writeChatMessage(json_data, self._client_lichess_api_main, self._current_game_id, self_log=self.log)
                    return 

            if valid_token_opponent == True and valid_game_id == True:

                if call_type == "makeMoveOpponent":
                    lh.makeMoveOpponent(json_data, self._client_lichess_api_opponent, self._current_game_id, self_log=self.log)
                    return 
                
                if call_type == "resignOpponent":
                    lh.resignOpponent(json_data, self._client_lichess_api_opponent, self._current_game_id, self_log=self.log)
                    return 
                
                if call_type == "drawOpponent":
                    lh.drawOpponent(json_data, self._client_lichess_api_opponent, self._current_game_id, self_log=self.log)
                    return
            
            # we should return till that point    
            self.log(f"API-Call " + json.dumps(json_data) + " could not be performed!", level="WARNING")


