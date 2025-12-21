import appdaemon.plugins.hass.hassapi as hass
import paho.mqtt.client as paho

import ssl
import lichess_helpers as lh

import berserk
import json
import time
from typing import Optional

IDLE_LICHESS_TOKEN = "idle"
IDLE_MQTT_STATE = "idle"
STATUS_OFFLINE = "offline"
STATUS_ONLINE = "online"

# MQTT topics
MQTT_TOKEN_MAIN_TOPIC = "chessboard/token_main"
MQTT_STATUS_TOPIC = "chessboard/status"
MQTT_RESPONSE_TOPIC = "chessboard/response"

SECRET_PATH = "/config/secrets.yaml"
CA_CERT_PATH = "/config/hivemq.pem"

CLASS_NAME = "LichessStreamEvent"


class LichessStreamEvent(hass.Hass):
     
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
        self.mqtt_client_id = {CLASS_NAME}
        self.mqtt_tls_enabled = bool(mqtt_cfg.get("mqtt_tls", False))
        self.mqtt_ca_cert_path = mqtt_cfg.get("mqtt_ca_cert", CA_CERT_PATH) 

        # current runtime state (protected by self._lock where needed)
        self._token_main = IDLE_LICHESS_TOKEN

        # Keep references to sessions so we can close them on token change
        # lichess (berserk) clients/sessions     
        self._session_lichess_event: Optional[berserk.TokenSession] = None     
        self._client_lichess_event: Optional[berserk.Client] = None

        self._lichess_stream_event_init_value = IDLE_LICHESS_TOKEN
      
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
            f"MQTT inputs for {CLASS_NAME}: token_main='{MQTT_TOKEN_MAIN_TOPIC}'"
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
            (MQTT_TOKEN_MAIN_TOPIC, 0),
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

        elif topic == MQTT_TOKEN_MAIN_TOPIC:
            self._on_mqtt_token_main(payload)
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
            self._client_mqtt.publish(MQTT_TOKEN_MAIN_TOPIC, payload=IDLE_LICHESS_TOKEN, qos=0, retain=False)
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
        self._stop_stream_worker()

        # update token
        self._token_main = new_decrypted    

        # start new session/client if not idle
        if new_decrypted != IDLE_LICHESS_TOKEN:
            self._init_session_lichess_event()
            self._run_stream_worker()            
            
    def _on_mqtt_chessboard_status(self, payload: str) -> None:
        # chessboard is online
        if payload == STATUS_ONLINE:
            self.log(f"Chessboard is online")
            self._init_sessions_all()
            self._run_stream_worker()

        # chessboard is offline
        elif payload == STATUS_OFFLINE:
            self.log(f"Chessboard is offline in {CLASS_NAME}")                       
            self._stop_stream_worker()         
            self.clear_topics()

    #####################################################################
    ######### ---------- INIT AND CLOSE BERSERK -------------  ##########
    ##################################################################### 

    def _init_session_lichess_event(self):
        if self._token_main != IDLE_LICHESS_TOKEN and self._session_lichess_event is None:
            # open session with new token and create client
            self._session_lichess_event = berserk.TokenSession(self._token_main)
            self._client_lichess_event = berserk.Client(self._session_lichess_event) 

    def _init_sessions_all(self):
        self._init_session_lichess_event()


    def _close_session_lichess_event(self):
        try:
            if self._session_lichess_event is not None:
                self._session_lichess_event.close()
                self._session_lichess_event: Optional[berserk.TokenSession] = None 
                self._client_lichess_event: Optional[berserk.Client] = None 
        except Exception:
            pass 

    def _close_sessions_all(self):
        self._close_session_lichess_event()


    #####################################################################
    ############## ---------- RUN WOKERS -------------  #################
    #####################################################################


    def _run_stream_worker(self):

        # check if api thread is already running or token still not set, we skip starting
        if (self._lichess_stream_event_init_value == self._token_main and self._token_main != IDLE_LICHESS_TOKEN) or self._token_main == IDLE_LICHESS_TOKEN:
            return

        self.log(f"Stream worker started") 
        # start stream worker thread
        self.handle_incoming_events(self._token_main)

    def _run_all_workers(self):
        self._run_stream_worker()

   
    #####################################################################
    ############## ---------- STOP WOKERS ------------  #################
    #####################################################################

    def _stop_stream_worker(self):    

        if self._lichess_stream_event_init_value != IDLE_LICHESS_TOKEN:
            self.log(f"Stream event worker is running, trying to stop")

            # we reset the token to idle to stop the stream
            self._token_main =  IDLE_LICHESS_TOKEN

            # close the session, so we expect the stream will be closed
            self._close_sessions_all()      

            # let the stream to be closed
            time.sleep(1)         

            # check if init variable is reseted
            if self._lichess_stream_event_init_value != IDLE_LICHESS_TOKEN:
                self.log(f"Stopping Stream event worker failed")
            else:
                self.log(f"Stream worker stopped")


    def _stop_all_workers(self):
        self._stop_stream_worker()


    #####################################################################
    ############## ---------- HANDLER FUNCTIONS ---------  ##############
    #####################################################################
 
    def handle_incoming_events(self, token_init):
        
        self._lichess_stream_event_init_value = token_init

        self.log(f"Starting the stream (event): {token_init}")

        # open the stream for whole chess game
        for event in self._client_lichess_event.board.stream_incoming_events():
            if event:                    
                reduced_data = json.dumps(lh.reduce_response_event(event))
                self.publish_response(reduced_data)
                self.log(f"Event: {reduced_data}")

            if self._lichess_stream_event_init_value != self._token_main:
                self.log(f"Terminating the event stream (token changed): {self._lichess_stream_event_init_value}->{self._token_main}")
                # close the stream
                break

        self.log(f"Terminated the stream (event): {self._lichess_stream_event_init_value}")            
        self._lichess_stream_event_init_value = IDLE_LICHESS_TOKEN


    #####################################################################
    ############## ---------- HELPER FUNCTIONS ----------  ##############
    #####################################################################
