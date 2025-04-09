import appdaemon.plugins.hass.hassapi as hass
import httpx
import json
import yaml


UNAVAILABLE_STATE = "unavailable"
UNKNOWN_STATE = "unknown"
EMPTY_STRING = ''
IDLE_GAME_ID = "idle"
IDLE_LICHESS_TOKEN = 'idle'

URL_TEMPLATE_STREAM_BOARD = "https://lichess.org/api/board/game/stream/{}"

LICHESS_STREAM_PARAMETER_IN_SENSOR = "sensor.chessboard_lichess_stream_call"
LICHESS_RESPONSE_OUT_SENSOR = 'sensor.chessboard_lichess_response_out'

class LichessStreamBoardOpponent(hass.Hass):

    _current_game_id = IDLE_GAME_ID
    _current_token = IDLE_LICHESS_TOKEN
    _current_secret_key = IDLE_LICHESS_TOKEN

    def initialize(self):
        self.log("AppDaemon LichessStreamBoardOpponent script initialized!")
        self.__class__._current_secret_key = self.get_secret()
        self.listen_state(self.parameter_in_changed, LICHESS_STREAM_PARAMETER_IN_SENSOR)
        # we are ready to go
        self.log(f"Waiting for new stream")

    def get_secret(self, path="/config/secrets.yaml"):
        with open(path, "r") as file:
            secrets = yaml.safe_load(file)
        return secrets.get('chessboard_secret_key')

    def parameter_in_changed(self, entity, attribute, old, new, kwargs):
        if new and new != old and new != UNAVAILABLE_STATE and new != UNKNOWN_STATE:
            # convert to json - object
            new_data = json.loads(new)
            if new_data: # valid json
                if (new_data.get('type') == 'setGameId'):
                    self.game_id_changed(new_data.get('gameId'))
                if (new_data.get('type') == 'initializeTokenOpponent' and new_data.get('token') != IDLE_LICHESS_TOKEN):
                    self.token_changed(new_data.get('token'))
        else:
            self.log("Not valid json: {}".format(new))
            if (new == UNAVAILABLE_STATE or new == UNKNOWN_STATE):
                self.__class__._current_game_id = IDLE_GAME_ID

    def game_id_changed(self, new):
        if new and new != self.__class__._current_game_id:
            self.log(f"Game ID: {self.__class__._current_game_id} -> {new}")
            self.__class__._current_game_id = new
            self.stream_opponent_game()

    def token_changed(self, new):
        if new and new != self.__class__._current_token and new != UNAVAILABLE_STATE and new != UNKNOWN_STATE and new != EMPTY_STRING:
            new_decrypted = self.decrypt_message(new)
            self.log(f"Token: {self.__class__._current_token} -> {new_decrypted}")
            self.__class__._current_token = new_decrypted
        else:
            if new is None or new == UNAVAILABLE_STATE or new == UNKNOWN_STATE: 
                self.log("Not allowed token: {}".format(new))

    def check_game_over(self, dat):
        break_game = False
        if (dat.get('type') == 'gameState' and dat.get('status') != 'started'):
            break_game = True
        if (dat.get('type') == 'gameFull' and dat.get('state', {}).get('status') != 'started'):
            break_game = True
        if (dat.get('type') == 'opponentGone' and dat.get('gone') == True and dat.get('claimWinInSeconds') == 0):
            break_game = True
        if (self.__class__._current_game_id == IDLE_GAME_ID):
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
                self.log(f"Error: {e}")
        return decrypted

    # function to stream game
    def stream_opponent_game(self):
        valid_game_id = self.__class__._current_game_id != IDLE_GAME_ID and self.__class__._current_game_id != UNAVAILABLE_STATE and self.__class__._current_game_id != UNKNOWN_STATE
        valid_token =   self.__class__._current_token != IDLE_LICHESS_TOKEN and self.__class__._current_token != UNAVAILABLE_STATE and self.__class__._current_token != UNKNOWN_STATE
        if (valid_game_id and valid_token):

            self.log(f"Starting the stream: {self.__class__._current_game_id}")
            headers = {
                    "Content-Type": "application/x-ndjson",
                    "Authorization": f"Bearer {self.__class__._current_token}",
                    "Connection": "keep-alive"
                }

            url = URL_TEMPLATE_STREAM_BOARD.format(self.__class__._current_game_id)

            # open the stream for whole chess game
            with httpx.stream("GET", url, headers=headers, timeout=60) as response:
                for line in response.iter_lines():
                    if line:
                        # convert to json - object
                        data = json.loads(line)
                        
                        if data and self.check_game_over(data):
                            self.log(f"Terminating the stream: {self.__class__._current_game_id}")
                            # close the stream
                            break
            # reset stream for board on HA (esphome needs to do it as well)
            self.__class__._current_game_id = IDLE_GAME_ID
            off_json = {
                        "type": "LichessStreamBoardOpponent",
                        "state": IDLE_GAME_ID
                    }
            off_json_str = json.dumps(off_json)
            self.set_state(LICHESS_RESPONSE_OUT_SENSOR, state=off_json_str)
            # we are ready to go
            self.log(f"Waiting for new stream")
