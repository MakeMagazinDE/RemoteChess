"""
FSD Remote Chess - Main Program (Phase 1: Board vs Board)
For Xiao ESP32-C3/C6 with MicroPython
"""

import time
from machine import Pin, I2C, Timer
from neopixel import NeoPixel
from umqtt.simple import MQTTClient
from secrets import *
from remote_chess_lib import (
    BoardState, MCP23017_ADDRESSES, NUM_NEOPIXELS,
    mcp23017_init, mcp23017_read_gpio,
    sensor_to_coordinate, coordinate_to_neopixel, coordinate_to_board_index,
    set_neopixel, clear_neopixels,
    connect_wifi,
)

# ============= CONFIGURATION =============

# LED Colors (R, G, B)
COLOR_LIFT  = (100,   0,   0)   # Red for lifted pieces
COLOR_PLACE = (  0, 100,   0)   # Green for placed pieces
COLOR_WRONG = (100, 100,   0)   # Yellow for wrong lift during sync
COLOR_OFF   = (  0,   0,   0)

# Timing (ms)
LIFT_DELAY         = 200   # Debounce before publishing L message
MOVE_DELAY         = 500   # Debounce before publishing P/R/X message
BLINK_FREQUENCY_MS = 400   # Wrong-lift blink period

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

# Opponent move indicators (set by mqtt_callback, cleared by sync or synced message)
LML = ""   # Last Move Lift coord (red LED)
LMP = ""   # Last Move Place coord (green LED, normal move)
LMK = ""   # Last Move Killed coord (green LED, capture destination)
lm_first_coord       = ""     # Attacker source coord (first L in capture sequence)
lm_piece_in_transit  = ""     # Piece from opponent's last L message
sync_pending         = False  # True while opponent's move still needs physical sync

# Own move state machine
FiguresInAir              = 0
first_lift_coord          = ""
second_lift_coord         = ""
piece_in_air              = ""
piece_in_air_coord        = ""
second_piece_in_air       = ""
second_piece_in_air_coord = ""

# Opponent piece being physically moved during sync on own board
opponent_piece_in_air       = ""
opponent_piece_in_air_coord = ""

# Pre-sync lift: opponent piece lifted before MQTT L message arrived
preemptive_sync_coord = ""
preemptive_sync_piece = ""

# Wrong-lift tracking during sync
wrong_lift_coord = ""

# Own last-move LEDs — stay on until opponent sends "synced" or next own move starts
own_lift_led  = ""   # coord of own lift (red LED)
own_place_led = ""   # coord of own placement (green LED)

# Setup phase — True until both boards confirm correct initial position
setup_phase       = True
own_setup_ok      = False
opponent_setup_ok = False
restart_needed    = False   # set by mqtt_callback when "restart" received

# Pending lift debounce
pending_lift_coord    = ""
pending_lift_piece    = ""
pending_lift_start_ms = 0

# Pending placement debounce
pending_coord    = ""
pending_msg      = ""
pending_piece    = ""
pending_action   = ""
pending_start_ms = 0
# State snapshot for cancel/restore
_s_fia = 0;  _s_first = "";  _s_second = ""
_s_pia = "";  _s_pia_c = "";  _s_spia = "";  _s_spia_c = ""
_s_opp = "";  _s_opp_c = "";  _s_lmp = "";  _s_sync = False

# Board state
board = BoardState()
previous_hall_states = [[False] * 8 for _ in range(8)]

# Hardware handles (set in main)
i2c         = None
np          = None
mqtt_client = None
blink_timer = None
blink_state = False

last_mqtt_activity = 0

# ============= TIMER CALLBACKS =============

def blink_callback(timer):
    global blink_state, wrong_lift_coord
    blink_state = not blink_state
    if wrong_lift_coord:
        set_neopixel(np, wrong_lift_coord, COLOR_WRONG if blink_state else COLOR_OFF)

# ============= PENDING HELPERS =============

def _start_pending(coord, msg, piece, action):
    global pending_coord, pending_msg, pending_piece, pending_action, pending_start_ms
    global _s_fia, _s_first, _s_second, _s_pia, _s_pia_c, _s_spia, _s_spia_c
    global _s_opp, _s_opp_c, _s_lmp, _s_sync
    if pending_coord:
        set_neopixel(np, pending_coord, COLOR_OFF)
    _s_fia  = FiguresInAir;       _s_first = first_lift_coord;   _s_second = second_lift_coord
    _s_pia  = piece_in_air;       _s_pia_c = piece_in_air_coord
    _s_spia = second_piece_in_air; _s_spia_c = second_piece_in_air_coord
    _s_opp  = opponent_piece_in_air; _s_opp_c = opponent_piece_in_air_coord
    _s_lmp  = LMP;                _s_sync  = sync_pending
    pending_coord  = coord;  pending_msg   = msg;  pending_piece  = piece
    pending_action = action; pending_start_ms = time.ticks_ms()
    set_neopixel(np, coord, COLOR_PLACE)

