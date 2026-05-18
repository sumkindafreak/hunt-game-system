# HUNT Game System for BBC micro:bit

Complete multi-node MicroPython game system for a physical chase/tag/medic game:

- `base_node.py` (1x micro:bit controller)
- `player_node.py` (10x wearable player nodes)

This project is designed to be beginner-friendly, fully commented, and easy to expand.

---

## 1) What HUNT does

HUNT is a live physical game inspired by tag, infection chase, and medic revival mechanics.

- Start state: everyone is a `SURVIVOR`
- Base randomly assigns one first `HUNTER`
- Nearby hunter presence infects survivors (`TAGGED`)
- Tagged players have up to **5 minutes** before bleed-out
- Medic revive is triggered on the tagged node using button **B**
- Revive fails if hunter proximity is still detected
- Bleed-out reaches zero -> `ELIMINATED`
- End condition: all active players are `HUNTER` or `ELIMINATED`

---

## 2) Files

- `player_node.py`  
  Flash to each wearable player micro:bit (10 units).

- `base_node.py`  
  Flash to the base/controller micro:bit (1 unit).

---

## 3) Flashing instructions

### Option A: micro:bit Python Editor (recommended)

1. Open [python.microbit.org](https://python.microbit.org/)
2. For base:
   - Paste `base_node.py`
   - Download and flash to the base micro:bit
3. For each player:
   - Paste `player_node.py`
   - Set unique `PLAYER_ID` at the top (1..10)
   - Download and flash to that specific player micro:bit

### Option B: Mu Editor / Thonny

1. Connect micro:bit over USB
2. Open the correct file (`base_node.py` or `player_node.py`)
3. Flash
4. Repeat for every unit

---

## 4) Changing PLAYER_ID on wearable nodes

In `player_node.py`, at the top:

```python
PLAYER_ID = 1
```

Set each player to a unique value:

- Player 1 micro:bit -> `PLAYER_ID = 1`
- Player 2 micro:bit -> `PLAYER_ID = 2`
- ...
- Player 10 micro:bit -> `PLAYER_ID = 10`

Do **not** duplicate IDs on two wearables.

---

## 5) Required top config values

Both files define these at the top:

- `PLAYER_ID`
- `RADIO_GROUP`
- `TAG_DISTANCE_THRESHOLD`
- `TAG_CONFIRM_TIME_MS`
- `TAGGED_COUNTDOWN_MS`
- `REVIVE_TIME_MS`
- `HEARTBEAT_INTERVALS`
- `DEBUG_MODE`

You can tune the game by adjusting these values.

---

## 6) Controls

## Player node controls

- **Button A**: ready toggle (in `WAITING`)
- **Button B**: medic revive attempt (when this node is `TAGGED`)
- **A+B**: show player ID + current state

## Base node controls

- **Button A**: scroll/select player count (lobby mode)
- **Button B**:
  - start game (if not running)
  - stop game (if running)
- **A+B**: reset game and return all nodes to lobby

---

## 7) LED icon/state legend

## Player states

- `WAITING` -> rotating arrow idle animation
- `SURVIVOR` -> heart icon
- `HUNTER` -> skull/chase icon
- `TAGGED` -> flashing warning/X pattern
- `REVIVING` -> clock/loading animation
- `ELIMINATED` -> solid X icon

## Base display

Lobby:
- alternates configured player count and ready count

Running:
- rotates summary views: `R`, survivor count, `H`, hunter count, eliminated count

---

## 8) How to start a game

1. Power all micro:bits (1 base + up to 10 players)
2. Confirm all use same `RADIO_GROUP`
3. On each player node, press **A** to mark ready
4. On base, use **A** to choose player count if desired
5. Press base **B** to start
6. Base broadcasts:
   - `BASE_START`
   - `BASE_ASSIGN_HUNTER` with random first hunter

---

## 9) Tagging/proximity logic

Hunters periodically send `PING` messages.

Survivors listen for hunter pings and apply proximity logic:

1. If RSSI is available (`radio.receive_full()` builds), signal must be stronger than `TAG_DISTANCE_THRESHOLD`.
2. Proximity must stay confirmed for `TAG_CONFIRM_TIME_MS`.
3. On confirm, survivor becomes `TAGGED`.

Debounce/cooldown logic prevents immediate repeated re-tag loops.

---

## 10) Medic revive logic

When a player is `TAGGED`:

- Press **B** on that tagged node to begin revive (`REVIVING`)
- Revive duration = `REVIVE_TIME_MS`
- If hunter proximity is still detected during revive, it fails:
  - state returns to `TAGGED`
  - emits `PLAYER_REVIVE_FAIL`
- If no hunter stays nearby for full revive duration:
  - state returns to `SURVIVOR`
  - emits `PLAYER_REVIVE_SUCCESS`

---

## 11) Countdown and bleed-out

Tagged nodes run a countdown of `TAGGED_COUNTDOWN_MS` (default 5 minutes).

- Warning display remains active while tagged/reviving
- Heartbeat buzzer plays and gets faster as time decreases
- At zero: node transitions to `ELIMINATED`

Eliminated players stay out until base reset or new game start sequence.

---

## 12) Radio packet protocol

Implemented packet types:

- `BASE_START`
- `BASE_RESET`
- `BASE_ASSIGN_HUNTER`
- `PLAYER_READY`
- `PLAYER_STATUS`
- `PLAYER_TAGGED`
- `PLAYER_REVIVE_REQUEST`
- `PLAYER_REVIVE_SUCCESS`
- `PLAYER_REVIVE_FAIL`
- `PLAYER_ELIMINATED`
- `HEARTBEAT`
- `PING`
- `ACK`

Packet format:

```text
TYPE|src=1;dst=255;state=SURVIVOR;ms=12345;key=value
```

This simple protocol is easy to bridge to:
- ESP32 serial link
- Wi-Fi gateway
- logging/analytics dashboard

---

## 13) Known micro:bit limitations

1. **RSSI support differs by firmware/runtime build**
   - Some builds support `radio.receive_full()` with RSSI.
   - Others only support `radio.receive()`.
   - Fallback implemented: repeated hunter pings in a short window simulate proximity.

2. **Radio collisions can happen with many active nodes**
   - Mitigated by short packets and periodic sending.
   - If needed, increase intervals or add slot-based timing later.

3. **No absolute distance measurement**
   - RSSI is environment-dependent (body blocking, walls, orientation).
   - Threshold tuning (`TAG_DISTANCE_THRESHOLD`) is required per venue.

4. **Audio differences**
   - V2 has built-in speaker.
   - Fallback pin0 buzzer support is included for external buzzers.

---

## 14) Recommended tuning steps before a live game

1. Stand at intended tag distance.
2. Enable `DEBUG_MODE = True` and watch serial logs.
3. Adjust:
   - `TAG_DISTANCE_THRESHOLD`
   - `TAG_CONFIRM_TIME_MS`
   - `HUNTER_PING_INTERVAL_MS` (inside code)
4. Validate revive behavior with hunter nearby vs. far away.

---

## 15) Future upgrade ideas

This code is structured for expansion to:

- ESP32 base-station bridge (serial or Wi-Fi)
- NeoPixel status strips per player
- Web dashboard (live map/state timeline)
- Extra sub-base nodes and sector relays
- Multiple game modes (classic infection, timed rescue, teams)
- Persistent game logs and replay

---

## 16) Quick checklist

- [ ] All players flashed with `player_node.py`
- [ ] Each player has unique `PLAYER_ID`
- [ ] Base flashed with `base_node.py`
- [ ] Same `RADIO_GROUP` on all nodes
- [ ] Players press **A** to ready
- [ ] Base press **B** to start

Enjoy running HUNT in the field.
