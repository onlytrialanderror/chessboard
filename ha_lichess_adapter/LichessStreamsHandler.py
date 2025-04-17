import appdaemon.plugins.hass.hassapi as hass
import berserk
import threading
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

LICHESS_STREAM_PARAMETER_IN_SENSOR = "sensor.chessboard_lichess_stream_call"
LICHESS_RESPONSE_OUT_SENSOR = 'sensor.chessboard_lichess_response_out'

class LichessStreamsHandler(hass.Hass):

    _current_game_id = IDLE_GAME_ID
    _current_token_main = IDLE_LICHESS_TOKEN
    _current_token_opponent = IDLE_LICHESS_TOKEN
    _current_secret_key = IDLE_LICHESS_TOKEN
    _client_main = EMPTY_STRING
    _client_opponent = EMPTY_STRING
    _stream_event = False
    _stop_event = EMPTY_STRING

    def initialize(self):
        self.log("AppDaemon LichessStreamsHandler script initialized!")
        self.__class__._current_secret_key = self.get_secret()
        self.listen_state(self.parameter_in_changed, LICHESS_STREAM_PARAMETER_IN_SENSOR)
        # we are ready to go
        self.log(f"Waiting for new stream")
        self._stop_event = threading.Event()

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
                if (new_data.get('type') == 'initializeToken'):
                    self.token_changed_main(new_data.get('token'))
                if (new_data.get('type') == 'initializeTokenOpponent'):
                    self.token_changed_opponent(new_data.get('token'))
                if (new_data.get('type') == 'streamEvents' and new_data.get('state')):
                    self.stream_events_trigger(new_data.get('state'))
        else:
            self.log("Not valid json: {}".format(new))
            if (new == UNAVAILABLE_STATE or new == UNKNOWN_STATE):
                self.__class__._current_game_id = IDLE_GAME_ID


    def game_id_changed(self, new):
        if new and new != self.__class__._current_game_id:
            self.log(f"Game ID changed: {self.__class__._current_game_id} -> {new}")
            self.__class__._current_game_id = new
            self.stream_game()

    def token_changed_main(self, new):
        if new and new != UNAVAILABLE_STATE and new != UNKNOWN_STATE and new != EMPTY_STRING:
            new_decrypted = self.decrypt_message(new)
            if (new_decrypted != self.__class__._current_token_main):
                self.log(f"Token changed (main): {self.__class__._current_token_main} -> {new_decrypted}")
                self.__class__._current_token_main = new_decrypted
                self._stop_event.set()
                if (self.__class__._current_token_main != IDLE_LICHESS_TOKEN):
                    session_main = berserk.TokenSession(self.__class__._current_token_main)
                    self.__class__._client_main = berserk.Client(session_main)
                else:
                    self.__class__._client_main = EMPTY_STRING
        else:
            if new is None or new == UNAVAILABLE_STATE or new == UNKNOWN_STATE: 
                self.log("Not allowed token (main): {}".format(new))

    def token_changed_opponent(self, new):
        if new  and new != UNAVAILABLE_STATE and new != UNKNOWN_STATE and new != EMPTY_STRING:
            new_decrypted = self.decrypt_message(new)
            if (new_decrypted != self.__class__._current_token_opponent):
                self.log(f"Token changed (opponent): {self.__class__._current_token_opponent} -> {new_decrypted}")
                self._stop_event.set()
                self.__class__._current_token_opponent = new_decrypted
                if (self.__class__._current_token_opponent != IDLE_LICHESS_TOKEN):
                    session_opponent = berserk.TokenSession(self.__class__._current_token_opponent)
                    self.__class__._client_opponent = berserk.Client(session_opponent)
                else:
                    self.__class__._client_opponent = EMPTY_STRING
        else:
            if new is None or new == UNAVAILABLE_STATE or new == UNKNOWN_STATE: 
                self.log("Not allowed token (opponent): {}".format(new))

    def stream_events_trigger(self, new):
        if new and new == ON_STATE:
            self.__class__._stream_event = True
        if new and (new == OFF_STATE or new == IDLE_STATE):
            self.__class__._stream_event = False
        if (self.__class__._stream_event):
            self.stream_event()


    def check_event_over(self, dat):
        break_game = False
        if (dat.get('type') == 'gameFinish'):
            break_game = True
        if (self.__class__._stream_event == False or self.__class__._current_token_main == UNAVAILABLE_STATE or self.__class__._current_token_main == UNKNOWN_STATE):
            break_game = True
        return break_game

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

    def reduce_response_board(self, dat):
        
        reduced_data = dat

        # gameState (we try to short the json, due to limit of 255 characters for HA sensors)
        if (dat.get('type') == 'gameState'):
            reduced_data = {
                "type": dat.get("type", ""),
                "wclk": "{}+{}".format(round(berserk.utils.to_millis(dat.get("wtime")) / 1000), round(berserk.utils.to_millis(dat.get("winc")) / 1000)),
                "bclk": "{}+{}".format(round(berserk.utils.to_millis(dat.get("btime")) / 1000), round(berserk.utils.to_millis(dat.get("binc")) / 1000)),  
                "state": dat.get("status", ""),
                "win": {"white": "w", "black": "b"}.get(dat.get("winner", ""), ""),
                "wdraw": int(dat.get("wdraw", False)),
                "bdraw": int(dat.get("bdraw", False)),
                "wback": int(dat.get("wtakeback", False)),
                "bback": int(dat.get("btakeback", False)),
                "n": int(len(dat.get("moves", "").split())) if dat.get("moves") else -1,
                "last": dat.get("moves", "").split()[-1] if dat.get("moves") else "",
                "id" : self.__class__._current_game_id
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
        
        if (self.__class__._stream_event == True and self.__class__._current_token_main != UNAVAILABLE_STATE and self.__class__._current_token_main != UNKNOWN_STATE and self.__class__._current_token_main != IDLE_LICHESS_TOKEN):

            self.log(f"Starting the stream (event): {self.__class__._current_token_main}")
            self._stop_event.clear()

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
                    if self._stop_event.is_set():
                        self.log("Stopping the stream (event)...")
                        break
            # reset flags
            self.__class__._stream_event = False
            off_json = {
                        "type": "streamEventsResponse",
                        "state": IDLE_STATE
                    }
            off_json_str = json.dumps(off_json)
            self.set_state(LICHESS_RESPONSE_OUT_SENSOR, state=off_json_str)
            # close board streams, if any still open
            self._stop_event.set()
        else:
            self.log(f"Waiting for new stream (event)")


    def handle_game_state_main(self):
        valid_game_id = self.__class__._current_game_id != IDLE_GAME_ID and self.__class__._current_game_id != UNAVAILABLE_STATE and self.__class__._current_game_id != UNKNOWN_STATE
        valid_token =   self.__class__._current_token_main != IDLE_LICHESS_TOKEN and self.__class__._current_token_main != UNAVAILABLE_STATE and self.__class__._current_token_main != UNKNOWN_STATE
        if (valid_game_id and valid_token):            
            self.log(f"Starting the stream (main): {self.__class__._current_game_id}")
            for line in self.__class__._client_main.board.stream_game_state(self.__class__._current_game_id):
                if line: # valid dic
                    # self.log("line main: " + str(line))
                    reduced_data = json.dumps(self.reduce_response_board(line))
                    # let ha know about the move
                    self.set_state(LICHESS_RESPONSE_OUT_SENSOR, state=reduced_data)
                    
                    # check if we have to abort the game
                    if self.check_game_over(line):
                        self.log(f"Terminating the stream (main): {self.__class__._current_game_id}")
                        # close the stream
                        break
                    if self._stop_event.is_set():
                        self.log("Stopping the stream (main)...")
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

    def handle_game_state_opponent(self):
        # do nothing, just keep stream alive
        valid_game_id = self.__class__._current_game_id != IDLE_GAME_ID and self.__class__._current_game_id != UNAVAILABLE_STATE and self.__class__._current_game_id != UNKNOWN_STATE
        valid_token =   self.__class__._current_token_opponent != IDLE_LICHESS_TOKEN and self.__class__._current_token_opponent != UNAVAILABLE_STATE and self.__class__._current_token_opponent != UNKNOWN_STATE
        if (valid_game_id and valid_token):
            self.log(f"Starting the stream (opponent): {self.__class__._current_game_id}")
            for line in self.__class__._client_opponent.board.stream_game_state(self.__class__._current_game_id):
                # self.log("line opponent: " + str(line))
                if line: # valid dic
                    # check if we have to abort the game
                    if self.check_game_over(line):
                        self.log(f"Terminating the stream (opponent): {self.__class__._current_game_id}")
                        # close the stream
                        break
                    if self._stop_event.is_set():
                        self.log("Stopping the stream (opponent)...")
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


    # function to stream game
    def stream_game(self):
        t_board_main = threading.Thread(target=self.handle_game_state_main)
        t_board_opponent = threading.Thread(target=self.handle_game_state_opponent)

        # Start threads
        t_board_main.start()
        t_board_opponent.start()

        # Wait for both threads to finish
        # t_board_main.join()
        # t_board_opponent.join()

    # function to stream game
    def stream_event(self):

        t_event_main = threading.Thread(target=self.handle_incoming_events)

        # Start threads
        t_event_main.start()

        

