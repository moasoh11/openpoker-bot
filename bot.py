#!/usr/bin/env python3
"""moasoh Open Poker bot v1 - tight-aggressive, value-oriented, disciplined.
Protocol: Open Poker WebSocket V2 (docs.openpoker.ai/llms-full.txt).
Strategy notes in strategy.md. No real money; virtual chips only.
"""
import asyncio, json, os, sys, time, uuid, random
import websockets
import strategy2 as S2

API_KEY = os.environ.get("OPEN_POKER_API_KEY") or open('/home/sandbox/vetting/.openpoker-secret').read().split('openpoker_apikey=')[1].split('\n')[0].strip()
WS_URL = "wss://openpoker.ai/ws"
BUY_IN = 2000          # 100bb at 10/20 - disciplined depth
SESSION_SECONDS = int(os.environ.get("SESSION_SECONDS", "0"))  # 0 = play indefinitely (was 720: forced restart every 12 min)
_log_path = os.environ.get("POKER_LOG", "sessions.log")
_d = os.path.dirname(_log_path)
if _d: os.makedirs(_d, exist_ok=True)
LOG = open(_log_path, "a")

RANKS = "23456789TJQKA"; RVAL = {r: i+2 for i, r in enumerate(RANKS)}

def log(*a):
    line = " ".join(str(x) for x in a)
    print(line, flush=True)
    LOG.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n"); LOG.flush()

