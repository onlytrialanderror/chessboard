import appdaemon.plugins.hass.hassapi as hass
import appdaemon.plugins.mqtt.mqttapi as mqtt

import lichess_helpers as lh

import berserk
import json
import threading
from datetime import datetime, timezone, timedelta
import queue

IDLE_GAME_ID = "idle"
UNAVAILABLE_STATE = "unavailable"
UNKNOWN_STATE = "unknown"
EMPTY_STRING = ""
IDLE_LICHESS_TOKEN = "idle"
ON_STATE = "ON"
OFF_STATE = "OFF"
IDLE_STATE = "idle"
STATUS_OFFLINE = "offline"
STATUS_ONLINE = "online"

# MQTT topics
MQTT_RESPONSE_TOPIC = "chessboard/response"

MQTT_GAME_ID_TOPIC = "chessboard/game_id"
MQTT_TOKEN_MAIN_TOPIC = "chessboard/token_main"
MQTT_TOKEN_OPP_TOPIC = "chessboard/token_opponent"

MQTT_STATUS_TOPIC = "chessboard/status"

MQTT_NAMESPACE = "mqtt" 

CLASS_NAME = "LichessStreamBoard"


class LichessStreamBoard(hass.Hass, mqtt.Mqtt):


    _client_main = EMPTY_STRING
    _client_opponent = EMPTY_STRING
    _current_secret_key = EMPTY_STRING


    def initialize(self):
        self.log(f"AppDaemon {CLASS_NAME} script initialized!")
        self._current_secret_key = lh.get_secret()

        # current runtime state (protected by self._lock where needed)
        self._current_game_id = IDLE_GAME_ID
        self._token_main = IDLE_LICHESS_TOKEN
        self._token_opponent = IDLE_LICHESS_TOKEN

        # Keep references to sessions so we can close them on token change
        self._session_main = None
        self._session_opponent = None

        # single-threaded API worker to serialize Lichess calls
        self._board_worker_main = None      
        self._board_worker_opponent = None  
        self._lock = threading.Lock()

        # --- Subscriptions ---

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

        self.log(f"MQTT ready {CLASS_NAME}. response='{MQTT_RESPONSE_TOPIC}'")
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

    def _on_mqtt_game_id(self, event_name, data, kwargs):
        payload = lh.payload_to_str(data.get("payload"))
        if payload is None:
            return

        # mimic old HA sensor behaviour: ignore unavailable/unknown/empty
        if payload in {UNAVAILABLE_STATE, UNKNOWN_STATE, EMPTY_STRING}:
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
            self.log(f"Chessboard is oflline at {CLASS_NAME}, stopping board worker")
            self._stop_board_worker()
        else:
            self.log(f"Chessboard is online at {CLASS_NAME}, starting board worker")
            self._run_board_worker()

    def _run_board_worker(self):
        current_game_id = self._current_game_id
        self.log(f"Board {current_game_id }: (main) worker starting at {CLASS_NAME}") 
        self._board_worker_main = threading.Thread(
            target=self.handle_game_state_main,
            args=(current_game_id ,),
            daemon=True
        )
        self.log(f"Board {current_game_id }: (opponent) worker starting at {CLASS_NAME}") 
        self._board_worker_opponent = threading.Thread(
            target=self.handle_game_state_opponent,
            args=(current_game_id ,),
            daemon=True
        )
        self._board_worker_main.start()
        self._board_worker_opponent.start()
        

    def _stop_board_worker(self):
        self._current_game_id = IDLE_GAME_ID
        self.log(f"Board worker stopping at {CLASS_NAME}") 
        if self._board_worker_main is not None:
            self._board_worker_main.join(timeout=1)
            self._board_worker_main = None
        if self._board_worker_opponent is not None:
            self._board_worker_opponent.join(timeout=1)
            self._board_worker_opponent = None
        self.log(f"Board worker stopped at {CLASS_NAME}")


    # ---------- Existing logic (unchanged) ----------

    def game_id_changed(self, new):
        if new is None:
            return
        if new in {UNAVAILABLE_STATE, UNKNOWN_STATE, EMPTY_STRING}:            
            return
        if not new or new == self._current_game_id:
            return

        old = self._current_game_id
        # overwrites current game id to stop existing stream
        self._stop_board_worker()
        self._current_game_id = new
        self.log(f"Game ID changed in {CLASS_NAME}: {old} -> {new}")
        
        self._run_board_worker()

    def token_changed_main(self, new):
        if not new or new in {UNAVAILABLE_STATE, UNKNOWN_STATE, EMPTY_STRING}:
            if new in {UNAVAILABLE_STATE, UNKNOWN_STATE}:
                self.log(f"Not allowed token (main) in {CLASS_NAME}: {new}")
            return

        new_decrypted = lh.decrypt_message(self._current_secret_key, new)

        if new_decrypted == self._token_main:
            return
        old = self._token_main
        self._token_main = new_decrypted

        self.log(f"Token changed (main) in {CLASS_NAME}: {old} -> {new_decrypted}")

        # Replace session/client
        try:
            if self._session_main is not None:
                self._session_main.close()
        except Exception:
            pass
        self._session_main = None

        if new_decrypted != IDLE_LICHESS_TOKEN:
            self._session_main = berserk.TokenSession(new_decrypted)
            self._client_main = berserk.Client(self._session_main)            
        else:
            self._client_main = EMPTY_STRING

    def token_changed_opponent(self, new):
        if not new or new in {UNAVAILABLE_STATE, UNKNOWN_STATE, EMPTY_STRING}:
            if new in {UNAVAILABLE_STATE, UNKNOWN_STATE}:
                self.log(f"Not allowed token (opponent) in {CLASS_NAME}: {new}")
            return

        new_decrypted = lh.decrypt_message(self._current_secret_key, new)

        if new_decrypted == self._token_opponent:
            return
        old = self._token_opponent
        self._token_opponent = new_decrypted

        self.log(f"Token changed (opponent) in {CLASS_NAME}: {old} -> {new_decrypted}")

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


    def handle_game_state_main(self, game_id):
        with  self._lock:
            valid_game_id = game_id != IDLE_GAME_ID and game_id != UNAVAILABLE_STATE and game_id != UNKNOWN_STATE
            valid_token =   self._token_main != IDLE_LICHESS_TOKEN and self._token_main != UNAVAILABLE_STATE and self._token_main != UNKNOWN_STATE
        if (valid_game_id and valid_token):            
            self.log(f"Starting the board stream (main): {game_id}")
            for line in self._client_main.board.stream_game_state(game_id):
                if line: # valid dic                    
                    with  self._lock:
                        reduced_data = json.dumps(lh.reduce_response_board(game_id, line))
                        # let ha know about the move
                        self.publish_response(reduced_data)
                        self.log(f"Board (main): {reduced_data}")
                        
                        # check if we have to abort the game                    
                        if self.check_game_over(line, game_id):
                            self.log(f"Terminating the board stream (main): {game_id}")
                            # close the stream
                            break
            off_json = {
                        "type": "streamBoardResponse",
                        "state": IDLE_GAME_ID
                    }
            off_json_str = json.dumps(off_json)
            with  self._lock:
                self.publish_response(off_json_str)
            # we are ready to go
            self.log(f"Waiting for new board stream (main)")



    def handle_game_state_opponent(self, game_id):
        with  self._lock:
            # do nothing, just keep stream alive
            valid_game_id = game_id != IDLE_GAME_ID and game_id != UNAVAILABLE_STATE and game_id != UNKNOWN_STATE
            valid_token =   self._token_opponent != IDLE_LICHESS_TOKEN and self._token_opponent != UNAVAILABLE_STATE and self._token_opponent != UNKNOWN_STATE
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
            # reset stream for board on HA (esphome needs to do it as well)
            off_json = {
                        "type": "streamBoardResponseOpponent",
                        "state": IDLE_GAME_ID
                    }
            off_json_str = json.dumps(off_json)
            with  self._lock:
                self.publish_response(off_json_str)
            # we are ready to go
            self.log(f"Waiting for new board stream (opponent)")

