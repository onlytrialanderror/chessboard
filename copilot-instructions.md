# Copilot Instructions for this repository

Purpose
- Provide immediate, actionable context for AI coding agents working on this repo (Home Assistant AppDaemon adapter + Lichess integration).

Big picture
- This project implements an AppDaemon app that bridges Home Assistant (HA) with Lichess via MQTT and the `berserk` Lichess client library.
- Core runtime is the AppDaemon app in `ha_lichess_adapter/LichessApiHandler.py` which:
  - Maintains Lichess token sessions and `berserk.Client` instances.
  - Listens for MQTT messages on topics like `chessboard/api_call`, `chessboard/game_id`, `chessboard/token_main`, `chessboard/token_opponent`, `chessboard/status`.
  - Spawns worker threads to serialize API calls (`_api_worker`), stream incoming events (`_stream_worker`), and per-board stream workers (`_board_worker_main`, `_board_worker_opponent`).

Key files & entry points
- `/ha_lichess_adapter/LichessApiHandler.py`: primary AppDaemon app — study threading, token/session lifecycle, and MQTT message handlers (`_mqtt_on_message`, `_on_mqtt_token_main`, `_on_mqtt_game_id`).
- `/ha_lichess_adapter/lichess_helpers.py`: helper functions used across the app (encryption/decryption, payload handling, Lichess API wrappers). Use it for examples of API call patterns and payload shapes.
- `/ha_lichess_adapter/apps.yaml`: AppDaemon app configuration (check how AppDaemon is expected to load this app).
- `/requrenments.txt`: dependency manifest (note: filename appears misspelled — confirm before installing).
- `/chessboard.yaml` and `/chess_validator.h`: additional project config/headers — inspect as needed for wiring and validation rules.

> Example pattern: API calls are enqueued as JSON strings on MQTT `chessboard/api_call` and handled in `perform_api_call()` which uses `lichess_helpers` functions like `makeMove`, `draw`, `createGame` etc.

Important runtime patterns & conventions
- MQTT topics are treated as control channels: tokens and game IDs may be encrypted; tokens are decrypted with a secret key loaded from `SECRET_PATH` in `LichessApiHandler.initialize()`.
- Token/session lifecycle: when tokens change, the app closes existing `berserk.TokenSession` objects and recreates clients; look for `_init_session_...` and `_close_session_...` helpers.
- Thread coordination: workers use threading Events and a Queue (`self._api_q`) with sentinel values like `"terminate_api_worker"`. Locks (`self._lock`) protect shared state such as current tokens and game IDs.
- Stream shutdown: streams are interrupted by setting stop events and by performing a small Lichess call (e.g., `lh.abort(...)`) to provoke a stream event and let the loop check for termination.

Secrets & TLS
- Secrets loaded from `/config/secrets.yaml` via `lichess_helpers.load_secrets`. Look at `SECRET_PATH` and `CA_CERT_PATH` usage in `LichessApiHandler` for TLS and MQTT cert handling.

Relevant integration points
- External services: Lichess (via `berserk`) and an MQTT broker (configurable in secrets). Ensure tokens and MQTT credentials in `secrets.yaml` are valid for runtime testing.
- AppDaemon: this code runs inside AppDaemon as an HA app. Use `apps.yaml` to configure and test in an AppDaemon environment.

Developer workflows (what I could infer)
- Install dependencies from `/requrenments.txt` (confirm filename and contents) in a Python venv. Typical commands:

  python -m venv .venv
  .venv\Scripts\activate
  python -m pip install -r requrenments.txt

- Run AppDaemon with configured `apps.yaml` to exercise the AppDaemon app (or run unit tests if added). There are no test files present; add tests around `lichess_helpers.py` for deterministic logic.

Project-specific notes & gotchas
- Filename typo: `requrenments.txt` — confirm before scripting automated installs.
- MQTT topics and payload shapes are central; when adding features, update `perform_api_call()` and document any new `type` values expected in `chessboard/api_call` payloads.
- Concurrency: thread stop/restart logic is sensitive to token and game-id changes — modify carefully and add tests that simulate token rotation and stream termination.

What to edit here
- Update this file if you add new MQTT topics, change token lifecycle, add AppDaemon config, or introduce automation scripts for running AppDaemon/tests.

If anything is unclear or you'd like more detail (examples of `chessboard/api_call` payloads, or a scaffolded test runner), tell me which area to expand.
