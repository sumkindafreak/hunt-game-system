"""
HUNT Game System - Base/Controller Node (BBC micro:bit MicroPython)
--------------------------------------------------------------------
This micro:bit acts as the game desk/controller:
- Lobby setup (player count)
- Start game
- Assign first hunter
- Monitor status
- End/reset rounds
"""

from microbit import *
import radio
import random
import music
try:
    import microphone
    HAS_MICROPHONE = True
except:
    microphone = None
    HAS_MICROPHONE = False


# ---------------------------------------------------------------------------
# REQUIRED TOP-LEVEL CONFIGURATION
# ---------------------------------------------------------------------------
PLAYER_ID = 0                     # Base/controller node id
RADIO_GROUP = 42                  # Same group for all nodes
TAG_DISTANCE_THRESHOLD = -65      # Shared config value for protocol visibility
TAG_CONFIRM_TIME_MS = 2500        # Shared config value for protocol visibility
TAGGED_COUNTDOWN_MS = 5 * 60 * 1000
REVIVE_TIME_MS = 4000
HEARTBEAT_INTERVALS = [1200, 950, 750, 550, 350]
DEBUG_MODE = True


# ---------------------------------------------------------------------------
# Base settings
# ---------------------------------------------------------------------------
MIN_PLAYERS = 2
MAX_PLAYERS = 10
DEFAULT_PLAYERS = 10
BROADCAST_ID = 255
STATUS_STALE_MS = 12000
HEARTBEAT_INTERVAL_MS = 1000
UI_FRAME_MS = 220
BRIGHTNESS_UPDATE_MS = 1200
LOGO_TOUCH_COOLDOWN_MS = 700
SHAKE_STOP_COOLDOWN_MS = 3000
CLAP_START_ENABLED = True
CLAP_START_THRESHOLD = 170
CLAP_START_WINDOW_MS = 1400


# ---------------------------------------------------------------------------
# State names
# ---------------------------------------------------------------------------
WAITING = "WAITING"
SURVIVOR = "SURVIVOR"
HUNTER = "HUNTER"
TAGGED = "TAGGED"
REVIVING = "REVIVING"
ELIMINATED = "ELIMINATED"


# ---------------------------------------------------------------------------
# Packet types required by spec
# ---------------------------------------------------------------------------
BASE_START = "BASE_START"
BASE_RESET = "BASE_RESET"
BASE_ASSIGN_HUNTER = "BASE_ASSIGN_HUNTER"
PLAYER_READY = "PLAYER_READY"
PLAYER_STATUS = "PLAYER_STATUS"
PLAYER_TAGGED = "PLAYER_TAGGED"
PLAYER_REVIVE_REQUEST = "PLAYER_REVIVE_REQUEST"
PLAYER_REVIVE_SUCCESS = "PLAYER_REVIVE_SUCCESS"
PLAYER_REVIVE_FAIL = "PLAYER_REVIVE_FAIL"
PLAYER_ELIMINATED = "PLAYER_ELIMINATED"
HEARTBEAT = "HEARTBEAT"
PING = "PING"
ACK = "ACK"
PLAYER_SENSOR = "PLAYER_SENSOR"
PLAYER_ALERT = "PLAYER_ALERT"


def debug(msg):
    if DEBUG_MODE:
        print("[BASE] " + msg)


def safe_int(value, default=0):
    try:
        return int(value)
    except:
        return default


def detect_logo_touch_support():
    try:
        pin_logo  # noqa: F821 (exists on micro:bit v2)
        return hasattr(pin_logo, "is_touched")
    except:
        return False


def read_sound_level():
    if not HAS_MICROPHONE:
        return -1
    try:
        return int(microphone.sound_level())
    except:
        return -1


def read_heading():
    try:
        if hasattr(compass, "is_calibrated") and (not compass.is_calibrated()):
            return -1
        return int(compass.heading())
    except:
        return -1


def adapt_display_brightness(now):
    global last_brightness_ms
    if now - last_brightness_ms < BRIGHTNESS_UPDATE_MS:
        return
    last_brightness_ms = now
    raw = display.read_light_level()
    level = 1 + int((raw * 8) / 255)
    if level < 1:
        level = 1
    if level > 9:
        level = 9
    try:
        display.set_brightness(level)
    except:
        pass


def encode_packet(msg_type, fields):
    parts = []
    for key in fields:
        raw_val = str(fields[key])
        cleaned = raw_val.replace("|", "/").replace(";", ",").replace("=", ":")
        parts.append(str(key) + "=" + cleaned)
    if parts:
        return msg_type + "|" + ";".join(parts)
    return msg_type


