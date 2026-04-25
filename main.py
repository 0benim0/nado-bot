"""
Nado.xyz Grid Trading Bot
Kaufe günstig, verkaufe teurer.
SIGNER_KEY = 1-Click Trading Key (app.nado.xyz → Settings)
"""

import time, random, requests, sys, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from datetime import datetime

try:
    from colorama import init, Fore, Style
    init(autoreset=True)
    G=Fore.GREEN; R=Fore.RED; Y=Fore.YELLOW; C=Fore.CYAN
    X=Style.RESET_ALL; B=Style.BRIGHT
except:
    G=R=Y=C=X=B=""

# ═══════════════════════════════════════════════════════════
WALLET_ADDR = "0xc15263578ce7fd6290f56Ab78a23D3b6C653B28C"
SIGNER_KEY  = "0x8097b0ec439aa91bd4f3c3ea79735be6688ce00589bbcd0e3dea2ab596580a4d"
SUBACCOUNT  = "0xc15263578ce7fd6290f56ab78a23d3b6c653b28c64656661756c740000000000"

PRODUCT_ID  = 2
CHAIN_ID    = 57073
GATEWAY     = "https://gateway.prod.nado.xyz/v1"
HEADERS     = {"Accept-Encoding": "gzip", "Content-Type": "application/json"}

ORDER_SIZE  = 0.0015  # BTC pro Level
GRID_LEVELS = 5
GRID_STEP   = 0.4     # % Abstand zwischen Levels
GRID_PROFIT = 0.4     # % Gewinn pro Level
INTERVAL    = 30
DRY_RUN     = False
# ═══════════════════════════════════════════════════════════

# Jedes Level hat: buy_price, sell_price, filled, bought_at_time
grid         = []    # Liste von Dicts
wins         = 0
total_pnl    = 0.0
prev_preis   = None
just_bought  = False  # Verhindert Sell im gleichen Tick wie Buy


def ts():
    return datetime.now().strftime("%H:%M:%S")

def log(m, c=""):
    print(f"{c}[{ts()}] {m}{X}" if c else f"[{ts()}] {m}")
    sys.stdout.flush()

def fmt(x):
    try: return f"${float(x):,.2f}"
    except: return "?"


# ─── API ──────────────────────────────────────────────────

def get_preis():
    try:
        r = requests.get(
            f"{GATEWAY}/query?type=all_products",
            headers={"Accept-Encoding": "gzip"},
            timeout=15, verify=False
        )
        data = r.json().get("data", r.json())
        for p in data.get("perp_products", []):
            if int(p.get("product_id", -1)) == PRODUCT_ID:
                px = float(p.get("oracle_price_x18") or p.get("mark_price_x18") or 0)
                if px > 0: return px / 1e18
    except Exception as e:
        log(f"Preis Fehler: {e}", Y)
    return None


def get_nado_size():
    try:
        r = requests.get(
            f"{GATEWAY}/query?type=subaccount_info&subaccount={SUBACCOUNT}",
            headers={"Accept-Encoding": "gzip"},
            timeout=15, verify=False
        )
        data = r.json().get("data", {})
        for pb in data.get("perp_balances", []):
            if pb.get("product_id") == PRODUCT_ID:
                return max(0.0, float(pb["balance"]["amount"]) / 1e18)
    except Exception as e:
        log(f"Nado API Fehler: {e}", Y)
    return None


# ─── ORDER ────────────────────────────────────────────────

def sender_hex():
    ab = bytes.fromhex(WALLET_ADDR.lower().replace("0x", ""))
    return "0x" + (ab + b"default".ljust(12, b"\x00")).hex()


