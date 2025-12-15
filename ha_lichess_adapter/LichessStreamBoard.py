import appdaemon.plugins.hass.hassapi as hass
import berserk
import json
import yaml

from datetime import timedelta




UNAVAILABLE_STATE = "unavailable"
UNKNOWN_STATE = "unknown"
EMPTY_STRING = ''
IDLE_GAME_ID = "idle"
IDLE_LICHESS_TOKEN = 'idle'
ON_STATE = "ON"
OFF_STATE = "OFF"
IDLE_STATE = 'idle'

LICHESS_RESPONSE_OUT_SENSOR = 'sensor.chessboard_lichess_response_out'
LICHESS_TOKEN_MAIN_SENSOR = 'sensor.chessboard_lichess_api_token_main'
LICHESS_GAME_ID_SENSOR = "sensor.chessboard_lichess_game_id"

class LichessStreamBoard(hass.Hass):

    _current_game_id = IDLE_GAME_ID
    _current_token_main = IDLE_LICHESS_TOKEN
    _current_secret_key = IDLE_LICHESS_TOKEN
    _client_main = EMPTY_STRING
    _session_main = EMPTY_STRING


    def initialize(self):
        self.log("AppDaemon LichessStreamBoard script initialized!")
        self.__class__._current_secret_key = self.get_secret()
        self.listen_state(self.game_id_changed, LICHESS_GAME_ID_SENSOR)
        self.listen_state(self.token_changed_main, LICHESS_TOKEN_MAIN_SENSOR)

        # we are ready to go
        self.log(f"Waiting for new stream (board)")

    def get_secret(self, path="/config/secrets.yaml"):
        with open(path, "r") as file:
            secrets = yaml.safe_load(file)
        return secrets.get('chessboard_secret_key')

    def game_id_changed(self, entity, attribute, old, new, kwargs):
        if new and new != self.__class__._current_game_id:
            self.log(f"Game ID changed: {self.__class__._current_game_id} -> {new}")
            self.__class__._current_game_id = new
            self.handle_game_state_main()

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

    def td_to_sec(self, x):
        if x is None:
            return 0
        if isinstance(x, timedelta):
            return int(round(x.total_seconds()))
        # fallback if it ever becomes ms-int again
        return int(round(int(x) / 1000))

    def reduce_response_board(self, dat):
        
        reduced_data = dat

        # gameState (we try to short the json, due to limit of 255 characters for HA sensors)
        if dat.get("type") == "gameState":
            reduced_data = {
                "type": dat.get("type", ""),
                "wclk": f"{self.td_to_sec(dat.get('wtime'))}+{self.td_to_sec(dat.get('winc'))}",
                "bclk": f"{self.td_to_sec(dat.get('btime'))}+{self.td_to_sec(dat.get('binc'))}",
                "state": dat.get("status", ""),
                "win": {"white": "w", "black": "b"}.get(dat.get("winner", ""), ""),
                "wdraw": int(bool(dat.get("wdraw", False))),
                "bdraw": int(bool(dat.get("bdraw", False))),
                "wback": int(bool(dat.get("wtakeback", False))),
                "bback": int(bool(dat.get("btakeback", False))),
                "n": len(dat.get("moves", "").split()) if dat.get("moves") else -1,
                "last": dat.get("moves", "").split()[-1] if dat.get("moves") else "",
                "id": self.__class__._current_game_id,
    }

        # gameFull (we try to short the json, due to limit of 255 characters for HA sensors)
        if (dat.get('type') == 'gameFull'):
            reduced_data = {
                "type": dat.get("type", ""),
                "wid": "{}: {}".format(dat.get('white', {}).get("name", "white"), dat.get('white', {}).get("rating", 0)),
                "bid": "{}: {}".format(dat.get('black', {}).get("name", "black"), dat.get('black', {}).get("rating", 0)),
                "wclk": "{}+{}".format(round(dat.get('state', {}).get("wtime", 0) / 1000), round(dat.get('state', {}).get("winc", 0) / 1000)),
                "bclk": "{}+{}".format(round(dat.get('state', {}).get("btime", 0) / 1000), round(dat.get('state', {}).get("binc", 0) / 1000)),                
                "state": dat.get('state', {}).get("status", ""),
                "win": {"white": "w", "black": "b"}.get(dat.get('state', {}).get("winner", ""), ""),
                "wdraw": int(dat.get('state', {}).get("wdraw", False)),
                "bdraw": int(dat.get('state', {}).get("bdraw", False)),
                "wback": int(dat.get('state', {}).get("wtakeback", False)),
                "bback": int(dat.get('state', {}).get("btakeback", False)),
                "last": dat.get('state', {}).get("moves", "").split()[-1] if dat.get("moves") else "",
                "id" : self.__class__._current_game_id
            }

        # chatline: cut the message, that we dont exeed 255 characters
        if (dat.get('type') == 'chatLine'):
            reduced_data = {
                "type": "chatLine",
                "text": dat.get("text", ""),
                "id" : self.__class__._current_game_id
            }
            # calulates: max-length - base length
            max_text_length = 255 - len(json.dumps({"type": "chatLine", "id": "12345678", "text": ""}))

            if len(reduced_data["text"]) > max_text_length:
                reduced_data["text"] = reduced_data["text"][:max_text_length-3] + "..."  # Truncate and add "..."
        
        #opponent gone
        if (dat.get('type') == 'opponentGone'):
            # we extend by ID
            reduced_data["id"] = self.__class__._current_game_id

        return reduced_data
    
    
    def handle_game_state_main(self):
        valid_game_id = self.__class__._current_game_id != IDLE_GAME_ID and self.__class__._current_game_id != UNAVAILABLE_STATE and self.__class__._current_game_id != UNKNOWN_STATE
        valid_token =   self.__class__._current_token_main != IDLE_LICHESS_TOKEN and self.__class__._current_token_main != UNAVAILABLE_STATE and self.__class__._current_token_main != UNKNOWN_STATE
        if (valid_game_id and valid_token):            
            self.log(f"Starting the stream (main): {self.__class__._current_game_id}")
            for line in self.__class__._client_main.board.stream_game_state(self.__class__._current_game_id):
                if line: # valid dic
                    reduced_data = json.dumps(self.reduce_response_board(line))
                    # let ha know about the move
                    self.set_state(LICHESS_RESPONSE_OUT_SENSOR, state=reduced_data)
                    
                    # check if we have to abort the game
                    if self.check_game_over(line):
                        self.log(f"Terminating the board stream (main): {self.__class__._current_game_id}")
                        # close the stream
                        break
                else:
                    if (self.__class__._current_game_id == IDLE_GAME_ID or self.__class__._current_game_id == UNAVAILABLE_STATE or self.__class__._current_game_id == UNKNOWN_STATE):
                        self.log(f"Terminating the board stream without game id (main): {self.__class__._current_game_id}")
                        break

            # reset stream for board on HA (esphome needs to do it as well)
            self.__class__._current_game_id = IDLE_GAME_ID
            off_json = {
                        "type": "streamBoardResponse",
                        "state": IDLE_GAME_ID
                    }
            off_json_str = json.dumps(off_json)
            self.set_state(LICHESS_RESPONSE_OUT_SENSOR, state=off_json_str)
            # we are ready to go
            self.log(f"Waiting for new stream (main)")



        
