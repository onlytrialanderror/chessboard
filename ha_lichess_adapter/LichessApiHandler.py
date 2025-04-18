import appdaemon.plugins.hass.hassapi as hass
import berserk
import json
import yaml
from datetime import datetime
import threading

IDLE_GAME_ID = "idle"
UNAVAILABLE_STATE = "unavailable"
UNKNOWN_STATE = "unknown"
EMPTY_STRING = ''
IDLE_LICHESS_TOKEN = "idle"
ON_STATE = "ON"
OFF_STATE = "OFF"
IDLE_STATE = 'idle'

LICHESS_SINGLE_CALL_PARAMETER_IN_SENSOR = 'sensor.chessboard_lichess_api_call'
LICHESS_RESPONSE_OUT_SENSOR = 'sensor.chessboard_lichess_response_out'

class LichessApiHandler(hass.Hass):

    _current_game_id = IDLE_GAME_ID
    _current_token_main = IDLE_LICHESS_TOKEN
    _current_token_opponent = IDLE_LICHESS_TOKEN
    _current_secret_key = IDLE_LICHESS_TOKEN
    _client_main = EMPTY_STRING
    _client_opponent = EMPTY_STRING
    _current_secret_key = EMPTY_STRING
    _stream_event = False
    _stop_event = EMPTY_STRING

    def initialize(self):        
        self.log("AppDaemon LichessApiHandler script initialized!")
        self.__class__._current_secret_key = self.get_secret()
        self.listen_state(self.api_call_changed, LICHESS_SINGLE_CALL_PARAMETER_IN_SENSOR)
        # we are ready to go
        self.log(f"Waiting for new api call")
        self._stop_event = threading.Event()

    def get_secret(self, path="/config/secrets.yaml"):
        with open(path, "r") as file:
            secrets = yaml.safe_load(file)
        return secrets.get('chessboard_secret_key')

    def game_id_changed(self, new):
        if new and new != self.__class__._current_game_id:
            self.log(f"Game ID changed: {self.__class__._current_game_id} -> {new}")
            self.__class__._current_game_id = new
            self.stream_game()

    def api_call_changed(self, entity, attribute, old, new, kwargs):
        if (new and new != IDLE_GAME_ID and new != UNAVAILABLE_STATE and new != UNKNOWN_STATE  and new != EMPTY_STRING):
            self.lichess_api_call(new)

    def token_changed_main(self, new):
        if new and new != UNAVAILABLE_STATE and new != UNKNOWN_STATE and new != EMPTY_STRING:
            new_decrypted = self.decrypt_message(new)
            if (new_decrypted != self.__class__._current_token_main):
                self.log(f"Token changed (main): {self.__class__._current_token_main} -> {new_decrypted}")
                self.__class__._current_token_main = new_decrypted
                if (self.__class__._current_token_main != IDLE_LICHESS_TOKEN):
                    session_main = berserk.TokenSession(self.__class__._current_token_main)
                    self.__class__._client_main = berserk.Client(session_main)
                else:
                    self.__class__._client_main = EMPTY_STRING
        else:
            if new is None or new == UNAVAILABLE_STATE or new == UNKNOWN_STATE: 
                self.log("Not allowed token (main): {}".format(new))

    def token_changed_opponent(self, new):
        if new and new != UNAVAILABLE_STATE and new != UNKNOWN_STATE and new != EMPTY_STRING:
            new_decrypted = self.decrypt_message(new)
            if (new_decrypted != self.__class__._current_token_opponent):
                self.log(f"Token changed (opponent): {self.__class__._current_token_opponent} -> {new_decrypted}")
                self.__class__._current_token_opponent = new_decrypted
                if (self.__class__._client_opponent != IDLE_LICHESS_TOKEN):
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

    def handle_call_trigger(self):
            # Convert JSON string to Python dictionary
            json_data = json.loads(self.__class__._api_call)
            call_type = json_data.get('type', None)

            if (json_data and call_type):
                ######################################
                ##### no token #####################
                ######################################

                # main token
                if (json_data.get('type') == 'initializeToken'):
                    self.token_changed_main(json_data.get('token'))
                # opponents token
                if (json_data.get('type') == 'initializeTokenOpponent'):
                    self.token_changed_opponent(json_data.get('token'))

                ######################################
                ##### token only #####################
                ######################################
                valid_token   = self.__class__._current_token_main != EMPTY_STRING and self.__class__._current_token_main != UNAVAILABLE_STATE and self.__class__._current_token_main != UNKNOWN_STATE
                valid_game_id = self.__class__._current_game_id != IDLE_GAME_ID and self.__class__._current_game_id != UNAVAILABLE_STATE and self.__class__._current_game_id != UNKNOWN_STATE

                if valid_token and not valid_game_id: 
                    # ------------------------------------
                    ############# set game id ############
                    # ------------------------------------
                    if (json_data.get('type') == 'setGameId'):
                        self.game_id_changed(json_data.get('gameId'))
                    # ------------------------------------
                    ############# start event stream ############
                    # ------------------------------------                    
                    if (json_data.get('type') == 'streamEvents' and json_data.get('state')):
                        self.stream_events_trigger(json_data.get('state'))
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
                                    if (len(game_data["id"])==8):
                                        self.__class__._client_opponent.challenges.accept(game_data["id"])
                                        self.game_id_changed(game_data["id"])
                               
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
                                    # open the stream
                                    if (len(game_data["id"])==8):
                                        self.game_id_changed(game_data["id"])                           
                            else:
                                # challenge AI
                                game_data = self.__class__._client_main.challenges.create_ai(
                                    level = level,
                                    clock_limit = json_data.get('time_s', 600), 
                                    clock_increment =  json_data.get('increment', 0), 
                                    color = json_data.get('color'), 
                                    variant =  "standard"
                                )
                                # open the stream
                                if (len(game_data["id"])==8):
                                    self.game_id_changed(game_data["id"])   
                            
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

                ######################################
                ##### token and id required ########
                ######################################                
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
            if (self.__class__._stream_event == True):
                self.stream_events_trigger(IDLE_STATE)
            self.__class__._stream_event = False
            off_json = {
                        "type": "streamEvents",
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
                        self.log(f"Stopping the stream (main): {self.__class__._current_game_id}")
                        break
            # close the stream
            if (len(self.__class__._current_game_id)==8):
                self.game_id_changed(IDLE_GAME_ID) 
            # reset stream for board on HA (esphome needs to do it as well)
            off_json = {
                        "type": "streamBoardMain",
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
                        self.log(f"Stopping the stream (opponent): {self.__class__._current_game_id}")
                        break
            # reset stream for board on HA (esphome needs to do it as well)
            off_json = {
                        "type": "streamBoardOpponent",
                        "state": IDLE_GAME_ID
                    }
            off_json_str = json.dumps(off_json)
            self.set_state(LICHESS_RESPONSE_OUT_SENSOR, state=off_json_str)
            # we are ready to go
            self.log(f"Waiting for new stream (opponent)")


    # function to stream game
    def stream_game(self):
        # create threads
        t_board_main = threading.Thread(target=self.handle_game_state_main)
        t_board_opponent = threading.Thread(target=self.handle_game_state_opponent)
        # Start threads
        t_board_main.start()
        t_board_opponent.start()

    # function to stream game
    def stream_event(self):
        # create thread
        t_event_main = threading.Thread(target=self.handle_incoming_events)
        # Start threads
        t_event_main.start()                            

    # function to stream game
    def lichess_api_call(self, new):
        # create thread
        t_api_call = threading.Thread(target=self.handle_call_trigger, args=(new,))
        # Start threads
        t_api_call.start()
        # we want to wait the call is finished
        t_api_call.join()


