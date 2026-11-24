"""
Remote Chess Library
Shared functions for hardware, board state, LED control, and WiFi management.
"""

import network
import time
import json
import socket
from machine import Pin, deepsleep

# ============= Hardware Constants =============
MCP23017_ADDRESSES = [0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27]
NUM_NEOPIXELS = 64

# WiFi Manager
WIFI_CONFIG_FILE = "wifi_config.json"
AP_SSID = "RemoteChess-Setup"

# ============= MCP23017 Functions =============
def mcp23017_init(i2c, address):
    """Initialize MCP23017 - set all pins as inputs with pull-ups"""
    i2c.writeto_mem(address, 0x00, b'\xFF')
    i2c.writeto_mem(address, 0x0C, b'\xFF')

def mcp23017_read_gpio(i2c, address):
    """Read GPIOA register (0x12) from MCP23017"""
    data = i2c.readfrom_mem(address, 0x12, 1)
    return data[0]

def is_any_field_occupied(i2c):
    """Check if any field on the board has a piece (magnet detected)."""
    for address in MCP23017_ADDRESSES:
        try:
            gpio_state = mcp23017_read_gpio(i2c, address)
            if gpio_state != 0xFF:
                return True
        except Exception as e:
            print(f"Error checking occupancy at 0x{address:02X}: {e}")
    return False

def read_occupancy_bitmap(i2c):
    """Read all hall sensors and return a 64-char bitmap string.
    Order: row 8 first (matching FEN), A-H within each row.
    '1' = occupied (magnet detected), '0' = empty.
    """
    bitmap = ""
    for row_idx, address in enumerate(MCP23017_ADDRESSES):
        try:
            gpio_state = mcp23017_read_gpio(i2c, address)
            for col_idx in range(8):
                if (gpio_state & (1 << col_idx)) == 0:
                    bitmap += "1"
                else:
                    bitmap += "0"
        except Exception as e:
            print(f"Error reading occupancy at 0x{address:02X}: {e}")
            bitmap += "00000000"
    return bitmap

def enter_deep_sleep(np, color_off, color_lift, indicator_ms, sleep_ms):
    """Blink A1 red as indicator and enter deep sleep mode."""
    print("Entering deep sleep mode...")
    for idx in range(NUM_NEOPIXELS):
        np[idx] = color_off
    np.write()
    a1_idx = coordinate_to_neopixel("A1")
    np[a1_idx] = color_lift
    np.write()
    time.sleep(indicator_ms / 1000)
    np[a1_idx] = color_off
    np.write()
    deepsleep(sleep_ms)

# ============= Coordinate Conversion =============
def sensor_to_coordinate(row_idx, col):
    """Convert row_idx (0-7) and column (0-7) to chess coordinate."""
    column_letter = chr(ord('A') + col)
    row_number = 8 - row_idx
    return f"{column_letter}{row_number}"

def coordinate_to_neopixel(coord):
    """Convert chess coordinate (e.g., 'A1') to neopixel index (0-63)."""
    col = ord(coord[0]) - ord('A')
    chess_row = int(coord[1])
    row_idx = 8 - chess_row
    return row_idx * 8 + col

def coordinate_to_board_index(coord):
    """Convert chess coordinate (e.g., 'A1') to board array indices (row, col)."""
    col_idx = ord(coord[0]) - ord('A')
    chess_row = int(coord[1])
    row_idx = 8 - chess_row
    return row_idx, col_idx

