"""
Nado.xyz Trading Bot — EMA Crossover
======================================
Strategie:
- 1H Kerzen: Trend-Filter (EMA21/EMA50)
- 5-Min Kerzen: EMA9 kreuzt EMA21 → Entry
- Während Trade: dynamisch prüfen ob Trend dreht
- Limit Orders 0.1% Slippage
- TP 1% / SL 0.5% / Trail 0.5%

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
TAKE_PROFIT = 0.5
STOP_LOSS   = 0.3
TRAIL_PCT   = 0.2
COOLDOWN    = 2

MIN_CANDLES_5M = 30
MIN_CANDLES_1H = 50
INTERVAL       = 30
DRY_RUN        = True
STATE_FILE     = "state.json"
# ═══════════════════════════════════════════════════════════

pos    = None
cool   = 0
trades = wins = loss = 0
prev_cross = None  # letzter EMA Crossover Status


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
                      "loss": loss, "cool": cool, "prev_cross": prev_cross}, f)
    except: pass

def load_state():
    global pos, trades, wins, loss, cool, prev_cross
    try:
        if os.path.exists(STATE_FILE):
            d = json.load(open(STATE_FILE))
            pos        = d.get("pos")
            trades     = d.get("trades", 0)
            wins       = d.get("wins", 0)
            loss       = d.get("loss", 0)
            cool       = d.get("cool", 0)
            prev_cross = d.get("prev_cross")
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
        candles = [{"o": float(c.get("open_x18",0))/1e18, "h": float(c.get("high_x18",0))/1e18,
                    "l": float(c.get("low_x18",0))/1e18,  "c": float(c.get("close_x18",0))/1e18,
                    "v": float(c.get("volume",0))/1e18}
                   for c in cs]
        return list(reversed(candles))  # älteste zuerst, neueste zuletzt
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

def volume_profile(cs):
    """
    Berechnet POC (Point of Control) — Preisniveau mit meistem Volumen.
    Gibt 'LONG' wenn aktueller Preis über POC, 'SHORT' wenn darunter.
    """
    if not cs or len(cs) < 10: return None
    # Volumen pro Preisniveau berechnen
    vol_map = {}
    for c in cs:
        price_level = round((c["h"] + c["l"]) / 2)  # Mitte der Kerze auf  gerundet
        vol = c.get("v", 0)
        vol_map[price_level] = vol_map.get(price_level, 0) + vol
    if not vol_map: return None
    poc = max(vol_map, key=vol_map.get)  # Preisniveau mit meistem Volumen
    cur = cs[-1]["c"]
    if cur > poc: return "LONG"
    if cur < poc: return "SHORT"
    return None

def ema_crossover(cs):
    """
    Prüft EMA9/EMA21 Crossover auf 5-Min Kerzen.
    Gibt 'LONG' wenn EMA9 über EMA21 kreuzt,
         'SHORT' wenn EMA9 unter EMA21 kreuzt,
         None wenn kein Crossover.
    """
    if len(cs) < 25: return None, None, None
    cl   = [c["c"] for c in cs]
    e9   = calc_ema(cl, 9)
    e21  = calc_ema(cl, 21)
    # Vorherige Werte
    e9_prev  = calc_ema(cl[:-1], 9)
    e21_prev = calc_ema(cl[:-1], 21)

    if not e9 or not e21 or not e9_prev or not e21_prev:
        return None, e9, e21

    # Crossover erkannt
    if e9_prev <= e21_prev and e9 > e21:
        return "LONG", e9, e21   # EMA9 kreuzt nach oben
    if e9_prev >= e21_prev and e9 < e21:
        return "SHORT", e9, e21  # EMA9 kreuzt nach unten

    # Kein Crossover — aber aktuelle Richtung
    return None, e9, e21

def trend_1h(cs_1h):
    """1H Trend-Filter basierend auf EMA21/EMA50."""
    if not cs_1h or len(cs_1h) < MIN_CANDLES_1H: return None
    cl  = [c["c"] for c in cs_1h]
    e21 = calc_ema(cl, 21)
    e50 = calc_ema(cl, 50)
    cur = cl[-1]
    if not e21 or not e50: return None

    # Letzten 2 Kerzen für schnelle Trendwende
    last2_bear = cs_1h[-1]["c"] < cs_1h[-1]["o"] and cs_1h[-2]["c"] < cs_1h[-2]["o"]
    last2_bull = cs_1h[-1]["c"] > cs_1h[-1]["o"] and cs_1h[-2]["c"] > cs_1h[-2]["o"]

    if cur > e21 and e21 > e50: return "LONG"
    if cur < e21 and e21 < e50: return "SHORT"
    if last2_bear and cur < e21: return "SHORT"
    if last2_bull and cur > e21: return "LONG"
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
    global cool, prev_cross
    tick = 0
    log(f"Bot | EMA Crossover | TP:{TAKE_PROFIT}% SL:{STOP_LOSS}% Trail:{TRAIL_PCT}% | {'DRY RUN' if DRY_RUN else 'LIVE'}", C)

    while True:
        try:
            tick += 1

            # Daten holen
            preis = get_preis()
            cs_5m = get_kerzen(300,  60)
            cs_1h = get_kerzen(3600, 100)

            if not preis or not cs_5m or not cs_1h:
                log("Daten fehlen — warte...", Y)
                time.sleep(INTERVAL); continue

            # Trend und Crossover berechnen
            trend  = trend_1h(cs_1h)
            cross, e9, e21 = ema_crossover(cs_5m)

            # EMA Richtung (auch ohne Crossover)
            ema_dir = "LONG" if (e9 and e21 and e9 > e21) else "SHORT" if (e9 and e21 and e9 < e21) else None

            if pos:
                is_long = pos["dir"] == "LONG"
                pnl = (preis-pos["entry"])/pos["entry"]*100 if is_long else (pos["entry"]-preis)/pos["entry"]*100
                fc  = G if pnl>0 else R

                # Trailing aktualisieren
                if is_long:
                    pos["best"]  = max(pos["best"], preis)
                    trail        = pos["best"] * (1 - TRAIL_PCT/100)
                else:
                    pos["worst"] = min(pos["worst"], preis)
                    trail        = pos["worst"] * (1 + TRAIL_PCT/100)

                trend_txt = f"{G}↑{X}" if trend=="LONG" else (f"{R}↓{X}" if trend=="SHORT" else f"{Y}→{X}")
                log(f"#{pos['id']} {pos['dir']} | {fmt(pos['entry'])}→{fmt(preis)} | P&L:{fc}{pnl:+.2f}%{X} | Trail:{fmt(trail)} | 1H:{trend_txt}")
                save_state()

                # TP / SL / Trail prüfen
                if (is_long and preis >= pos["tp"]) or (not is_long and preis <= pos["tp"]):
                    close_pos("TAKE PROFIT ✅", preis)
                elif (is_long and preis <= pos["sl"]) or (not is_long and preis >= pos["sl"]):
                    close_pos("STOP LOSS ❌", preis)
                elif (is_long and preis <= trail) or (not is_long and preis >= trail):
                    close_pos("TRAILING STOP 📉", preis)
                # Dynamisch: EMA Crossover gegen aktuelle Position → sofort schließen
                elif cross == "SHORT" and is_long:
                    log("EMA Crossover gegen LONG → schließen!", Y)
                    close_pos("EMA UMKEHRUNG 🔄", preis)
                    if trend == "SHORT":
                        open_pos("SHORT", preis)
                elif cross == "LONG" and not is_long:
                    log("EMA Crossover gegen SHORT → schließen!", Y)
                    close_pos("EMA UMKEHRUNG 🔄", preis)
                    if trend == "LONG":
                        open_pos("LONG", preis)

            else:
                if cool > 0:
                    cool -= 1
                    log(f"Cooldown: {cool} | BTC {fmt(preis)} | 1H:{trend or 'seitwärts'}", Y)
                    time.sleep(INTERVAL); continue

                if not trend:
                    if tick % 3 == 0:
                        log(f"BTC {fmt(preis)} | 1H: Seitwärts — warten", Y)
                    time.sleep(INTERVAL); continue

                # Volume Profile berechnen
                vp = volume_profile(cs_5m)

                # Entry: EMA Richtung + 1H Trend + Volume Profile alle übereinstimmen
                if (cross == "LONG" or ema_dir == "LONG") and trend == "LONG" and vp == "LONG":
                    log(f"🎯 LONG (1H:{trend} EMA:{ema_dir} VP:{vp})", M)
                    open_pos("LONG", preis)
                elif (cross == "SHORT" or ema_dir == "SHORT") and trend == "SHORT" and vp == "SHORT":
                    log(f"🎯 SHORT (1H:{trend} EMA:{ema_dir} VP:{vp})", M)
                    open_pos("SHORT", preis)
                else:
                    if tick % 2 == 0:
                        trend_txt = f"{G}LONG{X}" if trend=="LONG" else f"{R}SHORT{X}"
                        e9_fmt  = fmt(e9)  if e9  else "?"
                        e21_fmt = fmt(e21) if e21 else "?"
                        cross_txt = f" 🔄{cross}" if cross else ""
                        vp_log = volume_profile(cs_5m)
                        vp_txt = f" VP:{G if vp_log=='LONG' else R}{vp_log}{X}" if vp_log else ""
                        log(f"BTC {fmt(preis)} | 1H:{trend_txt} | EMA9:{e9_fmt} EMA21:{e21_fmt}{cross_txt}{vp_txt} → warten")

            prev_cross = cross
            time.sleep(INTERVAL)

        except KeyboardInterrupt:
            log("Bot gestoppt.", Y)
            if pos: log(f"⚠️ OFFENE POSITION: {pos['dir']} @ {fmt(pos['entry'])} — MANUELL SCHLIESSEN!", R)
            break
        except Exception as e:
            log(f"Fehler: {e}", R); time.sleep(5)


def main():
    print(f"\n{B}{C}  ╔══════════════════════════════════════════╗")
    print(f"  ║    Nado.xyz — EMA Crossover Bot          ║")
    print(f"  ║  1H Trend + EMA9/21 Cross + Trail Stop   ║")
    print(f"  ╚══════════════════════════════════════════╝{X}\n")
    print(f"  Wallet: {WALLET_ADDR[:12]}...{WALLET_ADDR[-6:]}")
    print(f"  TP:{TAKE_PROFIT}%  SL:{STOP_LOSS}%  Trail:{TRAIL_PCT}%")
    print(f"  Entry: EMA9 kreuzt EMA21 (5-Min)")
    print(f"  Filter: 1H EMA Trend")
    modus = f"{Y}DRY RUN{X}" if DRY_RUN else f"{R}{B}LIVE{X}"
    print(f"  Modus: {modus}\n")
    load_state()
    loop()


if __name__ == "__main__":
    main()