def parse_packet(payload):
    if payload is None:
        return None
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8")
        except:
            return None

    payload = str(payload)
    if "|" in payload:
        msg_type, raw_fields = payload.split("|", 1)
    else:
        msg_type = payload
        raw_fields = ""

    fields = {}
    if raw_fields:
        chunks = raw_fields.split(";")
        for chunk in chunks:
            if not chunk:
                continue
            if "=" in chunk:
                key, value = chunk.split("=", 1)
                fields[key] = value
    return {"type": msg_type, "fields": fields}


def send_packet(msg_type, extra_fields=None, dest=BROADCAST_ID):
    if extra_fields is None:
        extra_fields = {}
    fields = {
        "src": PLAYER_ID,
        "dst": dest,
        "round": round_number,
        "running": int(game_running),
        "ms": running_time(),
    }
    for key in extra_fields:
        fields[key] = extra_fields[key]
    payload = encode_packet(msg_type, fields)
    radio.send(payload)
    debug("TX " + payload)


def receive_packet():
    incoming = radio.receive()
    if incoming is None:
        return None
    return parse_packet(incoming)


def ensure_player(player_id):
    if player_id not in players:
        players[player_id] = {
            "ready": False,
            "state": WAITING,
            "remaining": 0,
            "last_seen_ms": running_time(),
            "temp": 0,
            "light": 0,
            "snd": -1,
            "head": -1,
            "ax": 0,
            "ay": 0,
            "az": 0,
            "v2": 0,
            "last_alert": "",
        }
    return players[player_id]


def active_player_ids():
    """
    Use explicitly ready players when possible.
    Fallback to configured range if no one has reported ready yet.
    """
    ids = []
    for pid in players:
        if players[pid]["ready"]:
            ids.append(pid)
    ids.sort()

    if ids:
        return ids
    # Beginner-friendly fallback: allows quick testing without ready packets.
    return [pid for pid in range(1, selected_player_count + 1)]


def count_states():
    counts = {
        WAITING: 0,
        SURVIVOR: 0,
        HUNTER: 0,
        TAGGED: 0,
        REVIVING: 0,
        ELIMINATED: 0,
    }
    now = running_time()

    for pid in players:
        info = players[pid]
        if now - info["last_seen_ms"] > STATUS_STALE_MS:
            continue
        current = info["state"]
        if current in counts:
            counts[current] += 1
    return counts


def min_tagged_remaining_seconds():
    """Return shortest remaining bleed-out time among tagged/reviving players."""
    min_remaining = None
    now = running_time()
    for pid in players:
        info = players[pid]
        if now - info["last_seen_ms"] > STATUS_STALE_MS:
            continue
        if info["state"] in (TAGGED, REVIVING):
            remaining = safe_int(info.get("remaining", 0), 0)
            if min_remaining is None or remaining < min_remaining:
                min_remaining = remaining

    if min_remaining is None:
        return -1
    seconds = int(min_remaining / 1000)
    if seconds < 0:
        return 0
    return seconds


def sensor_averages():
    """Average player telemetry for dashboard pages."""
    now = running_time()
    temp_total = 0
    light_total = 0
    sound_total = 0
    valid_temp = 0
    valid_light = 0
    valid_sound = 0

    for pid in players:
        info = players[pid]
        if now - info["last_seen_ms"] > STATUS_STALE_MS:
            continue

        if "temp" in info:
            temp_total += safe_int(info["temp"], 0)
            valid_temp += 1
        if "light" in info:
            light_total += safe_int(info["light"], 0)
            valid_light += 1
        snd = safe_int(info.get("snd", -1), -1)
        if snd >= 0:
            sound_total += snd
            valid_sound += 1

    avg_temp = -1 if valid_temp == 0 else int(temp_total / valid_temp)
    avg_light = -1 if valid_light == 0 else int(light_total / valid_light)
    avg_sound = -1 if valid_sound == 0 else int(sound_total / valid_sound)
    return avg_temp, avg_light, avg_sound


def broadcast_reset(reason):
    send_packet(BASE_RESET, {"reason": reason, "players": selected_player_count})


def tone(freq, duration_ms):
    try:
        music.pitch(freq, duration_ms, wait=False)
        return
    except:
        pass
    try:
        pin0.set_analog_period_microseconds(int(1000000 / freq))
        pin0.write_analog(512)
        sleep(duration_ms)
        pin0.write_digital(0)
    except:
        pass


