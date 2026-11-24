"""
Remote Chess - Simplified Board-to-Board Program
For Xiao ESP32-C3/C6 with MicroPython

No move logic: the board only transmits raw lift/place events.
- Own lift   -> red LED + publish "L:<coord>"
- Own place  -> green LED + publish "P:<coord>"
- Opponent events light the same LEDs on this board; when the local
  sensors confirm the mirrored move, the LEDs turn off and "synced"
  is published. The moving board turns its LEDs off on "synced".
- If LEDs are still on SYNC_TIMEOUT after the last event, both boards
  exchange occupancy bitmaps ("occ:<64 chars>") and mismatching squares
  blink yellow until the players correct them.
The game is never blocked by a misdetected move: captures, castling and
en passant are just sequences of lift/place events, and any error ends
in the yellow-blink reconciliation instead of a stuck state machine.
"""

import time
from machine import Pin, I2C
from neopixel import NeoPixel
from umqtt.simple import MQTTClient
from secrets import *
from remote_chess_lib import (
    MCP23017_ADDRESSES, NUM_NEOPIXELS,
    mcp23017_init, mcp23017_read_gpio, read_occupancy_bitmap,
    sensor_to_coordinate, coordinate_to_board_index,
    set_neopixel, connect_wifi,
)

# ============= CONFIGURATION =============

# LED Colors (R, G, B)
COLOR_LIFT     = (100,   0,   0)   # Red: piece lifted
COLOR_PLACE    = (  0, 100,   0)   # Green: piece placed
COLOR_MISMATCH = (100, 100,   0)   # Yellow: occupancy mismatch / setup error
COLOR_OFF      = (  0,   0,   0)

# Timing
LIFT_DELAY   = 200     # ms debounce before publishing an L event
MOVE_DELAY   = 500     # ms debounce before publishing a P event
SYNC_TIMEOUT = 15000   # ms until occupancy reconciliation starts
OCC_SETTLE   = 500     # ms of sensor silence before re-publishing occupancy
BLINK_MS     = 400     # blink period for yellow LEDs

# Publish/subscribe topics and rook squares derived from PLAYER_COLOR in secrets.py
if PLAYER_COLOR == "white":
    PUBLISH_TOPIC   = MQTT_TOPIC_WHITE
    SUBSCRIBE_TOPIC = MQTT_TOPIC_BLACK
    ROOK_COORDS     = ("A1", "H1")
else:
    PUBLISH_TOPIC   = MQTT_TOPIC_BLACK
    SUBSCRIBE_TOPIC = MQTT_TOPIC_WHITE
    ROOK_COORDS     = ("A8", "H8")

# ============= GLOBAL STATE =============

# Own move indicators: coord -> 'L' or 'P'. Cleared when "synced" arrives.
own_leds = {}

# Opponent move to mirror: coord -> expected occupancy
# (False after "L:coord" = square must become empty,
#  True  after "P:coord" = square must become occupied).
remote_expect    = {}
remote_satisfied = set()   # coords in remote_expect currently matching the sensors

# Debounced own events: coord -> ('L'/'P', start ticks_ms)
pending = {}

# Occupancy reconciliation
resolution_active   = False
opp_bitmap          = None    # last occupancy bitmap received from opponent
mismatch_coords     = set()   # squares blinking yellow
occ_dirty           = False   # own occupancy changed during resolution
occ_change_ms       = 0
resolution_start_ms = 0

blink_on      = False
last_blink_ms = 0

last_event_ms = 0             # last event on either board (SYNC_TIMEOUT reference)

# Setup phase
setup_phase       = True
own_setup_ok      = False
opponent_setup_ok = False
restart_needed    = False

previous_hall_states = [[False] * 8 for _ in range(8)]

# Hardware handles (set in main)
i2c         = None
np          = None
mqtt_client = None
last_mqtt_activity = 0

# ============= HELPERS =============

def occupied(coord):
    """Current sensor state of a square (from the last scan)."""
    row_idx, col_idx = coordinate_to_board_index(coord)
    return previous_hall_states[row_idx][col_idx]

