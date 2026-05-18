"""
HUNT Game System - Player Node (BBC micro:bit MicroPython)
-----------------------------------------------------------
Each wearable node runs this file.

State machine:
WAITING -> SURVIVOR/HUNTER -> TAGGED -> REVIVING -> SURVIVOR or ELIMINATED

Radio protocol is string-based and human-readable:
TYPE|key=value;key=value
"""

from microbit import *
import radio
import music


# ---------------------------------------------------------------------------
# REQUIRED TOP-LEVEL CONFIGURATION
# ---------------------------------------------------------------------------
PLAYER_ID = 1                     # Change this on each wearable: 1..10
RADIO_GROUP = 42                  # Same group for all game nodes
TAG_DISTANCE_THRESHOLD = -65      # RSSI threshold in dBm (higher = closer)
TAG_CONFIRM_TIME_MS = 2500        # Hunter must stay "close" this long to tag
TAGGED_COUNTDOWN_MS = 5 * 60 * 1000
REVIVE_TIME_MS = 4000             # How long medic revive takes
HEARTBEAT_INTERVALS = [1200, 950, 750, 550, 350]  # ms, faster near bleed-out
DEBUG_MODE = True


# ---------------------------------------------------------------------------
# Additional tuning values (safe to adjust)
# ---------------------------------------------------------------------------
BASE_ID = 0
BROADCAST_ID = 255
HUNTER_PING_INTERVAL_MS = 350
STATUS_INTERVAL_MS = 2000
HEARTBEAT_TX_INTERVAL_MS = 5000
PROXIMITY_MEMORY_MS = 1500
TAG_COOLDOWN_MS = 5000
DISPLAY_FRAME_MS = 180
REVIVE_FAIL_COOLDOWN_MS = 1200


# ---------------------------------------------------------------------------
# Player states
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


# ---------------------------------------------------------------------------
# LED icons and animation frames
# ---------------------------------------------------------------------------
ICON_HUNTER = Image("90909:09990:90909:09990:90009")
ICON_TAGGED = Image.NO
ICON_ELIMINATED = Image("90009:09090:00900:09090:90009")
ICON_WARNING = Image("00900:09990:90909:00900:00900")

WAITING_FRAMES = [Image.ARROW_N, Image.ARROW_E, Image.ARROW_S, Image.ARROW_W]
REVIVE_FRAMES = [Image.CLOCK1, Image.CLOCK3, Image.CLOCK6, Image.CLOCK9]


def debug(msg):
    """Print logs when DEBUG_MODE is enabled."""
    if DEBUG_MODE:
        print("[P{0}] {1}".format(PLAYER_ID, msg))


def safe_int(value, default=0):
    try:
        return int(value)
    except:
        return default


def encode_packet(msg_type, fields):
    """Encode TYPE and key/value fields into one radio string packet."""
    parts = []
    for key in fields:
        raw_val = str(fields[key])
        cleaned = raw_val.replace("|", "/").replace(";", ",").replace("=", ":")
        parts.append(str(key) + "=" + cleaned)
    if parts:
        return msg_type + "|" + ";".join(parts)
    return msg_type


def parse_packet(payload):
    """Parse TYPE|k=v;k=v into {'type': TYPE, 'fields': {...}}."""
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
    """Send a packet and include common fields automatically."""
    if extra_fields is None:
        extra_fields = {}

    packet_fields = {
        "src": PLAYER_ID,
        "dst": dest,
        "state": state,
        "ms": running_time(),
    }
    for key in extra_fields:
        packet_fields[key] = extra_fields[key]

    payload = encode_packet(msg_type, packet_fields)
    radio.send(payload)
    debug("TX " + payload)


def receive_packet():
    """
    Receive one packet with RSSI when possible.
    On micro:bit builds without receive_full(), RSSI is None.
    """
    if HAS_RECEIVE_FULL:
        incoming = radio.receive_full()
        if incoming is None:
            return None, None
        packet = parse_packet(incoming[0])
        return packet, incoming[1]

    incoming = radio.receive()
    if incoming is None:
        return None, None
    packet = parse_packet(incoming)
    return packet, None


def tone(freq, duration_ms):
    """Non-blocking tone when possible. Falls back to pin0 buzzer."""
    try:
        music.pitch(freq, duration_ms, wait=False)
        return
    except:
        pass

    # External buzzer fallback (pin0)
    try:
        if freq <= 0:
            return
        pin0.set_analog_period_microseconds(int(1000000 / freq))
        pin0.write_analog(512)
        sleep(duration_ms)
        pin0.write_digital(0)
    except:
        pass


