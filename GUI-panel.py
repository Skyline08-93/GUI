import ccxt
import time
import os
import requests
from datetime import datetime
from colorama import Fore, init
from dotenv import load_dotenv

init(autoreset=True)
load_dotenv()

# 📌 Настройки
API_KEY = os.getenv('API_KEY')
SECRET = os.getenv('SECRET')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('CHAT_ID')

FEE = 0.001
MIN_PROFIT_PCT = 0.5
MAX_PROFIT_PCT = 10
MIN_LIQUIDITY = 100
TRADE_USD = 100

STABLES = {'USDT', 'USDC', 'DAI', 'USDE', 'USDR', 'TUSD', 'BUSD'}
BASE_COINS = {'BTC', 'ETH', 'BNB', 'SOL'}

def get_symbol_type(symbol):
    if symbol in STABLES:
        return 'stable'
    elif symbol in BASE_COINS:
        return 'base'
    return 'alt'

bybit = ccxt.bybit({
    'apiKey': API_KEY,
    'secret': SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'spot'}
})

markets = bybit.load_markets()
all_symbols = list(markets.keys())

unique_symbols = set()
for sym in all_symbols:
    if '/' in sym:
        base, quote = sym.split('/')
        unique_symbols.add(base)
        unique_symbols.add(quote)

symbol_types = {s: get_symbol_type(s) for s in unique_symbols}

def get_orderbook(symbol):
    try:
        return bybit.fetch_order_book(symbol)
    except:
        return {'asks': [], 'bids': []}

def get_best_price(book, side, amount_needed):
    total_cost = 0
    total_qty = 0
    for price, qty in book.get(side, []):
        cost = price * qty
        if total_cost + cost >= amount_needed:
            partial = (amount_needed - total_cost) / price
            total_qty += partial
            total_cost += partial * price
            break
        else:
            total_qty += qty
            total_cost += cost
    if total_qty == 0:
        return None, 0
    return total_cost / total_qty, total_qty

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, data={'chat_id': CHAT_ID, 'text': text, 'parse_mode': 'HTML'})

def calc_triangle(p1, p2, p3, invert2):
    coinA = p1.split('/')[0]
    coinB = p2.split('/')[0] if not invert2 else p2.split('/')[1]
    coinC = p3.split('/')[1]

    typeA = symbol_types.get(coinA, 'alt')
    typeB = symbol_types.get(coinB, 'alt')
    typeC = symbol_types.get(coinC, 'alt')

    # Шаг 1 — купить A за USDT
    book1 = get_orderbook(p1)
    p1_price, qty_a = get_best_price(book1, 'asks', TRADE_USD)
    if not p1_price:
        return
    amount_a = TRADE_USD / p1_price * (1 - FEE)
    liq1 = book1['asks'][0][0] * book1['asks'][0][1] if book1['asks'] else 0

    # Шаг 2 — обмен A на B
    book2 = get_orderbook(p2)
    if not book2['asks'] or not book2['bids']:
        return

    if typeA == 'stable':
        p2_price, _ = get_best_price(book2, 'asks', amount_a)
        if not p2_price: return
        amount_b = amount_a / p2_price if invert2 else amount_a * p2_price
    elif typeB == 'stable':
        p2_price, _ = get_best_price(book2, 'bids', amount_a)
        if not p2_price: return
        amount_b = amount_a * p2_price if invert2 else amount_a / p2_price
    else:
        p2_price, _ = get_best_price(book2, 'asks', amount_a)
        if not p2_price: return
        amount_b = amount_a / p2_price if invert2 else amount_a * p2_price

    amount_b *= (1 - FEE)
    liq2 = book2['asks'][0][0] * book2['asks'][0][1] if book2['asks'] else 0

    # Шаг 3 — продать B за USDT
    book3 = get_orderbook(p3)
    p3_price, _ = get_best_price(book3, 'bids', amount_b)
    if not p3_price:
        return
    total_usdt = amount_b * p3_price * (1 - FEE)
    liq3 = book3['bids'][0][0] * book3['bids'][0][1] if book3['bids'] else 0

    min_liq = min(liq1, liq2, liq3)
    if min_liq < MIN_LIQUIDITY:
        return

    profit = total_usdt - TRADE_USD
    pct = profit / TRADE_USD * 100

    if pct >= MIN_PROFIT_PCT and pct <= MAX_PROFIT_PCT:
        msg = (
            f"<b>[{datetime.now().strftime('%H:%M:%S')}] Найден маршрут</b>\n"
            f"1. {p1} по <b>{p1_price:.6f}</b> (${liq1:,.0f})\n"
            f"2. {p2} по <b>{p2_price:.6f}</b> (${liq2:,.0f})\n"
            f"3. {p3} по <b>{p3_price:.6f}</b> (${liq3:,.0f})\n\n"
            f"💰 Прибыль: <b>{profit:.2f} USDT</b>\n"
            f"📈 Спред: <b>{pct:.2f}%</b>\n"
            f"💧 Мин. ликвидность: <b>{min_liq:,.0f} USDT</b>"
        )
        print(Fore.GREEN + msg)
        send_telegram_message(msg)

def build_routes():
    routes = []
    stable_pairs = [p for p in all_symbols if any(p.endswith(f'/{s}') for s in STABLES)]
    coins = set()
    for p in stable_pairs:
        base, quote = p.split('/')
        coins.add(base)
        coins.add(quote)

    # Варианты маршрутов (1, 2, 3)
    for s1 in STABLES:
        for s2 in STABLES:
            if s1 == s2: continue
            for coin in coins:
                p1 = f"{coin}/{s1}" if f"{coin}/{s1}" in markets else None
                p2a = f"{coin}/{s2}" if f"{coin}/{s2}" in markets else None
                p2b = f"{s2}/{coin}" if f"{s2}/{coin}" in markets else None
                p3 = f"{s2}/{s1}" if f"{s2}/{s1}" in markets else None

                if p1 and p2a and p3:
                    routes.append((p1, p2a, p3, False))
                elif p1 and p2b and p3:
                    routes.append((p1, p2b, p3, True))
    return routes

def run():
    print("🔁 Загрузка маршрутов...")
    routes = build_routes()
    print(f"📌 Найдено маршрутов: {len(routes)}\n")
    while True:
        for i, (p1, p2, p3, inv) in enumerate(routes):
            print(Fore.CYAN + f"[{i+1}/{len(routes)}] Проверка: {p1} → {p2} → {p3}")
            calc_triangle(p1, p2, p3, inv)
            time.sleep(0.1)
        print(Fore.YELLOW + "♻️ Повтор через 10 сек...\n")
        time.sleep(10)

if __name__ == "__main__":
    run()