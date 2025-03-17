import appdaemon.plugins.hass.hassapi as hass
import httpx
import json

debug = True

IDLE_GAME_ID = "idle"
LICHESS_TOKEN = ''

URL_TEMPLATE = "https://lichess.org/api/board/game/stream/{}"

LICHESS_TOKEN_SENSOR = 'sensor.chessboard_lichess_token'
LICHESS_GAME_ID_SENSOR = 'sensor.chessboard_lichess_game_id'
LICHESS_LAST_MOVE_SENSOR = 'sensor.chessboard_lichess_last_move'

class LichessStreamer(hass.Hass):

    _current_game_id = IDLE_GAME_ID
    _current_token = LICHESS_TOKEN

    def initialize(self):
        self.log("AppDaemon LichessStream script initialized!")
        self.__class__._current_token = self.get_state(LICHESS_TOKEN_SENSOR)
        self.__class__._current_game_id = self.get_state(LICHESS_GAME_ID_SENSOR)
        self.log(f"Initialized Game ID: {self.__class__._current_game_id}")
        self.log(f"Initialized Token: {self.__class__._current_token}")
        self.listen_state(self.game_id_changed, LICHESS_GAME_ID_SENSOR)
        self.listen_state(self.token_changed, LICHESS_TOKEN_SENSOR)
        # continue stream if any
        self.stream_game()

    def game_id_changed(self, entity, attribute, old, new, kwargs):
        if new and new != old:
            self.log(f"Game ID changed: {old} -> {new}")
            self.__class__._current_game_id = new
            self.stream_game()

    def token_changed(self, entity, attribute, old, new, kwargs):
        if new and new != old:
            self.log(f"Token changed: {old} -> {new}")
            self.__class__._current_token = new

    def check_game_over(self, dat):
        break_game = False
        if (dat.get('type') == 'gameState' and dat.get('status') != 'started'):
            break_game = True
        if (dat.get('type') == 'gameFull' and dat.get('state', {}).get('status') != 'started'):
            break_game = True
        if (dat.get('type') == 'opponentGone' and dat.get('gone') == True):
            break_game = True
        if (self.__class__._current_game_id == IDLE_GAME_ID):
            break_game = True
        return break_game

    def reduce_response(self, dat):
        
        reduced_data = dat

        # chat-line and opponent gone is not reduced

        # gameState
        if (dat.get('type') == 'gameState'):
            reduced_data = {
                "type": dat.get("type", ""),
                "wtime": dat.get("wtime", ""),
                "btime": dat.get("btime", ""),
                "status": dat.get("status", ""),
                "win": dat.get("winner", ""),
                "wdraw": dat.get("wdraw", False),
                "bdraw": dat.get("bdraw", False),
                "wback": dat.get("wtakeback", False),
                "bback": dat.get("btakeback", False),
                "last": dat.get("moves", "").split()[-1] if dat.get("moves") else ""
            }

        # gameFull
        if (dat.get('type') == 'gameFull'):
            reduced_data = {
                "type": dat.get('state', {}).get("type", ""),
                "wtime": dat.get('state', {}).get("wtime", ""),
                "btime": dat.get('state', {}).get("btime", ""),
                "status": dat.get('state', {}).get("status", ""),
                "win": dat.get('state', {}).get("winner", ""),
                "wdraw": dat.get('state', {}).get("wdraw", False),
                "bdraw": dat.get('state', {}).get("bdraw", False),
                "wback": dat.get('state', {}).get("wtakeback", False),
                "bback": dat.get('state', {}).get("btakeback", False),
                "last": dat.get('state', {}).get("moves", "").split()[-1] if dat.get("moves") else ""
            }

        return reduced_data

    # function to stream game
    def stream_game(self):

        if (self.__class__._current_game_id != IDLE_GAME_ID):

            headers = {
                    "Content-Type": "application/x-ndjson",
                    "Authorization": f"Bearer {self.__class__._current_token}",
                    "Connection": "keep-alive"
                }

            url = URL_TEMPLATE.format(self.__class__._current_game_id)

            with httpx.stream("GET", url, headers=headers, timeout=60) as response:
                for line in response.iter_lines():
                    if line:

                        data = json.loads(line)
                        
                        if data:
                            reduced_data = self.reduce_response(data)
                            last_move = ""
                            if (reduced_data.get('last')):
                                last_move = reduced_data.get('last', '')
                            self.set_state(LICHESS_LAST_MOVE_SENSOR, state=last_move, attributes={"response": reduced_data})
                            
                        if debug:
                            self.log(reduced_data)  # Process the JSON object

                        # check if we have to abort the game
                        if data and self.check_game_over(data):
                            self.log("Terminating the stream")
                            self.__class__._current_game_id = IDLE_GAME_ID
                            self.set_state(LICHESS_GAME_ID_SENSOR, state=IDLE_GAME_ID)
                            pass
