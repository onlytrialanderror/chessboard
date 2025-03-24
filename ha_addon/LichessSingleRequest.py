import appdaemon.plugins.hass.hassapi as hass
import requests
import json

IDLE_GAME_ID = "idle"
UNAVAILABLE_STATE = "unavailable"
UNKNOWN_STATE = "unknown"
EMPTY_STRING = ''
EMPTY_CALL = "{}"
EMPTY_HEADER = {"Content-Type": "application/json" }


LICHESS_TOKEN_SENSOR = 'sensor.chessboard_lichess_token'
LICHESS_GAME_ID_SENSOR = 'sensor.chessboard_lichess_game_id'
LICHESS_CALL_SENSOR = 'sensor.chessboard_lichess_api_call'

URL_TEMPLATE_SEEK = "https://lichess.org/api/board/seek"
URL_TEMPLATE_MOVE = "https://lichess.org/api/board/game/{}/move/{}"
URL_TEMPLATE_ABORT = "https://lichess.org/api/board/game/{}/abort"
URL_TEMPLATE_RESIGN = "https://lichess.org/api/board/game/{}/resign"
URL_TEMPLATE_CLAIM_VICTORY = "https://lichess.org/api/board/game/{}/claim-victory"
URL_TEMPLATE_DRAW = "https://lichess.org/api/board/game/{}/draw/{}"
URL_TEMPLATE_TAKEBACK = "https://lichess.org/api/board/game/{}/takeback/{}"
URL_TEMPLATE_CHALLENGE = "https://lichess.org/api/challenge/{}"

