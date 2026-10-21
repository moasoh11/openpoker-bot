"""Self-contained v2 strategy module (no circular imports). Inlines eval helpers from bot.py."""
import random
from collections import Counter

RANKS = "23456789TJQKA"; ALL_CARDS = [r + s for r in "23456789TJQKA" for s in "shdc"]
RVAL = {r: i+2 for i, r in enumerate(RANKS)}
RVAL = {r: i+2 for i, r in enumerate(RANKS)}

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
def preflop_score(hole):
    (r1, s1), (r2, s2) = hole
    a, b = RVAL[r1], RVAL[r2]
    hi, lo = max(a, b), min(a, b)
    pair = a == b
    suited = s1 == s2
    score = hi * 2 + lo
    if pair: score += 18
    if suited: score += 4
    gap = hi - lo - 1
    if not pair:
        if gap == 0: score += 3
        elif gap == 1: score += 1
        elif gap >= 4: score -= 3
    if hi == 14: score += 2
    return score

"""Strategy v2.0 - texture-aware exploitation. Fixes v1's core bug: classify()
counted BOARD-ONLY hands (board trips/two-pair/straight) as hero monsters, and
had no board-danger model. v2 computes true hole-card contribution + danger flags.
Aggression becomes value-only vs a pool that folds ~12% postflop."""

STRATEGY_VERSION = "v2.0"

V2 = dict(B.__dict__.get("x", None)) if False else {
    # preflop: raise or fold. no calling raises except premium.
    "pf_open_tight": 34,
    "pf_open_late": 24,        # steal more vs nits/stations
    "pf_reraise_premium": 34,
    "open_size_bb": 3,
    # v2.3: v1's aggression values applied to CORRECT classes (sim can't re-price aggression;
    # live data only proved: classifier bug + call-preflop leak + no-hero-calls-vs-big-bets)
    "value_frac_monster": 0.75,
    "value_frac_strong": 0.6,
    "monster_bet_freq": 0.85,
    "strong_bet_freq": 0.75,
    # calling discipline
    "strong_call_max_pot_frac": 0.8,
    "medium_call_max_pot_frac": 0.35,
    "big_draw_outs": 12,
    # semibluffs kept at v1 values BUT only fire on real draws now (correct outs)
    "semibluff_min_outs": 8,
    "semibluff_freq": 0.4,
    "semibluff_frac": 0.55,
}

def board_danger(board):
    """returns (n_flags, flags:set) - paired/trips-on-board/flush/straight danger."""
    flags = set()
    ranks = [RVAL[c[0]] for c in board]
    suits = [c[1] for c in board]
    rc, sc = Counter(ranks), Counter(suits)
    if rc.most_common(1)[0][1] >= 3: flags.add("board_trips")
    elif rc.most_common(1)[0][1] == 2:
        flags.add("paired")
        if sum(1 for v in rc.values() if v >= 2) >= 2: flags.add("double_paired")
    if sc.most_common(1)[0][1] >= 4: flags.add("flush4")   # one-card flush possible
    elif sc.most_common(1)[0][1] == 3: flags.add("flush3")
    u = sorted(set(ranks))
    for w in range(len(u)):
        window = [r for r in u if u[w] <= r <= u[w] + 4]
        if len(window) >= 4: flags.add("straight4"); break
    else:
        if len(u) >= 3 and (max(u) - min(u) <= 6 or (14 in u and any(r <= 5 for r in u))):
            flags.add("straight3")
    if 14 in u and {2,3,4,5} & set(ranks): flags.add("wheel_window")
    return len(flags), flags

def playing_board(hole, board):
    """True if hero's best hand uses no hole cards (river-exact, flop/turn approx)."""
    score = eval7(hole + board)
    if len(board) == 5:
        b5 = eval5(board) if "eval5" in globals() else None
        if b5 is None:
            # board + two dummies that cannot pair board ranks or add board suits
            used = set(hole + board)
            dummy = []
            for r in "234567":
                for s in "cdhs":
                    if (r, s) not in used and all(RVAL[r] != RVAL[c[0]] for c in board):
                        dummy.append((r, s))
                    if len(dummy) == 2: break
                if len(dummy) == 2: break
            b5 = eval7(board + dummy)
        return score <= b5
    return False