def play_event_sound(event_name):
    if event_name == "start":
        tone(523, 90)
        sleep(15)
        tone(659, 90)
    elif event_name == "stop":
        tone(220, 130)
    elif event_name == "reset":
        tone(250, 110)
    elif event_name == "end":
        tone(180, 160)
    elif event_name == "alert":
        tone(700, 60)


def start_game():
    global game_running, round_number, current_hunter_id, game_started_ms

    ids = active_player_ids()
    if len(ids) < MIN_PLAYERS:
        display.scroll("N<2", delay=70)
        debug("Not enough active players to start")
        return

    # Mark all active players as survivors before assigning first hunter.
    for pid in ids:
        info = ensure_player(pid)
        info["state"] = SURVIVOR
        info["remaining"] = TAGGED_COUNTDOWN_MS
        info["last_seen_ms"] = running_time()

    chosen_index = random.randint(0, len(ids) - 1)
    current_hunter_id = ids[chosen_index]
    ensure_player(current_hunter_id)["state"] = HUNTER

    round_number += 1
    game_started_ms = running_time()
    game_running = True

    send_packet(BASE_START, {"players": selected_player_count, "hunter": current_hunter_id})
    sleep(60)
    send_packet(BASE_ASSIGN_HUNTER, {"target": current_hunter_id})
    play_event_sound("start")
    debug("Round {0} started. First hunter=P{1}".format(round_number, current_hunter_id))


def hard_reset():
    global game_running, current_hunter_id, game_started_ms

    game_running = False
    current_hunter_id = -1
    game_started_ms = 0

    for pid in players:
        players[pid]["state"] = WAITING
        players[pid]["remaining"] = 0
        players[pid]["ready"] = False

    broadcast_reset("manual_reset")
    play_event_sound("reset")
    debug("Hard reset complete")


def stop_game():
    """Button B while running: stop current game and return everyone to lobby."""
    global game_running, current_hunter_id, game_started_ms
    game_running = False
    current_hunter_id = -1
    game_started_ms = 0
    broadcast_reset("base_stop")
    play_event_sound("stop")
    debug("Game stopped from base")


def handle_player_message(packet):
    msg_type = packet["type"]
    fields = packet["fields"]
    src = safe_int(fields.get("src", "-1"), -1)
    dst = safe_int(fields.get("dst", str(BROADCAST_ID)), BROADCAST_ID)

    if src <= 0:
        return
    if dst not in (BROADCAST_ID, PLAYER_ID):
        return

    info = ensure_player(src)
    info["last_seen_ms"] = running_time()

    if msg_type == PLAYER_READY:
        info["ready"] = safe_int(fields.get("ready", "0"), 0) == 1
        info["state"] = WAITING if info["ready"] else WAITING
        send_packet(ACK, {"for": PLAYER_READY, "target": src}, dest=src)
        debug("P{0} ready={1}".format(src, info["ready"]))
        return

    if msg_type == PLAYER_STATUS:
        info["state"] = fields.get("state", info["state"])
        info["ready"] = safe_int(fields.get("ready", "0"), 0) == 1 or info["ready"]
        info["remaining"] = safe_int(fields.get("remaining", "0"), 0)
        info["temp"] = safe_int(fields.get("temp", info.get("temp", 0)), info.get("temp", 0))
        info["light"] = safe_int(fields.get("light", info.get("light", 0)), info.get("light", 0))
        info["snd"] = safe_int(fields.get("snd", info.get("snd", -1)), info.get("snd", -1))
        info["head"] = safe_int(fields.get("head", info.get("head", -1)), info.get("head", -1))
        return

    if msg_type == PLAYER_TAGGED:
        info["state"] = TAGGED
        send_packet(ACK, {"for": PLAYER_TAGGED, "target": src}, dest=src)
        debug("P{0} tagged".format(src))
        return

    if msg_type == PLAYER_REVIVE_REQUEST:
        info["state"] = REVIVING
        send_packet(ACK, {"for": PLAYER_REVIVE_REQUEST, "target": src}, dest=src)
        debug("P{0} reviving".format(src))
        return

    if msg_type == PLAYER_REVIVE_SUCCESS:
        info["state"] = SURVIVOR
        send_packet(ACK, {"for": PLAYER_REVIVE_SUCCESS, "target": src}, dest=src)
        debug("P{0} revive success".format(src))
        return

    if msg_type == PLAYER_REVIVE_FAIL:
        info["state"] = TAGGED
        send_packet(ACK, {"for": PLAYER_REVIVE_FAIL, "target": src}, dest=src)
        debug("P{0} revive fail".format(src))
        return

    if msg_type == PLAYER_ELIMINATED:
        info["state"] = ELIMINATED
        send_packet(ACK, {"for": PLAYER_ELIMINATED, "target": src}, dest=src)
        debug("P{0} eliminated".format(src))
        return

    if msg_type == PLAYER_SENSOR:
        info["temp"] = safe_int(fields.get("temp", info.get("temp", 0)), info.get("temp", 0))
        info["light"] = safe_int(fields.get("light", fields.get("l", info.get("light", 0))), info.get("light", 0))
        info["snd"] = safe_int(fields.get("snd", info.get("snd", -1)), info.get("snd", -1))
        info["head"] = safe_int(fields.get("head", fields.get("h", info.get("head", -1))), info.get("head", -1))
        info["ax"] = safe_int(fields.get("ax", info.get("ax", 0)), info.get("ax", 0))
        info["ay"] = safe_int(fields.get("ay", info.get("ay", 0)), info.get("ay", 0))
        info["az"] = safe_int(fields.get("az", info.get("az", 0)), info.get("az", 0))
        info["v2"] = safe_int(fields.get("v2", info.get("v2", 0)), info.get("v2", 0))
        return

    if msg_type == PLAYER_ALERT:
        info["last_alert"] = fields.get("event", "alert")
        play_event_sound("alert")
        send_packet(ACK, {"for": PLAYER_ALERT, "target": src}, dest=src)
        debug("P{0} alert={1}".format(src, info["last_alert"]))
        return

    if msg_type == HEARTBEAT:
        return

    if msg_type == PING:
        return

    if msg_type == ACK:
        return


