"""
Nado.xyz Trading Bot — Support/Resistance + EMA + Trend
=========================================================
Strategie:
- 1H Kerzen: Trend Filter (EMA21/EMA50)
- 5-Min Kerzen: Support/Resistance Levels berechnen
- Einstieg nur wenn Preis an Support/Resistance + EMA bestätigt
- Limit Orders 0.1% Slippage
- TP 1% / SL 0.5% / Trail 0.3%

Einrichten:
    SIGNER_KEY = 1-Click Trading Key (app.nado.xyz → Settings)
"""

import time, random, requests, sys, json, os, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from datetime import datetime

try:
    from colorama import init, Fore, Style
    init(autoreset=True)
    G=Fore.GREEN; R=Fore.RED; Y=Fore.YELLOW; C=Fore.CYAN; M=Fore.MAGENTA
    X=Style.RESET_ALL; B=Style.BRIGHT
except:
    G=R=Y=C=M=X=B=""

# ═══════════════════════════════════════════════════════════
#  EINSTELLUNGEN
# ═══════════════════════════════════════════════════════════
WALLET_ADDR = "0xc15263578ce7fd6290f56Ab78a23D3b6C653B28C"
SIGNER_KEY  = "0x8097b0ec439aa91bd4f3c3ea79735be6688ce00589bbcd0e3dea2ab596580a4d"

PRODUCT_ID  = 2
CHAIN_ID    = 57073
GATEWAY     = "https://gateway.prod.nado.xyz/v1"
ARCHIVE     = "https://archive.prod.nado.xyz/v1"
HEADERS     = {"Accept-Encoding": "gzip", "Content-Type": "application/json"}

ORDER_SIZE  = 0.0015
TAKE_PROFIT = 1.0
STOP_LOSS   = 0.5
TRAIL_PCT   = 0.3
COOLDOWN    = 2

SR_TOLERANCE = 0.002   # 0.2% Toleranz um Support/Resistance Level
SR_TOUCHES   = 2       # Mindestanzahl Berührungen für gültiges Level
INTERVAL     = 30
DRY_RUN      = False
STATE_FILE   = "state.json"
# ═══════════════════════════════════════════════════════════

pos    = None
cool   = 0
trades = wins = loss = 0


def ts():    return datetime.now().strftime("%H:%M:%S")
def log(m, c=""): print(f"{c}[{ts()}] {m}{X}" if c else f"[{ts()}] {m}"); sys.stdout.flush()
def fmt(x):
    try: return f"${float(x):,.2f}"
    except: return "?"


# ─── STATE ────────────────────────────────────────────────

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"pos": pos, "trades": trades, "wins": wins,
                      "loss": loss, "cool": cool}, f)
    except: pass

def load_state():
    global pos, trades, wins, loss, cool
    try:
        if os.path.exists(STATE_FILE):
            d = json.load(open(STATE_FILE))
            pos    = d.get("pos")
            trades = d.get("trades", 0)
            wins   = d.get("wins", 0)
            loss   = d.get("loss", 0)
            cool   = d.get("cool", 0)
            if pos: log(f"State: {pos['dir']} @ {fmt(pos['entry'])} | {trades}T {wins}W {loss}L", Y)
            else:   log(f"State: kein Trade | {trades}T {wins}W {loss}L", C)
    except Exception as e:
        log(f"State Fehler: {e}", Y)


# ─── API ──────────────────────────────────────────────────

def get_kerzen(granularity, limit):
    try:
        r = requests.post(
            ARCHIVE,
            json={"candlesticks": {"product_id": PRODUCT_ID, "granularity": granularity, "limit": limit}},
            headers=HEADERS, timeout=15, verify=False
        )
        if r.status_code != 200: return None
        cs = r.json().get("candlesticks", [])
        if not cs: return None
        candles = [{"o": float(c.get("open_x18",0))/1e18,
                    "h": float(c.get("high_x18",0))/1e18,
                    "l": float(c.get("low_x18",0))/1e18,
                    "c": float(c.get("close_x18",0))/1e18,
                    "v": float(c.get("volume",0))/1e18}
                   for c in cs]
        return list(reversed(candles))
    except Exception as e:
        log(f"Kerzen Fehler: {e}", Y); return None

def get_preis():
    try:
        r = requests.get(f"{GATEWAY}/query?type=all_products",
                        headers={"Accept-Encoding": "gzip"}, timeout=15, verify=False)
        if r.status_code != 200: return None
        body = r.json()
        data = body.get("data", body)
        for p in data.get("perp_products", []):
            if int(p.get("product_id", -1)) == PRODUCT_ID:
                px = float(p.get("oracle_price_x18") or p.get("mark_price_x18") or 0)
                if px > 0: return px / 1e18
    except Exception as e:
        log(f"Preis Fehler: {e}", Y)
    return None


