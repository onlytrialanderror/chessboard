import appdaemon.plugins.hass.hassapi as hass
import requests
import json
import yaml

IDLE_GAME_ID = "idle"
UNAVAILABLE_STATE = "unavailable"
UNKNOWN_STATE = "unknown"
EMPTY_STRING = ''
EMPTY_CALL = "idle"
IDLE_LICHESS_TOKEN = "idle"
EMPTY_HEADER = {"Content-Type": "application/json" }


LICHESS_SINGLE_CALL_PARAMETER_IN_SENSOR = 'sensor.chessboard_lichess_api_call'
LICHESS_STREAM_PARAMETER_IN_SENSOR = "sensor.chessboard_lichess_stream_call"
LICHESS_RESPONSE_OUT_SENSOR = 'sensor.chessboard_lichess_response_out'

URL_TEMPLATE_MOVE = "https://lichess.org/api/board/game/{}/move/{}"
URL_TEMPLATE_RESIGN = "https://lichess.org/api/board/game/{}/resign"
URL_TEMPLATE_CHALLENGE_LIST = "https://lichess.org/api/challenge"
URL_TEMPLATE_CHALLENGE_ACCEPT = "https://lichess.org/api/challenge/{}/accept"

class LichessSingleRequestOpponent(hass.Hass):

    _current_game_id = IDLE_GAME_ID
    _current_token = EMPTY_STRING
    _current_call_description = EMPTY_STRING
    _current_url = EMPTY_STRING
    _current_header = EMPTY_HEADER
    _current_body = EMPTY_CALL
    _current_secret_key = EMPTY_STRING

    def initialize(self):        
        self.log("AppDaemon LichessSingleRequestOpponent script initialized!")
        self.__class__._current_secret_key = self.get_secret()
        self.listen_state(self.parameter_in_changed, LICHESS_STREAM_PARAMETER_IN_SENSOR)
        self.listen_state(self.handle_call_trigger, LICHESS_SINGLE_CALL_PARAMETER_IN_SENSOR)
        # we are ready to go
        self.log(f"Waiting for new api opponent call")

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
                if (new_data.get('type') == 'initializeOpponentToken' and new_data.get('token') != IDLE_LICHESS_TOKEN):
                    self.token_changed(new_data.get('token'))
        else:
            self.log("Not valid json (api opponent): {}".format(new))

    def game_id_changed(self, new):
        if new and new != self.__class__._current_game_id:
            self.log(f"Game ID changed (api opponent): {self.__class__._current_game_id} -> {new}")
            self.__class__._current_game_id = new

    def token_changed(self, new):
        if new and new != self.__class__._current_token and new != UNAVAILABLE_STATE and new != UNKNOWN_STATE and new != EMPTY_STRING:
            new_decrypted = self.decrypt_message(new)
            self.log(f"Token changed (api opponent): {self.__class__._current_token} -> {new_decrypted}")
            self.__class__._current_token = new_decrypted
        else:
            if new is None or new == UNAVAILABLE_STATE or new == UNKNOWN_STATE: 
                self.log("Not allowed token (api opponent): {}".format(new))
   
    def decrypt_message(self, hex_string):
        decrypted = hex_string
        if hex_string is not None and hex_string != UNAVAILABLE_STATE and hex_string != UNKNOWN_STATE and hex_string != EMPTY_STRING and hex_string != IDLE_LICHESS_TOKEN:
            try:
                # Convert hex to bytes
                encrypted_bytes = bytes.fromhex(hex_string)
                decrypted = ''.join(chr(b ^ ord(self.__class__._current_secret_key[i % len(self.__class__._current_secret_key)])) for i, b in enumerate(encrypted_bytes))
            except ValueError as e:
                self.log("Not valid hex-string: {}".format(hex_string))
                self.log(f"Error: {e}")
        return decrypted

    def handle_call_trigger(self, entity, attribute, old, new, kwargs):

        if (new and new != UNAVAILABLE_STATE and new != UNKNOWN_STATE and new != EMPTY_CALL):
            # Convert JSON string to Python dictionary
            json_data = json.loads(new)
            call_type = json_data.get('type', None)

            if (json_data and call_type):

                ######################################
                ##### token only #####################
                ######################################
                valid_token   = self.__class__._current_token != EMPTY_STRING and self.__class__._current_token != UNAVAILABLE_STATE and self.__class__._current_token != UNKNOWN_STATE
                if valid_token: 

                    if (call_type == 'acceptChallenge' and json_data.get('parameter')):
              
                        self.__class__._current_call_description = call_type + ": list of " + json_data.get('parameter')
                        self.__class__._current_url = URL_TEMPLATE_CHALLENGE_LIST
                        self.__class__._current_body = EMPTY_CALL 
                        response = self.lichess_api_call_get()

                        # Load JSON data
                        if 'application/json' in response.headers.get('Content-Type', ''):

                            try: 
                                json_response = response.json()  # This will raise an exception if invalid

                                curr_challenge_id = ""
                                # Loop through each item in the "in" list
                                for in_challenge in json_response['in']:
                                    if in_challenge['challenger']['name'] == json_data.get('parameter'):
                                        curr_challenge_id = in_challenge['id'] # we found the challenger from the board
                                        break

                                if curr_challenge_id != "":
                                    # we accept challenge with this ID
                                    self.__class__._current_url = URL_TEMPLATE_CHALLENGE_ACCEPT.format(curr_challenge_id)
                                    self.__class__._current_call_description = call_type + ": accept " + curr_challenge_id
                                    # post api body                    
                                    self.lichess_api_call_post()       
                            except requests.exceptions.JSONDecodeError:
                                self.log(f"Invalid json-response: {call_type}")
                                
                valid_game_id = self.__class__._current_game_id != IDLE_GAME_ID and self.__class__._current_game_id != UNAVAILABLE_STATE and self.__class__._current_game_id != UNKNOWN_STATE
                if (valid_token and valid_game_id):  
                    ####################################
                    ######### CALLS WITHOUT BODY #######                           
                    ####################################
                    if (call_type in {'makeMoveOpponent', 'resignOpponent'}):   
                        self.__class__._current_call_description = call_type                                                 
                        if (call_type == 'makeMoveOpponent'):
                            self.__class__._current_url = URL_TEMPLATE_MOVE.format(self.__class__._current_game_id, json_data.get('move'))  
                            self.__class__._current_call_description += " " + json_data.get('move')                          
                        if (call_type == 'resignOpponent'):
                            self.__class__._current_url = URL_TEMPLATE_RESIGN.format(self.__class__._current_game_id)
                        self.__class__._current_body = EMPTY_CALL

                        # post api body                    
                        self.lichess_api_call_post()

    # function to post the request to lichess api
    def lichess_api_call_post(self):
        valid = False
        if (self.__class__._current_url != EMPTY_STRING):

            self.log(f"Starting the api call (opponent): {self.__class__._current_call_description}")            
            self.log(f"URL: {self.__class__._current_url}")
            self.log(f"Body: {self.__class__._current_body}")

            try:
                if (self.__class__._current_body == EMPTY_CALL):
                    self.__class__._current_header = {
                                        "Authorization": f"Bearer {self.__class__._current_token}"
                                    }
                    response = requests.post(self.__class__._current_url, headers=self.__class__._current_header)
                else:
                    self.__class__._current_header = {
                                        "Content-Type": "application/json",
                                        "Authorization": f"Bearer {self.__class__._current_token}"
                                    } 
                    response = requests.post(self.__class__._current_url , json=self.__class__._current_body, headers=self.__class__._current_header)

                if (response.status_code == 200 or response.status_code == 201):
                    self.log("Succsessed api post call (opponent)")
                    valid = True
                else:
                    self.log(f"Error: {response.status_code}, Response: {response.text}")

            except requests.exceptions.RequestException as e:
                self.log(f"Error: {e}")
            
            # reset all values
            self.__class__._current_url = EMPTY_STRING
            self.__class__._current_body = EMPTY_CALL
            self.__class__._current_call_description = EMPTY_STRING

            # we are ready to go
            self.log(f"Waiting for new api call (opponent)")
        return valid


    def lichess_api_call_get(self):
        response = ""
        if (self.__class__._current_url != EMPTY_STRING):

            self.log(f"Starting the api call (opponent): {self.__class__._current_call_description}")            
            self.log(f"URL: {self.__class__._current_url}")
            self.log(f"Body: {self.__class__._current_body}")

            try:
                if (self.__class__._current_body == EMPTY_CALL):
                    self.__class__._current_header = {
                                        "Authorization": f"Bearer {self.__class__._current_token}"
                                    }
                    response = requests.get(self.__class__._current_url, headers=self.__class__._current_header)
                else:
                    self.__class__._current_header = {
                                        "Content-Type": "application/json",
                                        "Authorization": f"Bearer {self.__class__._current_token}"
                                    } 
                    response = requests.get(self.__class__._current_url , json=self.__class__._current_body, headers=self.__class__._current_header)  

                if (response.status_code == 200):
                    self.log("Succsessed api get call (opponent)")
                else:
                    self.log(f"Error: {response.status_code}, Response: {response.text}")

            except requests.exceptions.RequestException as e:
                self.log(f"Error: {e}")
            
            # reset all values
            self.__class__._current_url = EMPTY_STRING
            self.__class__._current_body = EMPTY_CALL
            self.__class__._current_call_description = EMPTY_STRING

            # we are ready to go
            self.log(f"Waiting for new api call (opponent)")
        return response

