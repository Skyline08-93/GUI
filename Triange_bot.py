import ccxt
import time
import requests
import os
from datetime import datetime

# === Константы и настройки ===
FEE = 0.001
MIN_PROFIT_PCT = 0.5
MIN_LIQUIDITY = 100
MAX_LIQUIDITY = 500000
TRADE_USD = 100

# === Переменные окружения (Railway .env) ===
API_KEY = os.environ.get('API_KEY')
SECRET = os.environ.get('SECRET')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
CHAT_ID = os.environ.get('CHAT_ID')

# Проверка ключей
if not API_KEY or not SECRET:
    raise Exception("❌ API_KEY и SECRET обязательны. Установи их в Railway → Variables")

# === Типы монет ===
STABLES = {'USDT', 'USDC', 'DAI', 'USDE', 'USDR', 'TUSD', 'BUSD'}
BASE_COINS = {'BTC', 'ETH', 'BNB', 'SOL'}

def get_symbol_type(symbol):
    if symbol in STABLES:
        return 'stable'
    elif symbol in BASE_COINS:
        return 'base'
    else:
        return 'alt'

# === Telegram ===
def send_telegram_message(text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={'chat_id': CHAT_ID, 'text': text, 'parse_mode': 'HTML'})
    except:
        pass

# === Инициализация Bybit ===
bybit = ccxt.bybit({
    'apiKey': API_KEY,
    'secret': SECRET,
    'enableRateLimit': True,
    'timeout': 15000,
    'options': {'defaultType': 'spot'}
})

# === Получение рынков и маршрутов ===
markets = bybit.load_markets({'type': 'spot'})
all_symbols = list(markets.keys())

unique_symbols = set()
for s in all_symbols:
    if '/' in s:
        base, quote = s.split('/')
        unique_symbols.add(base)
        unique_symbols.add(quote)

symbol_types = {sym: get_symbol_type(sym) for sym in unique_symbols}

base_symbols = {s.split('/')[0] for s in all_symbols if any(s.endswith(f'/{sfx}') for sfx in STABLES)}

routes = []
for s in STABLES:
    for a in base_symbols:
        for b in base_symbols:
            if a == b:
                continue
            pair1 = f"{a}/{s}"
            if pair1 not in markets:
                continue
            if f"{b}/{a}" in markets:
                pair2 = f"{b}/{a}"
                invert2 = True
            elif f"{a}/{b}" in markets:
                pair2 = f"{a}/{b}"
                invert2 = False
            else:
                continue
            pair3 = f"{b}/{s}"
            if pair3 in markets:
                routes.append((pair1, pair2, pair3, invert2))

print(f"🔁 Всего маршрутов найдено: {len(routes)}")

# === Стаканы и цены ===
def get_orderbook(symbol):
    try:
        return bybit.fetch_order_book(symbol)
    except:
        return {'asks': [], 'bids': []}

def get_best_price(book, side, amount_needed):
    total = 0
    qty = 0
    for price, volume in book[side]:
        deal = price * volume
        if total + deal >= amount_needed:
            partial = (amount_needed - total) / price
            qty += partial
            total += partial * price
            break
        total += deal
        qty += volume
    if qty == 0:
        return None, 0
    avg_price = total / qty
    return avg_price, total

def get_second_leg_price(book_func, coinA, coinB, amount_a, markets, fee=FEE):
    """
    Определяет, существует ли пара coinA/coinB или coinB/coinA,
    затем возвращает количество coinB, использованную пару, сторону стакана и цену.
    """
    pair_ab = f"{coinA}/{coinB}"
    pair_ba = f"{coinB}/{coinA}"

    if pair_ab in markets:
        book = book_func(pair_ab)
        if not book or not book['bids']:
            return None
        price, _ = get_best_price(book, 'bids', amount_a)
        if not price:
            return None
        amount_b = amount_a * price * (1 - fee)
        return amount_b, pair_ab, 'bids', price

    elif pair_ba in markets:
        book = book_func(pair_ba)
        if not book or not book['asks']:
            return None
        price, _ = get_best_price(book, 'asks', amount_a)
        if not price:
            return None
        amount_b = amount_a / price * (1 - fee)
        return amount_b, pair_ba, 'asks', price

    else:
        # Пары нет на рынке
        return None