def check_end_condition():
    """
    End game when every active player is infected or eliminated.
    Tagged/reviving are treated as infected hunter-equivalent states.
    """
    if not game_running:
        return

    ids = active_player_ids()
    if not ids:
        return

    for pid in ids:
        info = ensure_player(pid)
        st = info["state"]
        if st in (SURVIVOR, WAITING):
            return

    # All players are HUNTER or ELIMINATED -> game over.
    debug("End condition met. Resetting to lobby.")
    display.scroll("END", delay=70)
    play_event_sound("end")
    stop_game()


def update_display(now):
    global ui_frame, last_ui_ms
    if now - last_ui_ms < UI_FRAME_MS:
        return
    last_ui_ms = now
    avg_temp, avg_light, avg_sound = sensor_averages()
    base_light = display.read_light_level()
    base_sound = last_base_sound_level

    if not game_running:
        # Lobby UI alternates between player setup and live environment telemetry.
        frame_mode = ui_frame % 6
        if frame_mode in (0, 1):
            display.show(str(selected_player_count))
        elif frame_mode == 2:
            ready_count = 0
            for pid in players:
                if players[pid]["ready"]:
                    ready_count += 1
            display.show(str(min(ready_count, 9)))
        elif frame_mode == 3:
            if avg_temp < 0:
                display.show("-")
            else:
                display.show(str(abs(avg_temp) % 10))
        elif frame_mode == 4:
            display.show(str(int(base_light / 28)))
        else:
            if base_sound < 0:
                display.show(".")
            else:
                display.show(str(int(base_sound / 28)))
        ui_frame += 1
        return

    counts = count_states()
    min_tag_seconds = min_tagged_remaining_seconds()
    frame_mode = ui_frame % 7

    # Dashboard page 0: core game counters.
    if dashboard_page == 0:
        if frame_mode == 0:
            display.show("R")
        elif frame_mode == 1:
            display.show(str(min(counts[SURVIVOR], 9)))
        elif frame_mode == 2:
            display.show("H")
        elif frame_mode == 3:
            display.show(str(min(counts[HUNTER], 9)))
        elif frame_mode == 4:
            display.show("E")
        elif frame_mode == 5:
            display.show(str(min(counts[ELIMINATED], 9)))
        else:
            display.show(str(min(counts[TAGGED], 9)))

    # Dashboard page 1: tagged countdown monitor.
    elif dashboard_page == 1:
        if frame_mode == 0:
            display.show("T")
        elif frame_mode == 1:
            display.show(str(min(counts[TAGGED], 9)))
        elif frame_mode == 2:
            display.show("V")
        elif frame_mode == 3:
            display.show(str(min(counts[REVIVING], 9)))
        elif frame_mode == 4:
            display.show("C")
        elif frame_mode == 5:
            if min_tag_seconds < 0:
                display.show("-")
            else:
                display.show(str(min_tag_seconds % 10))
        else:
            display.show(str(min(counts[ELIMINATED], 9)))

    # Dashboard page 2: hardware telemetry (temp/light/sound/heading).
    else:
        head = read_heading()
        if frame_mode == 0:
            display.show("T")
        elif frame_mode == 1:
            if avg_temp < 0:
                display.show("-")
            else:
                display.show(str(abs(avg_temp) % 10))
        elif frame_mode == 2:
            display.show("L")
        elif frame_mode == 3:
            display.show(str(int(avg_light / 28) if avg_light >= 0 else 0))
        elif frame_mode == 4:
            display.show("S")
        elif frame_mode == 5:
            display.show(str(int(avg_sound / 28) if avg_sound >= 0 else 0))
        else:
            if head < 0:
                display.show("?")
            else:
                display.show(str(head % 10))
    ui_frame += 1


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
HAS_LOGO_TOUCH = detect_logo_touch_support()

