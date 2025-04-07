import appdaemon.plugins.hass.hassapi as hass
import httpx
import json
import yaml

UNAVAILABLE_STATE = "unavailable"
UNKNOWN_STATE = "unknown"
EMPTY_STRING = ''
ON_STATE = "ON"
OFF_STATE = "OFF"
IDLE_LICHESS_TOKEN = 'idle'

URL_TEMPLATE_STREAM_EVENT = "https://lichess.org/api/stream/event"

LICHESS_STREAM_PARAMETER_IN_SENSOR = "sensor.chessboard_lichess_stream_call"
LICHESS_RESPONSE_OUT_SENSOR = 'sensor.chessboard_response_out'

class LichessStreamEvent(hass.Hass):

    _current_token = IDLE_LICHESS_TOKEN
    _stream_event = False
    _current_secret_key = IDLE_LICHESS_TOKEN

    def initialize(self):
        self.log("AppDaemon LichessStreamEvent script initialized!")
        self.__class__._current_secret_key = self.get_secret()
        self.listen_state(self.parameter_in_changed, LICHESS_STREAM_PARAMETER_IN_SENSOR)
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
                if (new_data.get('type') == 'initializeToken' and new_data.get('token') != IDLE_LICHESS_TOKEN):
                    self.token_changed(new_data.get('token'))
        else:
            self.log("Not valid json: {}".format(new))



    def token_changed(self, new):
        if new and new != self.__class__._current_token and new != UNAVAILABLE_STATE and new != UNKNOWN_STATE and new != EMPTY_STRING:
            new_decrypted = self.decrypt_message(new)
            self.log(f"Token changed (event): {self.__class__._current_token} -> {new_decrypted}")
            self.__class__._current_token = new_decrypted
        else:
            if new is None or new == UNAVAILABLE_STATE or new == UNKNOWN_STATE: 
                self.log("Not allowed token (event): {}".format(new))


    def stream_events_trigger(self, new):
        if new and new == ON_STATE:
            self.__class__._stream_event = True
        if new and new == OFF_STATE:
            self.__class__._stream_event = False
        if (self.__class__._stream_event):
            self.stream_event()


    def check_event_over(self, dat):
        break_game = False
        if (dat.get('type') == 'gameFinish'):
            break_game = True
        if (self.__class__._stream_event == False or self.__class__._current_token == UNAVAILABLE_STATE or self.__class__._current_token == UNKNOWN_STATE):
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

    def reduce_response(self, dat):
        
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
                    "win": {"white": "w", "black": "b"}.get(dat.get('game', {}).get("winner", ""), "")
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

    # function to stream game
    def stream_event(self):
        if (self.__class__._stream_event == True and self.__class__._current_token != UNAVAILABLE_STATE and self.__class__._current_token != UNKNOWN_STATE):

            self.log(f"Starting the stream: {self.__class__._current_token}")
            headers = {
                    "Content-Type": "application/x-ndjson",
                    "Authorization": f"Bearer {self.__class__._current_token}"
                }

            url = URL_TEMPLATE_STREAM_EVENT

            # open the stream for whole chess game
            with httpx.stream("GET", url, headers=headers, timeout=60) as response:
                for line in response.iter_lines():
                    if line:
                        # convert to json - object
                        data = json.loads(line)
                        
                        if data: # valid json
                            reduced_data = json.dumps(self.reduce_response(data))
                            self.set_state(LICHESS_RESPONSE_OUT_SENSOR, state=reduced_data)
                            
                            # check if we have to abort the game
                            if (self.check_event_over(data)==True):
                                self.log(f"Terminating the stream: {self.__class__._current_token}")
                                # close the stream
                                break
            # reset flags
            self.__class__._stream_event = False
            off_json = {
                        "type": "streamEventsResponse",
                        "state": "OFF"
                    }
            off_json_str = json.dumps(off_json)
            self.set_state(LICHESS_RESPONSE_OUT_SENSOR, state=off_json_str)
        else:
            self.log(f"Waiting for new stream (event)")
