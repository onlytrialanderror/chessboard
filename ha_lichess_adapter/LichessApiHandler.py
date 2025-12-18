import appdaemon.plugins.hass.hassapi as hass
import appdaemon.plugins.mqtt.mqttapi as mqtt

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

MQTT_NAMESPACE = "mqtt" 

CLASS_NAME = "LichessApiHandler"
SECRET_PATH = "/config/secrets.yaml"


class LichessApiHandler(hass.Hass, mqtt.Mqtt):


    _client_main: Optional[berserk.Client] = None 
    _client_opponent: Optional[berserk.Client] = None 
    _current_secret_key = None


    def initialize(self):
        self.log(f"AppDaemon {CLASS_NAME} script initialized!")
        self._current_secret_key = lh.get_secret("chessboard_secret_key", SECRET_PATH)

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

        # --- Subscriptions ---
        # API calls in
        self.mqtt_subscribe(MQTT_API_CALL_TOPIC, namespace=MQTT_NAMESPACE)
        self.listen_event(self._on_mqtt_api_call, "MQTT_MESSAGE",
                          topic=MQTT_API_CALL_TOPIC, namespace=MQTT_NAMESPACE)

        # Game id in
        self.mqtt_subscribe(MQTT_GAME_ID_TOPIC, namespace=MQTT_NAMESPACE)
        self.listen_event(self._on_mqtt_game_id, "MQTT_MESSAGE",
                          topic=MQTT_GAME_ID_TOPIC, namespace=MQTT_NAMESPACE)

        # Tokens in
        self.mqtt_subscribe(MQTT_TOKEN_MAIN_TOPIC, namespace=MQTT_NAMESPACE)
        self.listen_event(self._on_mqtt_token_main, "MQTT_MESSAGE",
                          topic=MQTT_TOKEN_MAIN_TOPIC, namespace=MQTT_NAMESPACE)

        self.mqtt_subscribe(MQTT_TOKEN_OPP_TOPIC, namespace=MQTT_NAMESPACE)
        self.listen_event(self._on_mqtt_token_opponent, "MQTT_MESSAGE",
                          topic=MQTT_TOKEN_OPP_TOPIC, namespace=MQTT_NAMESPACE)
        
        # Status in
        self.mqtt_subscribe(MQTT_STATUS_TOPIC, namespace=MQTT_NAMESPACE)
        self.listen_event(self._on_mqtt_status, "MQTT_MESSAGE",
                          topic=MQTT_STATUS_TOPIC, namespace=MQTT_NAMESPACE)

        self.log(f"MQTT ready {CLASS_NAME}. api_call='{MQTT_API_CALL_TOPIC}', response='{MQTT_RESPONSE_TOPIC}'")
        self.log(f"MQTT inputs {CLASS_NAME}: game_id='{MQTT_GAME_ID_TOPIC}', token_main='{MQTT_TOKEN_MAIN_TOPIC}', token_opponent='{MQTT_TOKEN_OPP_TOPIC}'")
        self.log(f"Initialization complete for {CLASS_NAME}")

        

    # ---------- MQTT helpers ----------

    def publish_response(self, payload: str):
        """Publish response JSON string to chessboard/response."""
        try:
            self.mqtt_publish(MQTT_RESPONSE_TOPIC, payload=payload, retain=False, qos=0, namespace=MQTT_NAMESPACE)
        except Exception as e:
            self.log(f"Failed to publish MQTT response in {CLASS_NAME}: {e}")

    # ---------- MQTT handlers ----------

    def _on_mqtt_api_call(self, event_name, data, kwargs):
        payload = lh.payload_to_str(data.get("payload"))
        if payload is None:
            self.log(f"MQTT api_call received without payload in {CLASS_NAME}")
            return

        if payload not in {IDLE_GAME_ID}:
            """Enqueue an API call payload to be handled by a single worker thread.
            This avoids blocking AppDaemon's event threads and prevents concurrent use of shared clients/state.
            """
            self._api_q.put(payload)

    def _on_mqtt_game_id(self, event_name, data, kwargs):
        payload = lh.payload_to_str(data.get("payload"))
        if payload is None:
            return

        self.game_id_changed(payload)

    def _on_mqtt_token_main(self, event_name, data, kwargs):
        payload = lh.payload_to_str(data.get("payload"))
        if payload is None:
            return
        # token_changed_main() already handles unavailable/unknown/empty checks + decrypt
        self.token_changed_main(payload)

    def _on_mqtt_token_opponent(self, event_name, data, kwargs):
        payload = lh.payload_to_str(data.get("payload"))
        if payload is None:
            return
        # token_changed_opponent() already handles unavailable/unknown/empty checks + decrypt
        self.token_changed_opponent(payload)

    def _on_mqtt_status(self, event_name, data, kwargs):
        payload = lh.payload_to_str(data.get("payload"))
        if payload is None or payload in {STATUS_OFFLINE}:
            self.log(f"Chessboard is oflline at {CLASS_NAME}, stopping API worker")            
            self._stop_api_worker()
            self._stop_stream_worker()
            self._stop_board_worker()
        else:
            self.log(f"Chessboard is online at {CLASS_NAME}, starting API worker")
            self._run_api_worker()
            self._run_stream_worker()
            self._run_board_worker()

    def _run_api_worker(self):
        # check if api thread is already running
        if self._api_worker and self._api_worker.is_alive():
            return
        self._stop_event_api.clear()

        self.log(f"API worker starting at {CLASS_NAME}") 
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
            self.log(f"Stopping API worker at {CLASS_NAME} failed")
        else:
            self.log(f"API worker stopped at {CLASS_NAME}")

    def _run_stream_worker(self):
        if self._token_main !=  IDLE_LICHESS_TOKEN:
            self._stop_event_stream.clear()
            self.log(f"Stream worker started at {CLASS_NAME}") 
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
            self.log(f"Stopping Stream worker at {CLASS_NAME}")
            # we reset the token to idle to stop the stream
            self._token_main =  IDLE_LICHESS_TOKEN
            # wait stream thread is finished
            self._stream_worker.join(timeout=2) 
            # check if stream thread is stopped           
            if self._stream_worker and self._stream_worker.is_alive():
                self.log(f"Stopping Stream worker at {CLASS_NAME} failed")
            else:
                self.log(f"Stream worker stopped at {CLASS_NAME}")

    def _run_board_worker(self):        
        current_game_id = self._current_game_id
        if current_game_id != IDLE_GAME_ID:
            self._stop_event_board.clear()
            self.log(f"Board {current_game_id }: (main) worker starting at {CLASS_NAME}") 
            # start board worker thread (main)
            self._board_worker_main = threading.Thread(
                target=self.handle_board_stream_main,
                args=(current_game_id ,),
                daemon=True
            )
            self.log(f"Board {current_game_id }: (opponent) worker starting at {CLASS_NAME}") 
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

            self.log(f"Main board worker stopping at {CLASS_NAME}")
            # wait main board thread is finished
            self._board_worker_main.join(timeout=1)

            # check if main thread is stopped
            if self._board_worker_main and self._board_worker_main.is_alive():
                self.log(f"Stopping board worker (main) at {CLASS_NAME} failed")
            else:
                self.log(f"Board worker (main) stopped at {CLASS_NAME}")

        # stop opponent board worker
        if self._board_worker_opponent is not None and self._board_worker_opponent.is_alive():

            self.log(f"Opponent board worker stopping at {CLASS_NAME}")
            # wait opponent board thread is finished
            self._board_worker_opponent.join(timeout=1)
        
            # check if opponent thread is stopped
            if self._board_worker_opponent and self._board_worker_opponent.is_alive():
                self.log(f"Stopping board worker (opponent) at {CLASS_NAME} failed")
            else:
                self.log(f"Board worker (opponent) stopped at {CLASS_NAME}")

    def game_id_changed(self, new):

        # check if game id really changed
        if not new or new == self._current_game_id:
            return
        
        self.log(f"Game ID changed in {CLASS_NAME}: {self._current_game_id} -> {new}")

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

        self.log(f"Token changed (main) in {CLASS_NAME}: {self._token_main} -> {new_decrypted}")
        
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

        self.log(f"Token changed (opponent) in {CLASS_NAME}: {self._token_opponent} -> {new_decrypted}")

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
        self.log(f"Starting main-loop at {CLASS_NAME}")
        while True:
            # get next item from the queue
            try:
                item = self._api_q.get(timeout=1)
            except queue.Empty:
                continue

            # check for stop event
            if self._stop_event_api.is_set():
                self.log(f"Terminating main-loop at {CLASS_NAME} due to stop event")
                break

            # check for sentinel
            if item == "terminate_api_worker":
                self.log(f"Terminating main-loop at {CLASS_NAME} due to sentinel")
                break

            # handle the API call
            try:
                if item:
                    self.perform_api_call(item)
                else:
                    self.log(f"Empty API call received at {CLASS_NAME}", level="WARNING")
            except Exception as e:
                self.log(f"API call error at {CLASS_NAME}: {e}", level="ERROR")
            finally:
                self._api_q.task_done()
        self.log(f"Terminated main-loop at {CLASS_NAME}")


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