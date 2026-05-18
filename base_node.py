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


def debug(msg):
    if DEBUG_MODE:
        print("[BASE] " + msg)


def safe_int(value, default=0):
    try:
        return int(value)
    except:
        return default


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


def broadcast_reset(reason):
    send_packet(BASE_RESET, {"reason": reason, "players": selected_player_count})


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
    debug("Hard reset complete")


def stop_game():
    """Button B while running: stop current game and return everyone to lobby."""
    global game_running, current_hunter_id, game_started_ms
    game_running = False
    current_hunter_id = -1
    game_started_ms = 0
    broadcast_reset("base_stop")
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
    stop_game()


def update_display(now):
    global ui_frame, last_ui_ms
    if now - last_ui_ms < UI_FRAME_MS:
        return
    last_ui_ms = now

    if not game_running:
        # Lobby UI alternates between configured player count and ready count.
        if (ui_frame % 4) in (0, 1):
            display.show(str(selected_player_count))
        else:
            ready_count = 0
            for pid in players:
                if players[pid]["ready"]:
                    ready_count += 1
            display.show(str(min(ready_count, 9)))
        ui_frame += 1
        return

    # Running UI rotates through key game counts + shortest tagged countdown.
    counts = count_states()
    min_tag_seconds = min_tagged_remaining_seconds()
    frame_mode = ui_frame % 6
    if frame_mode == 0:
        display.show("R")
    elif frame_mode == 1:
        display.show(str(min(counts[SURVIVOR], 9)))
    elif frame_mode == 2:
        display.show("H")
    elif frame_mode == 3:
        display.show(str(min(counts[HUNTER], 9)))
    elif frame_mode == 4:
        display.show(str(min(counts[ELIMINATED], 9)))
    else:
        if min_tag_seconds < 0:
            display.show("-")
        else:
            # Last digit of shortest countdown keeps UI readable on 5x5 display.
            display.show(str(min_tag_seconds % 10))
    ui_frame += 1


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
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

display.scroll("BASE", delay=70)
debug("Base ready on radio group {0}".format(RADIO_GROUP))


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
while True:
    now = running_time()

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
            },
        )
        last_heartbeat_tx_ms = now

    check_end_condition()
    update_display(now)
    sleep(45)
