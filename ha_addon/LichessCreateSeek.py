import appdaemon.plugins.hass.hassapi as hass
import requests
import json

UNAVAILABLE_STATE = "unavailable"
UNKNOWN_STATE = "unknown"
LICHESS_TOKEN = ''
EMPTY_SEEK = "{}"

URL_TEMPLATE = "https://lichess.org/api/board/seek"

LICHESS_TOKEN_SENSOR = 'sensor.chessboard_lichess_token'
LICHESS_SEEK_SENSOR = 'sensor.chessboard_lichess_create_seek'

class LichessCreateSeek(hass.Hass):

    _current_token = LICHESS_TOKEN
    _current_seek = EMPTY_SEEK

    def initialize(self):
        self.log("AppDaemon LichessCreateSeek script initialized!")
        self.__class__._current_token = self.get_state(LICHESS_TOKEN_SENSOR)
        self.log(f"Initialized Token (seek): {self.__class__._current_token}")
        self.listen_state(self.token_changed, LICHESS_TOKEN_SENSOR)
        self.listen_state(self.create_seek_trigger, LICHESS_SEEK_SENSOR)

    def token_changed(self, entity, attribute, old, new, kwargs):
        if new and new != old and new != UNAVAILABLE_STATE and new != UNKNOWN_STATE:
            self.log(f"Token changed (seek): {old} -> {new}")
            self.__class__._current_token = new
        else:
            if new is None or new == UNAVAILABLE_STATE or new == UNKNOWN_STATE: 
                self.log("Not allowed token (seek): {}".format(new))

    def create_seek_trigger(self, entity, attribute, old, new, kwargs):
        if (new and new != EMPTY_SEEK and new != UNAVAILABLE_STATE and new != UNKNOWN_STATE):
            if (new != self.__class__._current_seek):  
                self.__class__._current_seek = new              
            self.create_seek()
            

    # function to stream game
    def create_seek(self):
        if (self.__class__._current_seek != EMPTY_SEEK and self.__class__._current_token != UNAVAILABLE_STATE and self.__class__._current_token != UNKNOWN_STATE):
            
            # Convert JSON string to Python dictionary
            json_data = json.loads(self.__class__._current_seek)

            url = URL_TEMPLATE
            if json_data:
                self.log(f"Creating the seek: {json_data}")
                headers = {
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.__class__._current_token}"
                    }
                
                try:
                    response = requests.post(url, json=json_data, headers=headers)

                    if response.status_code == 200:
                        self.log("Info: New game seeked")
                    else:
                        self.log(f"Error: {response.status_code}, Response: {response.text}")

                except requests.exceptions.RequestException as e:
                    print(f"Error: {e}")

                # reset flags
                self.__class__._current_seek = EMPTY_SEEK
                self.set_state(LICHESS_SEEK_SENSOR, state=EMPTY_SEEK)

                self.log(f"Waiting for new seek")
            
