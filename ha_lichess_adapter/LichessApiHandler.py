import appdaemon.plugins.hass.hassapi as hass

import lichess_helpers as lh
from lichess_components import (LichessClientPool, MqttGateway, LichessWorkerBoard)

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
MQTT_API_CALL_TOPIC = "chessboard/api_call"
MQTT_GAME_ID_TOPIC = "chessboard/game_id"
MQTT_TOKEN_MAIN_TOPIC = "chessboard/token_main"
MQTT_TOKEN_OPP_TOPIC = "chessboard/token_opponent"
MQTT_STATUS_TOPIC = "chessboard/status"
MQTT_RESPONSE_TOPIC = "chessboard/response"

SECRET_PATH = "/config/secrets.yaml"

CLIENT_NAME_API_MAIN = "client_api_main"
CLIENT_NAME_API_OPPONENT = "client_api_opponent"
CLIENT_NAME_EVENT = "client_event"
CLIENT_NAME_BOARD_MAIN = "client_board_main"
CLIENT_NAME_BOARD_OPPONENT = "client_board_opponent"


class LichessApiHandler(hass.Hass):
     
    # store the encryption secret key
    _current_secret_key = None

    def initialize(self):
        self.log(f"AppDaemon LichessApiHandler script initialized!")
                       
        # ---- Load secrets (local file) ----
        self.secrets = lh.load_secrets(SECRET_PATH)

        # ---- Chess secret key ----
        self._current_secret_key = self.secrets.get("chessboard_secret_key", None)
        if not self._current_secret_key:
            raise RuntimeError("Missing 'chessboard_secret_key' in {}.".format(SECRET_PATH))

        # ---- MQTT config (from secrets.yaml) ----
        self._mqtt = MqttGateway(self.secrets, self.log)

        # ---- MQTT topic routing ----
        self._mqtt.register_handler(MQTT_API_CALL_TOPIC, self._on_mqtt_api_call)
        self._mqtt.register_handler(MQTT_GAME_ID_TOPIC, self._on_mqtt_game_id)
        self._mqtt.register_handler(MQTT_TOKEN_MAIN_TOPIC, self._on_mqtt_token_main)
        self._mqtt.register_handler(MQTT_TOKEN_OPP_TOPIC, self._on_mqtt_token_opponent)
        self._mqtt.register_handler(MQTT_STATUS_TOPIC, self._on_mqtt_chessboard_status)

        # current runtime state (protected by self._lock where needed)
        self._current_game_id = IDLE_GAME_ID
        self._token_main = IDLE_LICHESS_TOKEN
        self._token_opponent = IDLE_LICHESS_TOKEN

        # Keep references to sessions so we can close them on token change
        # lichess (berserk) clients/sessions
        self._clients = LichessClientPool(log=self.log)

        self._lichess_api_init_value = IDLE_LICHESS_TOKEN
        self._lichess_stream_event_init_value = IDLE_LICHESS_TOKEN
        self._game_id_init = IDLE_GAME_ID

        # single-threaded API worker to serialize Lichess calls
        self._api_q = queue.Queue()  
        self._api_worker: Optional[threading.Thread] = None   
        self._stream_worker: Optional[threading.Thread] = None
        self._lock = threading.Lock()   
        self._stop_event_api = threading.Event()
        self._stop_event_stream = threading.Event()

        self._worker_board_main = LichessWorkerBoard(main_worker = True, idle_token = IDLE_LICHESS_TOKEN, idle_game_id = IDLE_GAME_ID, log = self.log)
        self._worker_board_opponent = LichessWorkerBoard(main_worker = False, idle_token = IDLE_LICHESS_TOKEN, idle_game_id = IDLE_GAME_ID, log = self.log)

        self._worker_board_main.register_publish_function(self.publish_response)
        self._worker_board_opponent.register_publish_function(self.publish_response)

        # ---- Initialize MQTT client ----
        self._mqtt.connect_and_loop(
            (
                (MQTT_API_CALL_TOPIC, 0),
                (MQTT_GAME_ID_TOPIC, 0),
                (MQTT_TOKEN_MAIN_TOPIC, 0),
                (MQTT_TOKEN_OPP_TOPIC, 0),
                (MQTT_STATUS_TOPIC, 0),
            )
        )

        self.log(f"Initialization complete for LichessApiHandler")

    def terminate(self):
        """Called by AppDaemon on shutdown/reload."""
        try:
            self._stop_all_workers()
        except Exception:
            pass

        try:
            self._clients.close_all()
        except Exception:
            pass

        try:
            if hasattr(self, "_mqtt") and self._mqtt:
                self._mqtt.stop()
        except Exception:
            pass


    #####################################################################
    ############## ---------- MQTT handlers ----------  #################
    #####################################################################

    # ------------------------------------------------------------------
    # MQTT publisher helpers
    # ------------------------------------------------------------------

    def publish_response(self, payload: str) -> None:
        self._mqtt.publish(MQTT_RESPONSE_TOPIC, payload)

    def clear_topics(self) -> None:
        self._mqtt.publish(MQTT_API_CALL_TOPIC, IDLE_MQTT_STATE)
        self._mqtt.publish(MQTT_GAME_ID_TOPIC, IDLE_MQTT_STATE)
        self._mqtt.publish(MQTT_TOKEN_MAIN_TOPIC, IDLE_MQTT_STATE)
        self._mqtt.publish(MQTT_TOKEN_OPP_TOPIC, IDLE_MQTT_STATE)
        self._mqtt.publish(MQTT_STATUS_TOPIC, IDLE_MQTT_STATE)
        self._mqtt.publish(MQTT_RESPONSE_TOPIC, IDLE_MQTT_STATE)
     

    def _on_mqtt_api_call(self, payload: str) -> None:

        if payload != IDLE_GAME_ID:
            """Enqueue an API call payload to be handled by a single worker thread.
            This avoids blocking AppDaemon's event threads and prevents concurrent use of shared clients/state.
            """
            self._api_q.put(payload)

    def _on_mqtt_token_main(self, payload: str) -> None:

        # get decrypted token
        new_token = lh.decrypt_message(self._current_secret_key, payload)

        # check if token really changed
        if new_token == self._token_main:
            return

        self.log(f"Token changed (main) in LichessApiHandler: {self._token_main} -> {new_token}")

        # Close sessions/clients dependent on main token
        self._clients.close(CLIENT_NAME_API_MAIN)
        self._clients.close(CLIENT_NAME_EVENT)
        self._clients.close(CLIENT_NAME_BOARD_MAIN)

        # trying to stop all running workers with previous token
        self._stop_api_worker()
        self._stop_stream_worker()

        # update token
        self._token_main = new_token    

        # start new session/client if not idle
        if new_token != IDLE_LICHESS_TOKEN:
            self._clients.set_token(CLIENT_NAME_API_MAIN, new_token, idle_token=IDLE_LICHESS_TOKEN)
            self._clients.set_token(CLIENT_NAME_EVENT, new_token, idle_token=IDLE_LICHESS_TOKEN)
            self._clients.set_token(CLIENT_NAME_BOARD_MAIN, new_token, idle_token=IDLE_LICHESS_TOKEN)
            self._run_api_worker()
            self._run_stream_worker()
            self._worker_board_main.update_lichess_client(self._clients.get(CLIENT_NAME_BOARD_MAIN), new_token)            
            

    def _on_mqtt_token_opponent(self, payload: str) -> None:
        # get decrypted token
        new_token = lh.decrypt_message(self._current_secret_key, payload)

        # check if token really changed
        if new_token == self._token_opponent:
            return

        self.log(f"Token changed (opponent) in LichessApiHandler: {self._token_opponent} -> {new_token}")

        # trying to stop all running workers with previous token
        self._clients.close(CLIENT_NAME_API_OPPONENT)
        self._clients.close(CLIENT_NAME_BOARD_OPPONENT)
        self._stop_board_worker_opponent()

        # update token
        self._token_opponent = new_token

        # start new session/client if not idle
        if new_token != IDLE_LICHESS_TOKEN:
            self._clients.set_token(CLIENT_NAME_API_OPPONENT, new_token, idle_token=IDLE_LICHESS_TOKEN)
            self._clients.set_token(CLIENT_NAME_BOARD_OPPONENT, new_token, idle_token=IDLE_LICHESS_TOKEN)
            self._worker_board_main.update_lichess_client(self._clients.get(CLIENT_NAME_BOARD_OPPONENT), new_token)

    def _on_mqtt_game_id(self, payload: str) -> None:

        # check if game id really changed
        if payload == self._current_game_id:
            return
        
        self.log(f"Game ID changed in LichessApiHandler: {self._current_game_id} -> {payload}")
       
        # update game id
        self._current_game_id = payload
        # start new board stream        
        self._worker_board_main.update_game_id(payload) 
        self._worker_board_opponent.update_game_id(payload) 

    def _on_mqtt_chessboard_status(self, payload: str) -> None:
        # chessboard is online
        if payload == STATUS_ONLINE:
            self.log(f"Chessboard is online")
            self._init_sessions_all()
            self._run_api_worker()
            self._run_stream_worker()

        # chessboard is offline
        elif payload == STATUS_OFFLINE:
            self.log(f"Chessboard is offline in LichessApiHandler")
            if self.is_any_thread_alive():
                self.log(f"At least one thread is alive, stopping ...")
                self._clients.close_all()
                self._stop_api_worker()
                self._stop_stream_worker()
                self._stop_board_workers()
            else:
                self.log(f"No threads running")            
            self.clear_topics()

    #####################################################################
    ######### ---------- INIT AND CLOSE BERSERK -------------  ##########
    ##################################################################### 

    def _init_sessions_all(self):
        self._clients.set_token(CLIENT_NAME_API_MAIN, self._token_main, idle_token=IDLE_LICHESS_TOKEN)
        self._clients.set_token(CLIENT_NAME_EVENT, self._token_main, idle_token=IDLE_LICHESS_TOKEN)
        self._clients.set_token(CLIENT_NAME_API_OPPONENT, self._token_opponent, idle_token=IDLE_LICHESS_TOKEN)
        self._clients.set_token(CLIENT_NAME_BOARD_MAIN, self._token_main, idle_token=IDLE_LICHESS_TOKEN)
        self._clients.set_token(CLIENT_NAME_BOARD_OPPONENT, self._token_opponent, idle_token=IDLE_LICHESS_TOKEN)


    #####################################################################
    ############## ---------- RUN WOKERS -------------  #################
    #####################################################################

    def _run_api_worker(self):
        # check if api thread is already running or token still not set, we skip starting
        if (self._lichess_api_init_value == self._token_main and self._lichess_api_init_value != IDLE_LICHESS_TOKEN and self._api_worker and self._api_worker.is_alive()) or self._token_main == IDLE_LICHESS_TOKEN:
            return
        
        self.log(f"API worker starting") 
        
        self._stop_event_api.clear()

        # start api worker thread
        self._api_worker = threading.Thread(
            target=self.handle_api_calls,
            args=(self._token_main,),
            daemon=True
        )
        self._api_worker.start()

    def _run_stream_worker(self):

        # check if api thread is already running or token still not set, we skip starting
        if (self._lichess_stream_event_init_value == self._token_main and self._lichess_stream_event_init_value != IDLE_LICHESS_TOKEN and self._stream_worker and self._stream_worker.is_alive()) or self._token_main == IDLE_LICHESS_TOKEN:
            return
        
        self._stop_event_stream.clear()

        self.log(f"Stream worker started") 
        # start stream worker thread
        self._stream_worker = threading.Thread(
            target=self.handle_incoming_events,
            args=(self._token_main,),
            daemon=True
        )
        self._stream_worker.start()

    def _run_all_workers(self):
        self._run_api_worker()
        self._run_stream_worker()
   
    #####################################################################
    ############## ---------- STOP WOKERS ------------  #################
    #####################################################################

    def _stop_api_worker(self):

        if self._api_worker and self._api_worker.is_alive():
            self.log(f"API main worker is running, trying to stop")
            # sentinel shutdown
            self._api_q.put("terminate_api_worker")
            # thread shutdown
            self._stop_event_api.set()

            if self._api_worker and self._api_worker.is_alive():
                self._api_worker.join(timeout=1)

            # check if api thread is stopped
            if self._api_worker and self._api_worker.is_alive():
                self.log(f"Stopping API main worker failed", level="WARNING")
            else:
                self.log(f"API main worker stopped")

        # check if init variable is reseted
        if self._lichess_api_init_value != IDLE_LICHESS_TOKEN:
            self.log(f"Thread init-variable for API main worker is still set", level="WARNING")

    def _stop_stream_worker(self):    

        if self._stream_worker and self._stream_worker.is_alive():
            self.log(f"Stream event worker is running, trying to stop")
            # thread shutdown
            self._stop_event_stream.set()
            # we reset the token to idle to stop the stream
            self._token_main =  IDLE_LICHESS_TOKEN

            if self._stream_worker and self._stream_worker.is_alive():
                self._stream_worker.join(timeout=2)

            # check if stream thread is stopped           
            if self._stream_worker and self._stream_worker.is_alive():
                self.log(f"Stopping Stream event worker failed")
            else:
                self.log(f"Stream worker stopped")

        # check if init variable is reseted
        if self._lichess_stream_event_init_value != IDLE_LICHESS_TOKEN:
            self.log(f"Thread init-variable for stream event worker is still set", level="WARNING")


    def _stop_board_workers(self):
        self._worker_board_main._stop_board_worker()
        self._worker_board_opponent._stop_board_worker()

    def _stop_all_workers(self):
        self._stop_api_worker()
        self._stop_stream_worker()
        self._stop_board_workers()


    #####################################################################
    ############## ---------- HANDLER FUNCTIONS ---------  ##############
    #####################################################################
 
    def handle_incoming_events(self, token_init):
        
        self._lichess_stream_event_init_value = token_init

        self.log(f"Starting the stream (event): {token_init}")

        # open the stream for whole chess game        
        for event in self._clients.get(CLIENT_NAME_EVENT).board.stream_incoming_events():
            if event:                    
                reduced_data = json.dumps(lh.reduce_response_event(event))
                self.publish_response(reduced_data)
                self.log(f"Event: {reduced_data}")
            if self._stop_event_stream.is_set():
                self.log(f"Stream stop event set, terminating the stream (event): {self._lichess_stream_event_init_value}")
                break
            with  self._lock:
                if self._lichess_stream_event_init_value != self._token_main:
                    self.log(f"Terminating the event stream (token changed): {self._lichess_stream_event_init_value}->{self._token_main}")
                    # close the stream
                    break

        self.log(f"Terminated the stream (event): {self._lichess_stream_event_init_value}")            
        self._lichess_stream_event_init_value = IDLE_LICHESS_TOKEN


    def handle_api_calls(self, init_value):
        # init_value is token main
        with  self._lock:
            self._lichess_api_init_value = init_value
        self.log(f"Starting main-loop {self._lichess_api_init_value}")
        while True:
            # get next item from the queue
            try:
                item = self._api_q.get(timeout=1)
            except queue.Empty:
                continue

            # check for stop event
            if self._stop_event_api.is_set():
                self.log(f"Terminating main-loop {self._lichess_api_init_value} due to stop event")
                break

            # check for sentinel
            if item == "terminate_api_worker":
                self.log(f"Terminating main-loop {self._lichess_api_init_value} due to sentinel")
                break

            # check if token changed during processing
            with  self._lock:
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
        with  self._lock:
            self._lichess_api_init_value = IDLE_LICHESS_TOKEN
            
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

            with self._lock:

                valid_token = self._token_main != IDLE_LICHESS_TOKEN and self._clients.get(CLIENT_NAME_API_MAIN) is not None
                valid_token_opponent = (self._token_opponent == IDLE_LICHESS_TOKEN and self._clients.get(CLIENT_NAME_API_OPPONENT) is None) or (self._token_opponent != IDLE_LICHESS_TOKEN and self._clients.get(CLIENT_NAME_API_OPPONENT) is not None)
                valid_game_id = self._current_game_id != IDLE_GAME_ID
                
                if valid_token == True:

                    if call_type == "getAccountInfoMain":
                        json_response = lh.getAccountInfoMain(self._clients.get(CLIENT_NAME_API_MAIN), self_log=self.log)
                        self.publish_response(json_response)
                        return

                    if call_type == "abortRunningGames":
                        lh.abortRunningGames(self._clients.get(CLIENT_NAME_API_MAIN), self_log=self.log)
                        return

                    if call_type == "withdrawTornament":
                        lh.withdrawTornament(json_data, self._clients.get(CLIENT_NAME_API_MAIN), self_log=self.log)
                        return

                    if call_type == "joinTournamentByName":
                        json_response = lh.joinTournamentByName(json_data, self._clients.get(CLIENT_NAME_API_MAIN), self_log=self.log)
                        self.publish_response(json_response)
                        return

                    if call_type == "joinTournamentById":
                        json_response = lh.joinTournamentById(json_data, self._clients.get(CLIENT_NAME_API_MAIN), self_log=self.log)
                        self.publish_response(json_response)
                        return
                
                if valid_token == True and valid_token_opponent == True:

                    if call_type == "createGame" :
                        json_response = lh.createGame(json_data, self._clients.get(CLIENT_NAME_API_MAIN), self._clients.get(CLIENT_NAME_API_OPPONENT), self_log=self.log)
                        self.publish_response(json_response)
                        return
                
                if valid_token == True and valid_game_id == True:

                    if call_type == "abort":
                        lh.abort(self._clients.get(CLIENT_NAME_API_MAIN), self._current_game_id, self_log=self.log)
                        return
                    
                    if call_type == "resign":
                        lh.resign(self._clients.get(CLIENT_NAME_API_MAIN), self._current_game_id, self_log=self.log)
                        return
                    
                    if call_type == "claim-victory":
                        lh.claimVictory(self._clients.get(CLIENT_NAME_API_MAIN), self._current_game_id, self_log=self.log)
                        return  
                                        
                    if call_type == "makeMove":
                        lh.makeMove(json_data, self._clients.get(CLIENT_NAME_API_MAIN), self._current_game_id, self_log=self.log)
                        return
                        
                    if call_type == "draw":
                        lh.draw(json_data, self._clients.get(CLIENT_NAME_API_MAIN), self._current_game_id, self_log=self.log)
                        return 
                    
                    if call_type == "takeback":
                        lh.takeback(json_data, self._clients.get(CLIENT_NAME_API_MAIN), self._current_game_id, self_log=self.log)
                        return 
                    
                    if call_type == "writeChatMessage":
                        lh.writeChatMessage(json_data, self._clients.get(CLIENT_NAME_API_MAIN), self._current_game_id, self_log=self.log)
                        return 

                if valid_token_opponent == True and valid_game_id == True:

                    if call_type == "makeMoveOpponent":
                        lh.makeMoveOpponent(json_data, self._clients.get(CLIENT_NAME_API_OPPONENT), self._current_game_id, self_log=self.log)
                        return 
                    
                    if call_type == "resignOpponent":
                        lh.resignOpponent(json_data, self._clients.get(CLIENT_NAME_API_OPPONENT), self._current_game_id, self_log=self.log)
                        return 
                    
                    if call_type == "drawOpponent":
                        lh.drawOpponent(json_data, self._clients.get(CLIENT_NAME_API_OPPONENT), self._current_game_id, self_log=self.log)
                        return
                
                # we should return till that point    
                self.log(f"API-Call " + json.dumps(json_data) + " could not be performed!", level="WARNING")

    def is_any_thread_alive(self):
        return  bool((self._api_worker and self._api_worker.is_alive()) 
                     or (self._stream_worker and self._stream_worker.is_alive()) 
                     or (self._board_worker_main and self._board_worker_main.is_alive()) 
                     or (self._board_worker_opponent and self._board_worker_opponent.is_alive()))
