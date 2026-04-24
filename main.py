"""
Nado.xyz Grid Trading Bot
==========================
Strategie: Grid Trading
- Bot kauft günstig und verkauft teurer automatisch
- Macht viel Volumen für Nado Airdrop
- Kein Trend-Filter nötig
- Funktioniert in jedem Markt

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
WALLET_ADDR  = "0xc15263578ce7fd6290f56Ab78a23D3b6C653B28C"
SIGNER_KEY   = "0x8097b0ec439aa91bd4f3c3ea79735be6688ce00589bbcd0e3dea2ab596580a4d"

PRODUCT_ID   = 2
CHAIN_ID     = 57073
GATEWAY      = "https://gateway.prod.nado.xyz/v1"
ARCHIVE      = "https://archive.prod.nado.xyz/v1"
HEADERS      = {"Accept-Encoding": "gzip", "Content-Type": "application/json"}

ORDER_SIZE   = 0.0015   # BTC pro Grid Order
GRID_LEVELS  = 5        # Anzahl Grid Levels
GRID_RANGE   = 2.0      # % Range über und unter aktuellem Preis
GRID_PROFIT  = 0.4      # % Gewinn pro Grid Level
INTERVAL     = 30       # Sekunden
DRY_RUN      = False
STATE_FILE   = "grid_state.json"
# ═══════════════════════════════════════════════════════════

grid             = []    # Liste aller Grid Levels
filled_buys      = {}    # Gekaufte Levels die auf Verkauf warten
grid_start_preis = 0     # Startpreis beim Grid Aufbau
trades      = wins = 0
total_pnl   = 0.0


def ts():    return datetime.now().strftime("%H:%M:%S")
def log(m, c=""): print(f"{c}[{ts()}] {m}{X}" if c else f"[{ts()}] {m}"); sys.stdout.flush()
def fmt(x):
    try: return f"${float(x):,.2f}"
    except: return "?"


# ─── STATE ────────────────────────────────────────────────

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"grid": grid, "filled_buys": filled_buys, "grid_start_preis": grid_start_preis,
                      "trades": trades, "wins": wins, "total_pnl": total_pnl}, f)
    except: pass

def load_state():
    global grid, filled_buys, trades, wins, total_pnl
    try:
        if os.path.exists(STATE_FILE):
            d = json.load(open(STATE_FILE))
            grid             = d.get("grid", [])
            filled_buys      = d.get("filled_buys", {})
            grid_start_preis = d.get("grid_start_preis", 0)
            trades      = d.get("trades", 0)
            wins        = d.get("wins", 0)
            total_pnl   = d.get("total_pnl", 0.0)
            if grid:
                log(f"State: Grid {fmt(grid[0])} - {fmt(grid[-1])} | {len(filled_buys)} offene Buys | {trades}T P&L:{total_pnl:+.4f}%", Y)
    except Exception as e:
        log(f"State Fehler: {e}", Y)


# ─── API ──────────────────────────────────────────────────

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
        # Limit Order mit 0.1% Slippage für schnelle Füllung
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
        log(f"❌ {d.get('error','')} (Code:{d.get('error_code','')})", R); return False
    except Exception as e:
        log(f"Order Exception: {e}", R); return False


# ─── GRID ─────────────────────────────────────────────────

def build_grid(preis):
    """Baut Grid Levels um aktuellen Preis.
    Levels UNTER Startpreis = BUY Zone
    Levels UBER Startpreis  = SELL Zone
    """
    global grid, grid_start_preis
    step  = preis * (GRID_RANGE / 100) / GRID_LEVELS
    lower = preis * (1 - GRID_RANGE/100)
    grid  = [round(lower + i * step) for i in range(GRID_LEVELS * 2 + 1)]
    grid_start_preis = round(preis)
    log(f"Grid gebaut @ {fmt(preis)} | {fmt(grid[0])} bis {fmt(grid[-1])} | Step:{fmt(step)}", C)
    log(f"BUY Zone: unter {fmt(grid_start_preis)} | SELL Zone: über {fmt(grid_start_preis)}", C)
    save_state()

def check_grid(preis):
    """Prüft ob Preis ein Grid Level erreicht hat."""
    global filled_buys, trades, wins, total_pnl

    if not grid: return

    for level in grid:
        level_key = str(level)

        # BUY: nur bei Levels UNTER dem Startpreis kaufen
        if level <= grid_start_preis and preis <= level * 1.001 and preis >= level * 0.999:
            if level_key not in filled_buys:
                log(f"🟢 GRID BUY @ {fmt(level)} (Preis: {fmt(preis)})", G)
                ok = place_order(True, level)
                if ok:
                    sell_price = level * (1 + GRID_PROFIT/100)
                    filled_buys[level_key] = {
                        "buy_price":  level,
                        "sell_price": round(sell_price),
                    }
                    trades += 1
                    save_state()

        # SELL: Preis steigt auf Verkaufslevel — nur EIN Sell pro Tick
        for buy_key, buy_info in list(filled_buys.items()):
            sell_price = buy_info["sell_price"]
            if preis >= sell_price * 0.999:
                log(f"🔴 GRID SELL @ {fmt(sell_price)} (Gekauft @ {fmt(buy_info['buy_price'])})", R)
                ok = place_order(False, sell_price, reduce_only=True)
                if ok:
                    pnl = GRID_PROFIT
                    total_pnl += pnl
                    wins += 1
                    log(f"✅ Grid Profit: +{pnl}% | Total P&L: {total_pnl:+.2f}% | {wins} Wins", G)
                    del filled_buys[buy_key]
                    save_state()
                    break  # Nur ein Sell pro Tick — kein Code 2064 mehr
                elif "2064" in str(ok):
                    # Position existiert nicht mehr — State bereinigen
                    del filled_buys[buy_key]
                    save_state()
                    break


# ─── HAUPT LOOP ───────────────────────────────────────────

def loop():
    global grid
    tick = 0
    log(f"Grid Bot | BTC | Range:±{GRID_RANGE}% | Levels:{GRID_LEVELS} | Profit/Level:{GRID_PROFIT}% | {'DRY RUN' if DRY_RUN else 'LIVE'}", C)

    while True:
        try:
            tick += 1
            preis = get_preis()

            if not preis:
                log("Kein Preis — warte...", Y)
                time.sleep(INTERVAL); continue

            # Grid aufbauen wenn noch keins existiert oder Preis zu weit vom Grid entfernt
            if not grid or preis < grid[0] * 0.97 or preis > grid[-1] * 1.03:
                log(f"Grid wird neu aufgebaut @ {fmt(preis)}", Y)
                filled_buys.clear()
                build_grid(preis)

            # Grid prüfen
            check_grid(preis)

            # Status anzeigen
            if tick % 3 == 0:
                grid_min = fmt(grid[0])  if grid else "?"
                grid_max = fmt(grid[-1]) if grid else "?"
                log(f"BTC {fmt(preis)} | Grid: {grid_min}-{grid_max} | Offene Buys: {len(filled_buys)} | Wins: {wins} | P&L: {total_pnl:+.2f}%")

            time.sleep(INTERVAL)

        except KeyboardInterrupt:
            log("Bot gestoppt.", Y)
            if filled_buys:
                log(f"⚠️ {len(filled_buys)} offene Buy Orders — bitte manuell auf app.nado.xyz schließen!", R)
            break
        except Exception as e:
            log(f"Fehler: {e}", R); time.sleep(5)


def main():
    print(f"\n{B}{C}  ╔══════════════════════════════════════════╗")
    print(f"  ║      Nado.xyz — Grid Trading Bot         ║")
    print(f"  ║   Kaufe günstig, verkaufe teurer         ║")
    print(f"  ╚══════════════════════════════════════════╝{X}\n")
    print(f"  Wallet:       {WALLET_ADDR[:12]}...{WALLET_ADDR[-6:]}")
    print(f"  Grid Range:   ±{GRID_RANGE}%")
    print(f"  Grid Levels:  {GRID_LEVELS}")
    print(f"  Profit/Level: +{GRID_PROFIT}%")
    print(f"  Order Size:   {ORDER_SIZE} BTC")
    modus = f"{Y}DRY RUN{X}" if DRY_RUN else f"{R}{B}LIVE{X}"
    print(f"  Modus:        {modus}\n")
    load_state()
    loop()


if __name__ == "__main__":
    main()