radio.on()
radio.config(group=RADIO_GROUP, power=7, length=120, queue=25)

selected_player_count = DEFAULT_PLAYERS
game_running = False
round_number = 0
current_hunter_id = -1
game_started_ms = 0

players = {}

last_heartbeat_tx_ms = 0
last_ui_ms = 0
ui_frame = 0
last_brightness_ms = 0
last_logo_touch_ms = -LOGO_TOUCH_COOLDOWN_MS
last_shake_stop_ms = -SHAKE_STOP_COOLDOWN_MS
last_clap_ms = -CLAP_START_WINDOW_MS
clap_count = 0
dashboard_page = 0
last_base_sound_level = -1

display.scroll("BASE", delay=70)
debug("Base ready group={0} logo={1} mic={2}".format(RADIO_GROUP, HAS_LOGO_TOUCH, HAS_MICROPHONE))


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
while True:
    now = running_time()
    adapt_display_brightness(now)

    current_sound = read_sound_level()
    if current_sound >= 0:
        last_base_sound_level = current_sound

    # -------- Button controls --------
    if button_a.was_pressed() and (not game_running):
        selected_player_count += 1
        if selected_player_count > MAX_PLAYERS:
            selected_player_count = MIN_PLAYERS
        debug("Selected players -> {0}".format(selected_player_count))
        display.scroll("P{0}".format(selected_player_count), delay=60)

    if button_b.was_pressed():
        if not game_running:
            start_game()
        else:
            stop_game()

    if button_a.is_pressed() and button_b.is_pressed():
        hard_reset()
        display.scroll("RST", delay=60)

    # Optional micro:bit v2 logo touch: cycle dashboard pages.
    if HAS_LOGO_TOUCH:
        touched = False
        try:
            touched = pin_logo.is_touched()
        except:
            touched = False
        if touched and (now - last_logo_touch_ms >= LOGO_TOUCH_COOLDOWN_MS):
            last_logo_touch_ms = now
            if button_a.is_pressed():
                try:
                    display.scroll("CAL", delay=60)
                    compass.calibrate()
                    display.show(Image.YES)
                except:
                    display.show(Image.NO)
            else:
                dashboard_page += 1
                if dashboard_page > 2:
                    dashboard_page = 0
                display.scroll("D{0}".format(dashboard_page), delay=60)

    # Shake gesture is an emergency stop input while game is running.
    if game_running and accelerometer.was_gesture("shake") and (now - last_shake_stop_ms >= SHAKE_STOP_COOLDOWN_MS):
        last_shake_stop_ms = now
        display.scroll("STOP", delay=60)
        stop_game()

    # Optional clap-start in lobby for hands-free game start.
    if (not game_running) and CLAP_START_ENABLED and HAS_MICROPHONE and (current_sound >= CLAP_START_THRESHOLD):
        if now - last_clap_ms <= CLAP_START_WINDOW_MS:
            clap_count += 1
        else:
            clap_count = 1
        last_clap_ms = now
        if clap_count >= 2:
            display.scroll("CLP", delay=60)
            start_game()
            clap_count = 0

    # -------- Read all pending packets --------
    while True:
        pkt = receive_packet()
        if pkt is None:
            break
        handle_player_message(pkt)

    # -------- Base heartbeat broadcast --------
    if now - last_heartbeat_tx_ms >= HEARTBEAT_INTERVAL_MS:
        counts = count_states()
        send_packet(
            HEARTBEAT,
            {
                "players": selected_player_count,
                "hunter_count": counts[HUNTER],
                "survivor_count": counts[SURVIVOR],
                "dash": dashboard_page,
                "snd": last_base_sound_level,
                "head": read_heading(),
            },
        )
        last_heartbeat_tx_ms = now

    check_end_condition()
    update_display(now)
    sleep(45)
