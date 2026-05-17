"""
Nado.xyz — Neutral Grid Bot
============================
Strategie: LONG Levels unter Markt + SHORT Levels über Markt gleichzeitig
Profit:    Bei jeder Preisbewegung — egal ob hoch oder runter
Kein Raten der Richtung — profitiert von natürlicher Volatilität

Funktionsweise:
  - Range wird automatisch aus ATR der letzten 24h berechnet
  - LONG Orders unter aktuellem Preis (kaufen wenn fällt)
  - SHORT Orders über aktuellem Preis (shorten wenn steigt)
  - Jeder Fill hat sofort einen fixen TP auf nächstem Level
  - SL schützt bei Ausbruch aus Range

Einrichten: SIGNER_KEY = 1-Click Trading Key (app.nado.xyz → Settings)
"""

import time, random, requests, sys, urllib3
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
WALLET_ADDR  = "0xc15263578ce7fd6290f56Ab78a23D3b6C653B28C"
SIGNER_KEY   = "0x8097b0ec439aa91bd4f3c3ea79735be6688ce00589bbcd0e3dea2ab596580a4d"
SUBACCOUNT   = "0xc15263578ce7fd6290f56ab78a23d3b6c653b28c64656661756c740000000000"

PRODUCT_ID   = 2
CHAIN_ID     = 57073
GATEWAY      = "https://gateway.prod.nado.xyz/v1"
ARCHIVE      = "https://archive.prod.nado.xyz/v1"
HEADERS      = {"Accept-Encoding": "gzip", "Content-Type": "application/json"}

ORDER_SIZE   = 0.0015  # BTC pro Level (LONG und SHORT)
GRID_LEVELS  = 5       # Levels pro Seite (5 LONG + 5 SHORT = 10 total)
GRID_STEP    = 0.1     # % Abstand zwischen Levels
GRID_PROFIT  = 0.1     # % TP pro Level (= 1 Step weiter)
SL_PCT       = 0.8     # % Range Ausbruch → alles schliessen
MIN_ORDER_WAIT = 5     # Sekunden Mindestabstand zwischen Orders
SYNC_WAIT    = 180     # Sek nach Order kein Sync
INTERVAL     = 30      # Sek pro Tick
DRY_RUN      = False
# ═══════════════════════════════════════════════════════════

# State
long_grid    = []   # LONG Levels unter Markt
short_grid   = []   # SHORT Levels über Markt
wins         = 0
losses       = 0
total_pnl    = 0.0
last_order_t = 0.0
center_price = None  # Preis beim Grid-Start
grid_aktiv   = False
order_lock   = False  # Verhindert doppelte Orders


def ts():     return datetime.now().strftime("%H:%M:%S")
def log(m, c=""): print(f"{c}[{ts()}] {m}{X}" if c else f"[{ts()}] {m}"); sys.stdout.flush()
def fmt(x):
    try:    return f"${float(x):,.2f}"
    except: return "?"

def long_offen():  return sum(1 for lv in long_grid  if lv["filled"])
def short_offen(): return sum(1 for lv in short_grid if lv["filled"])


# ─── API ──────────────────────────────────────────────────

def get_preis():
    try:
        r = requests.get(f"{GATEWAY}/query?type=all_products",
                         headers={"Accept-Encoding":"gzip"}, timeout=15, verify=False)
        data = r.json().get("data", r.json())
        for p in data.get("perp_products", []):
            if int(p.get("product_id", -1)) == PRODUCT_ID:
                px = float(p.get("oracle_price_x18") or p.get("mark_price_x18") or 0)
                if px > 0: return px / 1e18
    except Exception as e: log(f"Preis Fehler: {e}", Y)
    return None


def get_kerzen(limit=100):
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": "5m", "limit": limit},
            timeout=15
        )
        data = r.json()
        if not data: return None
        return [{"h":float(c[2]),"l":float(c[3]),"c":float(c[4]),"v":float(c[5])} for c in data]
    except Exception as e:
        log(f"Kerzen Fehler: {e}", Y)
    return None


def get_nado_position():
    try:
        r = requests.get(f"{GATEWAY}/query?type=subaccount_info&subaccount={SUBACCOUNT}",
                         headers={"Accept-Encoding":"gzip"}, timeout=15, verify=False)
        for pb in r.json().get("data", {}).get("perp_balances", []):
            if pb.get("product_id") == PRODUCT_ID:
                return float(pb["balance"]["amount"]) / 1e18
    except Exception as e: log(f"Nado API Fehler: {e}", Y)
    return None


def calc_atr(candles, n=14):
    """ATR — misst aktuelle Volatilität für Range-Berechnung."""
    if len(candles) < n+1: return None
    trs = []
    for i in range(1, len(candles)):
        h=candles[i]["h"]; l=candles[i]["l"]; pc=candles[i-1]["c"]
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    atr = sum(trs[:n])/n
    for tr in trs[n:]: atr = (atr*(n-1)+tr)/n
    return atr