# ============= Board State Class =============
class BoardState:
    INITIAL = [
        ["r", "n", "b", "q", "k", "b", "n", "r"],  # Row 8
        ["p", "p", "p", "p", "p", "p", "p", "p"],  # Row 7
        ["",  "",  "",  "",  "",  "",  "",  "" ],  # Row 6
        ["",  "",  "",  "",  "",  "",  "",  "" ],  # Row 5
        ["",  "",  "",  "",  "",  "",  "",  "" ],  # Row 4
        ["",  "",  "",  "",  "",  "",  "",  "" ],  # Row 3
        ["P", "P", "P", "P", "P", "P", "P", "P"],  # Row 2
        ["R", "N", "B", "Q", "K", "B", "N", "R"],  # Row 1
    ]

    def __init__(self):
        self.state = [row[:] for row in self.INITIAL]

    def reset(self):
        """Reset board state to initial chess position."""
        self.state = [row[:] for row in self.INITIAL]
        print("Board state reset to initial position")

    def get_piece_at(self, coord):
        """Get piece at coordinate from board state."""
        row_idx, col_idx = coordinate_to_board_index(coord)
        return self.state[row_idx][col_idx]

    def set_piece_at(self, coord, piece):
        """Set piece at coordinate in board state."""
        row_idx, col_idx = coordinate_to_board_index(coord)
        self.state[row_idx][col_idx] = piece

    def to_fen(self):
        """Convert board state to FEN notation (piece placement only)."""
        fen_rows = []
        for row in self.state:
            fen_row = ""
            empty_count = 0
            for piece in row:
                if piece == "":
                    empty_count += 1
                else:
                    if empty_count > 0:
                        fen_row += str(empty_count)
                        empty_count = 0
                    fen_row += piece
            if empty_count > 0:
                fen_row += str(empty_count)
            fen_rows.append(fen_row)
        return "/".join(fen_rows)

    def from_fen(self, fen):
        """Parse FEN and update board state (inverse of to_fen)."""
        rows = fen.split("/")
        if len(rows) != 8:
            print(f"Invalid FEN: {len(rows)} rows")
            return
        new_state = []
        for fen_row in rows:
            row = []
            for char in fen_row:
                if char.isdigit():
                    row.extend([""] * int(char))
                else:
                    row.append(char)
            if len(row) != 8:
                print(f"Invalid FEN row length: {len(row)}")
                return
            new_state.append(row)
        self.state = new_state
        print("Board state updated from FEN correction")
        self.print_state()

    def handle_pawn_promotion(self, coord, piece):
        """Handle pawn promotion/demotion when placed on same square in row 1 or 8."""
        chess_row = int(coord[1])
        if chess_row == 1 or chess_row == 8:
            if piece == "P":
                print(f"Pawn promoted to Queen at {coord}")
                return "Q"
            elif piece == "Q":
                print(f"Queen demoted to Pawn at {coord}")
                return "P"
            elif piece == "p":
                print(f"Pawn promoted to Queen at {coord}")
                return "q"
            elif piece == "q":
                print(f"Queen demoted to Pawn at {coord}")
                return "p"
        return piece

    def print_state(self):
        """Print current board state for debugging."""
        print("Current board state:")
        for i, row in enumerate(self.state):
            row_str = " ".join(p if p else "." for p in row)
            print(f"  Row {8-i}: {row_str}")

# ============= NeoPixel Functions =============
def set_neopixel(np, coord, color, demo_mode=False):
    """Set neopixel at coordinate to specified color"""
    if coord:
        idx = coordinate_to_neopixel(coord)
        np[idx] = color
        np.write()
        if demo_mode:
            print(f"[DEMO] NeoPixel #{idx} ({coord}) -> RGB{color}")

def clear_neopixels(np, coords, color_off):
    """Clear neopixels at given coordinates"""
    for coord in coords:
        if coord:
            set_neopixel(np, coord, color_off)

def startup_led_test(np, i2c, brightness, row_duration_ms, color_lift, color_place, color_off):
    """Display hall sensor status row by row during startup"""
    print("Running startup LED test...")
    color_red = tuple(int(c * brightness) for c in color_lift)
    color_green = tuple(int(c * brightness) for c in color_place)

    for row_idx, address in enumerate(MCP23017_ADDRESSES):
        try:
            gpio_state = mcp23017_read_gpio(i2c, address)
            for col_idx in range(8):
                magnet_present = (gpio_state & (1 << col_idx)) == 0
                led_idx = row_idx * 8 + col_idx
                if magnet_present:
                    np[led_idx] = color_red
                else:
                    np[led_idx] = color_green
            np.write()
            print(f"Row {row_idx + 1} (0x{address:02X}): displayed")
            time.sleep(row_duration_ms / 1000)
            for col_idx in range(8):
                led_idx = row_idx * 8 + col_idx
                np[led_idx] = color_off
            np.write()
        except Exception as e:
            print(f"Error in startup test row {row_idx + 1}: {e}")
    print("Startup LED test complete")

# ============= WiFi Functions =============
def url_decode(s):
    """Simple URL decoder for form data"""
    s = s.replace('+', ' ')
    parts = s.split('%')
    result = parts[0]
    for part in parts[1:]:
        if len(part) >= 2:
            try:
                result += chr(int(part[:2], 16)) + part[2:]
            except ValueError:
                result += '%' + part
        else:
            result += '%' + part
    return result

def load_wifi_config():
    """Load WiFi credentials from permanent storage"""
    try:
        with open(WIFI_CONFIG_FILE, 'r') as f:
            config = json.load(f)
            return config.get('ssid', ''), config.get('password', '')
    except:
        return '', ''

def save_wifi_config(ssid, password):
    """Save WiFi credentials to permanent storage"""
    config = {'ssid': ssid, 'password': password}
    with open(WIFI_CONFIG_FILE, 'w') as f:
        json.dump(config, f)
    print(f"WiFi config saved for network: {ssid}")

def scan_wifi_networks():
    """Scan for available WiFi networks"""
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    networks = wlan.scan()
    wlan.active(False)
    seen = set()
    unique = []
    for net in networks:
        ssid = net[0].decode('utf-8') if isinstance(net[0], bytes) else net[0]
        if ssid and ssid not in seen:
            seen.add(ssid)
            unique.append((ssid, net[3]))
    unique.sort(key=lambda x: x[1], reverse=True)
    return unique

