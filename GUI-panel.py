import os
import time
import threading
import ccxt
from telegram import (
    Bot,
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Updater,
    CommandHandler,
    CallbackQueryHandler,
    CallbackContext,
)

API_KEY = os.getenv('BYBIT_API_KEY', '')
API_SECRET = os.getenv('BYBIT_SECRET', '')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('CHAT_ID')

FEE = 0.001
MIN_PROFIT_PCT = 0.5
MIN_LIQUIDITY = 100
MAX_LIQUIDITY = 500000
TRADE_USD = 100

# Bybit client
exchange = ccxt.bybit({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True,
    'timeout': 15000,
    'options': {'defaultType': 'spot'}
})

bot = Bot(token=TELEGRAM_TOKEN)
updater = Updater(token=TELEGRAM_TOKEN)

running = False
worker_thread = None

# Загрузка рынков
markets = exchange.load_markets()
all_symbols = list(markets.keys())
STABLES = {'USDT', 'USDC', 'DAI', 'USDE', 'USDR', 'TUSD', 'BUSD'}

# Функция для получения типа монеты
def get_coin_type(coin):
    if coin in STABLES:
        return 'stable'
    elif coin in {'BTC', 'ETH', 'BNB', 'SOL'}:
        return 'base'
    else:
        return 'alt'

symbol_types = {}
unique_coins = set()
for symbol in all_symbols:
    base, quote = symbol.split('/')
    unique_coins.add(base)
    unique_coins.add(quote)
for coin in unique_coins:
    symbol_types[coin] = get_coin_type(coin)

# Построение маршрутов (треугольников)
def build_routes():
    routes = []
    for stable in STABLES:
        # пары с этим stable
        coins_with_stable = [s.split('/')[0] for s in all_symbols if s.endswith('/' + stable)]
        for a in coins_with_stable:
            for b in coins_with_stable:
                if a == b:
                    continue
                pair1 = f"{a}/{stable}"
                if pair1 not in markets:
                    continue
                # Найдем pair2 между a и b
                invert2 = False
                if f"{b}/{a}" in markets:
                    pair2 = f"{b}/{a}"
                    invert2 = True
                elif f"{a}/{b}" in markets:
                    pair2 = f"{a}/{b}"
                else:
                    continue
                pair3 = f"{b}/{stable}"
                if pair3 not in markets:
                    continue
                routes.append((pair1, pair2, pair3, invert2))
    return routes

routes = build_routes()

def fetch_orderbook(symbol):
    try:
        return exchange.fetch_order_book(symbol)
    except Exception:
        return {'bids': [], 'asks': []}

def get_best_price(book, side, amount_needed):
    total = 0
    qty = 0
    for price, vol in book.get(side, []):
        deal = price * vol
        if total + deal >= amount_needed:
            partial = (amount_needed - total) / price
            qty += partial
            total += partial * price
            break
        total += deal
        qty += vol
    if qty == 0:
        return None, 0
    avg_price = total / qty
    return avg_price, total

def calc_triangle(p1, p2, p3, invert2):
    base1, quote1 = p1.split('/')
    coinA = base1
    coinB = p2.split('/')[0] if not invert2 else p2.split('/')[1]

    typeA = symbol_types.get(coinA, 'alt')
    typeB = symbol_types.get(coinB, 'alt')

    book1 = fetch_orderbook(p1)
    p1_price, spent_usdt = get_best_price(book1, 'asks', TRADE_USD)
    if not p1_price:
        return None
    amount_a = TRADE_USD / p1_price * (1 - FEE)
    liq1 = book1['asks'][0][0] * book1['asks'][0][1] if book1['asks'] else 0

    book2 = fetch_orderbook(p2)
    if not book2['asks'] or not book2['bids']:
        return None

    # Логика: stable - купюра, base - товар1, alt - товар2
    if typeA == 'stable':
        p2_price, _ = get_best_price(book2, 'asks', amount_a)
        if not p2_price:
            return None
        amount_b = amount_a / p2_price if invert2 else amount_a * p2_price
        amount_b *= (1 - FEE)
        liq2 = book2['asks'][0][0] * book2['asks'][0][1]
    elif typeB == 'stable':
        p2_price, _ = get_best_price(book2, 'bids', amount_a)
        if not p2_price:
            return None
        amount_b = amount_a * p2_price if invert2 else amount_a / p2_price
        amount_b *= (1 - FEE)
        liq2 = book2['bids'][0][0] * book2['bids'][0][1]
    else:
        p2_price, _ = get_best_price(book2, 'asks', amount_a)
        if not p2_price:
            return None
        amount_b = amount_a / p2_price if invert2 else amount_a * p2_price
        amount_b *= (1 - FEE)
        liq2 = book2['asks'][0][0] * book2['asks'][0][1]

    book3 = fetch_orderbook(p3)
    p3_price, _ = get_best_price(book3, 'bids', amount_b)
    if not p3_price:
        return None
    total_usdt = amount_b * p3_price * (1 - FEE)
    liq3 = book3['bids'][0][0] * book3['bids'][0][1]

    min_liq = min(liq1, liq2, liq3)
    if min_liq < MIN_LIQUIDITY or min_liq > MAX_LIQUIDITY:
        return None

    if total_usdt <= 0 or total_usdt > 10 * TRADE_USD:
        return None

    profit = total_usdt - TRADE_USD
    pct = (profit / TRADE_USD) * 100
    if pct < MIN_PROFIT_PCT:
        return None

    return profit, pct, min_liq

def format_routes(results):
    text = "<b>📈 ТОП прибыльных маршрутов:</b>\n\n"
    for i, (p1, p2, p3, profit, pct, liq) in enumerate(results[:10], start=1):
        color = '🟢' if pct >= MIN_PROFIT_PCT else '🟡'
        text += f"{color} {i}. {p1} → {p2} → {p3}\n"
        text += f"   💰 Прибыль: {profit:.2f} USDT | 📈 Спред: {pct:.2f}% | 💧 Ликвидность: {liq:,.0f} USDT\n\n"
    return text

def scan_arbitrage(update: Update, context: CallbackContext):
    global running
    if running:
        update.message.reply_text("⏳ Уже запущено, подождите...")
        return

    running = True
    update.message.reply_text("🚀 Запуск сканирования арбитража...")

    def worker():
        while running:
            results = []
            for p1, p2, p3, invert2 in routes:
                res = calc_triangle(p1, p2, p3, invert2)
                if res:
                    profit, pct, liq = res
                    results.append((p1, p2, p3, profit, pct, liq))
                time.sleep(0.05)
            results.sort(key=lambda x: x[4], reverse=True)
            if results:
                text = format_routes(results)
                context.bot.send_message(chat_id=update.effective_chat.id, text=text, parse_mode='HTML')
            else:
                context.bot.send_message(chat_id=update.effective_chat.id, text="❌ Нет прибыльных маршрутов")
            time.sleep(10)

    threading.Thread(target=worker, daemon=True).start()

def stop_scan(update: Update, context: CallbackContext):
    global running
    if not running:
        update.message.reply_text("❌ Сканирование не запущено.")
        return
    running = False
    update.message.reply_text("🛑 Остановлено.")

def main():
    dp = updater.dispatcher
    dp.add_handler(CommandHandler('start', scan_arbitrage))
    dp.add_handler(CommandHandler('stop', stop_scan))

    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    print("Бот запущен...")
    main()