# ─── INDIKATOREN ──────────────────────────────────────────

def calc_ema(c, n):
    if len(c) < n: return None
    k=2/(n+1); e=sum(c[:n])/n
    for x in c[n:]: e=x*k+e*(1-k)
    return e

def trend_1h(cs_1h):
    """1H Trend Filter."""
    if not cs_1h or len(cs_1h) < 50: return None
    cl  = [c["c"] for c in cs_1h]
    e21 = calc_ema(cl, 21)
    e50 = calc_ema(cl, 50)
    cur = cl[-1]
    if not e21 or not e50: return None
    last2_bear = cs_1h[-1]["c"] < cs_1h[-1]["o"] and cs_1h[-2]["c"] < cs_1h[-2]["o"]
    last2_bull = cs_1h[-1]["c"] > cs_1h[-1]["o"] and cs_1h[-2]["c"] > cs_1h[-2]["o"]
    if cur > e21 and e21 > e50: return "LONG"
    if cur < e21 and e21 < e50: return "SHORT"
    if last2_bear and cur < e21: return "SHORT"
    if last2_bull and cur > e21: return "LONG"
    return None

def ema_richtung(cs):
    """EMA9/21 Richtung."""
    if len(cs) < 25: return None
    cl  = [c["c"] for c in cs]
    e9  = calc_ema(cl, 9)
    e21 = calc_ema(cl, 21)
    if not e9 or not e21: return None
    if e9 > e21: return "LONG"
    if e9 < e21: return "SHORT"
    return None

def find_sr_levels(cs):
    """
    Findet Support und Resistance Levels aus lokalen Hochs/Tiefs.
    Ein Level ist gültig wenn der Preis mindestens SR_TOUCHES mal
    innerhalb der SR_TOLERANCE reagiert hat.
    """
    if len(cs) < 10: return [], []

    supports    = []
    resistances = []

    # Lokale Tiefs (Support) und Hochs (Resistance) finden
    for i in range(2, len(cs)-2):
        # Lokales Tief — Support
        if cs[i]["l"] < cs[i-1]["l"] and cs[i]["l"] < cs[i-2]["l"] and \
           cs[i]["l"] < cs[i+1]["l"] and cs[i]["l"] < cs[i+2]["l"]:
            supports.append(cs[i]["l"])

        # Lokales Hoch — Resistance
        if cs[i]["h"] > cs[i-1]["h"] and cs[i]["h"] > cs[i-2]["h"] and \
           cs[i]["h"] > cs[i+1]["h"] and cs[i]["h"] > cs[i+2]["h"]:
            resistances.append(cs[i]["h"])

    # Levels gruppieren die nah beieinander sind
    def group_levels(levels):
        if not levels: return []
        levels = sorted(levels)
        grouped = []
        current_group = [levels[0]]
        for level in levels[1:]:
            if abs(level - current_group[-1]) / current_group[-1] < SR_TOLERANCE:
                current_group.append(level)
            else:
                if len(current_group) >= SR_TOUCHES:
                    grouped.append(sum(current_group) / len(current_group))
                current_group = [level]
        if len(current_group) >= SR_TOUCHES:
            grouped.append(sum(current_group) / len(current_group))
        return grouped

    return group_levels(supports), group_levels(resistances)

def preis_an_level(preis, levels, toleranz):
    """Prüft ob aktueller Preis an einem Level ist."""
    for level in levels:
        if abs(preis - level) / level <= toleranz:
            return level
    return None


# ─── ORDER ────────────────────────────────────────────────

def sender_hex():
    ab = bytes.fromhex(WALLET_ADDR.lower().replace("0x",""))
    return "0x" + (ab + b"default".ljust(12, b"\x00")).hex()