def _cancel_pending():
    global pending_coord, pending_msg, pending_piece, pending_action, pending_start_ms
    global FiguresInAir, first_lift_coord, second_lift_coord
    global piece_in_air, piece_in_air_coord, second_piece_in_air, second_piece_in_air_coord
    global opponent_piece_in_air, opponent_piece_in_air_coord, LMP, sync_pending
    set_neopixel(np, pending_coord, COLOR_OFF)
    FiguresInAir          = _s_fia;   first_lift_coord  = _s_first;  second_lift_coord     = _s_second
    piece_in_air          = _s_pia;   piece_in_air_coord = _s_pia_c
    second_piece_in_air   = _s_spia;  second_piece_in_air_coord = _s_spia_c
    opponent_piece_in_air = _s_opp;   opponent_piece_in_air_coord = _s_opp_c
    LMP = _s_lmp;  sync_pending = _s_sync
    pending_coord = pending_msg = pending_piece = pending_action = ""
    pending_start_ms = 0
    print(f"Placement cancelled (re-lifted within {MOVE_DELAY}ms)")

def _confirm_lift():
    global pending_lift_coord, pending_lift_piece, pending_lift_start_ms
    global FiguresInAir, first_lift_coord, piece_in_air, piece_in_air_coord
    coord = pending_lift_coord
    piece = pending_lift_piece
    FiguresInAir       = 1
    first_lift_coord   = coord
    piece_in_air       = piece
    piece_in_air_coord = coord
    pending_lift_coord = ""
    publish_move(f"{coord}-L|{piece}")
    print(f"Lift confirmed: {piece} at {coord}")

def _confirm_pending():
    global pending_coord, pending_msg, pending_piece, pending_action, pending_start_ms
    global FiguresInAir, first_lift_coord, second_lift_coord
    global piece_in_air, piece_in_air_coord, second_piece_in_air, second_piece_in_air_coord
    global own_lift_led, own_place_led
    coord      = pending_coord
    action     = pending_action
    lift_coord = first_lift_coord   # save before clearing
    board.set_piece_at(coord, pending_piece)
    FiguresInAir = 0;  first_lift_coord = second_lift_coord = ""
    piece_in_air = piece_in_air_coord = ""
    second_piece_in_air = second_piece_in_air_coord = ""
    msg = pending_msg
    pending_coord = pending_msg = pending_piece = pending_action = ""
    pending_start_ms = 0
    if action == 'R':
        # Return to origin: clear own LED immediately; publish R so opponent clears theirs
        set_neopixel(np, coord, COLOR_OFF)
        own_lift_led = own_place_led = ""
    else:
        # LEDs stay on — cleared when opponent sends "synced" or at next own move
        own_lift_led  = lift_coord
        own_place_led = coord
    publish_move(msg)

# ============= MQTT =============

