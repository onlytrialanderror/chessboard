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


LICHESS_SINGLE_CALL_PARAMETER_IN_SENSOR = 'sensor.chessboard_lichess_api_call_opponent'
LICHESS_GAME_ID_SENSOR = "sensor.chessboard_lichess_game_id"
LICHESS_RESPONSE_OUT_SENSOR = 'sensor.chessboard_lichess_response_out'
LICHESS_TOKEN_OPPONENT_SENSOR = 'sensor.chessboard_lichess_api_token_opponent'

class LichessRequestHandlerOpponent(hass.Hass):

    _current_game_id = IDLE_GAME_ID
    _current_token_opponent = IDLE_LICHESS_TOKEN
    _client_opponent = EMPTY_STRING
    _session_opponent = EMPTY_STRING
    _current_secret_key = EMPTY_STRING

    def initialize(self):        
        self.log("AppDaemon LichessRequestHandler (opponent) script initialized!")
        self.__class__._current_secret_key = self.get_secret()
        self.listen_state(self.game_id_changed, LICHESS_GAME_ID_SENSOR)
        self.listen_state(self.handle_call_trigger, LICHESS_SINGLE_CALL_PARAMETER_IN_SENSOR)
        self.listen_state(self.token_changed_opponent, LICHESS_TOKEN_OPPONENT_SENSOR)
        # we are ready to go
        self.log(f"Waiting for new api call (opponent)")

    def get_secret(self, path="/config/secrets.yaml"):
        with open(path, "r") as file:
            secrets = yaml.safe_load(file)
        return secrets.get('chessboard_secret_key')

    def game_id_changed(self, entity, attribute, old, new, kwargs):
        if new and new != self.__class__._current_game_id:
            self.log(f"Game ID changed (opponent request): {self.__class__._current_game_id} -> {new}")
            self.__class__._current_game_id = new   

    def token_changed_opponent(self, entity, attribute, old, new, kwargs):
        if new  and new != UNAVAILABLE_STATE and new != UNKNOWN_STATE and new != EMPTY_STRING and self.decrypt_message(new) != self.__class__._current_token_opponent:
            new_decrypted = self.decrypt_message(new)
            self.log(f"Opponent token changed (opponent request opponent): {self.__class__._current_token_opponent} -> {new_decrypted}")
            self.__class__._current_token_opponent = new_decrypted
            self.__class__._session_opponent = berserk.TokenSession(self.__class__._current_token_opponent)
            self.__class__._client_opponent = berserk.Client(self.__class__._session_opponent)
        else:
            if new is None or new == UNAVAILABLE_STATE or new == UNKNOWN_STATE: 
                self.log("Not allowed token (opponent request opponent): {}".format(new))
   
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
                valid_token   = self.__class__._current_token_opponent != EMPTY_STRING and self.__class__._current_token_opponent != UNAVAILABLE_STATE and self.__class__._current_token_opponent != UNKNOWN_STATE

                if valid_token: 

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

                            




