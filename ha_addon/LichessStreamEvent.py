import appdaemon.plugins.hass.hassapi as hass
import httpx
import json

UNAVAILABLE_STATE = "unavailable"
UNKNOWN_STATE = "unknown"
ON_STATE = "ON"
OFF_STATE = "OFF"
LICHESS_TOKEN = ''

URL_TEMPLATE = "https://lichess.org/api/stream/event"

LICHESS_TOKEN_SENSOR = 'sensor.chessboard_lichess_token'
LICHESS_EVENT_SENSOR = 'sensor.chessboard_lichess_stream_events'
LICHESS_LAST_EVENT_SENSOR = 'sensor.chessboard_lichess_last_event_out'

class LichessStreamEvent(hass.Hass):

    _current_token = LICHESS_TOKEN
    _stream_event = False

    def initialize(self):
        self.log("AppDaemon LichessStreamEvent script initialized!")
        self.__class__._current_token = self.get_state(LICHESS_TOKEN_SENSOR)
        self.log(f"Initialized Token: {self.__class__._current_token}")
        self.listen_state(self.token_changed, LICHESS_TOKEN_SENSOR)
        self.listen_state(self.stream_events_trigger, LICHESS_EVENT_SENSOR)
        if (self.get_state(LICHESS_EVENT_SENSOR) == ON_STATE):
            self.__class__._stream_event = True
            self.stream_event()
        else:
            self.__class__._stream_event = False

    def token_changed(self, entity, attribute, old, new, kwargs):
        if new and new != old and new != UNAVAILABLE_STATE and new != UNKNOWN_STATE:
            self.log(f"Token changed (event): {old} -> {new}")
            self.__class__._current_token = new
        else:
            if new is None or new == UNAVAILABLE_STATE or new == UNKNOWN_STATE: 
                self.log("Not allowed token (event): {}".format(new))

    def stream_events_trigger(self, entity, attribute, old, new, kwargs):
        if new and new == ON_STATE:
            self.__class__._stream_event = True
        if new and new == OFF_STATE:
            self.__class__._stream_event = False
        if (new and new != old and self.__class__._stream_event == True):
            self.stream_event()


    def check_event_over(self, dat):
        break_game = False
        if (dat.get('type') == 'gameFinish'):
            break_game = True
        if (self.__class__._stream_event == False or self.__class__._current_token == UNAVAILABLE_STATE or self.__class__._current_token == UNKNOWN_STATE):
            break_game = True
        return break_game

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
                # challange
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

            url = URL_TEMPLATE

            # open the stream for whole chess game
            with httpx.stream("GET", url, headers=headers, timeout=30) as response:
                for line in response.iter_lines():
                    if line:
                        # convert to json - object
                        data = json.loads(line)
                        
                        if data: # valid json
                            reduced_data = self.reduce_response(data)
                            self.set_state(LICHESS_LAST_EVENT_SENSOR, state=reduced_data)
                            
                            # check if we have to abort the game
                            if (self.check_event_over(data)==True):
                                self.log(f"Terminating the stream: {self.__class__._current_token}")
                                # close the stream
                                break
            # reset flags
            self.__class__._stream_event = False
            self.set_state(LICHESS_EVENT_SENSOR, state=OFF_STATE)
        else:
            self.log(f"Waiting for new stream (event)")