def generate_wifi_html(networks):
    """Generate HTML page for WiFi network selection"""
    options = ''
    for ssid, rssi in networks:
        options += '<option value="' + ssid + '">' + ssid + ' (' + str(rssi) + ' dBm)</option>'
    return ('<!DOCTYPE html><html><head>'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>Remote Chess WiFi Setup</title>'
        '<style>'
        'body{font-family:Arial;margin:20px;background:#1a1a2e;color:#eee;}'
        'h1{color:#e94560;}h2{color:#aaa;}'
        'select,input,button{width:100%;padding:12px;margin:8px 0;'
        'box-sizing:border-box;font-size:16px;border-radius:4px;border:1px solid #555;}'
        'select,input{background:#16213e;color:#eee;}'
        'button{background:#e94560;color:white;border:none;cursor:pointer;}'
        '.c{max-width:400px;margin:0 auto;}'
        '</style></head><body><div class="c">'
        '<h1>Remote Chess</h1><h2>WiFi Setup</h2>'
        '<form action="/connect" method="POST">'
        '<label>Select Network:</label>'
        '<select name="ssid">' + options + '</select>'
        '<label>Password:</label>'
        '<input type="password" name="password" placeholder="Enter WiFi password">'
        '<button type="submit">Connect</button>'
        '</form></div></body></html>')

def start_wifi_manager():
    """Start WiFi Manager AP with web portal for network selection"""
    print("Scanning WiFi networks...")
    networks = scan_wifi_networks()
    print(f"Found {len(networks)} networks")

    ap = network.WLAN(network.AP_IF)
    ap.active(True)
    ap.config(essid=AP_SSID)

    print(f"WiFi Manager AP started: {AP_SSID}")
    print("Connect to this network and open http://192.168.4.1")

    addr = socket.getaddrinfo('0.0.0.0', 80)[0][-1]
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(addr)
    s.listen(1)

    html_page = generate_wifi_html(networks)

    try:
        while True:
            cl, cl_addr = s.accept()
            try:
                request = cl.recv(1024).decode('utf-8')
                if 'POST /connect' in request:
                    body = request.split('\r\n\r\n')[-1]
                    params = {}
                    for param in body.split('&'):
                        if '=' in param:
                            key, value = param.split('=', 1)
                            params[key] = url_decode(value)
                    ssid = params.get('ssid', '')
                    password = params.get('password', '')
                    if ssid:
                        response = ('<!DOCTYPE html><html><head>'
                            '<meta name="viewport" content="width=device-width, initial-scale=1">'
                            '<style>body{font-family:Arial;margin:20px;background:#1a1a2e;'
                            'color:#eee;text-align:center;}h1{color:#e94560;}</style>'
                            '</head><body>'
                            '<h1>Connecting to ' + ssid + '...</h1>'
                            '<p>The board will connect to the selected network.</p>'
                            '<p>You can close this page.</p>'
                            '</body></html>')
                        cl.send('HTTP/1.0 200 OK\r\nContent-Type: text/html\r\n\r\n')
                        cl.send(response)
                        cl.close()
                        save_wifi_config(ssid, password)
                        ap.active(False)
                        s.close()
                        return ssid, password
                else:
                    cl.send('HTTP/1.0 200 OK\r\nContent-Type: text/html\r\n\r\n')
                    cl.send(html_page)
            except Exception as e:
                print(f"WiFi Manager error: {e}")
            finally:
                try:
                    cl.close()
                except:
                    pass
    except Exception as e:
        print(f"WiFi Manager server error: {e}")
        s.close()
        ap.active(False)
        raise

def connect_wifi(wifi_ssid, wifi_password):
    """Connect to WiFi with fallback to WiFi Manager hotspot"""
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)

    saved_ssid, saved_password = load_wifi_config()
    credentials = []
    if saved_ssid:
        credentials.append((saved_ssid, saved_password))
    if wifi_ssid and wifi_ssid != saved_ssid:
        credentials.append((wifi_ssid, wifi_password))

    for ssid, password in credentials:
        print(f'Trying WiFi: {ssid}...')
        try:
            wlan.connect(ssid, password)
            timeout = 10
            while not wlan.isconnected() and timeout > 0:
                time.sleep(1)
                timeout -= 1
            if wlan.isconnected():
                print('WiFi connected:', wlan.ifconfig())
                return
            print(f'Failed to connect to {ssid}')
            wlan.disconnect()
        except OSError as e:
            print(f'WiFi error for {ssid}: {e}')
            wlan.disconnect()

    print("No WiFi connection established - starting WiFi Manager hotspot...")
    wlan.active(False)

    while True:
        ssid, password = start_wifi_manager()
        wlan = network.WLAN(network.STA_IF)
        wlan.active(True)
        wlan.connect(ssid, password)
        timeout = 10
        while not wlan.isconnected() and timeout > 0:
            time.sleep(1)
            timeout -= 1
        if wlan.isconnected():
            print('WiFi connected:', wlan.ifconfig())
            return
        print(f'Failed to connect to {ssid} - restarting WiFi Manager...')
        wlan.active(False)