def mqtt_callback(topic, msg):
    global LML, LMP, LMK, blink_timer
    global sync_pending, lm_piece_in_transit, lm_first_coord
    global own_lift_led, own_place_led
    global setup_phase, own_setup_ok, opponent_setup_ok
    global preemptive_sync_coord, preemptive_sync_piece
    global opponent_piece_in_air, opponent_piece_in_air_coord

    message = msg.decode('utf-8')
    print(f"Received: {message}")

    # During setup phase only handle setup_ok and restart
    if setup_phase and message not in ("setup_ok", "restart"):
        return

    if message == "setup_ok":
        opponent_setup_ok = True
        print("Opponent setup complete")
        return

    # Opponent confirmed sync complete — clear own last-move LEDs
    if message == "synced":
        set_neopixel(np, own_lift_led, COLOR_OFF)
        set_neopixel(np, own_place_led, COLOR_OFF)
        own_lift_led = own_place_led = ""
        return

    if message == "restart":
        global FiguresInAir, first_lift_coord, second_lift_coord, wrong_lift_coord
        global piece_in_air, piece_in_air_coord, second_piece_in_air, second_piece_in_air_coord
        global opponent_piece_in_air, opponent_piece_in_air_coord
        global pending_lift_coord, pending_coord, restart_needed
        print("Restart received — will restart setup phase")
        LML = LMP = LMK = lm_first_coord = lm_piece_in_transit = wrong_lift_coord = ""
        own_lift_led = own_place_led = ""
        preemptive_sync_coord = preemptive_sync_piece = ""
        sync_pending = False
        FiguresInAir = 0
        first_lift_coord = second_lift_coord = ""
        piece_in_air = piece_in_air_coord = ""
        second_piece_in_air = second_piece_in_air_coord = ""
        opponent_piece_in_air = opponent_piece_in_air_coord = ""
        pending_lift_coord = pending_coord = ""
        if blink_timer: blink_timer.deinit()
        restart_needed = True
        return

    if len(message) < 4:
        return

    parts         = message.split('|')
    move_part     = parts[0]
    piece_payload = parts[1] if len(parts) >= 2 else None
    fen_payload   = parts[2] if len(parts) >= 3 else None
    coord         = move_part[:-2]
    action        = move_part[-1]

    if action == 'L':
        sync_pending = True
        lm_piece_in_transit = piece_payload or ""
        if preemptive_sync_coord == coord:
            # Player already physically lifted this opponent piece before the MQTT L arrived.
            # Treat the lift as already done: record opponent_piece_in_air, skip LML indicator.
            opponent_piece_in_air       = piece_payload or preemptive_sync_piece
            opponent_piece_in_air_coord = coord
            if lm_first_coord == "":
                lm_first_coord = coord
            preemptive_sync_coord = preemptive_sync_piece = ""
            lm_piece_in_transit = ""
            set_neopixel(np, coord, COLOR_OFF)
            print(f"Pre-sync lift consumed at {coord}")
        else:
            if lm_first_coord:
                # Second L in a capture sequence — preserve the attacker-source (lm_first_coord) red LED
                if LML and LML != lm_first_coord:
                    set_neopixel(np, LML, COLOR_OFF)
                set_neopixel(np, LMP, COLOR_OFF); LMP = ""
                set_neopixel(np, LMK, COLOR_OFF); LMK = ""
                LML = coord
                # lm_first_coord preserved; no new red LED (X will show green)
            else:
                # First L — normal
                clear_neopixels(np, [LML, LMP, LMK], COLOR_OFF)
                LMP = LMK = ""
                lm_first_coord = coord
                LML = coord
                set_neopixel(np, coord, COLOR_LIFT)

    elif action == 'P':
        set_neopixel(np, coord, COLOR_PLACE)
        if piece_payload:
            if LML: board.set_piece_at(LML, "")
            board.set_piece_at(coord, piece_payload)
        lm_first_coord = ""   # lm_piece_in_transit kept until sync lift consumes it
        LMP = coord
        if LMK:
            set_neopixel(np, LMK, COLOR_OFF)
            LMK = ""

    elif action == 'R':
        set_neopixel(np, coord, COLOR_OFF)
        if piece_payload: board.set_piece_at(coord, piece_payload)
        lm_first_coord = lm_piece_in_transit = ""
        LML = ""

    elif action == 'X':
        set_neopixel(np, coord, COLOR_PLACE)
        if lm_first_coord:       board.set_piece_at(lm_first_coord, "")
        if LML and LML != coord: board.set_piece_at(LML, "")
        if piece_payload:        board.set_piece_at(coord, piece_payload)
        # lm_first_coord preserved — needed so sync_lift can detect the attacker source
        # lm_piece_in_transit stores the attacker piece for placement after sync lift
        lm_piece_in_transit = piece_payload or ""
        LMK = coord

    if fen_payload and action in ('P', 'X'):
        local_fen = board.to_fen()
        if local_fen != fen_payload:
            print(f"FEN mismatch — local: {local_fen}  remote: {fen_payload}")

def publish_move(message):
    global last_mqtt_activity
    try:
        mqtt_client.publish(PUBLISH_TOPIC, message)
        last_mqtt_activity = time.time()
        print(f"Published: {message}")
    except Exception as e:
        print(f"MQTT publish error: {e}")

# ============= HALL SENSOR SCANNING =============

