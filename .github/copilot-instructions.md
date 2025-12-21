**Project Snapshot**

- **Purpose:** Firmware + ESPhome config for a physical chessboard plus a Home Assistant AppDaemon adapter that relays board events to Lichess and accepts Lichess API actions via MQTT.
- **Key runtime pieces:**
  - ESPhome device and firmware: [chessboard.yaml](chessboard.yaml)
  - AppDaemon Lichess adapter: [ha_lichess_adapter/LichessApiHandler.py](ha_lichess_adapter/LichessApiHandler.py#L1-L40)
  - Adapter helpers: [ha_lichess_adapter/lichess_helpers.py](ha_lichess_adapter/lichess_helpers.py#L1-L40)
  - App registration: [ha_lichess_adapter/apps.yaml](ha_lichess_adapter/apps.yaml)
  - Declared Python requirements (incomplete): [ha_lichess_adapter/requrenments.txt](ha_lichess_adapter/requrenments.txt)

**Big-picture architecture (how data flows)**
- The ESPhome firmware (device) publishes board state, tokens and incoming API requests over MQTT topics defined and used by the AppDaemon adapter. See MQTT config and `on_connect` flows in [chessboard.yaml](chessboard.yaml#L1-L60).
- The AppDaemon app (`LichessApiHandler`) subscribes to the MQTT topics below, decrypts tokens, opens Berserk sessions and maintains streaming and API threads to Lichess. Responses/events are published back to `chessboard/response`.

**Important topics & constants (source of truth)**
- Read topic constants in `LichessApiHandler.py`: MQTT_API_CALL_TOPIC=`chessboard/api_call`, MQTT_GAME_ID_TOPIC=`chessboard/game_id`, MQTT_TOKEN_MAIN_TOPIC=`chessboard/token_main`, MQTT_TOKEN_OPP_TOPIC=`chessboard/token_opponent`, MQTT_RESPONSE_TOPIC=`chessboard/response` ([LichessApiHandler.py#L1-L40]).

**Secrets & encryption**
- The AppDaemon adapter expects an XOR-like encrypted token hex string (helper `decrypt_message`) and reads `chessboard_secret_key` from `/config/secrets.yaml` (constant `SECRET_PATH` in `LichessApiHandler`). See decrypt/secret load in [lichess_helpers.py](ha_lichess_adapter/lichess_helpers.py#L1-L40) and `LichessApiHandler.initialize()`.

**Concurrency / lifetime model**
- `LichessApiHandler` serializes API requests with a queue worker (`_api_q`) to avoid concurrent client usage. Streams (events/board) run in their own daemon threads. When tokens or game IDs change the adapter stops/restarts the relevant worker threads. See worker lifecycle functions (`_run_*`, `_stop_*`) in [LichessApiHandler.py](ha_lichess_adapter/LichessApiHandler.py#L401-L700).

**Project-specific conventions & patterns**
- Tokens/game-id sentinel values: `"idle"` (IDLE_GAME_ID / IDLE_LICHESS_TOKEN) drive worker start/stop checks — treat these as canonical "no value".
- Token+GameID concatenation uses `concat_values(val1,val2)` with separator `<+>` (helpers in `lichess_helpers.py`) — used to form worker-init identifiers for board streams.
- Adapter returns compact JSON payloads to the firmware via `publish_response()` — `reduce_response_board()` and `reduce_response_event()` in `lichess_helpers.py` show the expected shapes.

**Common API-call payload examples**
- Minimal move request from firmware → adapter via `chessboard/api_call`:

  - Move (main): {"type":"makeMove","move":"e2e4"}
  - Move (opponent): {"type":"makeMoveOpponent","move":"e7e5"}
  - Start game (create): {"type":"createGame","opponentname":"random","time_m":15,"increment":0}
  - Account info: {"type":"getAccountInfoMain"}

See full `perform_api_call()` switch in [LichessApiHandler.py](ha_lichess_adapter/LichessApiHandler.py#L700-L900) for all supported `type` values and expected fields.

**External integrations & required packages**
- The adapter uses `berserk` (Lichess API), `paho-mqtt` (direct MQTT client), and `PyYAML`. The repo's `requrenments.txt` currently lists only `httpx` — treat that file as incomplete. Before running the adapter in AppDaemon, install at least:

  - `berserk`, `paho-mqtt`, `PyYAML`, `httpx` (example: `pip install berserk paho-mqtt PyYAML httpx`).

**Developer workflows / quick start**
- To iterate on the firmware: use `esphome` with [chessboard.yaml](chessboard.yaml). The device expects `secrets.yaml` entries (wifi, mqtt, chessboard_secret_key) under `/config` when running with Home Assistant.
- To run the adapter locally or in AppDaemon:
  - Ensure AppDaemon is configured and `ha_lichess_adapter` is placed in AppDaemon's `apps` directory. `apps.yaml` maps the module/class.
  - Install missing Python deps (see above).
  - Ensure `/config/secrets.yaml` contains `chessboard_secret_key` and an `mqtt` block matching `chessboard.yaml`.

**Where to look when debugging**
- Firmware state and MQTT traffic: `chessboard.yaml` (logger level and mqtt `on_connect` hooks) and the device web server logs.
- Adapter logs: AppDaemon `self.log()` calls sprinkled through `LichessApiHandler.py` — worker start/stop, token changes, and API call errors are logged. Grep for `self.log(` inside [ha_lichess_adapter/LichessApiHandler.py](ha_lichess_adapter/LichessApiHandler.py#L1-L40).

**Small gotchas discovered from reading the code**
- `requrenments.txt` is misspelled and incomplete — don't assume all dependencies are listed.
- Secrets path is hard-coded to `/config/secrets.yaml` (constant `SECRET_PATH`) — when running outside Home Assistant, mirror this path or change the code.
- The token decryption is a custom XOR-bytes-from-hex implementation in `lichess_helpers.decrypt_message` — tokens must be prepared accordingly.

If any of these sections are unclear or you want more automation (e.g., a script to validate and list missing Python packages, or to generate sample MQTT payloads), tell me which part to expand and I will iterate.
