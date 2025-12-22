import json
import yaml
from datetime import timedelta, datetime, timezone
from typing import Optional


def get_secret(key, path="./secrets.yaml"):
    with open(path, "r") as file:
        secrets = yaml.safe_load(file)
    return secrets.get(key, "")
    
def load_secrets(path: str = "./secrets.yaml") -> dict:
    """Load YAML secrets from a local file path."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError as e:
        raise RuntimeError(f"Secrets file not found: {path}") from e
    except Exception as e:
        raise RuntimeError(f"Failed to load secrets.yaml: {e}") from e

def payload_to_str(payload):
    if payload is None:
        return None
    if isinstance(payload, bytes):
        try:
            return payload.decode("utf-8", errors="strict")
        except Exception:
            try:
                return payload.decode("utf-8", errors="replace")
            except Exception:
                return None
    if isinstance(payload, str):
        return payload
    try:
        return str(payload)
    except Exception:
        return None

def concat_values(val1, val2, sep = '<+>'):
    return val1 + sep + val2   

def split_concated_values(val: str, sep = '<+>'):
    return val.split(sep)
    
def parse_username_string(input_string):
    username = input_string
    level = 0
    if input_string.startswith("AI_") and len(input_string) == 4:
        parts = input_string.split("_")
        if len(parts) == 2 and parts[1].isdigit():
            username = parts[0]
            level = int(parts[1])
            if level < 1 or level > 8:
                level = 1
    return username, level

def decrypt_message(current_secret_key, hex_string):
    decrypted = hex_string
    if (hex_string is not None and hex_string not in ("idle", "unknown", "unavailable", "") and current_secret_key is not None and current_secret_key != ""):
        try:
            encrypted_bytes = bytes.fromhex(hex_string)
            decrypted = "".join(
                chr(b ^ ord(current_secret_key[i % len(current_secret_key)]))
                for i, b in enumerate(encrypted_bytes)
            )
        except ValueError as e:
            # just pass
            decrypted = hex_string            
    return decrypted

def td_to_sec(x):
    if x is None:
        return 0
    if isinstance(x, timedelta):
        return int(round(x.total_seconds()))
    if isinstance(x, datetime):
        # Berserk should not send absolute datetimes for clocks,
        # but if it does, treat it as 0 or extract seconds safely
        return int(x.timestamp())
    # fallback if it ever becomes ms-int again
    return int(round(int(x) / 1000))

def check_game_over(dat):
    break_game = False     
    if isinstance(dat, str):   
        dat = json.loads(dat)
    if (dat.get('type', None) == 'gameState' and dat.get('status', None) != 'started'):
        break_game = True
    if (dat.get('type', None) == 'gameFull' and dat.get('state', {}).get('status', None) != 'started'):
        break_game = True
    if (dat.get('type', None) == 'opponentGone' and dat.get('gone', None) == True and dat.get('claimWinInSeconds', None) == 0):
        break_game = True
    
    return break_game

def reduce_response_board(gid, dat):
    reduced_data = dat

    if dat.get("type") == "gameState":
        reduced_data = {
            "type": dat.get("type", ""),
            "wclk": f"{td_to_sec(dat.get('wtime'))}+{td_to_sec(dat.get('winc'))}",
            "bclk": f"{td_to_sec(dat.get('btime'))}+{td_to_sec(dat.get('binc'))}",
            "state": dat.get("status", ""),
            "win": {"white": "w", "black": "b"}.get(dat.get("winner", ""), ""),
            "wdraw": int(bool(dat.get("wdraw", False))),
            "bdraw": int(bool(dat.get("bdraw", False))),
            "wback": int(bool(dat.get("wtakeback", False))),
            "bback": int(bool(dat.get("btakeback", False))),
            "n": len(dat.get("moves", "").split()) if dat.get("moves") else -1,
            "last": dat.get("moves", "").split()[-1] if dat.get("moves") else "",
            "id": gid,
        }

    if dat.get("type") == "gameFull":
        reduced_data = {
            "type": dat.get("type", ""),
            "wid": "{}: {}".format(
                dat.get("white", {}).get("name", "white"),
                dat.get("white", {}).get("rating", 0),
            ),
            "bid": "{}: {}".format(
                dat.get("black", {}).get("name", "black"),
                dat.get("black", {}).get("rating", 0),
            ),
            "wclk": "{}+{}".format(
                round(dat.get("state", {}).get("wtime", 0) / 1000),
                round(dat.get("state", {}).get("winc", 0) / 1000),
            ),
            "bclk": "{}+{}".format(
                round(dat.get("state", {}).get("btime", 0) / 1000),
                round(dat.get("state", {}).get("binc", 0) / 1000),
            ),
            "state": dat.get("state", {}).get("status", ""),
            "win": {"white": "w", "black": "b"}.get(dat.get("state", {}).get("winner", ""), ""),
            "wdraw": int(dat.get("state", {}).get("wdraw", False)),
            "bdraw": int(dat.get("state", {}).get("bdraw", False)),
            "wback": int(dat.get("state", {}).get("wtakeback", False)),
            "bback": int(dat.get("state", {}).get("btakeback", False)),
            "last": dat.get("state", {}).get("moves", "").split()[-1] if dat.get("moves") else "",
            "id": gid,
        }

    if dat.get("type") == "chatLine":
        reduced_data = {"type": "chatLine", "text": dat.get("text", ""), "id": gid}
        max_text_length = 255 - len(json.dumps({"type": "chatLine", "id": "12345678", "text": ""}))
        if len(reduced_data["text"]) > max_text_length:
            reduced_data["text"] = reduced_data["text"][: max_text_length - 3] + "..."

    if dat.get("type") == "opponentGone":
        reduced_data["id"] = gid

    return reduced_data

def reduce_response_event(dat):
    reduced_data = dat

    if dat.get("type") == "gameStart":
        reduced_data = {
            "type": dat.get("type", ""),
            "gameId": dat.get("game", {}).get("gameId", ""),
            "color": dat.get("game", {}).get("color", ""),
            "isMyTurn": dat.get("game", {}).get("isMyTurn", False),
            "lastMove": dat.get("game", {}).get("lastMove", ""),
            "opponent": "{}: {}".format(
                dat.get("game", {}).get("opponent", {}).get("username", "player"),
                dat.get("game", {}).get("opponent", {}).get("rating", 0),
            ),
            "rated": dat.get("game", {}).get("rated", False),
            "speed": dat.get("game", {}).get("speed", ""),
            "secondsLeft": dat.get("game", {}).get("secondsLeft", 0),
        }
    else:
        if dat.get("type") == "gameFinish":
            reduced_data = {
                "type": dat.get("type", ""),
                "gameId": dat.get("game", {}).get("gameId", ""),
                "color": dat.get("game", {}).get("color", ""),
                "isMyTurn": dat.get("game", {}).get("isMyTurn", False),
                "lastMove": dat.get("game", {}).get("lastMove", ""),
                "opponent": "{}: {}".format(
                    dat.get("game", {}).get("opponent", {}).get("username", "player"),
                    dat.get("game", {}).get("opponent", {}).get("rating", 0),
                ),
                "rated": dat.get("game", {}).get("rated", False),
                "speed": dat.get("game", {}).get("speed", ""),
                "status": dat.get("game", {}).get("status", {}).get("name", ""),
                "win": {"white": "1-0", "black": "0-1"}.get(dat.get("game", {}).get("winner", ""), ""),
            }
        else:
            if dat.get("type") in {"challenge", "challengeCanceled", "challengeDeclined"}:
                reduced_data = {
                    "type": dat.get("type", ""),
                    "id": dat.get("challenge", {}).get("id", ""),
                    "status": dat.get("challenge", {}).get("status", ""),
                }

    return reduced_data

def getAccountInfoMain(lichess_client, self_log=None):
    account_info = lichess_client.account.get()
    if self_log:
        self_log("getAccountInfoMain: " + account_info["username"])
    data = {
        "type": "accountInfoMain",
        "name": account_info["username"],
        "blitz": account_info["perfs"]["blitz"]["rating"],
        "rapid": account_info["perfs"]["rapid"]["rating"],
        "classical": account_info["perfs"]["classical"]["rating"],
    }
    return json.dumps(data)


def abortRunningGames(lichess_client, self_log=None):
    my_games = lichess_client.games.get_ongoing()
    if len(my_games) > 0:
        if self_log:
            self_log(f"Number of running games: {len(my_games)}")
        for game in my_games:
            if self_log:
                self_log("Aborting: " + game["gameId"])
                lichess_client.board.abort_game(game_id=game["gameId"])

def createGame(json_data, lichess_client, lichess_client_opponent, self_log=None):

    if json_data.get("opponentname"):

        opponetns_name = json_data.get("opponentname", None)

        game_id = "idle"
        game_data = {"error": "Failed to create game"}

        if opponetns_name == "random":
            if self_log:
                self_log(f"Seek new game ({json_data.get('time_m', 15)}+{json_data.get('increment', 0)})")
            lichess_client.board.seek(
                time=json_data.get("time_m", 15),
                increment=json_data.get("increment", 0),
                rated=json_data.get("rated", False),
                color=json_data.get("color", "random"),
            )
        else:
            username, level = parse_username_string(opponetns_name)
            if self_log:
                self_log(f"Seek new challenge with {opponetns_name} " + f"({json_data.get('time_s')}+{json_data.get('increment')}), otb={json_data.get('otb')}")

            if level == 0:
                if json_data.get("otb") == "yes":
                    game_data = lichess_client.challenges.create(
                        username=username,
                        rated=json_data.get("rated", False),
                        clock_limit=json_data.get("time_s", 600),
                        clock_increment=json_data.get("increment", 0),
                        color=json_data.get("color"),
                        variant="standard",
                    )
                    if len(game_data.get("id", "")) == 8:
                        if lichess_client_opponent is not None:
                            lichess_client_opponent.challenges.accept(game_data["id"])
                        else:
                            self_log(f"Accepting of the challenge is not possible. Wait till opponent accepts the challenge!")
                        game_id = game_data["id"]
                    else:
                        game_id = ""
                else:
                    game_data = lichess_client.challenges.create(
                        username=username,
                        rated=json_data.get("rated", False),
                        clock_limit=json_data.get("time_s", 600),
                        clock_increment=json_data.get("increment", 0),
                        color=json_data.get("color"),
                        variant="standard",
                    )
                    if len(game_data.get("id", "")) != 8:
                        game_id = ""
            else:
                game_data = lichess_client.challenges.create_ai(
                    level=level,
                    clock_limit=json_data.get("time_s", 600),
                    clock_increment=json_data.get("increment", 0),
                    color=json_data.get("color"),
                    variant="standard",
                )
                if len(game_data.get("id", "")) == 8:
                    game_id = game_data["id"]
                else:
                    game_id = ""

        if game_id == "idle":
            data = {"type": "createdGameId", "id": "seek", "status": "success", "error": ""}
        else:
            if len(game_id) == 8:
                data = {"type": "createdGameId", "id": game_id, "status": "success", "error": ""}
            else:
                data = {
                    "type": "createdGameId",
                    "id": "-",
                    "status": "failed",
                    "error": game_data.get("error", "Failed to create game"),
                }
        return json.dumps(data)
    else:
        return json.dumps(
            {
                "type": "createdGameId",
                "id": "-",
                "status": "error",
                "error": "No opponent name provided",
            }
        )

    

def withdrawTornament(json_data, lichess_client, self_log=None):
    lichess_client.tournaments.withdraw_arena(json_data.get("id"))

def joinTournamentByName(json_data, lichess_client, self_log=None):

    all_tournaments = lichess_client.tournaments.get()

    starts_in = "-"
    tournament_id = ""
    for tournament in all_tournaments[json_data.get("tournamentStatus")]:
        if self_log:
            self_log("Tournament: " + tournament["fullName"])
        if (
            tournament["fullName"] == json_data.get("tournamentName")
            and tournament["createdBy"] == "lichess"
            and tournament["system"] == "arena"
            and tournament["clock"]["limit"] == json_data.get("limit")
            and tournament["clock"]["increment"] == json_data.get("increment")
        ):
            difference_minutes = (tournament["startsAt"] - datetime.now(timezone.utc)).total_seconds() / 60
            tournament_id = tournament["id"]
            starts_in = f"{difference_minutes:.2f}min"
            break

    if len(tournament_id) == 8:
        if self_log:
            self_log("Joining arena tournament: " + tournament_id)
        lichess_client.tournaments.join_arena(
            tournament_id=tournament_id, should_pair_immediately=True
        )
        data = {
            "type": "tournamentJoinedByName",
            "id": tournament_id,
            "starts_in": starts_in,
            "status": "success",
        }
    else:
        data = {
            "type": "tournamentJoinedByName",
            "id": tournament_id,
            "starts_in": "-",
            "status": "failed",
        }

    return json.dumps(data)

def joinTournamentById(json_data, lichess_client, self_log=None):
    tournament_id = json_data.get("id")
    if len(tournament_id) == 8:
        lichess_client.tournaments.join_arena(
            tournament_id=tournament_id, should_pair_immediately=True
        )
        data = {
            "type": "tournamentJoinedById",
            "id": tournament_id,
            "starts_in": "-",
            "status": "success",
        }
    else:
        data = {
            "type": "tournamentJoinedByName",
            "id": tournament_id,
            "starts_in": "-",
            "status": "failed",
        }

    return json.dumps(data)


def abort(lichess_client, current_game_id, self_log=None):
    lichess_client.board.abort_game(game_id=current_game_id)


def resign(lichess_client,current_game_id, self_log=None):
    lichess_client.board.resign_game(game_id=current_game_id)


def claimVictory(lichess_client,current_game_id, self_log=None):
    lichess_client.board.claim_victory(game_id=current_game_id)


def makeMove(json_data, lichess_client,current_game_id, self_log=None):
    if self_log:
        self_log("Move: " + json_data.get("move"))
    lichess_client.board.make_move(
        game_id=current_game_id, move=json_data.get("move")
    )


def draw(json_data, lichess_client,current_game_id, self_log=None):
    lichess_client.board.handle_draw_offer(
        game_id=current_game_id, accept=json_data.get("parameter")
    )


def takeback(json_data, lichess_client,current_game_id, self_log=None):
    lichess_client.board.handle_takeback_offer(
        game_id=current_game_id, accept=json_data.get("parameter")
    )


def writeChatMessage(json_data, lichess_client,current_game_id, self_log=None):
    lichess_client.board.post_message(
        game_id=current_game_id, text=json_data.get("text")
    )

def makeMoveOpponent(json_data, lichess_client,current_game_id, self_log=None):
    if self_log:
        self_log("Opponents move: " + json_data.get("move"))
    lichess_client.board.make_move(
        game_id=current_game_id, move=json_data.get("move")
    )

def resignOpponent(lichess_client,current_game_id, self_log=None):
    lichess_client.board.resign_game(game_id=current_game_id)

def drawOpponent(json_data, lichess_client,current_game_id, self_log=None):
    lichess_client.board.handle_draw_offer(
        game_id=current_game_id, accept=json_data.get("parameter")
    )

def write_into_chat(json_data, lichess_client, current_game_id, room="player", self_log=None):
    lichess_client.board.chat(game_id=current_game_id, room=room, text=json_data.get("text"))