# ---------- hand evaluation ----------
def eval5(cards):
    vals = sorted((RVAL[c[0]] for c in cards), reverse=True)
    suits = [c[1] for c in cards]
    flush = len(set(suits)) == 1
    counts = {}
    for v in vals: counts[v] = counts.get(v, 0) + 1
    groups = sorted(counts.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
    uniq = sorted(set(vals), reverse=True)
    straight_high = 0
    if len(uniq) == 5:
        if uniq[0] - uniq[4] == 4: straight_high = uniq[0]
        elif uniq == [14, 5, 4, 3, 2]: straight_high = 5
    if flush and straight_high: return (8, straight_high)
    if groups[0][1] == 4: return (7, groups[0][0], groups[1][0])
    if groups[0][1] == 3 and groups[1][1] == 2: return (6, groups[0][0], groups[1][0])
    if flush: return (5,) + tuple(vals)
    if straight_high: return (4, straight_high)
    if groups[0][1] == 3: return (3, groups[0][0]) + tuple(v for v in vals if v != groups[0][0])
    if groups[0][1] == 2 and groups[1][1] == 2:
        p = sorted([groups[0][0], groups[1][0]], reverse=True)
        k = max(v for v in vals if v not in p)
        return (2, p[0], p[1], k)
    if groups[0][1] == 2: return (1, groups[0][0]) + tuple(v for v in vals if v != groups[0][0])
    return (0,) + tuple(vals)

def eval7(cards):
    from itertools import combinations
    return max(eval5(list(c)) for c in combinations(cards, 5))

# ---------- hand reading helpers ----------
def outs_and_draws(hole, board):
    """returns (outs, is_flush_draw, is_oesd_or_gutshot, made_straight_or_flush)"""
    allc = hole + board
    suits = {}
    for c in allc: suits[c[1]] = suits.get(c[1], 0) + 1
    fd = max(suits.values()) == 4
    made_flush = max(suits.values()) >= 5
    vals = sorted(set(RVAL[c[0]] for c in allc))
    if 14 in vals: vals = [1] + vals
    oesd = gut = False
    for start in range(min(vals), max(vals) - 2):
        window = set(range(start, start + 5))
        have = [v for v in window if v in vals]
        missing = [v for v in window if v not in vals]
        if len(have) == 4 and len(missing) == 1:
            if missing[0] in (start, start + 4): oesd = True
            else: gut = True
    outs = (9 if fd else 0) + (8 if oesd else 0) + (4 if (gut and not oesd and not fd) else 0)
    return outs, fd, (oesd or gut), made_flush

def board_pairs(board):
    v = [c[0] for c in board]
    return len(v) != len(set(v))

def street_name(n): return {0: "preflop", 3: "flop", 4: "turn", 5: "river"}.get(n, "?")

# ---------- preflop ----------
def preflop_score(hole):
    a, b = RVAL[hole[0][0]], RVAL[hole[1][0]]
    hi, lo = max(a, b), min(a, b)
    suited = hole[0][1] == hole[1][1]
    pair = a == b
    if pair: return 60 + hi * 2                      # 22=64 .. AA=88
    score = hi * 2 + lo
    if suited: score += 4
    gap = hi - lo - 1
    score -= max(0, gap - 1) * 2
    if hi == 14: score += 6
    if hi >= 12 and lo >= 10: score += 8
    return score

def preflop_decision(hole, actions, facing_raise, position_late):
    s = preflop_score(hole)
    hi = max(RVAL[hole[0][0]], RVAL[hole[1][0]])
    pair = hole[0][0] == hole[1][0]
    if not facing_raise:
        if s >= 34 or (position_late and s >= 26) or pair: return ("raise", None)
        return ("fold", None)
    # facing a raise
    premium = (pair and hi >= 12) or (s >= 40 and hi >= 13)  # QQ+, AKs-ish
    strong = (pair and hi >= 9) or s >= 36
    if premium: return ("reraise", None)
    if strong and "call" in actions: return ("call", None)
    return ("fold", None)

# ---------- postflop ----------
def classify(hole, board):
    """returns (strength, desc): monster/strong/medium/weak + description"""
    score = eval7(hole + board)
    cat = score[0]
    bvals = sorted((RVAL[c[0]] for c in board), reverse=True)
    hvals = [RVAL[c[0]] for c in hole]
    outs, fd, sd, made_flush = outs_and_draws(hole, board)
    if cat >= 5: return ("monster", f"made hand rank {cat}", outs, sd)
    if cat == 4: return ("monster", "straight", outs, sd)
    if cat == 3: return ("monster", "trips/set", outs, sd)
    if cat == 2:
        top_pair = score[1] >= bvals[0]
        return ("monster" if top_pair else "strong", "two pair", outs, sd)
    if cat == 1:
        pairv = score[1]
        hole_pair = hvals[0] == hvals[1]
        if hole_pair and pairv > bvals[0]: return ("strong", "overpair", outs, sd)
        if pairv >= bvals[0]:
            kicker = score[2]
            return ("strong" if kicker >= 12 else "medium", f"top pair kicker {kicker}", outs, sd)
        if pairv >= (bvals[1] if len(bvals) > 1 else 0): return ("medium", "second pair", outs, sd)
        return ("weak", "underpair/low pair", outs, sd)
    # high card
    if outs >= 8: return ("strong", f"combo/big draw ({outs} outs)", outs, sd)
    if outs >= 4: return ("medium", f"draw ({outs} outs)", outs, sd)
    return ("weak", "high card", outs, sd)

def clamp(amount, lo, hi): return max(lo, min(hi, int(amount)))

class Bot:
    def __init__(self):
        self.hole = []; self.board = []; self.hand_id = None
        self.hands = 0; self.won_hands = 0; self.chips_delta = 0
        self.start_stack = None; self.deadline = (time.time() + SESSION_SECONDS) if SESSION_SECONDS > 0 else float("inf")
        self.session_pnl = 0
        self.facing_raise_pf = False
        self.my_seat = None; self.dealer_seat = None; self.n_players = 2
        self.seat_names = {}   # seat -> opponent name (player_joined / table_state)
        self.opp_models = S2.load_opp_models() if hasattr(S2, "load_opp_models") else {}

    async def play(self):
        async with websockets.connect(WS_URL, additional_headers={"Authorization": f"Bearer {API_KEY}"},
                                      ping_interval=20, ping_timeout=30, close_timeout=5) as ws:
            hello = json.loads(await ws.recv())
            log("connected:", hello.get("type"), hello.get("name") or str(hello)[:120])
            if hello.get("type") != "table_state":
                await ws.send(json.dumps({"type": "join_lobby", "buy_in": BUY_IN}))
            else:
                log("rejoined existing seat; skipping join_lobby")
            await ws.send(json.dumps({"type": "set_auto_rebuy", "enabled": True}))
            while True:
                remaining = self.deadline - time.time()
                if remaining <= 0:
                    try:
                        await ws.send(json.dumps({"type": "leave_table"}))
                        await asyncio.sleep(2)
                    except Exception:
                        pass
                    log("session window over; leaving table")
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 20))
                except asyncio.TimeoutError:
                    self.silence = getattr(self, "silence", 0) + 20
                    if self.silence >= 45:  # was 120: table-break stall cost up to 2 min idle
                        log("silence watchdog: rejoining lobby")
                        try:
                            await ws.send(json.dumps({"type": "join_lobby", "buy_in": BUY_IN}))
                        except Exception:
                            pass
                        self.silence = 0
                    continue
                except websockets.exceptions.ConnectionClosed as e:
                    log("connection closed by server - raising for reconnect")
                    raise RuntimeError("connection closed by server") from e
                msg = json.loads(raw)
                self.silence = 0
                t = msg.get("type")
                try:
                    await self.handle(t, msg, ws)
                except Exception as e:
                    log("handler error:", repr(e))

    async def act(self, ws, msg, action, amount=None):
        payload = {"type": "action", "action": action, "hand_id": msg["hand_id"] if "hand_id" in msg else self.hand_id,
                   "client_action_id": str(uuid.uuid4()), "turn_token": msg["turn_token"]}
        if amount is not None: payload["amount"] = amount
        await ws.send(json.dumps(payload))

    KNOWN = {"hand_start","hole_cards","community_cards","player_action","your_turn","rebuy_confirmed",
             "hand_result","busted","table_closed","season_ended","table_state",
             "player_joined","player_left","chat","rebuy_result","joined_table","lobby_update"}

    async def handle(self, t, msg, ws):
        if t not in self.KNOWN or "error" in msg:
            log("MSG", t, str(msg)[:200])
        if t == "error" and msg.get("code") == "cannot_rebuy":
            await ws.send(json.dumps({"type": "join_lobby", "buy_in": BUY_IN}))
        if t == "error" and msg.get("code") == "insufficient_season_chips":
            # felted below min buy-in: request season rebuy (1500 chips, 0 penalty),
            # then rejoin on rebuy_confirmed; on cooldown, wait it out
            now = time.time()
            if now - getattr(self, "_last_rebuy_req", 0) > 60:
                self._last_rebuy_req = now
                log("insufficient_season_chips: requesting season rebuy")
                await ws.send(json.dumps({"type": "rebuy", "amount": 0}))
        if t == "rebuy_confirmed":
            log("rebuy confirmed:", msg.get("chip_balance"))
            await ws.send(json.dumps({"type": "join_lobby", "buy_in": BUY_IN}))
        if t == "table_state":
            st = msg.get("state") or msg
            if self.my_seat is None:
                self.my_seat = st.get("your_seat", st.get("seat"))
                self.dealer_seat = st.get("dealer_seat")
        if t == "hand_start":
            self.hand_id = msg["hand_id"]; self.board = []
            self.my_seat = msg.get("seat"); self.dealer_seat = msg.get("dealer_seat")
            self.facing_raise_pf = False
            self.hands += 1
        elif t == "hole_cards":
            self.hole = msg["cards"]
        elif t == "community_cards":
            self.board = msg.get("cards", self.board)
        elif t == "player_joined":
            if msg.get("seat") is not None and msg.get("name"):
                self.seat_names[msg["seat"]] = msg["name"]
        elif t == "player_left":
            self.seat_names.pop(msg.get("seat"), None)
        elif t == "player_action":
            if msg.get("street") == "preflop" and msg.get("action") in ("raise", "all_in") and msg.get("seat") != self.my_seat:
                self.facing_raise_pf = True
        elif t == "your_turn":
            await self.decide(msg, ws)
        elif t == "hand_result":
            winners = msg.get("winners") or []
            pot = msg.get("total_pot") or msg.get("pot") or 0
            mine = [w for w in winners if w.get("seat") == self.my_seat]
            final = (msg.get("final_stacks") or {}).get(str(self.my_seat))
            if mine: self.won_hands += 1
            if self.start_stack is None and final is not None: self.start_stack = final
            if final is not None and self.start_stack is not None:
                self.session_pnl = final - self.start_stack
            log(f"hand {self.hands}: {'WON' if mine else 'lost/fold'} pot={pot} stack={final} pnl={self.session_pnl}")
        elif t == "busted":
            await ws.send(json.dumps({"type": "join_lobby", "buy_in": BUY_IN}))
        elif t == "table_closed" or t == "season_ended":
            await ws.send(json.dumps({"type": "join_lobby", "buy_in": BUY_IN}))

    def table_archetypes(self):
        types = set()
        for name in self.seat_names.values():
            m = (self.opp_models or {}).get(name)
            if not m: continue
            vp, pfr, wtsd = m.get("vpip", 0), m.get("pfr", 0), m.get("wtsd", 0)
            if vp >= 55 and pfr >= 40: types.add("maniac")
            elif vp >= 50 or wtsd >= 55: types.add("station")
            elif vp < 25: types.add("nit")
        return types

    async def decide(self, msg, ws):
        actions = {a["action"]: a for a in msg["valid_actions"]}
        pot = msg.get("pot") or 0
        call_amt = (actions.get("call") or {}).get("amount", 0)
        can_check = "check" in actions
        can_raise = "raise" in actions
        rmin = (actions.get("raise") or {}).get("min", 0)
        rmax = (actions.get("raise") or {}).get("max", 0)
        street = street_name(len(self.board))

        if street == "preflop":
            late = False
            if self.my_seat is not None and self.dealer_seat is not None:
                late = (self.my_seat == self.dealer_seat)
            p_pf = dict(S2.V2)
            tt_pf = self.table_archetypes()
            if tt_pf and tt_pf <= {"nit"}: p_pf["pf_open_late"] = 20
            move = S2.preflop_decision_v2(self.hole, self.facing_raise_pf, late, p_pf)
            if move == "raise" and can_raise:
                amt = clamp(max(3 * 20, pot // 2 + 3 * 20), rmin, rmax)
                return await self.act(ws, msg, "raise", amt)
            if move == "reraise" and can_raise:
                amt = clamp(max(3 * call_amt + call_amt, rmin), rmin, rmax)
                return await self.act(ws, msg, "raise", amt)
            if move in ("call",) and "call" in actions:
                return await self.act(ws, msg, "call")
            if can_check and move != "fold":
                return await self.act(ws, msg, "check")
            return await self.act(ws, msg, "fold" if "fold" in actions else ("check" if can_check else "call"))

        # postflop
        strength, desc, outs, sd, flags = S2.classify2(self.hole, self.board)
        table_types = self.table_archetypes()
        to_call = call_amt

        # v2: texture-aware, value-only aggression, archetype-adjusted
        P = dict(S2.V2)
        if "station" in table_types:
            P["value_frac_monster"] = 1.0; P["semibluff_freq"] = 0.0
        if "maniac" in table_types:
            P["medium_call_max_pot_frac"] = 0.30
        move, frac = S2.postflop_decision_v2(strength, outs, to_call, pot, can_check, can_raise, len(self.board), P, random, flags)
        if move == "raise" and can_raise:
            amt = clamp(pot * (frac or 0.6), rmin, rmax)
            return await self.act(ws, msg, "raise", amt)
        if move == "call" and to_call > 0 and "call" in actions:
            return await self.act(ws, msg, "call")
        if can_check:
            return await self.act(ws, msg, "check")
        if move == "call" and "call" in actions:
            return await self.act(ws, msg, "call")
        if "fold" in actions and to_call > 0:
            return await self.act(ws, msg, "fold")
        return await self.act(ws, msg, "call")

def start_health_server():
    """Tiny HTTP responder so Render's free web service binds a port and stays alive."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        def do_HEAD(self):
            self.send_response(200); self.end_headers()
        def log_message(self, *a): pass
    port = int(os.environ.get("PORT", "10000"))
    threading.Thread(target=lambda: HTTPServer(("0.0.0.0", port), H).serve_forever(), daemon=True).start()

async def main():
    if os.environ.get("HEALTH_SERVER", "1") == "1":
        try: start_health_server()
        except Exception as e: log("health server failed:", repr(e))
    bot = Bot()
    while time.time() < bot.deadline:
        try:
            await bot.play()
            break
        except Exception as e:
            log("connection dropped:", repr(e), "- reconnecting in 5s")
            await asyncio.sleep(5)
    log(f"SESSION END hands={bot.hands} won={bot.won_hands} pnl={bot.session_pnl}")

if __name__ == "__main__":
    asyncio.run(main())