def calc_triangle(p1, p2, p3, invert2):
    coinA = p1.split('/')[0]  # например, монета A (например, INJ)
    quote = p1.split('/')[1]  # например, USDT

    # Вторая монета для второго шага — та, что в p2
    # Но порядок надо определить динамически, поэтому выделим по разделителю "/"
    parts2 = p2.split('/')
    coinB_candidates = set(parts2)

    # coinB — это монета в p2, которая не равна coinA
    coinB = (coinB_candidates - {coinA}).pop()

    # Получаем стакан для первой пары
    book1 = get_orderbook(p1)
    if not book1 or not book1['asks']:
        return
    p1_price, spent_usdt = get_best_price(book1, 'asks', TRADE_USD)
    if not p1_price:
        return
    amount_a = TRADE_USD / p1_price * (1 - FEE)
    liq1 = book1['asks'][0][0] * book1['asks'][0][1]

    # Используем универсальную функцию для второго шага
    second_leg = get_second_leg_price(get_orderbook, coinA, coinB, amount_a, markets, fee=FEE)
    if not second_leg:
        return
    amount_b, used_pair, side_used, p2_price = second_leg
    liq2_side = 'asks' if side_used == 'asks' else 'bids'
    liq2 = get_orderbook(used_pair)[liq2_side][0][0] * get_orderbook(used_pair)[liq2_side][0][1]

    # Третий шаг — продать coinB за quote (USDT или stable)
    book3 = get_orderbook(p3)
    if not book3 or not book3['bids']:
        return
    p3_price, _ = get_best_price(book3, 'bids', amount_b)
    if not p3_price:
        return
    total_usdt = amount_b * p3_price * (1 - FEE)
    liq3 = book3['bids'][0][0] * book3['bids'][0][1]

    min_liq = min(liq1, liq2, liq3)
    if min_liq < MIN_LIQUIDITY or min_liq > MAX_LIQUIDITY:
        return

    if total_usdt <= 0 or total_usdt > 10 * TRADE_USD:
        return

    profit = total_usdt - TRADE_USD
    pct = (profit / TRADE_USD) * 100

    if pct >= MIN_PROFIT_PCT:
        now = datetime.now().strftime('%H:%M:%S')
        msg = (
            f"<b>[{now}]</b>\n"
            f"🟥 1. {p1} - {p1_price:.6f}, стакан: ${liq1:,.0f}\n"
            f"{'🟢' if side_used == 'bids' else '🟥'} 2. {used_pair} - {p2_price:.6f}, стакан: ${liq2:,.0f}\n"
            f"🟢 3. {p3} - {p3_price:.6f}, стакан: ${liq3:,.0f}\n\n"
            f"💰 Прибыль: <b>{profit:.2f} USDT</b>\n"
            f"📈 Спред: <b>{pct:.2f}%</b>\n"
            f"💧 Ликвидность: <b>{min_liq:,.0f} USDT</b>"
        )
        send_telegram_message(msg)

    return profit, pct, min_liq

# === Цикл ===
def main():
    while True:
        results = []
        for i, (p1, p2, p3, invert2) in enumerate(routes):
            print(f"🔍 [{i+1}/{len(routes)}] {p1} → {p2} → {p3}")
            try:
                res = calc_triangle(p1, p2, p3, invert2)
                if res:
                    results.append((p1, p2, p3, *res))
            except Exception as e:
                print(f"⚠️ Ошибка: {e}")
            time.sleep(0.1)

        results = [r for r in results if 0 < r[4] < 10]
        results.sort(key=lambda x: x[4], reverse=True)

        print("\033c")  # очистка экрана
        print("📈 ТОП-10 прибыльных маршрутов:")
        for i, (a, b, c, profit, pct, vol) in enumerate(results[:10]):
            print(f"{i+1}. {a} → {b} → {c}")
            print(f"   🔹 Прибыль: {profit:.4f} USDT | Спред: {pct:.2f}% | Ликвидность: {vol:,.0f} USDT\n")

        print("♻️ Обновление через 10 секунд...")
        time.sleep(10)

if __name__ == '__main__':
    main()