def clear_all_leds():
    for i in range(NUM_NEOPIXELS):
        np[i] = COLOR_OFF
    np.write()

def bitmap_index_to_coord(idx):
    return sensor_to_coordinate(idx // 8, idx % 8)

# ============= MQTT =============

def publish(message):
    global last_mqtt_activity
    try:
        mqtt_client.publish(PUBLISH_TOPIC, message)
        last_mqtt_activity = time.time()
        print(f"Published: {message}")
    except Exception as e:
        print(f"MQTT publish error: {e}")

def mqtt_callback(topic, msg):
    global opponent_setup_ok, restart_needed, last_event_ms

    message = msg.decode('utf-8')
    print(f"Received: {message}")

    # During setup phase only handle setup_ok and restart
    if setup_phase and message not in ("setup_ok", "restart"):
        return

    if message == "setup_ok":
        opponent_setup_ok = True
        print("Opponent setup complete")

    elif message == "restart":
        print("Restart received — will restart setup phase")
        reset_game_state()
        restart_needed = True

    elif message == "synced":
        # Opponent mirrored our move — clear own indicators
        for coord in own_leds:
            set_neopixel(np, coord, COLOR_OFF)
        own_leds.clear()

    elif message.startswith("occ:"):
        handle_occ(message[4:])

    elif message.startswith("L:") or message.startswith("P:"):
        if resolution_active or len(message) < 4:
            return
        coord = message[2:4].upper()
        if coord[0] < 'A' or coord[0] > 'H' or coord[1] < '1' or coord[1] > '8':
            return
        if message[0] == 'L':
            remote_expect[coord] = False
            set_neopixel(np, coord, COLOR_LIFT)
        else:
            remote_expect[coord] = True
            set_neopixel(np, coord, COLOR_PLACE)
        remote_satisfied.discard(coord)
        last_event_ms = time.ticks_ms()

# ============= OWN MOVE EVENTS =============

def handle_sensor_change(coord, occupied_now):
    """A hall sensor changed state. Decide what the change means."""
    global occ_dirty, occ_change_ms

    if resolution_active:
        # Player is correcting squares — re-publish occupancy after it settles
        occ_dirty = True
        occ_change_ms = time.ticks_ms()
        return

    if remote_expect:
        # Player is mirroring the opponent's move — handled by check_remote_sync()
        return

    now = time.ticks_ms()
    prev = pending.get(coord)

    if occupied_now:
        # PLACE — cancels a bouncing lift on the same square
        if prev and prev[0] == 'L' and time.ticks_diff(now, prev[1]) < LIFT_DELAY:
            del pending[coord]
            print(f"Lift cancelled (replaced within {LIFT_DELAY}ms) at {coord}")
        else:
            pending[coord] = ('P', now)
    else:
        # LIFT — cancels a bouncing placement on the same square
        if prev and prev[0] == 'P' and time.ticks_diff(now, prev[1]) < MOVE_DELAY:
            del pending[coord]
            print(f"Placement cancelled (re-lifted within {MOVE_DELAY}ms) at {coord}")
        else:
            pending[coord] = ('L', now)

def flush_pending():
    """Publish debounced events once their delay has passed. The LED
    lights only now — a cancelled bounce never touches any LED."""
    global last_event_ms
    now = time.ticks_ms()
    for coord in list(pending):
        typ, start_ms = pending[coord]
        delay = LIFT_DELAY if typ == 'L' else MOVE_DELAY
        if time.ticks_diff(now, start_ms) >= delay:
            del pending[coord]
            own_leds[coord] = typ
            set_neopixel(np, coord, COLOR_LIFT if typ == 'L' else COLOR_PLACE)
            publish(f"{typ}:{coord.lower()}")
            last_event_ms = now

# ============= MIRRORING THE OPPONENT'S MOVE =============

def check_remote_sync():
    """Compare sensors against the opponent's expected occupancy.
    Squares matching the expectation turn off; when the whole move is
    mirrored (including at least one placement), publish "synced"."""
    global last_event_ms

    all_satisfied = True
    any_place = False
    for coord, expect_occ in remote_expect.items():
        if expect_occ:
            any_place = True
        satisfied = (occupied(coord) == expect_occ)
        if satisfied and coord not in remote_satisfied:
            remote_satisfied.add(coord)
            set_neopixel(np, coord, COLOR_OFF)
            last_event_ms = time.ticks_ms()
        elif not satisfied and coord in remote_satisfied:
            remote_satisfied.discard(coord)
            set_neopixel(np, coord, COLOR_PLACE if expect_occ else COLOR_LIFT)
            last_event_ms = time.ticks_ms()
        if not satisfied:
            all_satisfied = False

    if all_satisfied and any_place:
        for coord in remote_expect:
            set_neopixel(np, coord, COLOR_OFF)
        remote_expect.clear()
        remote_satisfied.clear()
        publish("synced")
        print("Move mirrored — sent synced")

# ============= OCCUPANCY RECONCILIATION =============

def start_resolution():
    """LEDs were not cleared within SYNC_TIMEOUT — exchange occupancy bitmaps."""
    global resolution_active, opp_bitmap, resolution_start_ms, occ_dirty
    resolution_active = True
    opp_bitmap = None
    occ_dirty = False
    pending.clear()
    mismatch_coords.clear()
    resolution_start_ms = time.ticks_ms()
    print("Sync timeout — starting occupancy reconciliation")
    publish("occ:" + read_occupancy_bitmap(i2c))

def handle_occ(payload):
    """Opponent sent its occupancy bitmap — compare with our own."""
    global resolution_active, opp_bitmap, resolution_start_ms
    if len(payload) != 64:
        print(f"Invalid occ bitmap length: {len(payload)}")
        return
    opp_bitmap = payload
    own_bitmap = read_occupancy_bitmap(i2c)
    if not resolution_active:
        # Opponent started the reconciliation — answer with our occupancy once
        resolution_active = True
        pending.clear()
        resolution_start_ms = time.ticks_ms()
        publish("occ:" + own_bitmap)
    update_mismatch(own_bitmap)

def update_mismatch(own_bitmap):
    """Recompute which squares differ; matching boards end the reconciliation."""
    global mismatch_coords
    new_mismatch = set()
    for idx in range(64):
        if own_bitmap[idx] != opp_bitmap[idx]:
            new_mismatch.add(bitmap_index_to_coord(idx))
    for coord in mismatch_coords - new_mismatch:
        set_neopixel(np, coord, COLOR_OFF)
    mismatch_coords = new_mismatch
    if mismatch_coords:
        print(f"Occupancy mismatch at: {sorted(mismatch_coords)}")
    else:
        finish_resolution()

def finish_resolution():
    """Boards agree again — clear all indicators and resume normal play."""
    global resolution_active, opp_bitmap, occ_dirty, last_event_ms
    clear_all_leds()
    own_leds.clear()
    remote_expect.clear()
    remote_satisfied.clear()
    pending.clear()
    mismatch_coords.clear()
    opp_bitmap = None
    occ_dirty = False
    resolution_active = False
    last_event_ms = time.ticks_ms()
    print("=== Boards back in sync — game continues ===")

def process_resolution():
    """Blink mismatching squares and re-publish occupancy after corrections."""
    global occ_dirty, blink_on, last_blink_ms, resolution_start_ms
    now = time.ticks_ms()

    if time.ticks_diff(now, last_blink_ms) >= BLINK_MS:
        blink_on = not blink_on
        last_blink_ms = now
        for coord in mismatch_coords:
            set_neopixel(np, coord, COLOR_MISMATCH if blink_on else COLOR_OFF)

    if occ_dirty and time.ticks_diff(now, occ_change_ms) >= OCC_SETTLE:
        occ_dirty = False
        own_bitmap = read_occupancy_bitmap(i2c)
        publish("occ:" + own_bitmap)
        if opp_bitmap:
            update_mismatch(own_bitmap)

    # Opponent never answered — repeat our bitmap every SYNC_TIMEOUT
    if opp_bitmap is None and time.ticks_diff(now, resolution_start_ms) >= SYNC_TIMEOUT:
        resolution_start_ms = now
        publish("occ:" + read_occupancy_bitmap(i2c))

# ============= HALL SENSOR SCANNING =============

def scan_hall_sensors():
    for row_idx, address in enumerate(MCP23017_ADDRESSES):
        try:
            gpio_state = mcp23017_read_gpio(i2c, address)
            for col_idx in range(8):
                current_state  = (gpio_state & (1 << col_idx)) == 0
                previous_state = previous_hall_states[row_idx][col_idx]
                if current_state != previous_state:
                    previous_hall_states[row_idx][col_idx] = current_state
                    coord = sensor_to_coordinate(row_idx, col_idx)
                    handle_sensor_change(coord, current_state)
        except Exception as e:
            print(f"Error reading MCP23017 at 0x{address:02X}: {e}")

def snapshot_hall_states():
    """Read all sensors without generating events."""
    for row_idx, address in enumerate(MCP23017_ADDRESSES):
        try:
            gpio_state = mcp23017_read_gpio(i2c, address)
            for col_idx in range(8):
                previous_hall_states[row_idx][col_idx] = (gpio_state & (1 << col_idx)) == 0
        except Exception as e:
            print(f"  Error reading row {row_idx}: {e}")

# ============= SETUP PHASE =============

def check_initial_setup():
    """Return list of coords whose occupancy doesn't match the starting position
    (rows 1, 2, 7 and 8 occupied, everything else empty)."""
    wrong = []
    for row_idx, address in enumerate(MCP23017_ADDRESSES):
        try:
            gpio_state = mcp23017_read_gpio(i2c, address)
            for col_idx in range(8):
                is_occupied = (gpio_state & (1 << col_idx)) == 0
                chess_row   = 8 - row_idx
                should_be   = chess_row in (1, 2, 7, 8)
                if is_occupied != should_be:
                    wrong.append(sensor_to_coordinate(row_idx, col_idx))
        except Exception as e:
            print(f"Setup scan error at 0x{address:02X}: {e}")
    return wrong

def reset_game_state():
    global resolution_active, opp_bitmap, occ_dirty, last_event_ms
    own_leds.clear()
    remote_expect.clear()
    remote_satisfied.clear()
    pending.clear()
    mismatch_coords.clear()
    opp_bitmap = None
    occ_dirty = False
    resolution_active = False
    last_event_ms = 0

def run_setup_phase():
    """Block until both boards confirm the correct starting position."""
    global setup_phase, own_setup_ok, opponent_setup_ok, restart_needed
    global last_mqtt_activity, last_event_ms

    setup_phase = True
    own_setup_ok = False
    opponent_setup_ok = False

    wrong_coords  = []
    setup_blink   = False
    last_blink    = time.ticks_ms()
    last_scan     = time.ticks_ms()
    SETUP_BLINK_MS  = 400
    SETUP_RESCAN_MS = 300

    while not (own_setup_ok and opponent_setup_ok):
        if restart_needed:
            restart_needed = False
            clear_all_leds()
            own_setup_ok = False
            opponent_setup_ok = False
            wrong_coords = []
            print("=== Restart during setup — restarting setup check ===")

        try:
            mqtt_client.check_msg()
            if time.time() - last_mqtt_activity > 30:
                mqtt_client.ping()
                last_mqtt_activity = time.time()
        except OSError:
            pass

        now = time.ticks_ms()

        if not own_setup_ok and time.ticks_diff(now, last_scan) >= SETUP_RESCAN_MS:
            new_wrong = check_initial_setup()
            for c in wrong_coords:
                if c not in new_wrong:
                    set_neopixel(np, c, COLOR_OFF)
            wrong_coords = new_wrong
            last_scan = now
            if not wrong_coords:
                own_setup_ok = True
                for c in ROOK_COORDS:
                    set_neopixel(np, c, COLOR_PLACE)
                publish("setup_ok")
                print("Own setup complete — sent setup_ok (waiting for opponent)")

        if time.ticks_diff(now, last_blink) >= SETUP_BLINK_MS:
            setup_blink = not setup_blink
            last_blink = now
            for c in wrong_coords:
                set_neopixel(np, c, COLOR_MISMATCH if setup_blink else COLOR_OFF)

        time.sleep(0.01)

    print("=== Both boards ready — game starting ===")
    clear_all_leds()
    reset_game_state()
    snapshot_hall_states()
    last_event_ms = time.ticks_ms()
    setup_phase = False
    print("=== Game Ready ===")

# ============= MAIN =============

def main():
    global i2c, np, mqtt_client, last_mqtt_activity, restart_needed

    print(f"=== Remote Chess (simple) Starting | Color: {PLAYER_COLOR} ===")
    print(f"  Publish  → {PUBLISH_TOPIC}")
    print(f"  Subscribe← {SUBSCRIBE_TOPIC}")

    # Initialize I2C and MCP23017
    print("Initializing I2C...")
    i2c = I2C(0, scl=Pin(PIN_SCL), sda=Pin(PIN_SDA), freq=I2C_FREQ)
    for address in MCP23017_ADDRESSES:
        try:
            mcp23017_init(i2c, address)
            print(f"  MCP23017 0x{address:02X} OK")
        except Exception as e:
            print(f"  MCP23017 0x{address:02X} error: {e}")

    # Initialize NeoPixels — all off
    print("Initializing NeoPixels...")
    np = NeoPixel(Pin(PIN_NEOPIXEL), NUM_NEOPIXELS)
    clear_all_leds()

    # Rook LEDs red until WiFi is connected
    for c in ROOK_COORDS:
        set_neopixel(np, c, COLOR_LIFT)
    connect_wifi(WIFI_SSID, WIFI_PASSWORD)

    # Rook LEDs yellow until MQTT is connected
    for c in ROOK_COORDS:
        set_neopixel(np, c, COLOR_MISMATCH)
    print(f"Connecting MQTT → {MQTT_BROKER}:{MQTT_PORT}")
    while True:
        try:
            mqtt_client = MQTTClient(
                MQTT_CLIENT_ID, MQTT_BROKER, port=MQTT_PORT,
                user=MQTT_USER if MQTT_USER else None,
                password=MQTT_PASSWORD if MQTT_PASSWORD else None
            )
            mqtt_client.set_callback(mqtt_callback)
            mqtt_client.connect()
            mqtt_client.subscribe(SUBSCRIBE_TOPIC)
            print(f"MQTT ready — subscribed to {SUBSCRIBE_TOPIC}")
            break
        except Exception as e:
            print(f"MQTT connection failed: {e} — retrying in 5s")
            time.sleep(5)

    last_mqtt_activity = time.time()
    for c in ROOK_COORDS:
        set_neopixel(np, c, COLOR_OFF)

    # Signal startup — triggers the other board to restart if already running
    publish("restart")

    print("=== Setup Check — place all pieces in starting position ===")
    run_setup_phase()

    PING_INTERVAL = 30

    while True:
        # Handle restart triggered by the other board powering on
        if restart_needed:
            restart_needed = False
            clear_all_leds()
            print("=== Restart received — re-entering setup phase ===")
            run_setup_phase()
            continue

        try:
            mqtt_client.check_msg()

            if time.time() - last_mqtt_activity > PING_INTERVAL:
                mqtt_client.ping()
                last_mqtt_activity = time.time()

            scan_hall_sensors()

            if resolution_active:
                process_resolution()
            else:
                flush_pending()
                if remote_expect:
                    check_remote_sync()
                # LEDs still on long after the last event -> reconcile occupancy
                if (own_leds or remote_expect) and \
                        time.ticks_diff(time.ticks_ms(), last_event_ms) >= SYNC_TIMEOUT:
                    start_resolution()

            time.sleep(0.01)

        except OSError as e:
            print(f"MQTT error: {e} — reconnecting...")
            time.sleep(2)
            try:
                mqtt_client.connect()
                mqtt_client.subscribe(SUBSCRIBE_TOPIC)
                last_mqtt_activity = time.time()
                print("Reconnected")
            except Exception as reconnect_error:
                print(f"Reconnect failed: {reconnect_error}")
                time.sleep(5)

        except KeyboardInterrupt:
            print("\nShutting down...")
            if mqtt_client:
                mqtt_client.disconnect()
            clear_all_leds()
            break

if __name__ == "__main__":
    main()
