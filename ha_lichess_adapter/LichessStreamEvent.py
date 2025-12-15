import appdaemon.plugins.hass.hassapi as hass
import berserk
import json
import yaml


UNAVAILABLE_STATE = "unavailable"
UNKNOWN_STATE = "unknown"
EMPTY_STRING = ''
IDLE_GAME_ID = "idle"
IDLE_LICHESS_TOKEN = 'idle'
ON_STATE = "ON"
OFF_STATE = "OFF"
IDLE_STATE = 'idle'

LICHESS_STREAM_PARAMETER_IN_SENSOR = "sensor.chessboard_lichess_stream_event"
LICHESS_RESPONSE_OUT_SENSOR = 'sensor.chessboard_lichess_response_out'
LICHESS_TOKEN_MAIN_SENSOR = 'sensor.chessboard_lichess_api_token_main'
LICHESS_TOKEN_OPPONENT_SENSOR = 'sensor.chessboard_lichess_api_token_opponent'

class LichessStreamEvent(hass.Hass):

    _current_token_main = IDLE_LICHESS_TOKEN
    _current_token_opponent = IDLE_LICHESS_TOKEN
    _current_secret_key = IDLE_LICHESS_TOKEN
    _client_main = EMPTY_STRING
    _session_main = EMPTY_STRING
    _client_opponent = EMPTY_STRING
    _session_opponent = EMPTY_STRING
    _stream_event = False


    def initialize(self):
        self.log("AppDaemon LichessStreamEvent script initialized!")
        self.__class__._current_secret_key = self.get_secret()
        self.listen_state(self.parameter_in_changed, LICHESS_STREAM_PARAMETER_IN_SENSOR)
        self.listen_state(self.token_changed_main, LICHESS_TOKEN_MAIN_SENSOR)
        self.listen_state(self.token_changed_opponent, LICHESS_TOKEN_OPPONENT_SENSOR)
        # we are ready to go
        self.log(f"Waiting for new stream (event)")

    def get_secret(self, path="/config/secrets.yaml"):
        with open(path, "r") as file:
            secrets = yaml.safe_load(file)
        return secrets.get('chessboard_secret_key')

    def parameter_in_changed(self, entity, attribute, old, new, kwargs):
        if new and new != old and new != UNAVAILABLE_STATE and new != UNKNOWN_STATE:
            # convert to json - object
            new_data = json.loads(new)
            if new_data: # valid json
                if (new_data.get('type') == 'streamEvents' and new_data.get('state')):
                    self.stream_events_trigger(new_data.get('state'))


    def token_changed_main(self, entity, attribute, old, new, kwargs):
        if new  and new != UNAVAILABLE_STATE and new != UNKNOWN_STATE and new != EMPTY_STRING and self.decrypt_message(new) != self.__class__._current_token_main:
            new_decrypted = self.decrypt_message(new)
            self.log(f"Main token changed (stream request main): {self.__class__._current_token_main} -> {new_decrypted}")
            self.__class__._current_token_main = new_decrypted
            self.__class__._session_main = berserk.TokenSession(self.__class__._current_token_main)
            self.__class__._client_main = berserk.Client(self.__class__._session_main)
        else:
            if new is None or new == UNAVAILABLE_STATE or new == UNKNOWN_STATE: 
                self.log("Not allowed token (stream request main): {}".format(new))

    def token_changed_opponent(self, entity, attribute, old, new, kwargs):
        if new  and new != UNAVAILABLE_STATE and new != UNKNOWN_STATE and new != EMPTY_STRING and self.decrypt_message(new) != self.__class__._current_token_opponent:
            new_decrypted = self.decrypt_message(new)
            self.log(f"Opponent token changed (stream main request for opponent): {self.__class__._current_token_opponent} -> {new_decrypted}")
            self.__class__._current_token_opponent = new_decrypted
            self.__class__._session_opponent = berserk.TokenSession(self.__class__._current_token_opponent)
            self.__class__._client_opponent = berserk.Client(self.__class__._session_opponent)
        else:
            if new is None or new == UNAVAILABLE_STATE or new == UNKNOWN_STATE: 
                self.log("Not allowed token (stream request opponent): {}".format(new))

    def stream_events_trigger(self, new):
        if new and new == ON_STATE:
            self.__class__._stream_event = True
        if new and (new == OFF_STATE or new == IDLE_STATE):
            self.__class__._stream_event = False
        if (self.__class__._stream_event == True):
            self.handle_incoming_events()


    def check_event_over(self, dat):
        break_game = False
        if (dat.get('type') == 'gameFinish'):
            break_game = True
        if (self.__class__._stream_event == False or self.__class__._current_token_main == UNAVAILABLE_STATE or self.__class__._current_token_main == UNKNOWN_STATE):
            break_game = True
        return break_game

    def decrypt_message(self, hex_string):
        decrypted = hex_string
        if hex_string is not None and hex_string != UNAVAILABLE_STATE and hex_string != UNKNOWN_STATE and hex_string != EMPTY_STRING and hex_string != IDLE_LICHESS_TOKEN:
            try:
                # Convert hex to bytes
                encrypted_bytes = bytes.fromhex(hex_string)
                decrypted = ''.join(chr(b ^ ord(self.__class__._current_secret_key[i % len(self.__class__._current_secret_key)])) for i, b in enumerate(encrypted_bytes))
            except ValueError as e:
                self.log("Not valid hex-string): {}".format(hex_string))
                self.log(f"Error: {e}")
        return decrypted

    def reduce_response_event(self, dat):
        
        reduced_data = dat

        # gameState (we try to short the json, due to limit of 255 characters for HA sensors)
        if (dat.get('type') == 'gameStart'):
            reduced_data = {
                "type": dat.get("type", ""),
                "gameId": dat.get('game', {}).get("gameId", ""),
                "color": dat.get('game', {}).get("color", ""),
                "isMyTurn": dat.get('game', {}).get("isMyTurn", False),
                "lastMove": dat.get('game', {}).get("lastMove", ""),
                "opponent": "{}: {}".format(dat.get('game', {}).get("opponent", {}).get("username", "player"), dat.get('game', {}).get("opponent", {}).get("rating", 0)),
                "rated": dat.get('game', {}).get("rated", False),
                "speed": dat.get('game', {}).get("speed", ""),
                "secondsLeft": dat.get('game', {}).get("secondsLeft", 0)
            } 
        else :
            # gameFull (we try to short the json, due to limit of 255 characters for HA sensors)
            if (dat.get('type') == 'gameFinish'):
                reduced_data = {
                    "type": dat.get("type", ""),
                    "gameId": dat.get('game', {}).get("gameId", ""),
                    "color": dat.get('game', {}).get("color", ""),
                    "isMyTurn": dat.get('game', {}).get("isMyTurn", False),
                    "lastMove": dat.get('game', {}).get("lastMove", ""),
                    "opponent": "{}: {}".format(dat.get('game', {}).get("opponent", {}).get("username", "player"), dat.get('game', {}).get("opponent", {}).get("rating", 0)),
                    "rated": dat.get('game', {}).get("rated", False),
                    "speed": dat.get('game', {}).get("speed", ""),
                    "status": dat.get('game', {}).get('status', {}).get("name", ""),
                    "win": {"white": "1-0", "black": "0-1"}.get(dat.get('game', {}).get("winner", ""), "")
                }
            else :
                # challenge
                if (dat.get('type') == 'challenge' or dat.get('type') == 'challengeCanceled' or dat.get('type') == 'challengeDeclined'):
                    reduced_data = {
                        "type": dat.get("type", ""),
                        "id": dat.get('challenge', {}).get("id", ""),
                        "status": dat.get('challenge', {}).get("status", "")
                    }
        return reduced_data
    
    def handle_incoming_events(self):
        
        if (self.__class__._stream_event == True and self.__class__._current_token_main != UNAVAILABLE_STATE and self.__class__._current_token_main != UNKNOWN_STATE):

            self.log(f"Starting the stream (event): {self.__class__._current_token_main}")

            # open the stream for whole chess game
            for event in self.__class__._client_main.board.stream_incoming_events():
                if event:
                    reduced_data = json.dumps(self.reduce_response_event(event))
                    self.set_state(LICHESS_RESPONSE_OUT_SENSOR, state=reduced_data)
                    
                    # check if we have to abort the game
                    if (self.check_event_over(event)==True):
                        self.log(f"Terminating the stream (event): {self.__class__._current_token_main}")
                        # close the stream
                        break
                else:
                    if (self.__class__._stream_event == False or self.__class__._current_token_main == UNAVAILABLE_STATE or self.__class__._current_token_main == UNKNOWN_STATE):
                        self.log(f"Terminating the stream without event (event): {self.__class__._current_token_main}")
                        break

            # reset flags
            self.__class__._stream_event = False
            off_json = {
                        "type": "streamEventsResponse",
                        "state": IDLE_STATE
                    }
            off_json_str = json.dumps(off_json)
            self.set_state(LICHESS_RESPONSE_OUT_SENSOR, state=off_json_str)

        else:
            self.log(f"Waiting for new stream (event)")




 

        