def place_order(is_buy, price, reduce_only=False):
    if DRY_RUN:
        log(f"[DRY] {'BUY' if is_buy else 'SELL'} {ORDER_SIZE} BTC @ {fmt(price)}", Y)
        return True
    try:
        from eth_account import Account
        px    = round(price * (1.001 if is_buy else 0.999)) * int(1e18)
        amt   = int(ORDER_SIZE*1e18) if is_buy else -int(ORDER_SIZE*1e18)
        exp   = int(time.time()) + 60
        nonce = ((int(time.time()*1000)+5000) << 20) + random.randint(0,999)
        apx   = 1 | (1<<11 if reduce_only else 0)
        sndr  = sender_hex()
        dom = {"name":"Nado","version":"0.0.1","chainId":CHAIN_ID,"verifyingContract":f"0x{PRODUCT_ID:040x}"}
        typ = {"Order":[{"name":"sender","type":"bytes32"},{"name":"priceX18","type":"int128"},
                        {"name":"amount","type":"int128"},{"name":"expiration","type":"uint64"},
                        {"name":"nonce","type":"uint64"},{"name":"appendix","type":"uint128"}]}
        msg = {"sender":sndr,"priceX18":px,"amount":amt,"expiration":exp,"nonce":nonce,"appendix":apx}
        acc = Account.from_key(SIGNER_KEY)
        sig = acc.sign_typed_data(domain_data=dom, message_types=typ, message_data=msg).signature.hex()
        if not sig.startswith("0x"): sig = "0x"+sig
        pld = {"place_order":{"product_id":PRODUCT_ID,"order":{
            "sender":sndr,"priceX18":str(px),"amount":str(amt),
            "expiration":str(exp),"nonce":str(nonce),"appendix":str(apx)
        },"signature":sig}}
        r = requests.post(f"{GATEWAY}/execute", json=pld, headers=HEADERS, timeout=15, verify=False)
        d = r.json()
        if d.get("status") == "success":
            log("✅ Order OK!", G); return True
        code = d.get("error_code","")
        if code == 2064:
            log("Position nicht auf Nado — State zurücksetzen", Y); return "RESET"
        log(f"❌ {d.get('error','')} (Code:{code})", R); return False
    except Exception as e:
        log(f"Order Exception: {e}", R); return False


# ─── POSITION ─────────────────────────────────────────────

def open_pos(richtung, preis):
    global pos, trades, cool
    is_buy = richtung == "LONG"
    ok = place_order(is_buy, preis)
    if not ok and not DRY_RUN: return
    tp = preis*(1+TAKE_PROFIT/100) if is_buy else preis*(1-TAKE_PROFIT/100)
    sl = preis*(1-STOP_LOSS/100)   if is_buy else preis*(1+STOP_LOSS/100)
    pos = {"dir":richtung,"entry":preis,"tp":tp,"sl":sl,
           "best":preis if is_buy else 0,
           "worst":preis if not is_buy else float('inf'),
           "id":trades}
    trades += 1; cool = 0
    save_state()
    print(f"\n{B}{'═'*55}")
    print(f"  {'🟢' if is_buy else '🔴'} {G if is_buy else R}POSITION #{pos['id']} — {richtung}{X}")
    print(f"  Entry:{fmt(preis)}  TP:{fmt(tp)}  SL:{fmt(sl)}  Trail:{TRAIL_PCT}%")
    if DRY_RUN: print(f"  {Y}[DRY RUN]{X}")
    print(f"{'═'*55}{X}\n")

def close_pos(grund, preis):
    global pos, wins, loss, cool
    if not pos: return
    is_buy = pos["dir"] != "LONG"
    ok = place_order(is_buy, preis, reduce_only=False)
    if ok == "RESET" or (not ok and not DRY_RUN):
        log("State zurückgesetzt!", Y)
        pos = None; cool = COOLDOWN; save_state(); return
    pnl = (preis-pos["entry"])/pos["entry"]*100 if pos["dir"]=="LONG" else (pos["entry"]-preis)/pos["entry"]*100
    if pnl > 0: wins += 1
    else: loss += 1
    wr  = wins/(wins+loss)*100 if (wins+loss)>0 else 0
    fc  = G if pnl>0 else R; emoji = "✅" if pnl>0 else "❌"
    print(f"\n{B}{'═'*55}")
    print(f"  {emoji} POSITION #{pos['id']} GESCHLOSSEN — {grund}")
    print(f"  Entry:{fmt(pos['entry'])}  Exit:{fmt(preis)}  P&L:{fc}{pnl:+.2f}%{X}")
    print(f"  {trades} Trades | {wins}W {loss}L | {wr:.0f}% Win Rate")
    print(f"{'═'*55}{X}\n")
    pos = None; cool = COOLDOWN
    save_state()


# ─── HAUPT LOOP ───────────────────────────────────────────

