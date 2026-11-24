"""
Remote Chess - Opponent Simulator (runs on a PC, not on the ESP32)

Test helper for the simplified board-to-board program when only ONE
physical board is available. The physical board plays White; this
script simulates the Black player's board:

- prints every L:/P: event received from the White board
- answers a completed White move with "synced" (as if Black had
  mirrored it on their board)
- then plays a random counter-move (L:/P: on the black stream);
  mirror it on the physical board so it can answer with "synced"
- answers "occ:" reconciliation requests by echoing the received
  bitmap, so the reconciliation always succeeds immediately
- handles the setup phase (replies to "setup_ok" / "restart")

Requirements:  pip install paho-mqtt
Configuration: credentials are read from secrets.py in this folder —
               the same (gitignored) file the ESP32 uses. Copy
               secrets_sample.py as secrets.py, fill it in, then
               run:  python opponent_simulator.py
"""

import random
import time

import paho.mqtt.client as mqtt

# Credentials and topics come from the local secrets.py (gitignored).
# It shadows Python's stdlib "secrets" module, which is fine here.
try:
    from secrets import MQTT_USER, MQTT_PASSWORD, MQTT_TOPIC_WHITE, MQTT_TOPIC_BLACK
except ImportError:
    raise SystemExit(
        "secrets.py not found next to this script — copy "
        "secrets_sample.py as secrets.py and fill in "
        "MQTT_USER (Adafruit IO username) and MQTT_PASSWORD (AIO key)."
    )

# ============= CONFIGURATION =============

BROKER   = "io.adafruit.com"
PORT     = 8883                  # TLS; use 1883 without TLS
USE_TLS  = True
USERNAME = MQTT_USER             # Adafruit IO username
AIO_KEY  = MQTT_PASSWORD         # Adafruit IO key (used as MQTT password)

TOPIC_WHITE = MQTT_TOPIC_WHITE   # physical board publishes here
TOPIC_BLACK = MQTT_TOPIC_BLACK   # this simulator publishes here

SYNC_DELAY_S    = 2.0    # settle time after White's last event before "synced"
REPLY_MIN_S     = 2.0    # random counter-move starts 2-4 s after "synced"
REPLY_MAX_S     = 4.0
PLACE_DELAY_S   = 1.5    # pause between our L and P event

# ============= OCCUPANCY MODEL =============
# rows[0] = chess row 8 ... rows[7] = chess row 1 (same order as the ESP bitmap)
# 'w' = white piece, 'b' = black piece, '?' = occupied/unknown, None = empty

def initial_position():
    rows = [[None] * 8 for _ in range(8)]
    rows[0] = ['b'] * 8   # row 8
    rows[1] = ['b'] * 8   # row 7
    rows[6] = ['w'] * 8   # row 2
    rows[7] = ['w'] * 8   # row 1
    return rows

board = initial_position()

def coord_to_index(coord):
    """'E2' -> (row_idx, col_idx) with row_idx 0 = chess row 8."""
    col = ord(coord[0].upper()) - ord('A')
    row = 8 - int(coord[1])
    return row, col

def index_to_coord(row_idx, col_idx):
    return chr(ord('A') + col_idx) + str(8 - row_idx)

def squares_with(value):
    return [
        index_to_coord(r, c)
        for r in range(8) for c in range(8)
        if board[r][c] == value
    ]

# ============= GAME STATE =============

state = {
    "setup_done": False,
    "white_events": [],       # events of the White move currently in progress
    "saw_place": False,       # move is complete once at least one P arrived
    "last_white_event": 0.0,
    "synced_due": None,       # time to send "synced" (None = nothing pending)
    "reply_due": None,        # time to start our random counter-move
    "place_due": None,        # time to send the P event of our counter-move
    "place_coord": None,
}

def publish(client, message):
    client.publish(TOPIC_BLACK, message)
    print(f"  -> sent: {message}")

# ============= MQTT CALLBACKS =============

def on_connect(client, userdata, flags, reason_code, properties=None):
    print(f"Connected to {BROKER} (result: {reason_code})")
    client.subscribe(TOPIC_WHITE)
    print(f"Subscribed to {TOPIC_WHITE}")
    # Force the physical board into a fresh setup phase
    publish(client, "restart")
    print("Waiting for the board's setup_ok ...")