def place_order(is_buy, price):
    """
    is_buy=True  → positive Menge  → LONG öffnen
    is_buy=False → negative Menge  → LONG schließen
    Kein reduce_only. Preis mit 0.2% Slippage gerundet.
    """
    if DRY_RUN:
        log(f"[DRY] {'BUY' if is_buy else 'SELL'} {ORDER_SIZE} BTC @ {fmt(price)}", Y)
        return True
    try:
        from eth_account import Account
        px    = round(price * (1.002 if is_buy else 0.998)) * int(1e18)
        amt   = int(ORDER_SIZE * 1e18) if is_buy else -int(ORDER_SIZE * 1e18)
        exp   = int(time.time()) + 60
        nonce = ((int(time.time() * 1000) + 5000) << 20) + random.randint(0, 99999)
        apx   = 1
        sndr  = sender_hex()
        dom = {"name": "Nado", "version": "0.0.1", "chainId": CHAIN_ID,
               "verifyingContract": f"0x{PRODUCT_ID:040x}"}
        typ = {"Order": [
            {"name": "sender",     "type": "bytes32"},
            {"name": "priceX18",   "type": "int128"},
            {"name": "amount",     "type": "int128"},
            {"name": "expiration", "type": "uint64"},
            {"name": "nonce",      "type": "uint64"},
            {"name": "appendix",   "type": "uint128"},
        ]}
        msg = {"sender": sndr, "priceX18": px, "amount": amt,
               "expiration": exp, "nonce": nonce, "appendix": apx}
        acc = Account.from_key(SIGNER_KEY)
        sig = acc.sign_typed_data(domain_data=dom, message_types=typ, message_data=msg).signature.hex()
        if not sig.startswith("0x"): sig = "0x" + sig
        pld = {"place_order": {"product_id": PRODUCT_ID, "order": {
            "sender": sndr, "priceX18": str(px), "amount": str(amt),
            "expiration": str(exp), "nonce": str(nonce), "appendix": str(apx)
        }, "signature": sig}}
        r = requests.post(f"{GATEWAY}/execute", json=pld, headers=HEADERS, timeout=15, verify=False)
        d = r.json()
        if d.get("status") == "success":
            log("✅ Order OK!", G)
            return True
        log(f"❌ {d.get('error','')} (Code:{d.get('error_code','')})", R)
        return False
    except Exception as e:
        log(f"Order Exception: {e}", R)
        return False


# ─── GRID ─────────────────────────────────────────────────

def build_grid(preis):
    global grid
    grid = []
    for i in range(1, GRID_LEVELS + 1):
        buy_p  = round(preis * (1 - i * GRID_STEP / 100))
        sell_p = round(buy_p  * (1 + GRID_PROFIT / 100))
        grid.append({
            "buy_price":    buy_p,
            "sell_price":   sell_p,
            "filled":       False,
            "bought_at":    0.0,
        })
    lvls = " | ".join(fmt(lv["buy_price"]) for lv in grid)
    log(f"Grid @ {fmt(preis)} | Levels: {lvls}", C)
    log(f"Warte bis BTC unter {fmt(grid[0]['buy_price'])} fällt...", Y)


def total_filled():
    return sum(1 for lv in grid if lv["filled"])


def total_size():
    return round(total_filled() * ORDER_SIZE, 4)


# ─── SYNC ─────────────────────────────────────────────────

def sync_nado(preis):
    """Vergleicht Bot-State mit Nado. Wartet 3 Min nach letztem Kauf."""
    now = time.time()
    for lv in grid:
        if lv["filled"] and (now - lv["bought_at"]) < 180:
            return  # Zu kürzlich gekauft

    nado = get_nado_size()
    if nado is None: return

    bot_size = total_size()
    if abs(nado - bot_size) < 0.0001: return

    log(f"Sync: Bot={bot_size:.4f} | Nado={nado:.4f}", Y)

    if nado == 0 and bot_size > 0:
        log("Nado hat keine Position — alle Levels zurücksetzen", Y)
        for lv in grid:
            lv["filled"]    = False
            lv["bought_at"] = 0.0
    elif nado < bot_size:
        # Weniger auf Nado — ein Level zurücksetzen
        for lv in reversed(grid):
            if lv["filled"]:
                lv["filled"]    = False
                lv["bought_at"] = 0.0
                break


# ─── HAUPT LOOP ───────────────────────────────────────────

