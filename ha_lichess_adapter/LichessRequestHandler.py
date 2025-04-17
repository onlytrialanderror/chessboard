import appdaemon.plugins.hass.hassapi as hass
import requests
import berserk
import json
import yaml
from datetime import datetime

IDLE_GAME_ID = "idle"
UNAVAILABLE_STATE = "unavailable"
UNKNOWN_STATE = "unknown"
EMPTY_STRING = ''
IDLE_LICHESS_TOKEN = "idle"


LICHESS_SINGLE_CALL_PARAMETER_IN_SENSOR = 'sensor.chessboard_lichess_api_call'
LICHESS_STREAM_PARAMETER_IN_SENSOR = "sensor.chessboard_lichess_stream_call"
LICHESS_RESPONSE_OUT_SENSOR = 'sensor.chessboard_lichess_response_out'



class LichessRequestHandler(hass.Hass):

    _current_game_id = IDLE_GAME_ID
    _current_token_main = IDLE_LICHESS_TOKEN
    _current_token_opponent = IDLE_LICHESS_TOKEN
    _current_secret_key = IDLE_LICHESS_TOKEN
    _client_main = EMPTY_STRING
    _client_opponent = EMPTY_STRING
    _current_secret_key = EMPTY_STRING

    def initialize(self):        
        self.log("AppDaemon LichessRequestHandler script initialized!")
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
                if (new_data.get('type') == 'initializeToken'):
                    self.token_changed_main(new_data.get('token'))
                if (new_data.get('type') == 'initializeTokenOpponent'):
                    self.token_changed_opponent(new_data.get('token'))
        else:
            self.log("Not valid json: {}".format(new))
            if (new == UNAVAILABLE_STATE or new == UNKNOWN_STATE):
                self.__class__._current_game_id = IDLE_GAME_ID


    def game_id_changed(self, new):
        if new and new != self.__class__._current_game_id:
            self.log(f"Game ID changed (request): {self.__class__._current_game_id} -> {new}")
            self.__class__._current_game_id = new

    def token_changed_main(self, new):
        if new and new != UNAVAILABLE_STATE and new != UNKNOWN_STATE and new != EMPTY_STRING:
            new_decrypted = self.decrypt_message(new)
            if (new_decrypted != self.__class__._current_token_main):
                self.log(f"Token changed (request main): {self.__class__._current_token_main} -> {new_decrypted}")
                self.__class__._current_token_main = new_decrypted
                if (self.__class__._current_token_main != IDLE_LICHESS_TOKEN):
                    session_main = berserk.TokenSession(self.__class__._current_token_main)
                    self.__class__._client_main = berserk.Client(session_main)
                else:
                    self.__class__._client_main = EMPTY_STRING
        else:
            if new is None or new == UNAVAILABLE_STATE or new == UNKNOWN_STATE: 
                self.log("Not allowed token (request main): {}".format(new))

    def token_changed_opponent(self, new):
        if new and new != UNAVAILABLE_STATE and new != UNKNOWN_STATE and new != EMPTY_STRING:
            new_decrypted = self.decrypt_message(new)
            if (new_decrypted != self.__class__._current_token_opponent):
                self.log(f"Token changed (request opponent): {self.__class__._current_token_opponent} -> {new_decrypted}")
                self.__class__._current_token_opponent = new_decrypted
                if (self.__class__._client_opponent != IDLE_LICHESS_TOKEN):
                    session_opponent = berserk.TokenSession(self.__class__._current_token_opponent)
                    self.__class__._client_opponent = berserk.Client(session_opponent)
                else:
                    self.__class__._client_opponent = EMPTY_STRING
        else:
            if new is None or new == UNAVAILABLE_STATE or new == UNKNOWN_STATE: 
                self.log("Not allowed token (request opponent): {}".format(new))

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

        if (new and new != UNAVAILABLE_STATE and new != UNKNOWN_STATE and new != IDLE_GAME_ID and new != EMPTY_STRING):
            # Convert JSON string to Python dictionary
            json_data = json.loads(new)
            call_type = json_data.get('type', None)

            if (json_data and call_type):

                ######################################
                ##### token only #####################
                ######################################
                valid_token   = self.__class__._current_token_main != EMPTY_STRING and self.__class__._current_token_main != UNAVAILABLE_STATE and self.__class__._current_token_main != UNKNOWN_STATE

                if valid_token: 
                    # ------------------------------------
                    ############# create new Game ############
                    # ------------------------------------
                    if (call_type == 'createGame' and json_data.get('opponentname')):

                        # we create a seek
                        if (json_data.get('opponentname') == 'random'): 
                            self.log(f"Seek new game ({json_data.get('time_m', 15)}+{json_data.get('increment', 0)})")
                            self.__class__._client_main.board.seek(
                                time=json_data.get('time_m', 15),
                                increment=json_data.get('increment', 0),
                                rated=json_data.get('rated', False),
                                color=json_data.get('color', "random")
                            )
                        # create a challenge
                        else: 
                            # check if challenge to a user or AI
                            username, level = self.parse_username_string(json_data.get('opponentname'))   
                            self.log(f"Seek new challenge with {json_data.get('opponentname')} ({json_data.get('time_s')}+{json_data.get('increment')}), otb={json_data.get('otb')}")                     
                            if (level == 0): 
                                if (json_data.get('otb') == 'yes'):
                                    # challenge a friend by name otb                       
                                    game_data = self.__class__._client_main.challenges.create(
                                        username = username,
                                        rated = json_data.get('rated', False), 
                                        clock_limit = json_data.get('time_s', 600), 
                                        clock_increment =  json_data.get('increment', 0), 
                                        color = json_data.get('color'), 
                                        variant =  "standard"                                        
                                    )
                                    # accept challenge by the firend otb
                                    if (len(game_data["id"])):
                                        self.__class__._client_opponent.challenges.accept(game_data["id"])
                               
                                else:
                                    # challenge a fried by name online                       
                                    game_data = self.__class__._client_main.challenges.create(
                                        username = username, 
                                        rated = json_data.get('rated', False), 
                                        clock_limit = json_data.get('time_s', 600), 
                                        clock_increment =  json_data.get('increment', 0), 
                                        color = json_data.get('color'), 
                                        variant =  "standard"
                                    )                             
                            else:
                                # challenge AI
                                self.__class__._client_main.challenges.create_ai(
                                    level = level,
                                    clock_limit = json_data.get('time_s', 600), 
                                    clock_increment =  json_data.get('increment', 0), 
                                    color = json_data.get('color'), 
                                    variant =  "standard"
                                )  
                            
                    # -----------------------------------------------------
                    ############# pause or withdraw tournament ############
                    # -----------------------------------------------------
                    if (call_type == 'withdrawTornament' and json_data.get('id')):
              
                        self.log(call_type + ": " + json_data.get('id'))
                        self.__class__._client_main.tournaments.withdraw_arena(json_data.get('id'))

                    # -----------------------------------------------------
                    ################# joinTournamentByName ################
                    # -----------------------------------------------------
                    if (call_type == 'joinTournamentByName' and json_data.get('tournamentName')):
                          
                        self.log(call_type + ": " + json_data.get('tournamentName'))
                        all_tournaments = self.__class__._client_main.tournaments.get()

                        # Loop through each item in the "in" list
                        starts_in = '-'
                        tournament_id = ""
                        for tournament in all_tournaments[json_data.get('tournamentStatus')]:
                            self.log("Tournament: " + tournament)
                            if tournament['fullName'] == json_data.get('tournamentName') and tournament['createdBy'] == 'lichess' and tournament['system'] == 'arena' and tournament['clock']['limit'] == json_data.get('limit') and tournament['clock']['increment'] == json_data.get('increment'):
                                # Difference
                                difference_minutes = berserk.utils.timedelta_from_millis(berserk.utils.to_millis(datetime.now()), berserk.utils.to_millis(tournament['startsAt'])) / 60000
                                tournament_id = tournament['id']
                                starts_in = f"{difference_minutes:.2f}min"
                                break # we found closes tournament, we can break

                        json_data = "{}"
                        if len(tournament_id) == 8:
                            # try to join
                            self.log("Joining arena tournament: " + tournament_id)
                            self.__class__._client_main.tournaments.join_arena(tournament_id = tournament_id, should_pair_immediately = True)
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

                    # -----------------------------------------------------
                    ################# joinTournamentByID ################
                    # -----------------------------------------------------
                    if (call_type == 'joinTournamentById' and json_data.get('id')):
                          
                        tournament_id = json_data.get('id')
                        json_data = "{}"
                        if len(tournament_id) == 8:

                            # try to join
                            self.log(call_type + ": " + tournament_id)
                            self.__class__._client_main.tournaments.join_arena(tournament_id = tournament_id, should_pair_immediately = True)                            
                            data = {
                                "type": "tournamentJoinedById",
                                "id": tournament_id,
                                "starts_in": "-",
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

                    # -----------------------------------------------------
                    ################# getAccountInfoMain ################
                    # -----------------------------------------------------
                    if (call_type == 'getAccountInfoMain'):
                          
                        # try to join
                        self.log(call_type)
                        account_info = self.__class__._client_main.account.get()
                        data = {
                            "type": "accountInfoMain",
                            "name": account_info["username"],
                            "blitz": account_info["perfs"]["blitz"]["rating"],
                            "rapid": account_info["perfs"]["rapid"]["rating"],
                            "classical": account_info["perfs"]["classical"]["rating"]
                        }
                        json_data = json.dumps(data)
                        # let chessboard know about the tournament id
                        self.set_state(LICHESS_RESPONSE_OUT_SENSOR, state=json_data)


                    # -----------------------------------------------------
                    ################# acceptChallenge ################
                    # -----------------------------------------------------
                    if (call_type == 'acceptChallenge' and json_data.get('parameter')):
              
                        self.log(call_type + ": list of " + json_data.get('parameter'))
                        my_challenges = self.__class__._client_opponent.challenges.get_mine()
                        curr_challenge_id = ""
                        # Loop through each item in the "in" list
                        for in_challenge in my_challenges['in']:
                            if in_challenge['challenger']['name'] == json_data.get('parameter'):
                                curr_challenge_id = in_challenge['id'] # we found the challenger from the board
                                break

                        if len(curr_challenge_id) == "":
                            # we accept challenge with this ID
                            self.log(call_type + ": accept " + curr_challenge_id)
                            self.__class__._client_opponent.challenges.accept(curr_challenge_id)      



                ######################################
                ##### token and id required ########
                ######################################
                valid_game_id = self.__class__._current_game_id != IDLE_GAME_ID and self.__class__._current_game_id != UNAVAILABLE_STATE and self.__class__._current_game_id != UNKNOWN_STATE
                if (valid_token and valid_game_id):  
                    ####################################
                    ######### CALLS WITHOUT PARAMETERS #######                           
                    ####################################
                    if (call_type in { 'abort', 'resign', 'claim-victory'}):   
                        self.log(call_type)
                        if (call_type == 'abort'):
                            self.__class__._client_main.board.abort_game(game_id = self.__class__._current_game_id)
                        if (call_type == 'resign'):
                            self.__class__._client_main.board.resign_game(game_id = self.__class__._current_game_id)
                        if (call_type == 'claim-victory'):
                            self.__class__._client_main.board.claim_victory(game_id = self.__class__._current_game_id)


                    ####################################
                    ######### CALLS WITH PARAMETERS ##########                           
                    ####################################

                    # handle draw / tackback offers or send offers
                    if (call_type in {'makeMove', 'draw', 'takeback', 'writeChatMessage' }):
                        if (call_type == 'makeMove'):                             
                            self.log(call_type + ": " + json_data.get('move'))    
                            self.__class__._client_main.board.make_move(game_id = self.__class__._current_game_id, move = json_data.get('move'))
                        if (call_type == 'draw'):
                            self.log(call_type + " accept " + str(json_data.get('parameter')))
                            self.__class__._client_main.board.handle_draw_offer(game_id = self.__class__._current_game_id, accept = json_data.get('parameter') )
                        if (call_type == 'takeback'):
                            self.log(call_type + " accept " + str(json_data.get('parameter')))
                            self.__class__._client_main.board.handle_takeback_offer(game_id = self.__class__._current_game_id, accept = json_data.get('parameter') )                 
                        if (call_type == 'writeChatMessage'):
                            self.log(call_type)
                            self.__class__._client_main.board.post_message(game_id = self.__class__._current_game_id, text = json_data.get('text') ) 

                    ####################################
                    ######### OPPONENTS CALLs #######                           
                    ####################################
                    if (call_type in {'makeMoveOpponent', 'resignOpponent', 'drawOpponent'}):   
                        if (call_type == 'makeMoveOpponent'):
                            self.log(call_type + ": " + json_data.get('move'))    
                            self.__class__._client_opponent.board.make_move(game_id = self.__class__._current_game_id, move = json_data.get('move'))                         
                        if (call_type == 'resignOpponent'):
                            self.log(call_type)
                            self.__class__._client_opponent.board.resign_game(game_id = self.__class__._current_game_id)
                        if (call_type == 'drawOpponent'):
                            self.log(call_type + " accept " + str(json_data.get('parameter')))
                            self.__class__._client_opponent.board.handle_draw_offer(game_id = self.__class__._current_game_id, accept = json_data.get('parameter') )

                            





