import appdaemon.plugins.hass.hassapi as hass
import httpx
import json

IDLE_GAME_ID = "idle"
UNAVAILABLE_STATE = "unavailable"
UNKNOWN_STATE = "unknown"
LICHESS_TOKEN = ''

URL_TEMPLATE = "https://lichess.org/api/board/game/stream/{}"

LICHESS_TOKEN_SENSOR = 'sensor.chessboard_lichess_token'
LICHESS_GAME_ID_SENSOR = 'sensor.chessboard_lichess_game_id'
LICHESS_LAST_MOVE_SENSOR = 'sensor.chessboard_lichess_last_move_out'

class LichessStreamBoard(hass.Hass):

    _current_game_id = IDLE_GAME_ID
    _current_token = LICHESS_TOKEN

    def initialize(self):
        self.log("AppDaemon LichessStreamBoard script initialized!")
        self.__class__._current_token = self.get_state(LICHESS_TOKEN_SENSOR)
        self.__class__._current_game_id = self.get_state(LICHESS_GAME_ID_SENSOR)
        self.log(f"Initialized Game ID: {self.__class__._current_game_id}")
        self.log(f"Initialized Token (board): {self.__class__._current_token}")
        self.listen_state(self.game_id_changed, LICHESS_GAME_ID_SENSOR)
        self.listen_state(self.token_changed, LICHESS_TOKEN_SENSOR)
        # we are ready to go
        self.log(f"Waiting for new stream (board)")

    def game_id_changed(self, entity, attribute, old, new, kwargs):
        if new and new != old and new != UNAVAILABLE_STATE and new != UNKNOWN_STATE:
            self.log(f"Game ID changed: {old} -> {new}")
            self.__class__._current_game_id = new
            self.stream_game()
        else:
            if new is None or new == UNAVAILABLE_STATE or new == UNKNOWN_STATE: 
                self.log("Not allowed game_id: {}".format(new))

    def token_changed(self, entity, attribute, old, new, kwargs):
        if new and new != old and new != UNAVAILABLE_STATE and new != UNKNOWN_STATE:
            self.log(f"Token changed (board): {old} -> {new}")
            self.__class__._current_token = new
        else:
            if new is None or new == UNAVAILABLE_STATE or new == UNKNOWN_STATE: 
                self.log("Not allowed token (board): {}".format(new))

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

    def reduce_response(self, dat):
        
        reduced_data = dat

        # gameState (we try to short the json, due to limit of 255 characters for HA sensors)
        if (dat.get('type') == 'gameState'):
            reduced_data = {
                "type": dat.get("type", ""),
                "wclk": "{}+{}".format(round(dat.get("wtime", 0) / 1000), round(dat.get("winc", 0) / 1000)),
                "bclk": "{}+{}".format(round(dat.get("btime", 0) / 1000), round(dat.get("binc", 0) / 1000)),  
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

    # function to stream game
    def stream_game(self):
        if (self.__class__._current_game_id != IDLE_GAME_ID and self.__class__._current_game_id != UNAVAILABLE_STATE and self.__class__._current_game_id != UNKNOWN_STATE):

            self.log(f"Starting the stream (board): {self.__class__._current_game_id}")
            headers = {
                    "Content-Type": "application/x-ndjson",
                    "Authorization": f"Bearer {self.__class__._current_token}",
                    "Connection": "keep-alive"
                }

            url = URL_TEMPLATE.format(self.__class__._current_game_id)

            # open the stream for whole chess game
            with httpx.stream("GET", url, headers=headers, timeout=7200) as response:
                for line in response.iter_lines():
                    if line:
                        # convert to json - object
                        data = json.loads(line)
                        
                        if data: # valid json
                            reduced_data = json.dumps(self.reduce_response(data))
                            # let ha know about the move
                            self.set_state(LICHESS_LAST_MOVE_SENSOR, state=reduced_data)
                            
                            # check if we have to abort the game
                            if self.check_game_over(data):
                                self.log(f"Terminating the stream (board): {self.__class__._current_game_id}")
                                # close the stream
                                break
            # listener will overwrite the global variable here
            # reset stream for board on HA (esphome needs to do it as well)
            self.__class__._current_game_id = IDLE_GAME_ID
            self.set_state(LICHESS_GAME_ID_SENSOR, state=IDLE_GAME_ID)
            # we are ready to go
            self.log(f"Waiting for new stream (board)")