def on_message(client, userdata, msg):
    message = msg.payload.decode("utf-8")
    print(f"<- received: {message}")

    if message == "restart":
        global board
        board = initial_position()
        state["setup_done"] = False
        state["white_events"] = []
        state["saw_place"] = False
        state["synced_due"] = state["reply_due"] = state["place_due"] = None
        print("Board restarted — occupancy model reset, waiting for setup_ok")

    elif message == "setup_ok":
        board[:] = initial_position()
        publish(client, "setup_ok")
        state["setup_done"] = True
        print("=== Setup complete — make a White move on the board ===")

    elif message == "synced":
        print("Board mirrored our move — waiting for the next White move")

    elif message.startswith("occ:"):
        bitmap = message[4:]
        if len(bitmap) != 64:
            print(f"Invalid occ bitmap length: {len(bitmap)}")
            return
        # Echo the bitmap so the reconciliation succeeds immediately,
        # and adopt it as our occupancy model (colors kept where known).
        for idx, char in enumerate(bitmap):
            r, c = idx // 8, idx % 8
            if char == '0':
                board[r][c] = None
            elif board[r][c] is None:
                board[r][c] = '?'
        publish(client, "occ:" + bitmap)
        state["white_events"] = []
        state["saw_place"] = False
        state["synced_due"] = None
        print("Occupancy adopted from board — reconciliation answered")

    elif message.startswith(("L:", "P:")) and len(message) >= 4:
        coord = message[2:4].upper()
        r, c = coord_to_index(coord)
        if message[0] == 'L':
            board[r][c] = None
        else:
            board[r][c] = 'w'
            state["saw_place"] = True
        state["white_events"].append(message)
        state["last_white_event"] = time.time()
        if state["saw_place"]:
            state["synced_due"] = time.time() + SYNC_DELAY_S

# ============= RANDOM COUNTER-MOVE =============

def start_counter_move(client):
    black_squares = squares_with('b')
    empty_squares = squares_with(None)
    if not black_squares or not empty_squares:
        print("No plausible counter-move available — skipping")
        return
    source = random.choice(black_squares)
    target = random.choice(empty_squares)
    print(f"=== Random counter-move: {source} -> {target} ===")
    print("    (mirror it on the board: lift the RED square,")
    print("     place the piece on the GREEN square)")
    r, c = coord_to_index(source)
    board[r][c] = None
    publish(client, f"L:{source.lower()}")
    state["place_coord"] = target
    state["place_due"] = time.time() + PLACE_DELAY_S

def finish_counter_move(client):
    target = state["place_coord"]
    r, c = coord_to_index(target)
    board[r][c] = 'b'
    publish(client, f"P:{target.lower()}")
    state["place_coord"] = None
    state["place_due"] = None

# ============= MAIN LOOP =============

def main():
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except AttributeError:              # paho-mqtt < 2.0
        client = mqtt.Client()
    client.username_pw_set(USERNAME, AIO_KEY)
    if USE_TLS:
        client.tls_set()
    client.on_connect = on_connect
    client.on_message = on_message

    print(f"Connecting to {BROKER}:{PORT} ...")
    client.connect(BROKER, PORT, keepalive=60)
    client.loop_start()

    try:
        while True:
            now = time.time()

            # White's move settled -> confirm with "synced", schedule our reply
            if state["synced_due"] and now >= state["synced_due"] \
                    and now - state["last_white_event"] >= SYNC_DELAY_S:
                moves = " ".join(state["white_events"])
                print(f"White move complete ({moves}) — sending synced")
                publish(client, "synced")
                state["white_events"] = []
                state["saw_place"] = False
                state["synced_due"] = None
                state["reply_due"] = now + random.uniform(REPLY_MIN_S, REPLY_MAX_S)

            # Time for our random counter-move
            if state["reply_due"] and now >= state["reply_due"]:
                state["reply_due"] = None
                start_counter_move(client)

            # Second half of our counter-move (the P event)
            if state["place_due"] and now >= state["place_due"]:
                finish_counter_move(client)

            time.sleep(0.05)

    except KeyboardInterrupt:
        print("\nShutting down...")
        client.loop_stop()
        client.disconnect()

if __name__ == "__main__":
    main()
