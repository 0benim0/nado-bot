"""
Nado.xyz — Neutral Grid Bot
============================
LONG Levels unter Markt + SHORT Levels über Markt gleichzeitig
Profit bei jeder Preisbewegung — kein Richtungsraten

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
SIGNER_KEY   = "0x8097b0ec439aa91bd4f3c3ea79735be6688ce00589bbcd0e3dea2ab596580a4d"
SUBACCOUNT   = "0xc15263578ce7fd6290f56ab78a23d3b6c653b28c64656661756c740000000000"

PRODUCT_ID   = 2
CHAIN_ID     = 57073
GATEWAY      = "https://gateway.prod.nado.xyz/v1"
HEADERS      = {"Accept-Encoding": "gzip", "Content-Type": "application/json"}

ORDER_SIZE   = 0.0015
GRID_LEVELS  = 2
GRID_STEP    = 0.1
GRID_PROFIT  = 0.2
SL_PCT       = 0.3
INTERVAL     = 30
DRY_RUN      = True
# ═══════════════════════════════════════════════════════════

long_grid    = []
short_grid   = []
wins         = 0
losses       = 0
total_pnl    = 0.0
last_order_t = 0.0
center_price = None
grid_aktiv   = False
order_lock   = False


def ts():     return datetime.now().strftime("%H:%M:%S")
def log(m, c=""): print(f"{c}[{ts()}] {m}{X}" if c else f"[{ts()}] {m}"); sys.stdout.flush()
def fmt(x):
    try:    return f"${float(x):,.2f}"
    except: return "?"

def long_offen():  return sum(1 for lv in long_grid  if lv["filled"])
def short_offen(): return sum(1 for lv in short_grid if lv["filled"])


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


def sender_hex():
    ab = bytes.fromhex(WALLET_ADDR.lower().replace("0x",""))
    return "0x" + (ab + b"default".ljust(12, b"\x00")).hex()


def place_order(is_buy, price, size, sl_order=False):
    global last_order_t, order_lock
    if order_lock: log("Order Lock aktiv", Y); return False
    order_lock = True
    try:
        if DRY_RUN:
            log(f"[DRY] {'BUY' if is_buy else 'SELL'} {size} BTC @ {fmt(price)}", Y)
            last_order_t = time.time(); return True
        from eth_account import Account
        slip  = 0.005 if sl_order else 0.002
        px    = round(price * (1+slip if is_buy else 1-slip)) * int(1e18)
        amt   = int(size*1e18) if is_buy else -int(size*1e18)
        exp   = int(time.time()) + 60
        nonce = get_nonce()
        sndr  = sender_hex()
        dom   = {"name":"Nado","version":"0.0.1","chainId":CHAIN_ID,
                 "verifyingContract":f"0x{PRODUCT_ID:040x}"}
        typ   = {"Order":[
            {"name":"sender","type":"bytes32"},{"name":"priceX18","type":"int128"},
            {"name":"amount","type":"int128"},{"name":"expiration","type":"uint64"},
            {"name":"nonce","type":"uint64"},{"name":"appendix","type":"uint128"}]}
        msg   = {"sender":sndr,"priceX18":px,"amount":amt,
                 "expiration":exp,"nonce":nonce,"appendix":1}
        acc   = Account.from_key(SIGNER_KEY)
        sig   = acc.sign_typed_data(domain_data=dom,message_types=typ,message_data=msg).signature.hex()
        if not sig.startswith("0x"): sig = "0x"+sig
        pld   = {"place_order":{"product_id":PRODUCT_ID,"order":{
            "sender":sndr,"priceX18":str(px),"amount":str(amt),
            "expiration":str(exp),"nonce":str(nonce),"appendix":"1"
        },"signature":sig}}
        r = requests.post(f"{GATEWAY}/execute", json=pld, headers=HEADERS, timeout=15, verify=False)
        d = r.json()
        if d.get("status") == "success":
            log("✅ Order OK!", G); last_order_t = time.time(); return True
        code = d.get("error_code", 0)
        if code == 2006: log("⚠️ Kein Kapital (2006)", Y); return "NO_MARGIN"
        log(f"❌ {d.get('error','')} (Code:{code})", R); return False
    except Exception as e:
        log(f"Order Exception: {e}", R); return False
    finally:
        order_lock = False


def build_neutral_grid(preis):
    global long_grid, short_grid, center_price, grid_aktiv
    center_price = preis; long_grid = []; short_grid = []
    for i in range(1, GRID_LEVELS+1):
        entry = round(preis * (1 - i * GRID_STEP/100))
        long_grid.append({"entry":entry,"tp":round(entry*(1+GRID_PROFIT/100)),"filled":False,"open_time":0.0})
    for i in range(1, GRID_LEVELS+1):
        entry = round(preis * (1 + i * GRID_STEP/100))
        short_grid.append({"entry":entry,"tp":round(entry*(1-GRID_PROFIT/100)),"filled":False,"open_time":0.0})
    grid_aktiv = True
    global last_order_t
    last_order_t = time.time()  # Verhindert sofortige doppelte Orders
    sl_u = fmt(center_price * (1 - (GRID_LEVELS*GRID_STEP + SL_PCT)/100))
    sl_o = fmt(center_price * (1 + (GRID_LEVELS*GRID_STEP + SL_PCT)/100))
    log(f"NEUTRAL GRID @ {fmt(preis)}", C)
    log(f"📗 LONG:  {' | '.join(fmt(lv['entry']) for lv in long_grid)}", G)
    log(f"📕 SHORT: {' | '.join(fmt(lv['entry']) for lv in short_grid)}", R)
    log(f"SL: {sl_u} <-> {sl_o}", Y)


def reset_grid():
    global long_grid, short_grid, center_price, grid_aktiv
    long_grid=[]; short_grid=[]; center_price=None; grid_aktiv=False


def close_all(preis, reason=""):
    global total_pnl, wins, losses
    n_long=long_offen(); n_short=short_offen()
    if n_long==0 and n_short==0: reset_grid(); return
    log(f"⛔ {reason} — Schließe {n_long}L + {n_short}S", R)
    if n_long > 0:
        ok = place_order(False, preis, round(n_long*ORDER_SIZE,4), sl_order=True)
        if ok is True:
            for lv in long_grid:
                if lv["filled"]:
                    pnl=(preis-lv["entry"])/lv["entry"]*100
                    total_pnl+=pnl
                    if pnl>=0: wins+=1
                    else: losses+=1
    if n_short > 0:
        ok = place_order(True, preis, round(n_short*ORDER_SIZE,4), sl_order=True)
        if ok is True:
            for lv in short_grid:
                if lv["filled"]:
                    pnl=(lv["entry"]-preis)/lv["entry"]*100
                    total_pnl+=pnl
                    if pnl>=0: wins+=1
                    else: losses+=1
    log(f"P&L: {total_pnl:+.2f}% | {wins}W {losses}L", G if total_pnl>=0 else R)
    reset_grid()


def loop():
    global wins, losses, total_pnl
    tick = 0
    log(f"Neutral Grid Bot | {'DRY' if DRY_RUN else 'LIVE'}", C)

    while True:
        try:
            tick += 1
            preis = get_preis()
            if not preis: log("Kein Preis...", Y); time.sleep(INTERVAL); continue

            if not grid_aktiv:
                build_neutral_grid(preis)
                time.sleep(INTERVAL); continue

            sl_u = center_price * (1 - (GRID_LEVELS*GRID_STEP + SL_PCT)/100)
            sl_o = center_price * (1 + (GRID_LEVELS*GRID_STEP + SL_PCT)/100)

            if preis <= sl_u:
                close_all(preis, "SL UNTEN"); time.sleep(INTERVAL); continue
            if preis >= sl_o:
                close_all(preis, "SL OBEN"); time.sleep(INTERVAL); continue

            just_acted = False
            # 3 Sekunden Wartezeit zwischen Orders — kein Doppelkauf
            order_bereit = (time.time() - last_order_t) >= 3

            # LONG LEVELS
            if order_bereit:
                for lv in long_grid:
                    if not lv["filled"] and preis <= lv["entry"]*1.001:
                        log(f"🟢 LONG @ {fmt(lv['entry'])} TP:{fmt(lv['tp'])}", G)
                        lv["filled"]=True; lv["open_time"]=time.time()
                        ok = place_order(True, preis, ORDER_SIZE)
                        if not ok and ok != "NO_MARGIN":
                            lv["filled"]=False; lv["open_time"]=0.0
                        elif ok == "NO_MARGIN":
                            lv["open_time"]=-1
                        just_acted=True; break

            # LONG TP — erst Order, dann State zurücksetzen
            if not just_acted:
                for lv in long_grid:
                    if not lv["filled"] or lv["open_time"]<=0: continue
                    if (time.time()-lv["open_time"])<30: continue
                    if preis >= lv["tp"]:
                        ok = place_order(False, preis, ORDER_SIZE)
                        if ok is True:
                            pnl=(preis-lv["entry"])/lv["entry"]*100
                            lv["filled"]=False; lv["open_time"]=0.0
                            total_pnl+=pnl; wins+=1
                            log(f"✅ LONG TP +{pnl:.2f}% | Total:{total_pnl:+.2f}% | {wins}W", G)
                        just_acted=True; break

            # SHORT LEVELS
            if not just_acted and order_bereit:
                for lv in short_grid:
                    if not lv["filled"] and preis >= lv["entry"]*0.999:
                        log(f"🔴 SHORT @ {fmt(lv['entry'])} TP:{fmt(lv['tp'])}", R)
                        lv["filled"]=True; lv["open_time"]=time.time()
                        ok = place_order(False, preis, ORDER_SIZE)
                        if not ok and ok != "NO_MARGIN":
                            lv["filled"]=False; lv["open_time"]=0.0
                        elif ok == "NO_MARGIN":
                            lv["open_time"]=-1
                        just_acted=True; break

            # SHORT TP — erst Order, dann State zurücksetzen
            if not just_acted:
                for lv in short_grid:
                    if not lv["filled"] or lv["open_time"]<=0: continue
                    if (time.time()-lv["open_time"])<30: continue
                    if preis <= lv["tp"]:
                        ok = place_order(True, preis, ORDER_SIZE)
                        if ok is True:
                            pnl=(lv["entry"]-preis)/lv["entry"]*100
                            lv["filled"]=False; lv["open_time"]=0.0
                            total_pnl+=pnl; wins+=1
                            log(f"✅ SHORT TP +{pnl:.2f}% | Total:{total_pnl:+.2f}% | {wins}W", G)
                        just_acted=True; break

            if tick % 2 == 0:
                log(f"BTC {fmt(preis)} | L:{long_offen()}/{GRID_LEVELS} S:{short_offen()}/{GRID_LEVELS} | "
                    f"{wins}W {losses}L P&L:{total_pnl:+.2f}%")

            time.sleep(INTERVAL)

        except KeyboardInterrupt:
            log("Bot gestoppt.", Y)
            if long_offen()>0 or short_offen()>0:
                log(f"⚠️ {long_offen()}L + {short_offen()}S offen — manuell schließen!", R)
            break
        except Exception as e:
            log(f"Fehler: {e}", R); time.sleep(5)


def main():
    print(f"\n{B}{C}  ╔══════════════════════════════════════════╗")
    print(f"  ║   Nado.xyz — Neutral Grid Bot            ║")
    print(f"  ║   LONG unten + SHORT oben gleichzeitig   ║")
    print(f"  ╚══════════════════════════════════════════╝{X}\n")
    print(f"  Wallet:  {WALLET_ADDR[:12]}...{WALLET_ADDR[-6:]}")
    print(f"  Step:    {GRID_STEP}% | Levels: {GRID_LEVELS}L+{GRID_LEVELS}S | TP: {GRID_PROFIT}%")
    print(f"  SL:      {SL_PCT}% nach letztem Level")
    modus = f"{Y}DRY RUN{X}" if DRY_RUN else f"{R}{B}LIVE{X}"
    print(f"  Modus:   {modus}\n")
    loop()

if __name__ == "__main__":
    main()