def _straight_completing_values(hole_vals, known_vals):
    """values v such that adding v completes a 5-straight using >=1 hole card"""
    out = set()
    for ws in range(1, 11):
        window = set(range(ws, ws + 5))
        missing = window - known_vals
        if len(missing) == 1:
            v = next(iter(missing))
            if hole_vals & (window - {v}):  # hole card participates
                out.add(v)
    return out

def outs_exact(hole, board):
    """Exact dead-card outs: enumerate UNSEEN cards (52 - hole - board) that complete
    a real flush or straight draw requiring hero hole-card participation.
    Returns (outs, fd, sd, made_flush). Replaces the 9/8/4 formula."""
    known = hole + board
    known_set = set(known)
    unseen = [c for c in ALL_CARDS if c not in known_set]
    hole_suits = [c[1] for c in hole]
    suit_known = {}
    for c in known: suit_known[c[1]] = suit_known.get(c[1], 0) + 1
    fd = False; flush_cards = set()
    for s in set(hole_suits):
        if suit_known.get(s, 0) == 4:  # hero holds >=1 of suit, 4 known -> real draw
            fd = True
            flush_cards |= {c for c in unseen if c[1] == s}   # exactly 13-4=9
    made_flush = any(n >= 5 for n in suit_known.values()) and any(suit_known.get(c[1],0) >= 5 for c in hole)
    hv = set(RVAL[c[0]] for c in hole)
    kv = set(RVAL[c[0]] for c in known)
    kv_w = kv | ({1} if 14 in kv else set())
    hv_w = hv | ({1} if 14 in hv else set())
    comp = _straight_completing_values(hv_w, kv_w)
    straight_cards = {c for c in unseen if RVAL[c[0]] in comp or (1 in comp and RVAL[c[0]] == 14)}
    sd = bool(comp)
    outs = len(flush_cards | straight_cards)
    return outs, fd, sd, made_flush


def outs_and_draws(hole, board):
    return outs_exact(hole, board)


def classify2(hole, board):
    """(strength, desc, outs, sd, flags) - strength in nuts/monster/strong/medium/weak."""
    score = eval7(hole + board)
    cat = score[0]
    nflags, flags = board_danger(board)
    outs, fd, sd, made_flush = outs_and_draws(hole, board)
    hv = sorted((RVAL[c[0]] for c in hole), reverse=True)
    bvals = sorted((RVAL[c[0]] for c in board), reverse=True)
    pocket_pair = hv[0] == hv[1]
    hole_suits = [c[1] for c in hole]
    danger = nflags >= 2 or "board_trips" in flags or "flush4" in flags

    if cat >= 7: return ("nuts", "quads/SF", outs, sd, flags)
    if cat == 6:  # full house
        return ("monster", "full house", outs, sd, flags)
    if cat == 5:  # flush
        fsuit = Counter([c[1] for c in board + hole]).most_common(1)[0][0]
        n_hole_flush = sum(1 for c in hole if c[1] == fsuit)
        if n_hole_flush == 2:
            top = max(RVAL[c[0]] for c in hole if c[1] == fsuit)
            return ("monster" if top >= 13 else "strong", f"flush (2 hole, top {top})", outs, sd, flags)
        if n_hole_flush == 1:
            k = max(RVAL[c[0]] for c in hole if c[1] == fsuit)
            return ("strong" if k >= 13 else "medium", f"flush (1 hole, {k})", outs, sd, flags | {"flush4"})
        return ("weak", "board flush", outs, sd, flags)
    if cat == 4:  # straight
        if playing_board(hole, board): return ("weak", "board straight", outs, sd, flags)
        uses2 = all(any(RVAL[c[0]] == RVAL[h[0]] for c in board + hole) for h in hole)
        return ("monster" if not danger else "strong", "straight", outs, sd, flags)
    if cat == 3:  # trips
        if pocket_pair: return ("monster", "set", outs, sd, flags)
        # one hole card matches board pair - kicker + FH danger matter
        if "paired" in flags or "double_paired" in flags or "board_trips" in flags:
            kicker = max(v for v in hv if v != score[1])
            if kicker >= 13 and not danger: return ("strong", f"trips good kicker {kicker}", outs, sd, flags)
            return ("medium", f"trips vulnerable k{kicker}", outs, sd, flags)
        return ("strong", "trips", outs, sd, flags)
    if cat == 2:  # two pair
        if playing_board(hole, board): return ("weak", "board two pair", outs, sd, flags)
        # both hole cards paired the board = real two pair
        h_on_board = sum(1 for v in hv if v in bvals)
        if h_on_board == 2 and not pocket_pair:
            return ("strong" if not danger else "medium", f"two pair real", outs, sd, flags)
        # pocket pair + board pair
        if pocket_pair:
            overpair2 = hv[0] > bvals[0]
            return ("strong" if (overpair2 and not danger) else "medium", f"two pair (pocket {hv[0]})", outs, sd, flags)
        return ("medium", "two pair (board pair + 1)", outs, sd, flags)
    if cat == 1:
        pairv = score[1]
        if pocket_pair and pairv > bvals[0]:
            return ("medium" if danger else "strong", f"overpair {pairv}", outs, sd, flags)
        if pairv >= bvals[0]:
            kicker = score[2]
            if danger: return ("medium", f"top pair wet k{kicker}", outs, sd, flags)
            # v2.1: dry-board top pair decent kicker IS a value hand vs stations (they call with worse)
            if kicker >= 10: return ("strong", f"top pair k{kicker} dry", outs, sd, flags)
            return ("medium", f"top pair k{kicker}", outs, sd, flags)
        if pairv >= (bvals[1] if len(bvals) > 1 else 0): return ("medium", "second pair", outs, sd, flags)
        return ("weak", "low pair", outs, sd, flags)
    # high card / draws
    if outs >= 12: return ("strong", f"combo draw {outs}", outs, sd, flags)
    if outs >= 8: return ("medium", f"draw {outs}", outs, sd, flags)
    return ("weak", "high card", outs, sd, flags)