def loop():
    global cool
    tick = 0
    log(f"Bot | S/R + EMA + Trend | TP:{TAKE_PROFIT}% SL:{STOP_LOSS}% Trail:{TRAIL_PCT}% | {'DRY RUN' if DRY_RUN else 'LIVE'}", C)

    while True:
        try:
            tick += 1

            # Daten holen
            preis = get_preis()
            cs_5m = get_kerzen(300,  100)
            cs_1h = get_kerzen(3600, 100)

            if not preis or not cs_5m or not cs_1h:
                log("Daten fehlen — warte...", Y)
                time.sleep(INTERVAL); continue

            # Indikatoren berechnen
            trend   = trend_1h(cs_1h)
            ema_dir = ema_richtung(cs_5m)
            supports, resistances = find_sr_levels(cs_5m)

            # Preis an Support oder Resistance?
            at_support    = preis_an_level(preis, supports,    SR_TOLERANCE)
            at_resistance = preis_an_level(preis, resistances, SR_TOLERANCE)

            if pos:
                is_long = pos["dir"] == "LONG"
                pnl = (preis-pos["entry"])/pos["entry"]*100 if is_long else (pos["entry"]-preis)/pos["entry"]*100
                fc  = G if pnl>0 else R

                if is_long:
                    pos["best"]  = max(pos["best"], preis)
                    trail        = pos["best"] * (1 - TRAIL_PCT/100)
                else:
                    pos["worst"] = min(pos["worst"], preis)
                    trail        = pos["worst"] * (1 + TRAIL_PCT/100)

                trend_txt = f"{G}↑{X}" if trend=="LONG" else (f"{R}↓{X}" if trend=="SHORT" else f"{Y}→{X}")
                log(f"#{pos['id']} {pos['dir']} | {fmt(pos['entry'])}→{fmt(preis)} | P&L:{fc}{pnl:+.2f}%{X} | Trail:{fmt(trail)} | 1H:{trend_txt}")
                save_state()

                if (is_long and preis >= pos["tp"]) or (not is_long and preis <= pos["tp"]):
                    close_pos("TAKE PROFIT ✅", preis)
                elif (is_long and preis <= pos["sl"]) or (not is_long and preis >= pos["sl"]):
                    close_pos("STOP LOSS ❌", preis)
                elif (is_long and preis <= trail) or (not is_long and preis >= trail):
                    close_pos("TRAILING STOP 📉", preis)
                # EMA dreht gegen Position → schließen
                elif ema_dir == "SHORT" and is_long and trend == "SHORT":
                    close_pos("EMA + TREND UMKEHRUNG 🔄", preis)
                elif ema_dir == "LONG" and not is_long and trend == "LONG":
                    close_pos("EMA + TREND UMKEHRUNG 🔄", preis)

            else:
                if cool > 0:
                    cool -= 1
                    log(f"Cooldown: {cool} | BTC {fmt(preis)}", Y)
                    time.sleep(INTERVAL); continue

                if not trend:
                    if tick % 3 == 0:
                        log(f"BTC {fmt(preis)} | 1H: Seitwärts — kein Trade", Y)
                    time.sleep(INTERVAL); continue

                # S/R Level Info
                sr_txt = ""
                if at_support:    sr_txt = f" {G}@ Support {fmt(at_support)}{X}"
                if at_resistance: sr_txt = f" {R}@ Resistance {fmt(at_resistance)}{X}"

                # Entry Bedingungen:
                # LONG: 1H LONG + EMA LONG + Preis an Support
                # SHORT: 1H SHORT + EMA SHORT + Preis an Resistance
                if trend == "LONG" and ema_dir == "LONG" and at_support:
                    log(f"🎯 LONG @ Support {fmt(at_support)} (1H:{trend} EMA:{ema_dir})", M)
                    open_pos("LONG", preis)
                elif trend == "SHORT" and ema_dir == "SHORT" and at_resistance:
                    log(f"🎯 SHORT @ Resistance {fmt(at_resistance)} (1H:{trend} EMA:{ema_dir})", M)
                    open_pos("SHORT", preis)
                else:
                    if tick % 2 == 0:
                        trend_txt = f"{G}LONG{X}" if trend=="LONG" else f"{R}SHORT{X}"
                        sr_info   = f" S:{len(supports)} R:{len(resistances)} Levels"
                        log(f"BTC {fmt(preis)} | 1H:{trend_txt} | EMA:{ema_dir}{sr_txt}{sr_info} → warten")

            time.sleep(INTERVAL)

        except KeyboardInterrupt:
            log("Bot gestoppt.", Y)
            if pos: log(f"⚠️ OFFENE POSITION: {pos['dir']} @ {fmt(pos['entry'])} — MANUELL SCHLIESSEN!", R)
            break
        except Exception as e:
            log(f"Fehler: {e}", R); time.sleep(5)


def main():
    print(f"\n{B}{C}  ╔══════════════════════════════════════════╗")
    print(f"  ║   Nado.xyz — Support/Resistance Bot      ║")
    print(f"  ║  1H Trend + EMA + S/R Levels + Trail     ║")
    print(f"  ╚══════════════════════════════════════════╝{X}\n")
    print(f"  Wallet: {WALLET_ADDR[:12]}...{WALLET_ADDR[-6:]}")
    print(f"  TP:{TAKE_PROFIT}%  SL:{STOP_LOSS}%  Trail:{TRAIL_PCT}%")
    print(f"  S/R Toleranz: {SR_TOLERANCE*100}%  Mindest-Berührungen: {SR_TOUCHES}")
    modus = f"{Y}DRY RUN{X}" if DRY_RUN else f"{R}{B}LIVE{X}"
    print(f"  Modus: {modus}\n")
    load_state()
    loop()


if __name__ == "__main__":
    main()
