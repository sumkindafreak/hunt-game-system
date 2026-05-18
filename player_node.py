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
try:
    import microphone
    HAS_MICROPHONE = True
except:
    microphone = None
    HAS_MICROPHONE = False


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
SENSOR_TX_INTERVAL_MS = 4000
BRIGHTNESS_UPDATE_MS = 1200
SHAKE_ALERT_COOLDOWN_MS = 5000
LOGO_TOUCH_COOLDOWN_MS = 700
LOUD_SOUND_THRESHOLD = 120
LOUD_TAG_CONFIRM_FACTOR_PERCENT = 65
COMBO_STATUS_COOLDOWN_MS = 2000


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
PLAYER_SENSOR = "PLAYER_SENSOR"
PLAYER_ALERT = "PLAYER_ALERT"


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


def safe_set_brightness(level):
    try:
        display.set_brightness(level)
    except:
        pass


def adapt_display_brightness(now):
    global last_brightness_update_ms
    if now - last_brightness_update_ms < BRIGHTNESS_UPDATE_MS:
        return
    last_brightness_update_ms = now

    # Re-use LED matrix as ambient light sensor input.
    light_raw = display.read_light_level()
    mapped = 1 + int((light_raw * 8) / 255)
    if mapped < 1:
        mapped = 1
    if mapped > 9:
        mapped = 9
    safe_set_brightness(mapped)


def external_state_output(now):
    """
    Optional external pin outputs:
    - pin1: analog state level for LED bars or recorder input.
    - pin2: heartbeat pulse mirror while tagged/reviving.
    """
    level = 0
    if state == WAITING:
        level = 80
    elif state == SURVIVOR:
        level = 220
    elif state == HUNTER:
        level = 450
    elif state == TAGGED:
        level = 700
    elif state == REVIVING:
        level = 860
    elif state == ELIMINATED:
        level = 1023

    try:
        pin1.write_analog(level)
    except:
        pass

    pulse_on = False
    if state in (TAGGED, REVIVING):
        if now - last_heartbeat_sound_ms < 100:
            pulse_on = True
    try:
        pin2.write_digital(1 if pulse_on else 0)
    except:
        pass


def send_sensor_packet():
    ax = 0
    ay = 0
    az = 0
    temp_c = 0
    light = 0
    try:
        ax = accelerometer.get_x()
        ay = accelerometer.get_y()
        az = accelerometer.get_z()
    except:
        pass
    try:
        temp_c = temperature()
    except:
        temp_c = 0
    try:
        light = display.read_light_level()
    except:
        light = 0

    send_packet(
        PLAYER_SENSOR,
        {
            "p": PLAYER_ID,
            "temp": temp_c,
            "l": light,
            "h": read_heading(),
            "snd": last_sound_level,
            "ax": ax,
            "ay": ay,
            "az": az,
            "v2": int(HAS_MICROPHONE or HAS_LOGO_TOUCH),
        },
        include_common=False,
    )


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


def send_packet(msg_type, extra_fields=None, dest=BROADCAST_ID, include_common=True):
    """Send a packet and include common fields automatically."""
    if extra_fields is None:
        extra_fields = {}

    packet_fields = {
        "src": PLAYER_ID,
        "dst": dest,
    }
    if include_common:
        packet_fields["state"] = state
        packet_fields["ms"] = running_time()
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


def play_transition_sound(event_name):
    """Short event cues to use the built-in speaker/buzzer."""
    if event_name == "start":
        tone(523, 90)
    elif event_name == "hunter":
        tone(190, 120)
    elif event_name == "tagged":
        tone(240, 140)
    elif event_name == "revive_ok":
        tone(523, 80)
        sleep(20)
        tone(659, 80)
    elif event_name == "revive_fail":
        tone(180, 110)
    elif event_name == "eliminated":
        tone(150, 220)
    elif event_name == "alert":
        tone(700, 60)


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


def toggle_ready_state():
    global ready
    if state != WAITING:
        return
    ready = not ready
    if ready:
        display.show(Image.YES)
    else:
        display.show(Image.NO)
    send_packet(PLAYER_READY, {"player": PLAYER_ID, "ready": int(ready)})
    send_status_snapshot()
    debug("Ready toggled -> {0}".format(ready))


def reset_for_lobby():
    """Return node to WAITING, used for power-on and BASE_RESET."""
    global game_running, state, ready
    global tagged_deadline_ms, revive_end_ms, proximity_confirm_start_ms
    global fallback_window_start_ms, fallback_ping_count, fallback_strong_until_ms
    global last_strong_hunter_ms, last_tagged_change_ms, last_revive_fail_ms
    global last_heartbeat_sound_ms
    global last_logo_touch_ms, last_shake_alert_ms, last_sensor_tx_ms, last_combo_status_ms

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
    last_logo_touch_ms = -LOGO_TOUCH_COOLDOWN_MS
    last_shake_alert_ms = -SHAKE_ALERT_COOLDOWN_MS
    last_sensor_tx_ms = 0
    last_combo_status_ms = -COMBO_STATUS_COOLDOWN_MS
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
    play_transition_sound("start")
    debug("Round started as SURVIVOR")


