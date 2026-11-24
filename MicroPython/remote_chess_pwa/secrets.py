"""
FSD Remote Chess - Secrets Configuration Template
Copy this file as 'secrets.py' and fill in your credentials.
The real file is listed in .gitignore and will never be pushed to GitHub.
"""

# WiFi Configuration
WIFI_SSID = "your_wifi_ssid"
WIFI_PASSWORD = "your_wifi_password"

# MQTT Broker Configuration
MQTT_BROKER = "broker.example.com"  # IP address or hostname
MQTT_PORT = 1883                     # Default MQTT port
MQTT_USER = "your_mqtt_username"     # Leave empty string "" if no auth
MQTT_PASSWORD = "your_mqtt_password" # Leave empty string "" if no auth
MQTT_CLIENT_ID = "remote_chess_board_1"  # Unique ID for this board
MQTT_TOPIC_WHITE = "DEIN_BENUTZERNAME/feeds/chess-white"  # MQTT topic for white player moves
MQTT_TOPIC_BLACK = "DEIN_BENUTZERNAME/feeds/chess-black"  # MQTT topic for black player moves

# Player color — determines which MQTT topic this board publishes/subscribes to
# "white" → publishes to chess-white, subscribes to chess-black
# "black" → publishes to chess-black, subscribes to chess-white
PLAYER_COLOR = "white"

# Pin Configuration — adjust per board hardware
# Xiao ESP32-C3: PIN_SDA=6, PIN_SCL=7, PIN_NEOPIXEL=5,  I2C_FREQ=50000
# Xiao ESP32-C6: PIN_SDA=22, PIN_SCL=23, PIN_NEOPIXEL=18, I2C_FREQ=100000
PIN_SDA = 6
PIN_SCL = 7
PIN_NEOPIXEL = 5
I2C_FREQ = 50000
