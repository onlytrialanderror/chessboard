import appdaemon.plugins.hass.hassapi as hass

import lichess_helpers as lh
from lichess_components import (LichessClientPool, MqttGateway, LichessWorkerBoard, LichessWorkerEvent, LichessWorkerApi)

import json
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
        self._game_id_init = IDLE_GAME_ID

        self._worker_board_main = LichessWorkerBoard(main_worker = True, idle_token = IDLE_LICHESS_TOKEN, idle_game_id = IDLE_GAME_ID, log = self.log)
        self._worker_board_opponent = LichessWorkerBoard(main_worker = False, idle_token = IDLE_LICHESS_TOKEN, idle_game_id = IDLE_GAME_ID, log = self.log)
        self._worker_event = LichessWorkerEvent(idle_token = IDLE_LICHESS_TOKEN, log = self.log)
        self._worker_api = LichessWorkerApi(idle_token = IDLE_LICHESS_TOKEN, idle_game_id = IDLE_GAME_ID, log = self.log)

        self._worker_board_main.register_publish_function(self.publish_response)
        self._worker_board_opponent.register_publish_function(self.publish_response)
        self._worker_event.register_publish_function(self.publish_response)
        self._worker_api.register_publish_function(self.publish_response)

        

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
            self._worker_api.stop_worker()
            self._worker_event.stop_worker()
            self._worker_board_main.stop_worker()
            self._worker_board_opponent.stop_worker()
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
        self._worker_api.perform_api_call(payload)

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
        self._worker_event.stop_worker()
        self._worker_board_main.stop_worker() 

        # update token
        self._token_main = new_token    

        # start new session/client if not idle
        if new_token != IDLE_LICHESS_TOKEN:
            self._clients.set_token(CLIENT_NAME_API_MAIN, new_token, idle_token=IDLE_LICHESS_TOKEN)
            self._clients.set_token(CLIENT_NAME_EVENT, new_token, idle_token=IDLE_LICHESS_TOKEN)
            self._clients.set_token(CLIENT_NAME_BOARD_MAIN, new_token, idle_token=IDLE_LICHESS_TOKEN)
            self._worker_api.update_lichess_client_main(self._clients.get(CLIENT_NAME_API_MAIN), new_token)
            self._worker_event.update_lichess_client(self._clients.get(CLIENT_NAME_EVENT), new_token) 
            self._worker_event.run_worker()
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
        self._worker_board_opponent.stop_worker()

        # update token
        self._token_opponent = new_token

        # start new session/client if not idle
        if new_token != IDLE_LICHESS_TOKEN:
            self._clients.set_token(CLIENT_NAME_API_OPPONENT, new_token, idle_token=IDLE_LICHESS_TOKEN)
            self._clients.set_token(CLIENT_NAME_BOARD_OPPONENT, new_token, idle_token=IDLE_LICHESS_TOKEN)
            self._worker_api.update_lichess_client_opponent(self._clients.get(CLIENT_NAME_API_OPPONENT), new_token)
            self._worker_board_opponent.update_lichess_client(self._clients.get(CLIENT_NAME_BOARD_OPPONENT), new_token)

    def _on_mqtt_game_id(self, payload: str) -> None:

        # check if game id really changed
        if payload == self._current_game_id:
            return
        
        self.log(f"Game ID changed in LichessApiHandler: {self._current_game_id} -> {payload}")
       
        # update game id
        self._current_game_id = payload
        # start new board stream 
        self._worker_api.update_game_id(payload)       
        self._worker_board_main.update_game_id(payload) 
        self._worker_board_opponent.update_game_id(payload) 
        self._worker_board_main.run_worker()
        self._worker_board_opponent.run_worker()

    def _on_mqtt_chessboard_status(self, payload: str) -> None:
        # chessboard is online
        if payload == STATUS_ONLINE:
            self.log(f"Chessboard is online")
            self._init_sessions_all()
            self._worker_api.run_worker()
            self._worker_event.run_worker()

        # chessboard is offline
        elif payload == STATUS_OFFLINE:
            self.log(f"Chessboard is offline in LichessApiHandler")
            if self.is_any_thread_alive():
                self.log(f"At least one thread is alive, stopping ...")
                self._clients.close_all()
                self._worker_api.stop_worker()
                self._worker_event.stop_worker()
                self._worker_board_main.stop_worker()
                self._worker_board_opponent.stop_worker()
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
    ############## ---------- HANDLER FUNCTIONS ---------  ##############
    #####################################################################
 

    def is_any_thread_alive(self):
        return  bool(
            self._worker_api.is_any_thread_alive() or
            self._worker_board_main.is_any_thread_alive() or
            self._worker_board_opponent.is_any_thread_alive() or
            self._worker_event.is_any_thread_alive()
        )
