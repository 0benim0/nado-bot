"""
Nado.xyz — Neutral Grid Bot (Dual Subaccount)
===============================================
LONG Grid  → Account 2 (default_1)
SHORT Grid → Account 1 (default)
Beide gleichzeitig — echter Neutral Grid!

NEU:
- Trailing SL: SL folgt dem Preis wenn Trade im Profit
- 5-Min Kerze Bestätigung: SHORT nur nach roter Kerze, LONG nur nach grüner Kerze

Einrichten: SIGNER_KEY = 1-Click Trading Key (app.nado.xyz -> Settings)
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
import os
SIGNER_KEY_SHORT = os.environ.get("", "")
SIGNER_KEY_LONG  = os.environ.get("", "")

# Account 1 (default) → SHORT Grid
SUBACCOUNT_SHORT = "0xc15263578ce7fd6290f56ab78a23d3b6c653b28c64656661756c740000000000"

# Account 2 (default_1) → LONG Grid
SUBACCOUNT_LONG  = "0xc15263578ce7fd6290f56ab78a23d3b6c653b28c64656661756c745f31000000"

PRODUCT_ID   = 2
CHAIN_ID     = 57073
GATEWAY      = "https://gateway.prod.nado.xyz/v1"
HEADERS      = {"Accept-Encoding": "gzip", "Content-Type": "application/json"}

ORDER_SIZE   = 0.0015
GRID_LEVELS  = 2
GRID_STEP    = 0.1
GRID_PROFIT  = 0.2
TRAIL_PCT    = 0.35   # % Trailing SL hinter bestem Preis
INTERVAL     = 30
DRY_RUN      = False
# ═══════════════════════════════════════════════════════════

long_grid    = []
short_grid   = []
wins         = 0
losses       = 0
total_pnl    = 0.0
last_order_long  = 0.0
last_order_short = 0.0
center_price = None
grid_aktiv   = False
lock_long    = False
lock_short   = False

# Trailing SL State
long_best    = None
long_tsl     = None
short_best   = None
short_tsl    = None


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


def get_letzte_kerze():
    """Letzte geschlossene 5-Min Kerze von Binance."""
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": "5m", "limit": 2},
            timeout=10
        )
        data = r.json()
        if not data or len(data) < 2: return None
        kerze = data[-2]  # letzte geschlossene Kerze
        return {
            "open":  float(kerze[1]),
            "close": float(kerze[4]),
            "rot":   float(kerze[4]) < float(kerze[1]),
            "gruen": float(kerze[4]) > float(kerze[1]),
        }
    except Exception as e:
        log(f"Kerze Fehler: {e}", Y)
    return None


def get_position(subaccount):
    try:
        r = requests.get(f"{GATEWAY}/query?type=subaccount_info&subaccount={subaccount}",
                         headers={"Accept-Encoding":"gzip"}, timeout=15, verify=False)
        for pb in r.json().get("data", {}).get("perp_balances", []):
            if pb.get("product_id") == PRODUCT_ID:
                return float(pb["balance"]["amount"]) / 1e18
    except Exception as e: log(f"Position Fehler: {e}", Y)
    return None


def get_nonce():
    try:
        r = requests.get(
            f"{GATEWAY}/query?type=nonces&address={WALLET_ADDR}",
            headers={"Accept-Encoding":"gzip"}, timeout=10, verify=False)
        data = r.json().get("data", {})
        nonce = data.get("order_nonce")
        if nonce: return int(nonce)
    except Exception as e: log(f"Nonce Fehler: {e}", Y)
    return ((int(time.time()) * 1000 + 100000) << 20) + random.randint(0, 99999)


def sender_hex(subaccount):
    hex_clean = subaccount.lower().replace("0x", "")
    hex_clean = hex_clean.ljust(64, "0")[:64]
    return "0x" + hex_clean


# ─── ORDER ────────────────────────────────────────────────

def place_order(is_buy, price, size, subaccount, sl_order=False):
    global last_order_long, last_order_short, lock_long, lock_short

    is_long_account = (subaccount == SUBACCOUNT_LONG)

    if is_long_account and lock_long:
        log("Lock LONG aktiv", Y); return False
    if not is_long_account and lock_short:
        log("Lock SHORT aktiv", Y); return False

    if is_long_account: lock_long = True
    else: lock_short = True

    try:
        if DRY_RUN:
            side = "LONG-ACC" if is_long_account else "SHORT-ACC"
            log(f"[DRY] {side} {'BUY' if is_buy else 'SELL'} {size} BTC @ {fmt(price)}", Y)
            if is_long_account: last_order_long = time.time()
            else: last_order_short = time.time()
            return True

        from eth_account import Account
        signer_key = SIGNER_KEY_LONG if is_long_account else SIGNER_KEY_SHORT
        slip  = 0.005 if sl_order else 0.002
        px    = round(price * (1+slip if is_buy else 1-slip)) * int(1e18)
        amt   = int(size*1e18) if is_buy else -int(size*1e18)
        exp   = int(time.time()) + 60
        nonce = get_nonce()
        sndr  = sender_hex(subaccount)
        dom   = {"name":"Nado","version":"0.0.1","chainId":CHAIN_ID,
                 "verifyingContract":f"0x{PRODUCT_ID:040x}"}
        typ   = {"Order":[
            {"name":"sender","type":"bytes32"},{"name":"priceX18","type":"int128"},
            {"name":"amount","type":"int128"},{"name":"expiration","type":"uint64"},
            {"name":"nonce","type":"uint64"},{"name":"appendix","type":"uint128"}]}
        msg   = {"sender":sndr,"priceX18":px,"amount":amt,
                 "expiration":exp,"nonce":nonce,"appendix":1}
        acc   = Account.from_key(signer_key)
        sig   = acc.sign_typed_data(domain_data=dom,message_types=typ,message_data=msg).signature.hex()
        if not sig.startswith("0x"): sig = "0x"+sig
        pld   = {"place_order":{"product_id":PRODUCT_ID,"order":{
            "sender":sndr,"priceX18":str(px),"amount":str(amt),
            "expiration":str(exp),"nonce":str(nonce),"appendix":"1"
        },"signature":sig}}
        r = requests.post(f"{GATEWAY}/execute", json=pld, headers=HEADERS, timeout=15, verify=False)
        d = r.json()
        if d.get("status") == "success":
            log("✅ Order OK!", G)
            if is_long_account: last_order_long = time.time()
            else: last_order_short = time.time()
            return True
        code = d.get("error_code", 0)
        if code == 2006: log("⚠️ Kein Kapital (2006)", Y); return "NO_MARGIN"
        log(f"❌ {d.get('error','')} (Code:{code})", R); return False
    except Exception as e:
        log(f"Order Exception: {e}", R); return False
    finally:
        if is_long_account: lock_long = False
        else: lock_short = False


# ─── TRAILING SL ──────────────────────────────────────────

def update_trailing_sl(preis):
    global long_best, long_tsl, short_best, short_tsl

    # LONG Trailing SL
    if long_offen() > 0:
        if long_best is None: long_best = preis
        if preis > long_best:
            long_best = preis
            long_tsl  = preis * (1 - TRAIL_PCT/100)
        elif long_tsl is None:
            long_tsl = preis * (1 - TRAIL_PCT/100)
    else:
        long_best = None; long_tsl = None

    # SHORT Trailing SL
    if short_offen() > 0:
        if short_best is None: short_best = preis
        if preis < short_best:
            short_best = preis
            short_tsl  = preis * (1 + TRAIL_PCT/100)
        elif short_tsl is None:
            short_tsl = preis * (1 + TRAIL_PCT/100)
    else:
        short_best = None; short_tsl = None


# ─── STARTUP CHECK ────────────────────────────────────────

def check_and_close(subaccount, preis, name):
    if DRY_RUN: return
    log(f"Prüfe {name} Positionen...", C)
    pos = get_position(subaccount)
    if pos is None: log(f"{name}: keine Verbindung", Y); return
    if abs(pos) < 0.0001: log(f"{name}: keine offenen Positionen ✅", G); return
    log(f"⚠️ {name}: {pos:.4f} BTC offen — schließe!", R)
    size = round(abs(pos), 4)
    if pos > 0:
        ok = place_order(False, preis, size, subaccount, sl_order=True)
    else:
        ok = place_order(True, preis, size, subaccount, sl_order=True)
    if ok is True:
        log(f"✅ {name} Position geschlossen", G)
        time.sleep(2)
    else:
        log(f"❌ {name} manuell schließen auf app.nado.xyz!", R)
        time.sleep(5)


# ─── GRID ─────────────────────────────────────────────────

def build_neutral_grid(preis):
    global long_grid, short_grid, center_price, grid_aktiv
    global last_order_long, last_order_short
    global long_best, long_tsl, short_best, short_tsl
    center_price = preis
    long_grid = []; short_grid = []
    long_best=None; long_tsl=None; short_best=None; short_tsl=None

    for i in range(1, GRID_LEVELS+1):
        entry = round(preis * (1 - i * GRID_STEP/100))
        long_grid.append({"entry":entry,"tp":round(entry*(1+GRID_PROFIT/100)),"filled":False,"open_time":0.0})

    for i in range(1, GRID_LEVELS+1):
        entry = round(preis * (1 + i * GRID_STEP/100))
        short_grid.append({"entry":entry,"tp":round(entry*(1-GRID_PROFIT/100)),"filled":False,"open_time":0.0})

    grid_aktiv = True
    last_order_long  = time.time()
    last_order_short = time.time()

    log(f"═══ NEUTRAL GRID @ {fmt(preis)} ═══", C)
    log(f"📗 LONG  (Acc2): {' | '.join(fmt(lv['entry']) for lv in long_grid)}", G)
    log(f"📕 SHORT (Acc1): {' | '.join(fmt(lv['entry']) for lv in short_grid)}", R)
    log(f"Trailing SL: {TRAIL_PCT}% | Einstieg: grüne/rote 5-Min Kerze", Y)


def reset_grid():
    global long_grid, short_grid, center_price, grid_aktiv
    global long_best, long_tsl, short_best, short_tsl
    long_grid=[]; short_grid=[]; center_price=None; grid_aktiv=False
    long_best=None; long_tsl=None; short_best=None; short_tsl=None


def close_all(preis, reason=""):
    global total_pnl, wins, losses
    n_long=long_offen(); n_short=short_offen()
    log(f"⛔ {reason}", R)
    if n_long > 0:
        ok = place_order(False, preis, round(n_long*ORDER_SIZE,4), SUBACCOUNT_LONG, sl_order=True)
        if ok is True:
            for lv in long_grid:
                if lv["filled"]:
                    pnl=(preis-lv["entry"])/lv["entry"]*100
                    total_pnl+=pnl
                    if pnl>=0: wins+=1
                    else: losses+=1
    if n_short > 0:
        ok = place_order(True, preis, round(n_short*ORDER_SIZE,4), SUBACCOUNT_SHORT, sl_order=True)
        if ok is True:
            for lv in short_grid:
                if lv["filled"]:
                    pnl=(lv["entry"]-preis)/lv["entry"]*100
                    total_pnl+=pnl
                    if pnl>=0: wins+=1
                    else: losses+=1
    log(f"P&L: {total_pnl:+.2f}% | {wins}W {losses}L", G if total_pnl>=0 else R)
    reset_grid()


# ─── LOOP ─────────────────────────────────────────────────

def loop():
    global wins, losses, total_pnl
    tick = 0
    log(f"Neutral Grid Bot | Dual Subaccount | {'DRY' if DRY_RUN else 'LIVE'}", C)

    # Startup: offene Positionen schließen
    startup_preis = None
    while not startup_preis:
        startup_preis = get_preis()
        if not startup_preis: time.sleep(5)
    check_and_close(SUBACCOUNT_LONG,  startup_preis, "LONG-Acc2")
    check_and_close(SUBACCOUNT_SHORT, startup_preis, "SHORT-Acc1")

    while True:
        try:
            tick += 1
            preis = get_preis()
            if not preis: log("Kein Preis...", Y); time.sleep(INTERVAL); continue

            if not grid_aktiv:
                build_neutral_grid(preis)
                time.sleep(INTERVAL); continue

            # Trailing SL aktualisieren
            update_trailing_sl(preis)

            # ── LONG TRAILING SL PRÜFEN ───────────────────
            if long_tsl and long_offen() > 0 and preis <= long_tsl:
                log(f"🔴 LONG TSL getroffen @ {fmt(preis)} (TSL:{fmt(long_tsl)}) [Acc2]", R)
                close_all(preis, "LONG TRAILING SL")
                time.sleep(INTERVAL); continue

            # ── SHORT TRAILING SL PRÜFEN ──────────────────
            if short_tsl and short_offen() > 0 and preis >= short_tsl:
                log(f"🟢 SHORT TSL getroffen @ {fmt(preis)} (TSL:{fmt(short_tsl)}) [Acc1]", G)
                close_all(preis, "SHORT TRAILING SL")
                time.sleep(INTERVAL); continue

            # ── LONG GRID (Account 2) ──────────────────────
            long_bereit = (time.time() - last_order_long) >= 3

            if long_bereit:
                for lv in long_grid:
                    if not lv["filled"] and preis <= lv["entry"]*1.001:
                        kerze = get_letzte_kerze()
                        if kerze and kerze["gruen"]:
                            log(f"🟢 LONG @ {fmt(lv['entry'])} TP:{fmt(lv['tp'])} ✅ grüne Kerze [Acc2]", G)
                            lv["filled"]=True; lv["open_time"]=time.time()
                            ok = place_order(True, preis, ORDER_SIZE, SUBACCOUNT_LONG)
                            if not ok and ok != "NO_MARGIN":
                                lv["filled"]=False; lv["open_time"]=0.0
                            elif ok == "NO_MARGIN": lv["open_time"]=-1
                        else:
                            log(f"⏳ LONG Level erreicht — warte auf grüne Kerze...", Y)
                        break

            # ── LONG TP ────────────────────────────────────
            for lv in long_grid:
                if not lv["filled"] or lv["open_time"]<=0: continue
                if (time.time()-lv["open_time"])<30: continue
                if preis >= lv["tp"]:
                    ok = place_order(False, preis, ORDER_SIZE, SUBACCOUNT_LONG)
                    if ok is True:
                        pnl=(preis-lv["entry"])/lv["entry"]*100
                        lv["filled"]=False; lv["open_time"]=0.0
                        total_pnl+=pnl; wins+=1
                        log(f"✅ LONG TP +{pnl:.2f}% | Total:{total_pnl:+.2f}% | {wins}W", G)
                    break

            # ── SHORT GRID (Account 1) ─────────────────────
            short_bereit = (time.time() - last_order_short) >= 3

            if short_bereit:
                for lv in short_grid:
                    if not lv["filled"] and preis >= lv["entry"]*0.999:
                        kerze = get_letzte_kerze()
                        if kerze and kerze["rot"]:
                            log(f"🔴 SHORT @ {fmt(lv['entry'])} TP:{fmt(lv['tp'])} ✅ rote Kerze [Acc1]", R)
                            lv["filled"]=True; lv["open_time"]=time.time()
                            ok = place_order(False, preis, ORDER_SIZE, SUBACCOUNT_SHORT)
                            if not ok and ok != "NO_MARGIN":
                                lv["filled"]=False; lv["open_time"]=0.0
                            elif ok == "NO_MARGIN": lv["open_time"]=-1
                        else:
                            log(f"⏳ SHORT Level erreicht — warte auf rote Kerze...", Y)
                        break

            # ── SHORT TP ───────────────────────────────────
            for lv in short_grid:
                if not lv["filled"] or lv["open_time"]<=0: continue
                if (time.time()-lv["open_time"])<30: continue
                if preis <= lv["tp"]:
                    ok = place_order(True, preis, ORDER_SIZE, SUBACCOUNT_SHORT)
                    if ok is True:
                        pnl=(lv["entry"]-preis)/lv["entry"]*100
                        lv["filled"]=False; lv["open_time"]=0.0
                        total_pnl+=pnl; wins+=1
                        log(f"✅ SHORT TP +{pnl:.2f}% | Total:{total_pnl:+.2f}% | {wins}W", G)
                    break

            if tick % 2 == 0:
                tsl_info = ""
                if long_tsl:  tsl_info += f" LTSL:{fmt(long_tsl)}"
                if short_tsl: tsl_info += f" STSL:{fmt(short_tsl)}"
                log(f"BTC {fmt(preis)} | L:{long_offen()}/{GRID_LEVELS}[Acc2] S:{short_offen()}/{GRID_LEVELS}[Acc1] | "
                    f"{wins}W {losses}L P&L:{total_pnl:+.2f}%{tsl_info}")

            time.sleep(INTERVAL)

        except KeyboardInterrupt:
            log("Bot gestoppt.", Y)
            if long_offen()>0 or short_offen()>0:
                log(f"⚠️ Offene Positionen manuell schließen!", R)
            break
        except Exception as e:
            log(f"Fehler: {e}", R); time.sleep(5)


def main():
    print(f"\n{B}{C}  ╔══════════════════════════════════════════╗")
    print(f"  ║   Nado.xyz — Neutral Grid Bot            ║")
    print(f"  ║   Dual Subaccount: LONG + SHORT          ║")
    print(f"  ╚══════════════════════════════════════════╝{X}\n")
    print(f"  Wallet:    {WALLET_ADDR[:12]}...{WALLET_ADDR[-6:]}")
    print(f"  LONG Acc:  default_1 (Account 2)")
    print(f"  SHORT Acc: default   (Account 1)")
    print(f"  Step:      {GRID_STEP}% | Levels: {GRID_LEVELS}L+{GRID_LEVELS}S | TP: {GRID_PROFIT}%")
    print(f"  Trailing:  {TRAIL_PCT}% hinter bestem Preis")
    print(f"  Einstieg:  LONG nach grüner | SHORT nach roter 5-Min Kerze")
    modus = f"{Y}DRY RUN{X}" if DRY_RUN else f"{R}{B}LIVE{X}"
    print(f"  Modus:     {modus}\n")
    loop()

if __name__ == "__main__":
    main()
