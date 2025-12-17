import appdaemon.plugins.hass.hassapi as hass
import appdaemon.plugins.mqtt.mqttapi as mqtt

import lichess_helpers as lh

import berserk
import json
import threading


UNAVAILABLE_STATE = "unavailable"
UNKNOWN_STATE = "unknown"
EMPTY_STRING = ""
IDLE_LICHESS_TOKEN = "idle"
IDLE_STATE = "idle"
STATUS_OFFLINE = "offline"
STATUS_ONLINE = "online"

# MQTT topics
MQTT_RESPONSE_TOPIC = "chessboard/response"
MQTT_TOKEN_MAIN_TOPIC = "chessboard/token_main"
MQTT_STATUS_TOPIC = "chessboard/status"

MQTT_NAMESPACE = "mqtt" 

CLASS_NAME = "LichessStreamEvent"


class LichessStreamEvent(hass.Hass, mqtt.Mqtt):


    _client_main = EMPTY_STRING
    _current_secret_key = EMPTY_STRING


    def initialize(self):
        self.log(f"AppDaemon {CLASS_NAME} script initialized!")
        self._current_secret_key = lh.get_secret("chessboard_secret_key")

        # current runtime state (protected by self._lock where needed)
        self._token_main = IDLE_LICHESS_TOKEN

        # Keep references to sessions so we can close them on token change
        self._session_main = None

        # single-threaded API worker to serialize Lichess calls
        self._stream_worker = None      

        # --- Subscriptions ---

        # Tokens in
        self.mqtt_subscribe(MQTT_TOKEN_MAIN_TOPIC, namespace=MQTT_NAMESPACE)
        self.listen_event(self._on_mqtt_token_main, "MQTT_MESSAGE",
                          topic=MQTT_TOKEN_MAIN_TOPIC, namespace=MQTT_NAMESPACE)


        # Status in
        self.mqtt_subscribe(MQTT_STATUS_TOPIC, namespace=MQTT_NAMESPACE)
        self.listen_event(self._on_mqtt_status, "MQTT_MESSAGE",
                          topic=MQTT_STATUS_TOPIC, namespace=MQTT_NAMESPACE)

        self.log(f"MQTT ready {CLASS_NAME}. response='{MQTT_RESPONSE_TOPIC}'")
        self.log(f"MQTT inputs {CLASS_NAME}: , token_main='{MQTT_TOKEN_MAIN_TOPIC}'")
        self.log(f"Initialization complete for {CLASS_NAME}")

        

    # ---------- MQTT helpers ----------

    def publish_response(self, payload: str):
        """Publish response JSON string to chessboard/response."""
        try:
            self.mqtt_publish(MQTT_RESPONSE_TOPIC, payload=payload, retain=False, qos=0, namespace=MQTT_NAMESPACE)
        except Exception as e:
            self.log(f"Failed to publish MQTT response in {CLASS_NAME}: {e}")

    # ---------- MQTT handlers ----------


    def _on_mqtt_token_main(self, event_name, data, kwargs):
        payload = lh.payload_to_str(data.get("payload"))
        if payload is None:
            return
        # token_changed_main() already handles unavailable/unknown/empty checks + decrypt
        self.token_changed_main(payload)

    def _on_mqtt_status(self, event_name, data, kwargs):
        payload = lh.payload_to_str(data.get("payload"))
        if payload is None or payload in {STATUS_OFFLINE}:
            self.log(f"Chessboard is oflline at {CLASS_NAME}, stopping Stream worker")
            self._stop_stream_worker()
        else:
            self.log(f"Chessboard is online at {CLASS_NAME}, starting Stream worker")
            self._run_stream_worker()

    def _run_stream_worker(self):
        if self._token_main !=  IDLE_LICHESS_TOKEN:
            self.log(f"Stream worker started at {CLASS_NAME}") 
            self._stream_worker = threading.Thread(
                target=self.handle_incoming_events,
                args=(self._token_main,),
                daemon=True
            )
            self._stream_worker.start()

    def _stop_stream_worker(self):
        if self._stream_worker and self._stream_worker.is_alive():
            self.log(f"Stopping Stream worker at {CLASS_NAME}")
            # we reset the token to idle to stop the stream
            self._token_main =  IDLE_LICHESS_TOKEN
            self._stream_worker.join()
            self.log(f"Stream worker stopped at {CLASS_NAME}")

    # ---------- Existing logic (unchanged) ----------

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
            self._run_stream_worker()         
        else:
            self._client_main = EMPTY_STRING
            self._stop_stream_worker()


    
    def handle_incoming_events(self, token_init=IDLE_LICHESS_TOKEN):
        
        if (token_init and token_init not in (IDLE_LICHESS_TOKEN, UNAVAILABLE_STATE, UNKNOWN_STATE)):

            self.log(f"Starting the stream (event): {token_init}")

            # open the stream for whole chess game
            for event in self._client_main.board.stream_incoming_events():
                if event:                    
                    reduced_data = json.dumps(lh.reduce_response_event(event))
                    self.publish_response(reduced_data)
                    self.log(f"Event: {reduced_data}")
                    
                    # check if we have to abort the game
                    if token_init != self._token_main:
                        self.log(f"Terminating the stream (event): {token_init}")
                        # close the stream
                        break
                else:
                    if token_init != self._token_main:
                        self.log(f"Terminating the stream (no event): {token_init}")
                        # close the stream
                        break

        else:
            self.log(f"Waiting for new stream (event)")