class LichessSingleRequest(hass.Hass):

    _current_game_id = IDLE_GAME_ID
    _current_token = EMPTY_STRING
    _current_call_description = EMPTY_STRING
    _current_url = EMPTY_STRING
    _current_header = EMPTY_HEADER
    _current_body = EMPTY_CALL

    def initialize(self):
        self.log("AppDaemon LichessSingleRequest script initialized!")
        self.__class__._current_token = self.get_state(LICHESS_TOKEN_SENSOR)
        self.__class__._current_game_id = self.get_state(LICHESS_GAME_ID_SENSOR)
        self.log(f"Initialized Game ID: {self.__class__._current_game_id}")
        self.log(f"Initialized Token (call): {self.__class__._current_token}")
        self.listen_state(self.game_id_changed, LICHESS_GAME_ID_SENSOR)
        self.listen_state(self.token_changed, LICHESS_TOKEN_SENSOR)
        self.listen_state(self.handle_call_trigger, LICHESS_CALL_SENSOR)
        self.__class__._current_header = {
                                        "Content-Type": "application/json",
                                        "Authorization": f"Bearer {self.__class__._current_token}"
                                    }
        # we are ready to go
        self.log(f"Waiting for new api call")

    def game_id_changed(self, entity, attribute, old, new, kwargs):
        if new and new != old and new != UNAVAILABLE_STATE and new != UNKNOWN_STATE:
            self.log(f"Game ID changed: {old} -> {new}")
            self.__class__._current_game_id = new
        else:
            if new is None or new == UNAVAILABLE_STATE or new == UNKNOWN_STATE: 
                self.log("Not allowed game_id: {}".format(new))

    def token_changed(self, entity, attribute, old, new, kwargs):
        if new and new != old and new != UNAVAILABLE_STATE and new != UNKNOWN_STATE:
            self.log(f"Token changed (call): {old} -> {new}")
            self.__class__._current_token = new
            self.__class__._current_header = {
                                        "Content-Type": "application/json",
                                        "Authorization": f"Bearer {new}"
                                    }
        else:
            if new is None or new == UNAVAILABLE_STATE or new == UNKNOWN_STATE: 
                self.log("Not allowed token (call): {}".format(new))

    def parse_username_string(self, input_string):
        username = input_string
        level = 0
        if (input_string.startswith("AI_") and len(input_string) == 4):
            parts = input_string.split("_")
            if len(parts) == 2 and parts[1].isdigit():
                username = parts[0]
                level = int(parts[1])
                if level < 1 or level > 8:
                    level = 1 # reset level
        return username, level

    def handle_call_trigger(self, entity, attribute, old, new, kwargs):

        if (new and new != UNAVAILABLE_STATE and new != UNKNOWN_STATE and new != EMPTY_CALL):
            # Convert JSON string to Python dictionary
            json_data = json.loads(new)
            call_type = json_data.get('type', None)

            if (json_data and call_type):
                valid_game_id = self.__class__._current_game_id != IDLE_GAME_ID and self.__class__._current_game_id != UNAVAILABLE_STATE and self.__class__._current_game_id != UNKNOWN_STATE
                valid_token   = self.__class__._current_token != EMPTY_STRING and self.__class__._current_token != UNAVAILABLE_STATE and self.__class__._current_token != UNKNOWN_STATE
                if (valid_token):
                    if (valid_game_id):                        
                        # make a call without body
                        if (call_type in {'makeMove', 'abort', 'resign', 'claim-victory'}):   
                            self.__class__._current_call_description = call_type                                                 
                            if (call_type == 'makeMove'):
                                self.__class__._current_url = URL_TEMPLATE_MOVE.format(self.__class__._current_game_id, json_data.get('move'))  
                                self.__class__._current_call_description += " " + json_data.get('move')                          
                            if (call_type == 'abort'):
                                self.__class__._current_url = URL_TEMPLATE_ABORT.format(self.__class__._current_game_id)
                            if (call_type == 'resign'):
                                self.__class__._current_url = URL_TEMPLATE_RESIGN.format(self.__class__._current_game_id)
                            if (call_type == 'claim-victory'):
                                self.__class__._current_url = URL_TEMPLATE_CLAIM_VICTORY.format(self.__class__._current_game_id)                            
                            self.__class__._current_body = EMPTY_CALL

                        # handle draw / tackback offers or send offers
                        if (json_data.get('type') in {'draw', 'takeback' } and json_data.get('parameter')):
                            if (call_type == 'draw'):
                                self.__class__._current_url = URL_TEMPLATE_DRAW.format(self.__class__._current_game_id, json_data.get('parameter'))
                            if (call_type == 'takeback'):
                                self.__class__._current_url = URL_TEMPLATE_TAKEBACK.format(self.__class__._current_game_id, json_data.get('parameter'))
                            self.__class__._current_call_description = call_type + " " + json_data.get('parameter')
                            self.__class__._current_body = EMPTY_CALL  

                    # handle challenge request
                    if (call_type == 'createGame' and json_data.get('opponentname')):

                        if (json_data.get('opponentname') == 'random'): # we create a seek

                            self.__class__._current_body = {      
                                "rated": json_data.get('rated', False),                      
                                "variant": "standard",
                                "color": json_data.get('color', "random"),
                                "time": json_data.get('time_m', 15),
                                "increment": json_data.get('increment', 0)
                            }
                            self.__class__._current_call_description = f"Seek new game ({json_data.get('time_m', 15)}+{json_data.get('increment', 0)})"
                            self.__class__._current_url = URL_TEMPLATE_SEEK

                        else: # create a challenge

                            self.__class__._current_body = {                            
                                "variant": "standard",
                                "color": json_data.get('color'),
                                "keepAliveStream": False,
                                "clock": {
                                            "limit": json_data.get('time_s'),
                                            "increment": json_data.get('increment')
                                            }                            
                            }

                            # check if challenge to a user or AI
                            username, level = self.parse_username_string(json_data.get('opponentname'))                        
                            if (level == 0): # challege a user                            
                                self.__class__._current_body["rated"] = json_data.get('rated', False)
                            else:
                                self.__class__._current_body["level"] = level
                                username = "ai" # we need lowercase for the url
                            
                            self.__class__._current_url = URL_TEMPLATE_CHALLENGE.format(username) 
                            self.__class__._current_call_description = f"Seek new challenge with {json_data.get('opponentname')} ({json_data.get('time_s')}+{json_data.get('increment')})"

                    # post api pody                    
                    self.lichess_api_call()
                    

    # function to post the request to lichess api
    def lichess_api_call(self):
        if (self.__class__._current_url != EMPTY_STRING):

            self.log(f"Starting the api call: {self.__class__._current_call_description}")            
            self.log(f"URL: {self.__class__._current_url}")
            self.log(f"Body: {self.__class__._current_body}")

            try:
                if (self.__class__._current_body == EMPTY_CALL):
                    response = requests.post(self.__class__._current_url, headers=self.__class__._current_header)
                else:
                    response = requests.post(self.__class__._current_url , json=self.__class__._current_body, headers=self.__class__._current_header)

                if (response.status_code == 200 or response.status_code == 201):
                    self.log("Succsessed api call")
                else:
                    self.log(f"Error: {response.status_code}, Response: {response.text}")

            except requests.exceptions.RequestException as e:
                self.log(f"Error: {e}")
            
            # reset all values
            self.__class__._current_url = EMPTY_STRING
            self.__class__._current_body = EMPTY_CALL
            self.__class__._current_call_description = EMPTY_STRING

            # we are ready to go
            self.log(f"Waiting for new api call")