def loop():
    global prev_preis, just_bought, wins, total_pnl
    tick = 0

    log(f"Bot | Step:{GRID_STEP}% | Profit:{GRID_PROFIT}% | {'DRY RUN' if DRY_RUN else 'LIVE'}", C)

    # Beim Start Grid aufbauen
    p = get_preis()
    if p: build_grid(p)

    # Bestehende Nado Position erkennen
    nado = get_nado_size()
    if nado and nado > 0 and grid:
        log(f"Bestehende Position: {nado:.4f} BTC", Y)
        for lv in grid[:round(nado / ORDER_SIZE)]:
            lv["filled"]    = True
            lv["bought_at"] = 0.0  # 0 = Sync sofort erlaubt

    while True:
        try:
            tick    += 1
            preis    = get_preis()
            just_bought = False  # Reset jeden Tick

            if not preis:
                log("Kein Preis — warte...", Y)
                time.sleep(INTERVAL)
                continue

            # Sync alle 4 Ticks (~2 Min)
            if tick % 4 == 0:
                sync_nado(preis)

            # Grid neu aufbauen wenn BTC über alle Levels gestiegen und nichts offen
            if grid and total_filled() == 0 and preis > grid[0]["buy_price"] * 1.005:
                log(f"BTC über Grid — neu aufbauen @ {fmt(preis)}", Y)
                build_grid(preis)

            falling = prev_preis is not None and preis < prev_preis

            # ── BUY: Preis fällt auf Level ────────────────
            if falling:
                for lv in grid:
                    if not lv["filled"]:
                        diff = abs(preis - lv["buy_price"]) / lv["buy_price"]
                        if preis <= lv["buy_price"] * 1.001:  # Nur wenn Preis am oder unter Level
                            log(f"🟢 BUY @ {fmt(lv['buy_price'])} | TP: {fmt(lv['sell_price'])}", G)
                            ok = place_order(True, preis)
                            if ok:
                                lv["filled"]    = True
                                lv["bought_at"] = time.time()
                                just_bought     = True
                            break  # 1 Buy pro Tick

            # ── SELL: Preis steigt auf Sell Level ─────────
            if not just_bought:  # Nie im gleichen Tick wie Buy
                for lv in grid:
                    if lv["filled"]:
                        # Mindestens 60 Sek nach Kauf warten
                        if (time.time() - lv["bought_at"]) < 60:
                            continue
                        if preis >= lv["sell_price"]:  # Exakt am Sell Level
                            log(f"🔴 SELL @ {fmt(lv['sell_price'])} | Gekauft @ {fmt(lv['buy_price'])}", R)
                            ok = place_order(False, preis)
                            if ok:
                                lv["filled"]    = False
                                lv["bought_at"] = 0.0
                                total_pnl      += GRID_PROFIT
                                wins           += 1
                                log(f"✅ +{GRID_PROFIT}% | Total:{total_pnl:+.2f}% | {wins}W", G)
                            break  # 1 Sell pro Tick

            prev_preis = preis

            # Status
            if tick % 2 == 0:
                log(f"BTC {fmt(preis)} | Offen:{total_filled()}/{GRID_LEVELS} | Pos:{total_size():.4f} BTC | {wins}W | P&L:{total_pnl:+.2f}%")

            time.sleep(INTERVAL)

        except KeyboardInterrupt:
            log("Bot gestoppt.", Y)
            if total_size() > 0:
                log(f"⚠️ {total_size():.4f} BTC offen — manuell auf app.nado.xyz schließen!", R)
            break
        except Exception as e:
            log(f"Fehler: {e}", R)
            time.sleep(5)


def main():
    print(f"\n{B}{C}  ╔══════════════════════════════════════════╗")
    print(f"  ║      Nado.xyz — Grid Trading Bot         ║")
    print(f"  ║   Kaufe günstig, verkaufe teurer         ║")
    print(f"  ╚══════════════════════════════════════════╝{X}\n")
    print(f"  Wallet:  {WALLET_ADDR[:12]}...{WALLET_ADDR[-6:]}")
    print(f"  Step:    {GRID_STEP}% | Levels: {GRID_LEVELS} | Profit: +{GRID_PROFIT}%")
    print(f"  Size:    {ORDER_SIZE} BTC pro Level")
    modus = f"{Y}DRY RUN{X}" if DRY_RUN else f"{R}{B}LIVE{X}"
    print(f"  Modus:   {modus}\n")
    loop()


if __name__ == "__main__":
    main()
