import appdaemon.plugins.hass.hassapi as hass
import requests
import json
import yaml
from datetime import datetime

IDLE_GAME_ID = "idle"
UNAVAILABLE_STATE = "unavailable"
UNKNOWN_STATE = "unknown"
EMPTY_STRING = ''
EMPTY_CALL = "idle"
IDLE_LICHESS_TOKEN = "idle"
EMPTY_HEADER = {"Content-Type": "application/json" }


LICHESS_SINGLE_CALL_PARAMETER_IN_SENSOR = 'sensor.chessboard_lichess_api_call'
LICHESS_STREAM_PARAMETER_IN_SENSOR = "sensor.chessboard_lichess_stream_call"
LICHESS_RESPONSE_OUT_SENSOR = 'sensor.chessboard_response_out'

URL_TEMPLATE_SEEK = "https://lichess.org/api/board/seek"
URL_TEMPLATE_MOVE = "https://lichess.org/api/board/game/{}/move/{}"
URL_TEMPLATE_ABORT = "https://lichess.org/api/board/game/{}/abort"
URL_TEMPLATE_RESIGN = "https://lichess.org/api/board/game/{}/resign"
URL_TEMPLATE_CHAT = "https://lichess.org/api/board/game/{}/chat"
URL_TEMPLATE_CLAIM_VICTORY = "https://lichess.org/api/board/game/{}/claim-victory"
URL_TEMPLATE_DRAW = "https://lichess.org/api/board/game/{}/draw/{}"
URL_TEMPLATE_TAKEBACK = "https://lichess.org/api/board/game/{}/takeback/{}"
URL_TEMPLATE_CHALLENGE = "https://lichess.org/api/challenge/{}"
URL_TEMPLATE_CHALLENGE_LIST = "https://lichess.org/api/challenge"
URL_TEMPLATE_CHALLENGE_ACCEPT = "https://lichess.org/api/challenge/{}/accept"
URL_TEMPLATE_TOURNAMENT_LIST = "https://lichess.org/api/tournament"
URL_TEMPLATE_TOURNAMENT_JOIN = "https://lichess.org/api/tournament/{}/join"
URL_TEMPLATE_TOURNAMENT_WITHDRAW = "https://lichess.org/api/tournament/{}/withdraw"