# ─── ORDER ────────────────────────────────────────────────

def sender_hex():
    ab = bytes.fromhex(WALLET_ADDR.lower().replace("0x",""))
    return "0x" + (ab + b"default".ljust(12, b"\x00")).hex()


def place_order(is_buy, price, size, sl_order=False):
    global last_order_t, order_lock
    # Globaler Lock — verhindert doppelte Orders
    if order_lock:
        log("⚠️ Order Lock aktiv — übersprungen", Y)
        return False
    order_lock = True
    try:
        if DRY_RUN:
            log(f"[DRY] {'BUY' if is_buy else 'SELL'} {size} BTC @ {fmt(price)}", Y)
            last_order_t = time.time()
            return True
        from eth_account import Account
        slip = 0.005 if sl_order else 0.002
        px   = round(price * (1+slip if is_buy else 1-slip)) * int(1e18)
        amt  = int(size*1e18) if is_buy else -int(size*1e18)
        exp   = int(time.time()) + 120
        nonce = int(time.time() * 1000) + random.randint(1, 999)
        sndr = sender_hex()
        dom  = {"name":"Nado","version":"0.0.1","chainId":CHAIN_ID,
                "verifyingContract":f"0x{PRODUCT_ID:040x}"}
        typ  = {"Order":[
            {"name":"sender","type":"bytes32"},{"name":"priceX18","type":"int128"},
            {"name":"amount","type":"int128"},{"name":"expiration","type":"uint64"},
            {"name":"nonce","type":"uint64"},{"name":"appendix","type":"uint128"}]}
        msg  = {"sender":sndr,"priceX18":px,"amount":amt,
                "expiration":exp,"nonce":nonce,"appendix":0}
        acc  = Account.from_key(SIGNER_KEY)
        sig  = acc.sign_typed_data(domain_data=dom,message_types=typ,message_data=msg).signature.hex()
        if not sig.startswith("0x"): sig = "0x"+sig
        pld  = {"place_order":{"product_id":PRODUCT_ID,"order":{
            "sender":sndr,"priceX18":str(px),"amount":str(amt),
            "expiration":str(exp),"nonce":str(nonce),"appendix":"0"
        },"signature":sig}}
        r = requests.post(f"{GATEWAY}/execute", json=pld, headers=HEADERS, timeout=15, verify=False)
        d = r.json()
        if d.get("status") == "success":
            log("✅ Order OK!", G); last_order_t = time.time(); return True
        code = d.get("error_code", 0)
        if code == 2006:
            log("⚠️ Kein Kapital (2006) — Level überspringen", Y); return "NO_MARGIN"
        log(f"❌ {d.get('error','')} (Code:{code})", R); return False
    except Exception as e:
        log(f"Order Exception: {e}", R); return False
    finally:
        order_lock = False  # Lock immer freigeben


# ─── NEUTRAL GRID AUFBAUEN ────────────────────────────────

def build_neutral_grid(preis):
    """Baut LONG Levels unter + SHORT Levels über dem Marktpreis."""
    global long_grid, short_grid, center_price, grid_aktiv

    center_price = preis
    long_grid  = []
    short_grid = []

    # LONG Levels — unter aktuellem Preis
    for i in range(1, GRID_LEVELS+1):
        entry = round(preis * (1 - i * GRID_STEP/100))
        tp    = round(entry * (1 + GRID_PROFIT/100))
        long_grid.append({
            "entry": entry,
            "tp":    tp,
            "filled": False,
            "open_time": 0.0
        })

    # SHORT Levels — über aktuellem Preis
    for i in range(1, GRID_LEVELS+1):
        entry = round(preis * (1 + i * GRID_STEP/100))
        tp    = round(entry * (1 - GRID_PROFIT/100))
        short_grid.append({
            "entry": entry,
            "tp":    tp,
            "filled": False,
            "open_time": 0.0
        })

    grid_aktiv = True

    # Log
    long_lvls  = " | ".join(fmt(lv["entry"]) for lv in long_grid)
    short_lvls = " | ".join(fmt(lv["entry"]) for lv in short_grid)
    log(f"═══ NEUTRAL GRID @ {fmt(preis)} ═══", C)
    log(f"📗 LONG  Levels: {long_lvls}", G)
    log(f"📕 SHORT Levels: {short_lvls}", R)
    log(f"Step: {GRID_STEP}% | TP: {GRID_PROFIT}% | {GRID_LEVELS} Long + {GRID_LEVELS} Short", C)


def reset_grid():
    """Grid komplett zurücksetzen."""
    global long_grid, short_grid, center_price, grid_aktiv
    long_grid  = []
    short_grid = []
    center_price = None
    grid_aktiv = False


