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

LICHESS_RESPONSE_OUT_SENSOR = 'sensor.chessboard_lichess_response_out'
LICHESS_TOKEN_OPPONENT_SENSOR = 'sensor.chessboard_lichess_api_token_opponent'
LICHESS_GAME_ID_SENSOR = "sensor.chessboard_lichess_game_id"

class LichessStreamBoardOpponent(hass.Hass):

    _current_game_id = IDLE_GAME_ID
    _current_token_opponent = IDLE_LICHESS_TOKEN
    _current_secret_key = IDLE_LICHESS_TOKEN
    _client_opponent = EMPTY_STRING
    _session_opponent = EMPTY_STRING

    def initialize(self):
        self.log("AppDaemon LichessStreamBoardOpponent script initialized!")
        self.__class__._current_secret_key = self.get_secret()
        self.listen_state(self.game_id_changed, LICHESS_GAME_ID_SENSOR)
        self.listen_state(self.token_changed_opponent, LICHESS_TOKEN_OPPONENT_SENSOR)
        # we are ready to go
        self.log(f"Keeping opponents stram alive (board opponent)")

    def get_secret(self, path="/config/secrets.yaml"):
        with open(path, "r") as file:
            secrets = yaml.safe_load(file)
        return secrets.get('chessboard_secret_key')


    def game_id_changed(self, entity, attribute, old, new, kwargs):
        if new and new != self.__class__._current_game_id:
            self.log(f"Game ID changed: {self.__class__._current_game_id} -> {new}")
            self.__class__._current_game_id = new
            self.handle_game_state_opponent()

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
                self.log("Not valid hex-string): {}".format(hex_string))
                self.log(f"Error: {e}")
        return decrypted

    
    def handle_game_state_opponent(self):
        # do nothing, just keep stream alive
        valid_game_id = self.__class__._current_game_id != IDLE_GAME_ID and self.__class__._current_game_id != UNAVAILABLE_STATE and self.__class__._current_game_id != UNKNOWN_STATE
        valid_token =   self.__class__._current_token_opponent != IDLE_LICHESS_TOKEN and self.__class__._current_token_opponent != UNAVAILABLE_STATE and self.__class__._current_token_opponent != UNKNOWN_STATE
        if (valid_game_id and valid_token):
            self.log(f"Starting the stream (opponent): {self.__class__._current_game_id}")
            for line in self.__class__._client_opponent.board.stream_game_state(self.__class__._current_game_id):
                if line: # valid dic
                    # check if we have to abort the game
                    if self.check_game_over(line):
                        self.log(f"Terminating the stream (opponent): {self.__class__._current_game_id}")
                        # close the stream
                        break
                else:
                    if (self.__class__._current_game_id == IDLE_GAME_ID or self.__class__._current_game_id == UNAVAILABLE_STATE or self.__class__._current_game_id == UNKNOWN_STATE):
                        break

            # reset stream for board on HA (esphome needs to do it as well)
            off_json = {
                        "type": "streamBoardResponseOpponent",
                        "state": IDLE_GAME_ID
                    }
            off_json_str = json.dumps(off_json)
            self.set_state(LICHESS_RESPONSE_OUT_SENSOR, state=off_json_str)
            # we are ready to go
            self.log(f"Waiting for new stream (opponent)")


        