class LichessSingleRequest(hass.Hass):

    _current_game_id = IDLE_GAME_ID
    _current_token = EMPTY_STRING
    _current_call_description = EMPTY_STRING
    _current_url = EMPTY_STRING
    _current_header = EMPTY_HEADER
    _current_body = EMPTY_CALL
    _current_secret_key = EMPTY_STRING

    def initialize(self):        
        self.log("AppDaemon LichessSingleRequest script initialized!")
        self.__class__._current_secret_key = self.get_secret()
        self.listen_state(self.parameter_in_changed, LICHESS_STREAM_PARAMETER_IN_SENSOR)
        self.listen_state(self.handle_call_trigger, LICHESS_SINGLE_CALL_PARAMETER_IN_SENSOR)
        # we are ready to go
        self.log(f"Waiting for new api call")

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
                if (new_data.get('type') == 'initializeToken' and new_data.get('token') != IDLE_LICHESS_TOKEN):
                    self.token_changed(new_data.get('token'))
        else:
            self.log("Not valid json: {}".format(new))

    def game_id_changed(self, new):
        if new and new != self.__class__._current_game_id:
            self.log(f"Game ID changed: {self.__class__._current_game_id} -> {new}")
            self.__class__._current_game_id = new
            self.stream_game()

    def token_changed(self, new):
        if new and new != self.__class__._current_token and new != UNAVAILABLE_STATE and new != UNKNOWN_STATE and new != EMPTY_STRING:
            new_decrypted = self.decrypt_message(new)
            self.log(f"Token changed (api): {self.__class__._current_token} -> {new_decrypted}")
            self.__class__._current_token = new_decrypted
        else:
            if new is None or new == UNAVAILABLE_STATE or new == UNKNOWN_STATE: 
                self.log("Not allowed token (api): {}".format(new))

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

    def handle_call_trigger(self, entity, attribute, old, new, kwargs):

        if (new and new != UNAVAILABLE_STATE and new != UNKNOWN_STATE and new != EMPTY_CALL):
            # Convert JSON string to Python dictionary
            json_data = json.loads(new)
            call_type = json_data.get('type', None)

            if (json_data and call_type):
                ######################################
                ##### no token or id required ########
                ######################################
                if (call_type == 'overwriteToken' and json_data.get('token')):
                    self.token_changed(None, None, self.__class__._current_token, json_data.get('token'), None)

                if (call_type == 'overwriteGameId' and json_data.get('gameId')):
                    self.game_id_changed(None, None, self.__class__._current_game_id, json_data.get('gameId'), None)

                ######################################
                ##### token only #####################
                ######################################
                valid_token   = self.__class__._current_token != EMPTY_STRING and self.__class__._current_token != UNAVAILABLE_STATE and self.__class__._current_token != UNKNOWN_STATE
                if valid_token: 

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

                        # post api body                    
                        self.lichess_api_call_post()

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

                    if (call_type == 'withdrawTornament' and json_data.get('id')):
              
                        self.__class__._current_call_description = call_type + ": " + json_data.get('id')
                        self.__class__._current_url = URL_TEMPLATE_TOURNAMENT_WITHDRAW.format(json_data.get('id'))
                        self.__class__._current_body = EMPTY_CALL 

                        self.lichess_api_call_post()

                    if (call_type == 'joinTournamentByName' and json_data.get('tournamentName')):
                          
                        self.__class__._current_call_description = call_type + ": " + json_data.get('tournamentName')
                        self.__class__._current_url = URL_TEMPLATE_TOURNAMENT_LIST
                        self.__class__._current_body = EMPTY_CALL 

                        response = self.lichess_api_call_get()

                        if 'application/json' in response.headers.get('Content-Type', ''):

                            try: 

                                json_response = response.json()  # This will raise an exception if invalid
                                starts_in = ""
                                
                                # Loop through each item in the "in" list
                                for tournament in json_response[json_data.get('tournamentStatus')]:
                                    if tournament['fullName'] == json_data.get('tournamentName') and tournament['createdBy'] == 'lichess' and tournament['system'] == 'arena' and tournament['clock']['limit'] == json_data.get('limit') and tournament['clock']['increment'] == json_data.get('increment'):
                                        # Difference
                                        difference = datetime.fromtimestamp(tournament['startsAt'] / 1000) - datetime.now()
                                        difference_minutes = difference.total_seconds() / 60
                                        tournament_id = tournament['id']
                                        starts_in = f"{difference_minutes:.2f}min"
                                        break # we found closes tournament, we can break

                            except requests.exceptions.JSONDecodeError:
                                print("Invalid json-response")

                        if len(tournament_id) == 8:

                            # try to join
                            self.__class__._current_call_description = "Joining tournament: " + tournament_id
                            self.__class__._current_url = URL_TEMPLATE_TOURNAMENT_JOIN.format(tournament_id)
                            self.__class__._current_body = EMPTY_CALL 

                            json_data = "{}"

                            # post api body                    
                            if (self.lichess_api_call_post()):
                                data = {
                                    "type": "tournamentJoinedByName",
                                    "id": tournament_id,
                                    "starts_in": starts_in,
                                    "status": "success"
                                }
                                json_data = json.dumps(data)
                            else:                                
                                data = {
                                    "type": "tournamentJoinedByName",
                                    "id": tournament_id,
                                    "starts_in": "-",
                                    "status": "failed"
                                }
                                json_data = json.dumps(data)
                            # let chessboard know about the tournament id
                            self.set_state(LICHESS_RESPONSE_OUT_SENSOR, state=json_data)

                    if (call_type == 'joinTournamentById' and json_data.get('id')):
                          
                        tournament_id = json_data.get('id')

                        if len(tournament_id) == 8:

                            # try to join
                            self.__class__._current_call_description = call_type + ": " + tournament_id
                            self.__class__._current_url = URL_TEMPLATE_TOURNAMENT_JOIN.format(tournament_id)
                            self.__class__._current_body = EMPTY_CALL 

                            json_data = "{}"

                            # post api body                    
                            if (self.lichess_api_call_post()):
                                data = {
                                    "type": "tournamentJoinedById",
                                    "id": tournament_id,
                                    "starts_in": "-",
                                    "status": "success"
                                }
                                json_data = json.dumps(data)
                            else:                                
                                data = {
                                    "type": "tournamentJoinedById",
                                    "id": tournament_id,
                                    "starts_in": "-",
                                    "status": "failed"
                                }
                                json_data = json.dumps(data)
                            # let chessboard know about the tournament id
                            self.set_state(LICHESS_RESPONSE_OUT_SENSOR, state=json_data)

                ######################################
                ##### no token and id required ########
                ######################################
                valid_game_id = self.__class__._current_game_id != IDLE_GAME_ID and self.__class__._current_game_id != UNAVAILABLE_STATE and self.__class__._current_game_id != UNKNOWN_STATE
                if (valid_token and valid_game_id):  
                    ####################################
                    ######### CALLS WITHOUT BODY #######                           
                    ####################################
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

                        # post api body                    
                        self.lichess_api_call_post()

                    # handle draw / tackback offers or send offers
                    if (call_type in {'draw', 'takeback' } and json_data.get('parameter')):
                        if (call_type == 'draw'):
                            self.__class__._current_url = URL_TEMPLATE_DRAW.format(self.__class__._current_game_id, json_data.get('parameter'))
                        if (call_type == 'takeback'):
                            self.__class__._current_url = URL_TEMPLATE_TAKEBACK.format(self.__class__._current_game_id, json_data.get('parameter'))
                        self.__class__._current_call_description = call_type + " " + json_data.get('parameter')
                        self.__class__._current_body = EMPTY_CALL  

                        # post api body                    
                        self.lichess_api_call_post()

                    ####################################
                    ######### CALLS WITH BODY ##########                           
                    ####################################
                    if (call_type in {'writeChatMessage' } and json_data.get('text')):
                        self.__class__._current_body = {      
                            "room": "player",                      
                            "text": json_data.get('text')
                        }
                        self.__class__._current_call_description = call_type
                        self.__class__._current_url = URL_TEMPLATE_CHAT.format(self.__class__._current_game_id)

                        # post api body                    
                        self.lichess_api_call_post()


                    

    # function to post the request to lichess api
    def lichess_api_call_post(self):
        valid = False
        if (self.__class__._current_url != EMPTY_STRING):

            self.log(f"Starting the api call: {self.__class__._current_call_description}")            
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
                    self.log("Succsessed api post call")
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
            self.log(f"Waiting for new api call")
        return valid

    def lichess_api_call_get(self):
        response = ""
        if (self.__class__._current_url != EMPTY_STRING):

            self.log(f"Starting the api call: {self.__class__._current_call_description}")            
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
                    self.log("Succsessed api get call")
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
        return response