def scan_hall_sensors():
    global FiguresInAir, first_lift_coord, second_lift_coord, previous_hall_states
    global LML, LMP, LMK, blink_timer, wrong_lift_coord, sync_pending
    global piece_in_air, piece_in_air_coord, second_piece_in_air, second_piece_in_air_coord
    global opponent_piece_in_air, opponent_piece_in_air_coord, lm_piece_in_transit, lm_first_coord
    global pending_coord, pending_lift_coord, pending_lift_piece, pending_lift_start_ms
    global own_lift_led, own_place_led
    global preemptive_sync_coord, preemptive_sync_piece

    for row_idx, address in enumerate(MCP23017_ADDRESSES):
        try:
            gpio_state = mcp23017_read_gpio(i2c, address)

            for col_idx in range(8):
                current_state  = (gpio_state & (1 << col_idx)) == 0
                previous_state = previous_hall_states[row_idx][col_idx]

                if current_state == previous_state:
                    continue

                coord = sensor_to_coordinate(row_idx, col_idx)

                # --- LIFT (magnet gone) ---
                if previous_state and not current_state:

                    if pending_coord and coord == pending_coord:
                        # Re-lifted from pending placement — cancel
                        _cancel_pending()

                    elif pending_lift_coord and coord == pending_lift_coord:
                        # Replaced before LIFT_DELAY — cancel pending lift
                        board.set_piece_at(coord, pending_lift_piece)
                        set_neopixel(np, coord, COLOR_OFF)
                        pending_lift_coord = ""
                        print(f"Lift cancelled (replaced within {LIFT_DELAY}ms)")

                    elif coord == LML or (lm_first_coord and coord == lm_first_coord):
                        if coord == LML and LMK and coord == LMK:
                            # Defender removed from capture destination (works in either sync order).
                            # Keep the green LED — it is still the placement target.
                            # opponent_piece_in_air may already be set (attacker lifted first)
                            # or still "" (defender lifted first); either way leave it unchanged.
                            board.set_piece_at(coord, "")
                            LML = ""
                            print(f"Sync defender removal at {coord}")
                        else:
                            # Normal sync lift — attacker source or single-piece move
                            opponent_piece_in_air       = lm_piece_in_transit if lm_piece_in_transit else board.get_piece_at(coord)
                            opponent_piece_in_air_coord = coord
                            board.set_piece_at(coord, "")
                            lm_piece_in_transit = ""
                            if coord == lm_first_coord and coord != LML:
                                lm_first_coord = ""
                            elif coord == LML:
                                LML = ""
                            set_neopixel(np, coord, COLOR_OFF)
                            print(f"Sync lift at {coord}")
                            # Scan-order fix: sync place may have already fired; check for completion
                            if not LML and not LMP and not LMK:
                                sync_pending = False
                                publish_move("synced")
                                print("Sync complete — sent synced")

                    elif sync_pending and LML and FiguresInAir == 0:
                        # Wrong lift during sync
                        print(f"Wrong lift at {coord} (expected {LML})")
                        wrong_lift_coord = coord
                        if blink_timer: blink_timer.deinit()
                        blink_timer = Timer(1)
                        blink_timer.init(mode=Timer.PERIODIC, period=BLINK_FREQUENCY_MS, callback=blink_callback)

                    elif coord == LMP:
                        # Captured piece removed from destination during sync
                        board.set_piece_at(coord, "")
                        print(f"Sync capture removal at {coord}")

                    elif FiguresInAir == 0 and not pending_lift_coord:
                        piece = board.get_piece_at(coord)
                        is_own = piece and (
                            (PLAYER_COLOR == "white" and piece.isupper()) or
                            (PLAYER_COLOR == "black" and piece.islower())
                        )
                        if is_own:
                            # Own first lift — red LED immediately, L message deferred
                            clear_neopixels(np, [LML, LMP, LMK, own_lift_led, own_place_led], COLOR_OFF)
                            LML = LMP = LMK = ""
                            own_lift_led = own_place_led = ""
                            if blink_timer: blink_timer.deinit()
                            pending_lift_coord    = coord
                            pending_lift_piece    = piece
                            pending_lift_start_ms = time.ticks_ms()
                            board.set_piece_at(coord, "")
                            set_neopixel(np, coord, COLOR_LIFT)
                            print(f"Lift pending: {piece} at {coord}")
                        elif piece:
                            # Opponent piece lifted before MQTT L arrived — pre-sync lift
                            preemptive_sync_coord = coord
                            preemptive_sync_piece = piece
                            board.set_piece_at(coord, "")
                            set_neopixel(np, coord, COLOR_LIFT)
                            print(f"Pre-sync lift at {coord}: {piece} (awaiting MQTT L)")

                    elif FiguresInAir == 1:
                        # Own second lift (piece to be captured) — yellow LED, L published immediately
                        second_lift_coord         = coord
                        second_piece_in_air       = board.get_piece_at(coord)
                        second_piece_in_air_coord = coord
                        board.set_piece_at(coord, "")
                        FiguresInAir = 2
                        set_neopixel(np, coord, COLOR_WRONG)   # yellow = to-be-captured piece
                        publish_move(f"{coord}-L|{second_piece_in_air}")
                        print(f"Capture lift: {second_piece_in_air} at {coord}")

                # --- PLACE (magnet detected) ---
                elif not previous_state and current_state:

                    if wrong_lift_coord and coord == wrong_lift_coord:
                        # Wrong lift corrected
                        set_neopixel(np, coord, COLOR_OFF)
                        wrong_lift_coord = ""
                        if not LMK:
                            if blink_timer: blink_timer.deinit()
                        print(f"Wrong lift corrected at {coord}")

                    elif coord == LMP or coord == LMK:
                        # Sync place complete — clear green LED, notify moving board
                        set_neopixel(np, coord, COLOR_OFF)
                        if coord == LMP:
                            board.set_piece_at(coord, opponent_piece_in_air)
                            opponent_piece_in_air = opponent_piece_in_air_coord = ""
                            LMP = ""
                        else:  # LMK (capture destination)
                            board.set_piece_at(coord, opponent_piece_in_air)
                            opponent_piece_in_air = opponent_piece_in_air_coord = ""
                            LMK = ""
                        # If both LML and LMK/LMP are now cleared, sync is complete
                        if not LML and not LMP and not LMK:
                            sync_pending = False
                            publish_move("synced")
                            print("Sync complete — sent synced")

                    elif FiguresInAir == 1:
                        if coord == first_lift_coord:
                            # Return to origin
                            promoted_piece = board.handle_pawn_promotion(coord, piece_in_air)
                            print(f"Return {promoted_piece} to {coord}")
                            _start_pending(coord, f"{coord}-R|{promoted_piece}", promoted_piece, 'R')
                        else:
                            # Normal move
                            board.set_piece_at(coord, piece_in_air)
                            fen = board.to_fen()
                            board.set_piece_at(coord, "")
                            print(f"Move {piece_in_air} to {coord}")
                            _start_pending(coord, f"{coord}-P|{piece_in_air}|{fen}", piece_in_air, 'P')

                    elif FiguresInAir == 2:
                        # Capture placement
                        capture_piece = second_piece_in_air if coord == first_lift_coord else piece_in_air
                        board.set_piece_at(coord, capture_piece)
                        fen = board.to_fen()
                        board.set_piece_at(coord, "")
                        print(f"Capture: {capture_piece} to {coord}")
                        _start_pending(coord, f"{coord}-X|{capture_piece}|{fen}", capture_piece, 'X')

                previous_hall_states[row_idx][col_idx] = current_state

        except Exception as e:
            print(f"Error reading MCP23017 at 0x{address:02X}: {e}")