def show_status_brief():
    """A+B: show player id and state abbreviation."""
    label = "?"
    if state == WAITING:
        label = "WT"
    elif state == SURVIVOR:
        label = "SV"
    elif state == HUNTER:
        label = "HN"
    elif state == TAGGED:
        label = "TG"
    elif state == REVIVING:
        label = "RV"
    elif state == ELIMINATED:
        label = "EL"
    display.scroll("P{0} {1}".format(PLAYER_ID, label), delay=70)


def reset_for_lobby():
    """Return node to WAITING, used for power-on and BASE_RESET."""
    global game_running, state, ready
    global tagged_deadline_ms, revive_end_ms, proximity_confirm_start_ms
    global fallback_window_start_ms, fallback_ping_count, fallback_strong_until_ms
    global last_strong_hunter_ms, last_tagged_change_ms, last_revive_fail_ms
    global last_heartbeat_sound_ms

    game_running = False
    state = WAITING
    ready = False
    tagged_deadline_ms = 0
    revive_end_ms = 0
    proximity_confirm_start_ms = 0
    fallback_window_start_ms = 0
    fallback_ping_count = 0
    fallback_strong_until_ms = 0
    last_strong_hunter_ms = 0
    last_tagged_change_ms = -TAG_COOLDOWN_MS
    last_revive_fail_ms = -REVIVE_FAIL_COOLDOWN_MS
    last_heartbeat_sound_ms = 0
    debug("Reset to lobby")


def start_round():
    """Handle BASE_START broadcast."""
    global game_running, state, tagged_deadline_ms, revive_end_ms
    global proximity_confirm_start_ms

    if not ready:
        debug("Ignored BASE_START because node is not ready")
        return

    game_running = True
    state = SURVIVOR
    tagged_deadline_ms = 0
    revive_end_ms = 0
    proximity_confirm_start_ms = 0
    debug("Round started as SURVIVOR")


def become_hunter():
    global state, proximity_confirm_start_ms
    if state == ELIMINATED:
        return
    state = HUNTER
    proximity_confirm_start_ms = 0
    debug("State -> HUNTER")


def become_tagged(new_infection):
    """
    Enter TAGGED state.
    new_infection=True resets the full bleed-out timer.
    """
    global state, tagged_deadline_ms, revive_end_ms, proximity_confirm_start_ms
    global last_tagged_change_ms

    now = running_time()
    if now - last_tagged_change_ms < TAG_COOLDOWN_MS and new_infection:
        return
    if state == ELIMINATED:
        return

    state = TAGGED
    revive_end_ms = 0
    proximity_confirm_start_ms = 0
    if new_infection or tagged_deadline_ms <= now:
        tagged_deadline_ms = now + TAGGED_COUNTDOWN_MS
    if new_infection:
        send_packet(PLAYER_TAGGED, {"player": PLAYER_ID, "remaining": tagged_deadline_ms - now})
    last_tagged_change_ms = now
    debug("State -> TAGGED (new={0})".format(new_infection))


def start_revive():
    global state, revive_end_ms
    if state != TAGGED:
        return
    state = REVIVING
    revive_end_ms = running_time() + REVIVE_TIME_MS
    send_packet(PLAYER_REVIVE_REQUEST, {"player": PLAYER_ID, "revive_ms": REVIVE_TIME_MS})
    debug("Revive requested")


def fail_revive():
    global state, revive_end_ms, last_revive_fail_ms
    now = running_time()
    state = TAGGED
    revive_end_ms = 0
    last_revive_fail_ms = now
    send_packet(PLAYER_REVIVE_FAIL, {"player": PLAYER_ID, "reason": "hunter_nearby"})
    debug("Revive failed (hunter nearby)")


def complete_revive():
    global state, tagged_deadline_ms, revive_end_ms, last_tagged_change_ms
    state = SURVIVOR
    tagged_deadline_ms = 0
    revive_end_ms = 0
    last_tagged_change_ms = running_time()
    send_packet(PLAYER_REVIVE_SUCCESS, {"player": PLAYER_ID})
    debug("Revive success -> SURVIVOR")


def eliminate():
    global state, revive_end_ms
    if state == ELIMINATED:
        return
    state = ELIMINATED
    revive_end_ms = 0
    send_packet(PLAYER_ELIMINATED, {"player": PLAYER_ID})
    debug("State -> ELIMINATED")