def close_all_positions(preis, reason=""):
    """Alle offenen Positionen schliessen."""
    global wins, losses, total_pnl

    n_long  = long_offen()
    n_short = short_offen()

    if n_long == 0 and n_short == 0:
        reset_grid()
        return

    log(f"⛔ {reason} — Schliesse {n_long}L + {n_short}S Positionen", R)

    # LONG Positionen schliessen (SELL)
    if n_long > 0:
        size = round(n_long * ORDER_SIZE, 4)
        ok = place_order(False, preis, size, sl_order=True)
        if ok is True or DRY_RUN:
            for lv in long_grid:
                if lv["filled"]:
                    pnl = ((preis - lv["entry"]) / lv["entry"]) * 100
                    total_pnl += pnl
                    if pnl >= 0: wins += 1
                    else: losses += 1
            log(f"✅ {n_long} LONG geschlossen @ {fmt(preis)}", G)

    # SHORT Positionen schliessen (BUY)
    if n_short > 0:
        size = round(n_short * ORDER_SIZE, 4)
        ok = place_order(True, preis, size, sl_order=True)
        if ok is True or DRY_RUN:
            for lv in short_grid:
                if lv["filled"]:
                    pnl = ((lv["entry"] - preis) / lv["entry"]) * 100
                    total_pnl += pnl
                    if pnl >= 0: wins += 1
                    else: losses += 1
            log(f"✅ {n_short} SHORT geschlossen @ {fmt(preis)}", R)

    log(f"P&L Total: {total_pnl:+.2f}% | {wins}W {losses}L", G if total_pnl >= 0 else R)
    reset_grid()


def sync_nado(preis):
    """Position mit Nado abgleichen."""
    if DRY_RUN: return
    if (time.time() - last_order_t) < SYNC_WAIT: return
    nado = get_nado_position()
    if nado is None: return
    bot_net = round((long_offen() - short_offen()) * ORDER_SIZE, 4)
    nado_net = round(nado, 4)
    if abs(bot_net - nado_net) > 0.001:
        log(f"⚠️ Sync: Bot={bot_net:.4f} | Nado={nado_net:.4f}", Y)


# ─── LOOP ─────────────────────────────────────────────────

