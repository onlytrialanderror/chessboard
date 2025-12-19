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


class LichessApiHandler(hass.Hass):
     
    # store the encryption secret key
    _current_secret_key = None

    # mqtt client
    _client_mqtt: Optional[paho.Client] = None

    # lichess (berserk) clients/sessions
    _client_main: Optional[berserk.Client] = None 
    _client_opponent: Optional[berserk.Client] = None


    def initialize(self):
        self.log(f"AppDaemon LichessApiHandler script initialized!")
                       
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
        self.mqtt_client_id = mqtt_cfg.get("client_id", "lichess_api_handler")
        self.mqtt_tls_enabled = bool(mqtt_cfg.get("mqtt_tls", False))
        self.mqtt_ca_cert_path = mqtt_cfg.get("mqtt_ca_cert", CA_CERT_PATH) 

        # current runtime state (protected by self._lock where needed)
        self._current_game_id = IDLE_GAME_ID
        self._token_main = IDLE_LICHESS_TOKEN
        self._token_opponent = IDLE_LICHESS_TOKEN

        # Keep references to sessions so we can close them on token change
        self._session_main: Optional[berserk.TokenSession] = None 
        self._session_opponent: Optional[berserk.TokenSession] = None 

        # single-threaded API worker to serialize Lichess calls
        self._api_q = queue.Queue()  
        self._api_worker: Optional[threading.Thread] = None   
        self._stream_worker: Optional[threading.Thread] = None
        self._board_worker_main: Optional[threading.Thread] = None    
        self._board_worker_opponent: Optional[threading.Thread] = None 
        self._lock = threading.Lock()   
        self._stop_event_api = threading.Event()
        self._stop_event_stream = threading.Event()
        self._stop_event_board = threading.Event()

        
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
        self._client_mqtt.connect(self.mqtt_host, self.mqtt_port, keepalive=self.mqtt_keepalive)
        self._client_mqtt.loop_start()

        self.log(
            f"MQTT ready LichessApiHandler via paho. host={self.mqtt_host} port={self.mqtt_port} "
            f"tls={'on' if self.mqtt_tls_enabled else 'off'} api_call='{MQTT_API_CALL_TOPIC}', response='{MQTT_RESPONSE_TOPIC}'"
        )
        self.log(
            f"MQTT inputs LichessApiHandler: game_id='{MQTT_GAME_ID_TOPIC}', "
            f"token_main='{MQTT_TOKEN_MAIN_TOPIC}', token_opponent='{MQTT_TOKEN_OPP_TOPIC}'"
        )
        self.log(f"Initialization complete for LichessApiHandler")

    def terminate(self):
        """Called by AppDaemon on shutdown/reload."""
        try:
            self._stop_api_worker()
            self._stop_stream_worker()
            self._stop_board_worker()
        except Exception:
            pass

        try:
            if self._client_mqtt is not None:
                self._client_mqtt.loop_stop()
                self._client_mqtt.disconnect()
        except Exception:
            pass

    # ---------- NEW: paho callbacks ----------

    def _mqtt_on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            self.log(f"MQTT connect failed in LichessApiHandler, rc={rc}", level="ERROR")
            return

        self.log(f"MQTT connected in LichessApiHandler, subscribing to topics...")
        client.subscribe([
            (MQTT_API_CALL_TOPIC, 0),
            (MQTT_GAME_ID_TOPIC, 0),
            (MQTT_TOKEN_MAIN_TOPIC, 0),
            (MQTT_TOKEN_OPP_TOPIC, 0),
            (MQTT_STATUS_TOPIC, 0),
        ])

    def _mqtt_on_disconnect(self, client, userdata, rc):
        # rc==0 means clean disconnect
        self.log(f"MQTT disconnected in LichessApiHandler, rc={rc}")

    def _mqtt_on_message(self, client, userdata, msg):
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
        else:
            # should not happen due to our subscriptions, but keep it safe
            self.log(f"MQTT message on unexpected topic '{topic}' in LichessApiHandler", level="WARNING")

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

    # ---------- MQTT helpers ----------

    def publish_response(self, payload: str):
        """Publish response JSON string to chessboard/response."""
        try:
            if self._client_mqtt is None:
                raise RuntimeError("MQTT client not initialized")
            self._client_mqtt.publish(MQTT_RESPONSE_TOPIC, payload=payload, qos=0, retain=False)
        except Exception as e:
            self.log(f"Failed to publish MQTT response in LichessApiHandler: {e}")
        

    # ---------- MQTT handlers ----------

    def _on_mqtt_api_call(self, payload: str) -> None:
        if payload is None:
            self.log(f"MQTT api_call received without payload in LichessApiHandler")
            return

        if payload not in {IDLE_GAME_ID}:
            """Enqueue an API call payload to be handled by a single worker thread.
            This avoids blocking AppDaemon's event threads and prevents concurrent use of shared clients/state.
            """
            self._api_q.put(payload)

    def _on_mqtt_game_id(self, payload: str) -> None:
        if payload is None:
            return

        self.game_id_changed(payload)

    def _on_mqtt_token_main(self, payload: str) -> None:
        if payload is None:
            return
        # token_changed_main() already handles unavailable/unknown/empty checks + decrypt
        self.token_changed_main(payload)

    def _on_mqtt_token_opponent(self, payload: str) -> None:
        if payload is None:
            return
        # token_changed_opponent() already handles unavailable/unknown/empty checks + decrypt
        self.token_changed_opponent(payload)

    def _on_mqtt_status(self, payload: str) -> None:
        if payload is None or payload in {STATUS_OFFLINE}:
            self.log(f"Chessboard is oflline at LichessApiHandler, stopping API worker")            
            self._stop_api_worker()
            self._stop_stream_worker()
            self._stop_board_worker()
        else:
            self.log(f"Chessboard is online at LichessApiHandler, starting API worker")
            self._run_api_worker()
            self._run_stream_worker()
            self._run_board_worker()

    def _run_api_worker(self):
        # check if api thread is already running
        if self._api_worker and self._api_worker.is_alive():
            return
        self._stop_event_api.clear()

        self.log(f"API worker starting at LichessApiHandler") 
        # start api worker thread
        self._api_worker = threading.Thread(
            target=self.handle_api_calls,
            daemon=True
        )
        self._api_worker.start()

    def _stop_api_worker(self):
        # sentinel shutdown
        self._api_q.put("terminate_api_worker")
        self._stop_event_api.set()
        if self._api_worker and self._api_worker.is_alive():
            self._api_worker.join(timeout=2)
        # check if api thread is stopped
        if self._api_worker and self._api_worker.is_alive():
            self.log(f"Stopping API worker at LichessApiHandler failed")
        else:
            self.log(f"API worker stopped at LichessApiHandler")

    def _run_stream_worker(self):
        if self._token_main !=  IDLE_LICHESS_TOKEN:
            self._stop_event_stream.clear()
            self.log(f"Stream worker started at LichessApiHandler") 
            # start stream worker thread
            self._stream_worker = threading.Thread(
                target=self.handle_incoming_events,
                args=(self._token_main,),
                daemon=True
            )
            self._stream_worker.start()

    def _stop_stream_worker(self):        
        if self._stream_worker and self._stream_worker.is_alive():
            self._stop_event_stream.set()
            self.log(f"Stopping Stream worker at LichessApiHandler")
            # we reset the token to idle to stop the stream
            self._token_main =  IDLE_LICHESS_TOKEN
            # wait stream thread is finished
            self._stream_worker.join(timeout=2) 
            # check if stream thread is stopped           
            if self._stream_worker and self._stream_worker.is_alive():
                self.log(f"Stopping Stream worker at LichessApiHandler failed")
            else:
                self.log(f"Stream worker stopped at LichessApiHandler")

    def _run_board_worker(self):        
        current_game_id = self._current_game_id
        if current_game_id != IDLE_GAME_ID:
            self._stop_event_board.clear()
            self.log(f"Board {current_game_id }: (main) worker starting at LichessApiHandler") 
            # start board worker thread (main)
            self._board_worker_main = threading.Thread(
                target=self.handle_board_stream_main,
                args=(current_game_id ,),
                daemon=True
            )
            self.log(f"Board {current_game_id }: (opponent) worker starting at LichessApiHandler") 
            # start board worker thread (opponent)
            self._board_worker_opponent = threading.Thread(
                target=self.handle_board_stream_opponent,
                args=(current_game_id ,),
                daemon=True
            )
            self._board_worker_main.start()
            self._board_worker_opponent.start()
        

    def _stop_board_worker(self):
        self._stop_event_board.set()
        # reset current game id to idle to stop existing streams
        self._current_game_id = IDLE_GAME_ID        
        # stop main board worker
        if self._board_worker_main is not None and self._board_worker_main.is_alive():

            self.log(f"Main board worker stopping at LichessApiHandler")
            # wait main board thread is finished
            self._board_worker_main.join(timeout=1)

            # check if main thread is stopped
            if self._board_worker_main and self._board_worker_main.is_alive():
                self.log(f"Stopping board worker (main) at LichessApiHandler failed")
            else:
                self.log(f"Board worker (main) stopped at LichessApiHandler")

        # stop opponent board worker
        if self._board_worker_opponent is not None and self._board_worker_opponent.is_alive():

            self.log(f"Opponent board worker stopping at LichessApiHandler")
            # wait opponent board thread is finished
            self._board_worker_opponent.join(timeout=1)
        
            # check if opponent thread is stopped
            if self._board_worker_opponent and self._board_worker_opponent.is_alive():
                self.log(f"Stopping board worker (opponent) at LichessApiHandler failed")
            else:
                self.log(f"Board worker (opponent) stopped at LichessApiHandler")

    def game_id_changed(self, new):

        # check if game id really changed
        if not new or new == self._current_game_id:
            return
        
        self.log(f"Game ID changed in LichessApiHandler: {self._current_game_id} -> {new}")

        # overwrites current game id to stop existing stream
        self._stop_board_worker()
        # update game id
        self._current_game_id = new
        # start new board stream        
        self._run_board_worker()

    def token_changed_main(self, new):
        # get decrypted token
        new_decrypted = lh.decrypt_message(self._current_secret_key, new)

        # check if token really changed
        if new_decrypted == self._token_main:
            return

        self.log(f"Token changed (main) in LichessApiHandler: {self._token_main} -> {new_decrypted}")
        
        # close session/client
        try:
            if self._session_main is not None:
                self._session_main.close()
        except Exception:
            pass
        self._session_main = None
        self._client_main = None

        # try to stop existing stream
        self._stop_stream_worker()
        # update token
        self._token_main = new_decrypted    

        # start new session/client if not idle
        if new_decrypted != IDLE_LICHESS_TOKEN:
            self._session_main = berserk.TokenSession(new_decrypted)
            self._client_main = berserk.Client(self._session_main) 
            self._run_stream_worker()            
            

    def token_changed_opponent(self, new):
        # get decrypted token
        new_decrypted = lh.decrypt_message(self._current_secret_key, new)

        # check if token really changed
        if new_decrypted == self._token_opponent:
            return

        self.log(f"Token changed (opponent) in LichessApiHandler: {self._token_opponent} -> {new_decrypted}")

        # close session/client
        try:
            if self._session_opponent is not None:
                self._session_opponent.close()
        except Exception:
            pass
        self._session_opponent = None
        self._client_opponent = None

        # update token
        self._token_opponent = new_decrypted

        # start new session/client if not idle
        if new_decrypted != IDLE_LICHESS_TOKEN:
            self._session_opponent = berserk.TokenSession(new_decrypted)
            self._client_opponent = berserk.Client(self._session_opponent)

    def check_game_over(self, dat, game_id):
        break_game = False
        if (self._current_game_id == IDLE_GAME_ID):
            break_game = True
        if (self._current_game_id != game_id):
            break_game = True
        if (dat.get('type', None) == 'gameState' and dat.get('status', None) != 'started'):
            break_game = True
        if (dat.get('type', None) == 'gameFull' and dat.get('state', {}).get('status', None) != 'started'):
            break_game = True
        if (dat.get('type', None) == 'opponentGone' and dat.get('gone', None) == True and dat.get('claimWinInSeconds', None) == 0):
            break_game = True
        
        return break_game

    def perform_api_call(self, new):

        try:
            json_data = json.loads(new)
        except json.JSONDecodeError as e:
            self.log(f"Invalid JSON in api_call: {e} payload={new!r}", level="WARNING")
            return

        call_type = json_data.get("type", None)

        self.log(f"API-call: " + json.dumps(json_data))

        if json_data and call_type:

            with self._lock:

                valid_token = self._token_main != IDLE_LICHESS_TOKEN and self._token_main is not None
                valid_game_id = self._current_game_id != IDLE_GAME_ID and self._current_game_id is not None
                
                if valid_token:

                    if call_type == "getAccountInfoMain":
                        json_response = lh.getAccountInfoMain(self._client_main, self_log=self.log)
                        self.publish_response(json_response)
                        return

                    if call_type == "abortRunningGames":
                        lh.abortRunningGames(self._client_main, self_log=self.log)
                        return

                    if call_type == "createGame":
                        json_response = lh.createGame(json_data, self._client_main, self._client_opponent, self_log=self.log)
                        self.publish_response(json_response)
                        return

                    if call_type == "withdrawTornament":
                        lh.withdrawTornament(json_data, self._client_main, self_log=self.log)
                        return

                    if call_type == "joinTournamentByName":
                        json_response = lh.joinTournamentByName(json_data, self._client_main, self_log=self.log)
                        self.publish_response(json_response)
                        return

                    if call_type == "joinTournamentById":
                        json_response = lh.joinTournamentById(json_data, self._client_main, self_log=self.log)
                        self.publish_response(json_response)
                        return
                
                if valid_token and valid_game_id:

                    if call_type == "abort":
                        lh.abort(self._client_main, self._current_game_id, self_log=self.log)
                        return
                    
                    if call_type == "resign":
                        lh.resign(self._client_main, self._current_game_id, self_log=self.log)
                        return
                    
                    if call_type == "claim-victory":
                        lh.claimVictory(self._client_main, self._current_game_id, self_log=self.log)
                        return  
                                        
                    if call_type == "makeMove":
                        lh.makeMove(json_data, self._client_main, self._current_game_id, self_log=self.log)
                        return
                        
                    if call_type == "draw":
                        lh.draw(json_data, self._client_main, self._current_game_id, self_log=self.log)
                        return 
                    
                    if call_type == "takeback":
                        lh.takeback(json_data, self._client_main, self._current_game_id, self_log=self.log)
                        return 
                    
                    if call_type == "writeChatMessage":
                        lh.writeChatMessage(json_data, self._client_main, self._current_game_id, self_log=self.log)
                        return 
                    
                    if call_type == "makeMoveOpponent":
                        lh.makeMoveOpponent(json_data, self._client_opponent, self._current_game_id, self_log=self.log)
                        return 
                    
                    if call_type == "resignOpponent":
                        lh.resignOpponent(json_data, self._client_opponent, self._current_game_id, self_log=self.log)
                        return 
                    
                    if call_type == "drawOpponent":
                        lh.drawOpponent(json_data, self._client_opponent, self._current_game_id, self_log=self.log)
                        return

    def handle_incoming_events(self, token_init=IDLE_LICHESS_TOKEN):
        
        if token_init and token_init != IDLE_LICHESS_TOKEN:

            self.log(f"Starting the stream (event): {token_init}")

            # open the stream for whole chess game
            for event in self._client_main.board.stream_incoming_events():
                if event:                    
                    reduced_data = json.dumps(lh.reduce_response_event(event))
                    self.publish_response(reduced_data)
                    self.log(f"Event: {reduced_data}")
                if self._stop_event_stream.is_set():
                    self.log(f"Stream stop event set, terminating the stream (event): {token_init}")
                    break
                with  self._lock:
                    if token_init != self._token_main:
                        self.log(f"Terminating the event stream (token changed): {token_init}->{self._token_main}")
                        # close the stream
                        break

        else:
            self.log(f"Waiting for new stream (event)")

    def handle_api_calls(self):
        self.log(f"Starting main-loop at LichessApiHandler")
        while True:
            # get next item from the queue
            try:
                item = self._api_q.get(timeout=1)
            except queue.Empty:
                continue

            # check for stop event
            if self._stop_event_api.is_set():
                self.log(f"Terminating main-loop at LichessApiHandler due to stop event")
                break

            # check for sentinel
            if item == "terminate_api_worker":
                self.log(f"Terminating main-loop at LichessApiHandler due to sentinel")
                break

            # handle the API call
            try:
                if item:
                    self.perform_api_call(item)
                else:
                    self.log(f"Empty API call received at LichessApiHandler", level="WARNING")
            except Exception as e:
                self.log(f"API call error at LichessApiHandler: {e}", level="ERROR")
            finally:
                self._api_q.task_done()
        self.log(f"Terminated main-loop at LichessApiHandler")


    def handle_board_stream_main(self, game_id):
        with  self._lock:
            valid_game_id = game_id != IDLE_GAME_ID
            valid_token =   self._token_main != IDLE_LICHESS_TOKEN
        if (valid_game_id and valid_token):            
            self.log(f"Starting the board stream (main): {game_id}")
            for line in self._client_main.board.stream_game_state(game_id):
                if line: # valid dic 
                    reduced_data = json.dumps(lh.reduce_response_board(game_id, line))
                    # let ha know about the move
                    self.publish_response(reduced_data)
                    self.log(f"Board (main): {reduced_data}")

                    with  self._lock:    
                        # check if we have to abort the game                    
                        if self.check_game_over(line, game_id):
                            self.log(f"Terminating the board stream (main): {game_id}")
                            # close the stream
                            break
                if self._stop_event_board.is_set():
                    self.log(f"Board stream stop event set, terminating the board stream (main): {game_id}")
                    break
            off_json = {
                        "type": "streamBoardResponse",
                        "state": IDLE_GAME_ID
                    }
            off_json_str = json.dumps(off_json)
            self.publish_response(off_json_str)
            # we are ready to go
            self.log(f"Waiting for new board stream (main)")



    def handle_board_stream_opponent(self, game_id):
        with  self._lock:
            # do nothing, just keep stream alive
            valid_game_id = game_id != IDLE_GAME_ID
            valid_token =   self._token_opponent != IDLE_LICHESS_TOKEN
        if (valid_game_id and valid_token):
            for line in self._client_opponent.board.stream_game_state(game_id):
                if line: # valid dic
                    self.log(f"Board (opponent): {line}")

                    # check if we have to abort the game
                    with  self._lock:
                        if self.check_game_over(line, game_id):
                            self.log(f"Terminating the board stream (opponent): {game_id}")
                            # close the stream
                            break
                if self._stop_event_board.is_set():
                    self.log(f"Board stream stop event set, terminating the board stream (opponent): {game_id}")
                    break
            # reset stream for board on HA (esphome needs to do it as well)
            off_json = {
                        "type": "streamBoardResponseOpponent",
                        "state": IDLE_GAME_ID
                    }
            off_json_str = json.dumps(off_json)
            self.publish_response(off_json_str)
            # we are ready to go
            self.log(f"Waiting for new board stream (opponent)")