# ============= SETUP CHECK =============

def check_initial_setup():
    """Scan all hall sensors and return list of coords that don't match INITIAL layout."""
    wrong = []
    for row_idx, address in enumerate(MCP23017_ADDRESSES):
        try:
            gpio_state = mcp23017_read_gpio(i2c, address)
            for col_idx in range(8):
                is_occupied  = (gpio_state & (1 << col_idx)) == 0
                coord        = sensor_to_coordinate(row_idx, col_idx)
                ri, ci       = coordinate_to_board_index(coord)
                should_be    = BoardState.INITIAL[ri][ci] != ""
                if is_occupied != should_be:
                    wrong.append(coord)
        except Exception as e:
            print(f"Setup scan error at 0x{address:02X}: {e}")
    return wrong

def _run_setup_phase():
    """Block until both boards confirm correct initial position, then show game-ready animation.
    Handles a nested restart (other board powers on while we are in setup)."""
    global setup_phase, own_setup_ok, opponent_setup_ok, last_mqtt_activity, restart_needed

    setup_phase = True;  own_setup_ok = False;  opponent_setup_ok = False

    wrong_coords  = []
    blink_on      = False
    last_blink_ms = time.ticks_ms()
    last_scan_ms  = time.ticks_ms()
    SETUP_BLINK_MS  = 400
    SETUP_RESCAN_MS = 300

    while not (own_setup_ok and opponent_setup_ok):
        # Handle restart received while we are still in setup phase
        if restart_needed:
            restart_needed = False
            for i in range(NUM_NEOPIXELS):
                np[i] = COLOR_OFF
            np.write()
            board.reset()
            own_setup_ok = False;  opponent_setup_ok = False
            wrong_coords = [];     blink_on = False
            print("=== Restart during setup — restarting setup check ===")

        try:
            mqtt_client.check_msg()
            if time.time() - last_mqtt_activity > 30:
                mqtt_client.ping();  last_mqtt_activity = time.time()
        except OSError:
            pass

        now = time.ticks_ms()

        if time.ticks_diff(now, last_scan_ms) >= SETUP_RESCAN_MS:
            new_wrong = check_initial_setup()
            for c in wrong_coords:
                if c not in new_wrong:
                    set_neopixel(np, c, COLOR_OFF)
            wrong_coords = new_wrong
            last_scan_ms = now
            if not wrong_coords and not own_setup_ok:
                own_setup_ok = True
                for c in ROOK_COORDS: set_neopixel(np, c, COLOR_PLACE)
                publish_move("setup_ok")
                print("Own setup complete — sent setup_ok")

        if time.ticks_diff(now, last_blink_ms) >= SETUP_BLINK_MS:
            blink_on      = not blink_on
            last_blink_ms = now
            for c in wrong_coords:
                set_neopixel(np, c, COLOR_WRONG if blink_on else COLOR_OFF)

    # Both boards ready — clear rooks, light white-piece squares green for 2 s
    print("=== Both boards ready — game starting ===")
    for c in ROOK_COORDS: set_neopixel(np, c, COLOR_OFF)
    white_squares = [f"{f}{r}" for r in (1, 2) for f in "ABCDEFGH"]
    for c in white_squares: set_neopixel(np, c, COLOR_PLACE)
    time.sleep(2)
    for c in white_squares: set_neopixel(np, c, COLOR_OFF)

    setup_phase = False

    # Snapshot hall-sensor state for the game loop
    print("Reading hall sensor states...")
    for row_idx, address in enumerate(MCP23017_ADDRESSES):
        try:
            gpio_state = mcp23017_read_gpio(i2c, address)
            for col_idx in range(8):
                previous_hall_states[row_idx][col_idx] = (gpio_state & (1 << col_idx)) == 0
        except Exception as e:
            print(f"  Error reading row {row_idx}: {e}")

    print("=== Game Ready ===")