def loop():
    global wins, losses, total_pnl

    tick = 0
    log(f"Neutral Grid Bot | {'DRY' if DRY_RUN else 'LIVE'}", C)

    while True:
        try:
            tick += 1
            preis = get_preis()
            if not preis:
                log("Kein Preis...", Y); time.sleep(INTERVAL); continue

            candles = get_kerzen(100)
            if not candles:
                log("Keine Kerzen...", Y); time.sleep(INTERVAL); continue

            # ── GRID AUFBAUEN wenn noch keins aktiv ───────
            if not grid_aktiv:
                atr = calc_atr(candles)
                atr_pct = (atr / preis * 100) if atr else 0
                log(f"BTC {fmt(preis)} | ATR: {atr_pct:.3f}% | Grid aufbauen...", C)
                build_neutral_grid(preis)
                time.sleep(INTERVAL)
                continue

            # ── SL PRÜFEN — Range Ausbruch ─────────────────
            sl_long  = center_price * (1 - (GRID_LEVELS * GRID_STEP + SL_PCT) / 100)
            sl_short = center_price * (1 + (GRID_LEVELS * GRID_STEP + SL_PCT) / 100)

            if preis <= sl_long:
                log(f"⛔ SL LONG getroffen @ {fmt(preis)} (Grenze: {fmt(sl_long)})", R)
                close_all_positions(preis, "SL LONG AUSBRUCH")
                time.sleep(INTERVAL); continue

            if preis >= sl_short:
                log(f"⛔ SL SHORT getroffen @ {fmt(preis)} (Grenze: {fmt(sl_short)})", R)
                close_all_positions(preis, "SL SHORT AUSBRUCH")
                time.sleep(INTERVAL); continue

            just_acted = False

            # Mindestabstand zwischen Orders prüfen
            if (time.time() - last_order_t) < MIN_ORDER_WAIT:
                if tick % 2 == 0:
                    n_long = long_offen(); n_short = short_offen()
                    log(f"BTC {fmt(preis)} | 🟢{n_long}/{GRID_LEVELS} 🔴{n_short}/{GRID_LEVELS} | "
                        f"{wins}W {losses}L P&L:{total_pnl:+.2f}%")
                time.sleep(INTERVAL); continue

            # ── LONG LEVELS prüfen ─────────────────────────
            for lv in long_grid:
                if not lv["filled"] and preis <= lv["entry"] * 1.001:
                    log(f"🟢 LONG  @ {fmt(lv['entry'])} → TP: {fmt(lv['tp'])}", G)
                    lv["filled"] = True
                    lv["open_time"] = time.time()
                    ok = place_order(True, preis, ORDER_SIZE)
                    if ok == "NO_MARGIN":
                        lv["open_time"] = -1
                    elif not ok:
                        lv["filled"] = False
                        lv["open_time"] = 0.0
                    just_acted = True  # auch bei Fehler — kein zweiter Versuch im gleichen Tick
                    break

            # ── LONG TP prüfen ─────────────────────────────
            if not just_acted:
                for lv in long_grid:
                    if not lv["filled"] or lv["open_time"] <= 0: continue
                    if (time.time() - lv["open_time"]) < 30: continue
                    if preis >= lv["tp"]:
                        log(f"✅ LONG TP @ {fmt(preis)} | Einstieg: {fmt(lv['entry'])} | +{GRID_PROFIT}%", G)
                        lv["filled"] = False
                        lv["open_time"] = 0.0
                        ok = place_order(False, preis, ORDER_SIZE)
                        if ok is True:
                            pnl = ((preis - lv["entry"]) / lv["entry"]) * 100
                            total_pnl += pnl; wins += 1
                            log(f"   Total: {total_pnl:+.2f}% | {wins}W {losses}L", G)
                        just_acted = True
                        break

            # ── SHORT LEVELS prüfen ────────────────────────
            if not just_acted:
                for lv in short_grid:
                    if not lv["filled"] and preis >= lv["entry"] * 0.999:
                        log(f"🔴 SHORT @ {fmt(lv['entry'])} → TP: {fmt(lv['tp'])}", R)
                        lv["filled"] = True
                        lv["open_time"] = time.time()
                        ok = place_order(False, preis, ORDER_SIZE)
                        if ok == "NO_MARGIN":
                            lv["open_time"] = -1
                        elif not ok:
                            lv["filled"] = False
                            lv["open_time"] = 0.0
                        just_acted = True  # auch bei Fehler
                        break

            # ── SHORT TP prüfen ────────────────────────────
            if not just_acted:
                for lv in short_grid:
                    if not lv["filled"] or lv["open_time"] <= 0: continue
                    if (time.time() - lv["open_time"]) < 30: continue
                    if preis <= lv["tp"]:
                        log(f"✅ SHORT TP @ {fmt(preis)} | Einstieg: {fmt(lv['entry'])} | +{GRID_PROFIT}%", G)
                        lv["filled"] = False
                        lv["open_time"] = 0.0
                        ok = place_order(True, preis, ORDER_SIZE)
                        if ok is True:
                            pnl = ((lv["entry"] - preis) / lv["entry"]) * 100
                            total_pnl += pnl; wins += 1
                            log(f"   Total: {total_pnl:+.2f}% | {wins}W {losses}L", G)
                        just_acted = True
                        break

            # ── SYNC ───────────────────────────────────────
            if tick % 4 == 0:
                sync_nado(preis)

            # ── STATUS LOG ─────────────────────────────────
            if tick % 2 == 0:
                sl_u = fmt(sl_long)
                sl_o = fmt(sl_short)
                log(f"BTC {fmt(preis)} | 🟢{long_offen()}/{GRID_LEVELS} 🔴{short_offen()}/{GRID_LEVELS} | "
                    f"{wins}W {losses}L P&L:{total_pnl:+.2f}% | SL:{sl_u}↔{sl_o}")

            time.sleep(INTERVAL)

        except KeyboardInterrupt:
            log("Bot gestoppt.", Y)
            preis = get_preis()
            if preis and (long_offen() > 0 or short_offen() > 0):
                log(f"⚠️ Offene Positionen: {long_offen()}L + {short_offen()}S", R)
                log("Manuell auf app.nado.xyz schliessen!", R)
            break
        except Exception as e:
            log(f"Fehler: {e}", R); time.sleep(5)


def main():
    print(f"\n{B}{C}  ╔══════════════════════════════════════════╗")
    print(f"  ║   Nado.xyz — Neutral Grid Bot            ║")
    print(f"  ║   LONG unten + SHORT oben gleichzeitig   ║")
    print(f"  ╚══════════════════════════════════════════╝{X}\n")
    print(f"  Wallet:    {WALLET_ADDR[:12]}...{WALLET_ADDR[-6:]}")
    print(f"  Step:      {GRID_STEP}% | Levels: {GRID_LEVELS}L + {GRID_LEVELS}S")
    print(f"  TP:        {GRID_PROFIT}% pro Level")
    print(f"  SL:        {SL_PCT}% nach letztem Level")
    print(f"  Order:     {ORDER_SIZE} BTC pro Level")
    print(f"  Strategie: Neutral — kein Richtungsraten!")
    modus = f"{Y}DRY RUN{X}" if DRY_RUN else f"{R}{B}LIVE{X}"
    print(f"  Modus:     {modus}\n")
    loop()

if __name__ == "__main__":
    main()
