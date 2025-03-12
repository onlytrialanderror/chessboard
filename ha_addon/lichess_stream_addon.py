import asyncio
import httpx
import json
import paho.mqtt.client as mqtt

debug = True

IDLE_GAME_ID = "idle"
current_game_id = IDLE_GAME_ID  # Initial game ID

def read_mqtt_config(file_path = "mqtt_config.json"):
    """Reads MQTT configuration from a JSON file in the same directory."""
    try:
        with open(file_path, "r") as file:
            config = json.load(file)
            return {
                "broker": config.get("MQTT_BROKER"),
                "port": config.get("MQTT_PORT", 1883),  # Default to 1883 if missing
                "username": config.get("MQTT_USERNAME"),
                "password": config.get("MQTT_PASSWORD")
            }
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Error reading MQTT config: {e}")
        return None
    
def read_lichess_config(file_path = "lichess_config.json"):
    """Reads lichess configuration from a JSON file in the same directory."""
    try:
        with open(file_path, "r") as file:
            config = json.load(file)
            return {
                "token": config.get("LICHESS_TOKEN")
            }
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Error reading lichess config: {e}")
        return None

# MQTT Client callbacks
def on_connect(client, userdata, flags, reason_code, properties):
    print(f"Connected to MQTT broker with result code {reason_code}")
    # Subscribe to the topics where the game_id and token will be published
    client.subscribe(GAME_ID_TOPIC)
    client.subscribe(TOKEN_TOPIC)


def on_message(client, userdata, msg):
    global current_game_id, current_token
    if debug:
        print(f"Received message on topic {msg.topic}: {msg.payload.decode('utf-8')}")

    try:
        if msg.topic == GAME_ID_TOPIC:
            new_game_id = msg.payload.decode('utf-8').strip()
            if new_game_id != "" and new_game_id != current_game_id:
                if debug:
                    print(f"Updating game_id to {new_game_id}")
                current_game_id = new_game_id

        elif msg.topic == TOKEN_TOPIC:
            new_token = msg.payload.decode('utf-8').strip()
            if new_token != current_token:
                if debug:
                    print(f"Updating token to {new_token}")
                current_token = new_token
                update_headers()

    except RuntimeError as e:
        print(f"Error processing message: {e}")

# Global variables
lichess_config = read_lichess_config()
current_token = lichess_config.get("token", '')

URL_TEMPLATE = "https://lichess.org/api/board/game/stream/{}"
HEADERS = {
    "Content-Type": "application/x-ndjson",
    "Authorization": f"Bearer {current_token}",
    "Connection": "keep-alive"
}


# MQTT Client setup
mqtt_config = read_mqtt_config()

MQTT_BROKER = mqtt_config.get("broker", 'localhost')
MQTT_PORT = mqtt_config.get("port", 1883)
MQTT_USERNAME = mqtt_config.get("username", 'mqtt')
MQTT_PASSWORD = mqtt_config.get("password", 'mqtt')

GAME_ID_TOPIC = "chessboard/game_id"  # Topic to listen for game_id
TOKEN_TOPIC = "chessboard/token"  # Topic to listen for token
STREAM_TOPIC = "chessboard/board_stream"  # Topic to listen for token


mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
# Set username and password
mqtt_client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message
mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)


# Start MQTT client loop in a background thread
def start_mqtt_loop():
    mqtt_client.loop_start()

def check_game_over(dat):
    global current_game_id
    if (dat.get('type') == 'gameState' and dat.get('status') != 'started'):
        current_game_id = IDLE_GAME_ID
    if (dat.get('type') == 'gameFull' and dat.get('state', {}).get('status') != 'started'):
        current_game_id = IDLE_GAME_ID
    if (dat.get('type') == 'opponentGone' and dat.get('gone') == True):
        current_game_id = IDLE_GAME_ID

def reduce_response(dat):
    
    reduced_data = dat

    # chat-line and opponent gone is not reduced

    # gameState
    if (dat.get('type') == 'gameState'):
        reduced_data = {
            "type": dat.get("type", ""),
            "wtime": dat.get("wtime", ""),
            "btime": dat.get("btime", ""),
            "status": dat.get("status", ""),
            "winner": dat.get("winner", ""),
            "wdraw": dat.get("wdraw", False),
            "bdraw": dat.get("bdraw", False),
            "wtakeback": dat.get("wtakeback", False),
            "btakeback": dat.get("btakeback", False),
            "last_move": dat.get("moves", "").split()[-1] if dat.get("moves") else ""
        }

    # gameFull
    if (dat.get('type') == 'gameFull'):
        reduced_data = {
            "type": dat.get('state', {}).get("type", ""),
            "black_name": dat.get('black', {}).get("name", "black"),
            "black_rating": dat.get('black', {}).get("rating", 0),
            "white_name": dat.get('white', {}).get("name", "white"),
            "white_rating": dat.get('white', {}).get("rating", 0),            
            "wtime": dat.get('state', {}).get("wtime", ""),
            "btime": dat.get('state', {}).get("btime", ""),
            "status": dat.get('state', {}).get("status", ""),
            "winner": dat.get('state', {}).get("winner", ""),
            "wdraw": dat.get('state', {}).get("wdraw", False),
            "bdraw": dat.get('state', {}).get("bdraw", False),
            "wtakeback": dat.get('state', {}).get("wtakeback", False),
            "btakeback": dat.get('state', {}).get("btakeback", False),
            "last_move": dat.get('state', {}).get("moves", "").split()[-1] if dat.get("moves") else ""
        }

    return reduced_data

# Async function to stream game
async def stream_game(game_id):

    # do not start a stream wihout valid game_id
    if game_id != IDLE_GAME_ID:
        global current_game_id
        url = URL_TEMPLATE.format(game_id)
        async with httpx.AsyncClient() as client:
            async with client.stream("GET", url, headers=HEADERS, timeout=None) as response:
                async for line in response.aiter_lines():
                    if line:
                        data = json.loads(line)
                        
                        if data:
                            reduced_data = reduce_response(data)
                            # Publish data to MQTT topic
                            mqtt_client.publish(STREAM_TOPIC, json.dumps(reduced_data))
                            
                        if debug:
                            print(reduced_data)  # Process the JSON object

                        

                        # check if we have to abort the game
                        if data:
                            check_game_over(data)

                        # Sleep to allow processing of new messages
                        await asyncio.sleep(0.5) 

                        # If game_id has changed dynamically, stop the stream
                        if game_id != current_game_id:                            
                            if debug:
                                print(f"Switching stream from {game_id} to {current_game_id}")
                            return  # Exit to restart with new game_id


# Update the headers with the new token
def update_headers():
    global current_token
    HEADERS["Authorization"] = f"Bearer {current_token}"
    if debug:
        print(f"Updated headers with new token: {current_token}")

# Main function that integrates the MQTT loop and game stream
async def main():
    # Start MQTT loop in the background
    start_mqtt_loop()

     # Keep the loop running indefinitely
    while True:

        # Start the first stream
        await stream_game(current_game_id)

        # Sleep to allow processing of new messages
        await asyncio.sleep(10)  

# Run the asyncio main loop
asyncio.run(main())