# ============= MAIN =============

def main():
    global i2c, np, mqtt_client, last_mqtt_activity

    print(f"=== Remote Chess Starting | Color: {PLAYER_COLOR} ===")
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
    for i in range(NUM_NEOPIXELS):
        np[i] = COLOR_OFF
    np.write()

    # Connect WiFi (all LEDs off during connection)
    connect_wifi(WIFI_SSID, WIFI_PASSWORD)

    # Connect MQTT
    print(f"Connecting MQTT → {MQTT_BROKER}:{MQTT_PORT}")
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

    except Exception as e:
        print(f"MQTT connection failed: {e}")
        raise

    last_mqtt_activity = time.time()

    # Signal startup — triggers the other board to restart if already running
    publish_move("restart")

    # Setup phase: both boards confirm correct initial piece positions
    print("=== Setup Check — place all pieces in starting position ===")
    _run_setup_phase()

    global pending_coord, pending_start_ms, restart_needed
    PING_INTERVAL = 30

    while True:
        # Handle restart triggered by the other board powering on
        if restart_needed:
            restart_needed = False
            for i in range(NUM_NEOPIXELS):
                np[i] = COLOR_OFF
            np.write()
            board.reset()
            print("=== Restart received — re-entering setup phase ===")
            _run_setup_phase()
            continue

        try:
            mqtt_client.check_msg()

            if time.time() - last_mqtt_activity > PING_INTERVAL:
                mqtt_client.ping()
                last_mqtt_activity = time.time()

            if pending_lift_coord and time.ticks_diff(time.ticks_ms(), pending_lift_start_ms) >= LIFT_DELAY:
                _confirm_lift()

            if pending_coord and time.ticks_diff(time.ticks_ms(), pending_start_ms) >= MOVE_DELAY:
                _confirm_pending()

            scan_hall_sensors()
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
            if mqtt_client: mqtt_client.disconnect()
            clear_neopixels(np, [LML, LMP, LMK], COLOR_OFF)
            break

if __name__ == "__main__":
    main()