def become_hunter():
    global state, proximity_confirm_start_ms
    if state == ELIMINATED:
        return
    state = HUNTER
    proximity_confirm_start_ms = 0
    play_transition_sound("hunter")
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
        play_transition_sound("tagged")
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
    play_transition_sound("revive_fail")
    send_packet(PLAYER_REVIVE_FAIL, {"player": PLAYER_ID, "reason": "hunter_nearby"})
    debug("Revive failed (hunter nearby)")


def complete_revive():
    global state, tagged_deadline_ms, revive_end_ms, last_tagged_change_ms
    state = SURVIVOR
    tagged_deadline_ms = 0
    revive_end_ms = 0
    last_tagged_change_ms = running_time()
    play_transition_sound("revive_ok")
    send_packet(PLAYER_REVIVE_SUCCESS, {"player": PLAYER_ID})
    debug("Revive success -> SURVIVOR")


def eliminate():
    global state, revive_end_ms
    if state == ELIMINATED:
        return
    state = ELIMINATED
    revive_end_ms = 0
    play_transition_sound("eliminated")
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
    temp_c = 0
    light = 0
    if state in (TAGGED, REVIVING) and tagged_deadline_ms > 0:
        now = running_time()
        remaining = tagged_deadline_ms - now
        if remaining < 0:
            remaining = 0
    try:
        temp_c = temperature()
    except:
        temp_c = 0
    try:
        light = display.read_light_level()
    except:
        light = 0
    send_packet(
        PLAYER_STATUS,
        {
            "player": PLAYER_ID,
            "ready": int(ready),
            "remaining": remaining,
            "temp": temp_c,
            "light": light,
            "snd": last_sound_level,
            "head": read_heading(),
        },
    )


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
HAS_LOGO_TOUCH = detect_logo_touch_support()

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
last_sensor_tx_ms = 0
last_heartbeat_sound_ms = 0
last_base_heartbeat_ms = 0
last_brightness_update_ms = 0
last_logo_touch_ms = -LOGO_TOUCH_COOLDOWN_MS
last_shake_alert_ms = -SHAKE_ALERT_COOLDOWN_MS
last_combo_status_ms = -COMBO_STATUS_COOLDOWN_MS
last_sound_level = -1

display_frame_idx = 0
last_display_frame_ms = 0

debug(
    "Player ready RSSI={0} logo={1} mic={2}".format(
        HAS_RECEIVE_FULL,
        HAS_LOGO_TOUCH,
        HAS_MICROPHONE,
    )
)
send_status_snapshot()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
while True:
    now = running_time()
    adapt_display_brightness(now)

    latest_sound = read_sound_level()
    if latest_sound >= 0:
        last_sound_level = latest_sound

    # -------- Button controls --------
    if button_a.was_pressed() and state == WAITING:
        toggle_ready_state()

    if button_b.was_pressed():
        # Medic action happens on the tagged player's node.
        if game_running and state == TAGGED and (now - last_revive_fail_ms > REVIVE_FAIL_COOLDOWN_MS):
            start_revive()
        else:
            show_status_brief()

    # micro:bit v2 logo touch adds an easy "glove-safe" control input.
    if HAS_LOGO_TOUCH:
        touched = False
        try:
            touched = pin_logo.is_touched()
        except:
            touched = False
        if touched and (now - last_logo_touch_ms >= LOGO_TOUCH_COOLDOWN_MS):
            last_logo_touch_ms = now
            if state == WAITING and button_a.is_pressed():
                try:
                    display.scroll("CAL", delay=70)
                    compass.calibrate()
                    display.show(Image.YES)
                except:
                    display.show(Image.NO)
            elif state == WAITING:
                toggle_ready_state()
            elif game_running and state == TAGGED and (now - last_revive_fail_ms > REVIVE_FAIL_COOLDOWN_MS):
                start_revive()
            else:
                show_status_brief()

    if button_a.is_pressed() and button_b.is_pressed() and (now - last_combo_status_ms >= COMBO_STATUS_COOLDOWN_MS):
        last_combo_status_ms = now
        show_status_brief()

    if accelerometer.was_gesture("shake") and (now - last_shake_alert_ms >= SHAKE_ALERT_COOLDOWN_MS):
        last_shake_alert_ms = now
        temp_now = 0
        try:
            temp_now = temperature()
        except:
            temp_now = 0
        play_transition_sound("alert")
        send_packet(
            PLAYER_ALERT,
            {
                "player": PLAYER_ID,
                "event": "shake",
                "snd": last_sound_level,
                "temp": temp_now,
            },
        )
        send_status_snapshot()
        debug("Shake alert broadcast")

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
        confirm_ms = TAG_CONFIRM_TIME_MS
        # Optional v2 stealth rule: loud survivor gets tagged faster.
        if HAS_MICROPHONE and (last_sound_level >= LOUD_SOUND_THRESHOLD):
            confirm_ms = int((TAG_CONFIRM_TIME_MS * LOUD_TAG_CONFIRM_FACTOR_PERCENT) / 100)
            if confirm_ms < 700:
                confirm_ms = 700
        if hunter_nearby(now):
            if now - proximity_confirm_start_ms >= confirm_ms:
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

    if now - last_sensor_tx_ms >= SENSOR_TX_INTERVAL_MS:
        send_sensor_packet()
        last_sensor_tx_ms = now

    external_state_output(now)
    update_display(now)
    sleep(40)