def heartbeat_interval_for_remaining(remaining_ms):
    """Pick heartbeat interval based on countdown progress."""
    if TAGGED_COUNTDOWN_MS <= 0:
        return HEARTBEAT_INTERVALS[-1]
    ratio = float(remaining_ms) / float(TAGGED_COUNTDOWN_MS)
    if ratio > 0.8:
        return HEARTBEAT_INTERVALS[0]
    if ratio > 0.6:
        return HEARTBEAT_INTERVALS[1]
    if ratio > 0.4:
        return HEARTBEAT_INTERVALS[2]
    if ratio > 0.2:
        return HEARTBEAT_INTERVALS[3]
    return HEARTBEAT_INTERVALS[4]


def update_fallback_proximity(now):
    """
    RSSI fallback for builds without receive_full():
    We treat 3 hunter pings in 1 second as "close".
    """
    global fallback_ping_count, fallback_window_start_ms, fallback_strong_until_ms
    if now - fallback_window_start_ms > 1000:
        fallback_window_start_ms = now
        fallback_ping_count = 1
    else:
        fallback_ping_count += 1

    if fallback_ping_count >= 3:
        fallback_strong_until_ms = now + PROXIMITY_MEMORY_MS


def hunter_nearby(now):
    if HAS_RECEIVE_FULL:
        return (now - last_strong_hunter_ms) <= PROXIMITY_MEMORY_MS
    return now <= fallback_strong_until_ms


def handle_incoming(packet, rssi):
    global last_strong_hunter_ms, proximity_confirm_start_ms
    global last_base_heartbeat_ms

    msg_type = packet["type"]
    fields = packet["fields"]
    src = safe_int(fields.get("src", "-1"), -1)
    dst = safe_int(fields.get("dst", str(BROADCAST_ID)), BROADCAST_ID)
    now = running_time()

    if dst not in (BROADCAST_ID, PLAYER_ID):
        return

    if msg_type == BASE_RESET:
        send_packet(ACK, {"for": BASE_RESET, "from_player": PLAYER_ID}, dest=BASE_ID)
        reset_for_lobby()
        return

    if msg_type == BASE_START:
        send_packet(ACK, {"for": BASE_START, "from_player": PLAYER_ID}, dest=BASE_ID)
        start_round()
        return

    if msg_type == BASE_ASSIGN_HUNTER:
        target = safe_int(fields.get("target", "-1"), -1)
        if target == PLAYER_ID and game_running:
            become_hunter()
            send_packet(ACK, {"for": BASE_ASSIGN_HUNTER, "from_player": PLAYER_ID}, dest=BASE_ID)
        return

    if msg_type == HEARTBEAT and src == BASE_ID:
        last_base_heartbeat_ms = now
        return

    if msg_type == PING:
        role = fields.get("role", "")
        if role != "hunter":
            return
        if src == PLAYER_ID:
            return
        if state == ELIMINATED:
            return

        # Primary proximity mode: RSSI (if environment supports receive_full()).
        if HAS_RECEIVE_FULL and (rssi is not None):
            if rssi >= TAG_DISTANCE_THRESHOLD:
                last_strong_hunter_ms = now
                if proximity_confirm_start_ms == 0:
                    proximity_confirm_start_ms = now
            elif now - last_strong_hunter_ms > PROXIMITY_MEMORY_MS:
                proximity_confirm_start_ms = 0
        else:
            # Reliable fallback when RSSI is unavailable.
            update_fallback_proximity(now)
            if proximity_confirm_start_ms == 0:
                proximity_confirm_start_ms = now


def send_status_snapshot():
    remaining = 0
    if state in (TAGGED, REVIVING) and tagged_deadline_ms > 0:
        now = running_time()
        remaining = tagged_deadline_ms - now
        if remaining < 0:
            remaining = 0
    send_packet(PLAYER_STATUS, {"player": PLAYER_ID, "ready": int(ready), "remaining": remaining})


def update_display(now):
    global display_frame_idx, last_display_frame_ms

    if now - last_display_frame_ms < DISPLAY_FRAME_MS:
        return
    last_display_frame_ms = now

    if state == WAITING:
        display.show(WAITING_FRAMES[display_frame_idx % len(WAITING_FRAMES)])
        display_frame_idx += 1
    elif state == SURVIVOR:
        display.show(Image.HEART)
    elif state == HUNTER:
        display.show(ICON_HUNTER)
    elif state == TAGGED:
        # Flash between warning and X to make danger obvious.
        if (display_frame_idx % 2) == 0:
            display.show(ICON_TAGGED)
        else:
            display.show(ICON_WARNING)
        display_frame_idx += 1
    elif state == REVIVING:
        display.show(REVIVE_FRAMES[display_frame_idx % len(REVIVE_FRAMES)])
        display_frame_idx += 1
    elif state == ELIMINATED:
        display.show(ICON_ELIMINATED)


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
HAS_RECEIVE_FULL = hasattr(radio, "receive_full")