def preflop_decision_v2(hole, facing_raise, position_late, p=None):
    p = p or V2
    s = B_preflop_score(hole)
    pair = hole[0][0] == hole[1][0]
    hi = max(RVAL[hole[0][0]], RVAL[hole[1][0]])
    if not facing_raise:
        if s >= p["pf_open_tight"] or (position_late and s >= p["pf_open_late"]) or pair:
            return "raise"
        return "fold"
    # facing a raise: 3-bet premium, everything else FOLDS (calling lost -228/hand live)
    premium = s >= p["pf_reraise_premium"] or (pair and hi >= 12)
    if premium: return "reraise"
    return "fold"

def B_preflop_score(hole):
    return preflop_score(hole)

def postflop_decision_v2(strength, outs, to_call, pot, can_check, can_raise, board_len, p=None, rng=None, flags=frozenset()):
    p = p or V2
    rng = rng or random
    pot_after = pot + to_call
    pot_odds = to_call / pot_after if pot_after > 0 else 1
    equity_est = min(0.85, outs * (0.04 if board_len == 3 else 0.02)) if outs else 0
    if strength == "nuts":
        if can_raise and rng.random() < 0.95: return ("raise", 1.0)
        return ("check" if can_check else "call", None)
    if strength == "monster":
        if can_raise and rng.random() < p["monster_bet_freq"]:
            return ("raise", p["value_frac_monster"])
        return ("check" if can_check else "call", None)
    if strength == "strong":
        if to_call == 0 and can_raise and rng.random() < p["strong_bet_freq"]:
            return ("raise", p["value_frac_strong"])
        if to_call > 0:
            if to_call <= pot * p["strong_call_max_pot_frac"] or outs >= p["big_draw_outs"]:
                return ("call", None)
            return ("fold", None)
        return ("check" if can_check else "call", None)
    if strength == "medium":
        # pot control: never raise, call only small/odds
        if to_call == 0:
            if can_raise and outs >= p["semibluff_min_outs"] and rng.random() < p["semibluff_freq"]:
                return ("raise", p["semibluff_frac"])
            return ("check" if can_check else "call", None)
        if board_len >= 4 and to_call > pot * 0.55 and equity_est < pot_odds:
            return ("fold", None)  # turn/river: fold only to BIG bets; call the rest (maniacs bluff)
        if to_call <= pot * p["medium_call_max_pot_frac"] or equity_est >= pot_odds:
            return ("call", None)
        return ("fold", None)
    if to_call == 0 and can_check: return ("check", None)
    if to_call > 0: return ("fold", None)
    return ("check" if can_check else "call", None)


def load_opp_models(path=None):
    import json as _json, os as _os
    p = path or _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "opponents.json")
    try:
        return _json.load(open(p))
    except Exception:
        return {}