radio.on()
radio.config(group=RADIO_GROUP, power=7, length=120, queue=25)
display.clear()
display.scroll("P{0}".format(PLAYER_ID), delay=70)

state = WAITING
ready = False
game_running = False

tagged_deadline_ms = 0
revive_end_ms = 0
proximity_confirm_start_ms = 0
last_strong_hunter_ms = 0
fallback_window_start_ms = 0
fallback_ping_count = 0
fallback_strong_until_ms = 0
last_tagged_change_ms = -TAG_COOLDOWN_MS
last_revive_fail_ms = -REVIVE_FAIL_COOLDOWN_MS

last_hunter_ping_tx_ms = 0
last_status_tx_ms = 0
last_heartbeat_tx_ms = 0
last_heartbeat_sound_ms = 0
last_base_heartbeat_ms = 0

display_frame_idx = 0
last_display_frame_ms = 0

debug("Player node ready (RSSI support={0})".format(HAS_RECEIVE_FULL))
send_status_snapshot()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
while True:
    now = running_time()

    # -------- Button controls --------
    if button_a.was_pressed() and state == WAITING:
        ready = not ready
        if ready:
            display.show(Image.YES)
        else:
            display.show(Image.NO)
        send_packet(PLAYER_READY, {"player": PLAYER_ID, "ready": int(ready)})
        send_status_snapshot()
        debug("Ready toggled -> {0}".format(ready))

    if button_b.was_pressed():
        # Medic action happens on the tagged player's node.
        if game_running and state == TAGGED and (now - last_revive_fail_ms > REVIVE_FAIL_COOLDOWN_MS):
            start_revive()

    if button_a.is_pressed() and button_b.is_pressed():
        show_status_brief()

    # -------- Receive all queued packets --------
    while True:
        pkt, pkt_rssi = receive_packet()
        if pkt is None:
            break
        handle_incoming(pkt, pkt_rssi)

    # -------- Hunter ping for proximity --------
    # Tagged players are also infected, so they broadcast hunter pings too.
    if game_running and state in (HUNTER, TAGGED) and (now - last_hunter_ping_tx_ms >= HUNTER_PING_INTERVAL_MS):
        send_packet(PING, {"role": "hunter", "hunter": PLAYER_ID, "infected": int(state == TAGGED)})
        last_hunter_ping_tx_ms = now

    # -------- Survivor gets tagged by sustained hunter proximity --------
    if game_running and state == SURVIVOR and proximity_confirm_start_ms > 0:
        if hunter_nearby(now):
            if now - proximity_confirm_start_ms >= TAG_CONFIRM_TIME_MS:
                become_tagged(True)
                send_status_snapshot()
        else:
            proximity_confirm_start_ms = 0

    # -------- Tagged/Reviving logic --------
    if state in (TAGGED, REVIVING):
        remaining = tagged_deadline_ms - now
        if remaining <= 0:
            eliminate()
            send_status_snapshot()
        else:
            interval = heartbeat_interval_for_remaining(remaining)
            if now - last_heartbeat_sound_ms >= interval:
                # Slightly rising tone as danger increases
                tone_freq = 160 + int((TAGGED_COUNTDOWN_MS - remaining) / 2500)
                tone(tone_freq, 70)
                last_heartbeat_sound_ms = now

    if state == REVIVING:
        # Revive fails immediately if a hunter remains close.
        if hunter_nearby(now):
            fail_revive()
            send_status_snapshot()
        elif now >= revive_end_ms:
            complete_revive()
            send_status_snapshot()

    # -------- Periodic status + heartbeat telemetry --------
    if now - last_status_tx_ms >= STATUS_INTERVAL_MS:
        send_status_snapshot()
        last_status_tx_ms = now

    if now - last_heartbeat_tx_ms >= HEARTBEAT_TX_INTERVAL_MS:
        send_packet(HEARTBEAT, {"player": PLAYER_ID, "ready": int(ready), "running": int(game_running)})
        last_heartbeat_tx_ms = now

    update_display(now)
    sleep(40)
