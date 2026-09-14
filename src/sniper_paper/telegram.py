"""Optional outbound alerts. Secrets live outside the repository."""

import json
import logging
import queue
import sqlite3
import threading
import time
from datetime import datetime
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

import requests


def stamp(ms):
    return datetime.fromtimestamp(ms / 1000, ZoneInfo('Europe/Kyiv')).strftime('%d.%m %H:%M:%S')


def near_message(event):
    side = 'опір' if event['level_side'] == 'HIGH' else 'підтримка'
    return (f"📍 <b>Ціна біля рівня</b>\n\n🪙 <b>{escape(event['symbol'])}</b>\n"
            f"📏 {side.capitalize()} · {escape(event['level_timeframe'])}\n"
            f"Рівень: <code>{event['level_price']:.8g}</code>\n"
            f"Ціна: <code>{event['observed_price']:.8g}</code>\n\n"
            f"👀 Спостерігаємо за реакцією та orderflow.\n"
            f"🕒 {stamp(event['occurred_at_ms'])} · Київ\n🧪 Дослідження сигналів")


def signal_message(symbol, side, price, level, lane, now_ms):
    buy = side == 'LONG'
    setup = 'Закол із поверненням' if lane == 'failed_sweep_reclaim' else 'Підтверджений пробій'
    return (f"{'🟢' if buy else '🔴'} <b>Точка входу: {'BUY' if buy else 'SELL'}</b>\n\n"
            f"🪙 <b>{escape(symbol)}</b>\n🎯 {setup}\n"
            f"Ціна сигналу: <code>{price:.8g}</code>\n"
            f"📏 Рівень: <code>{level.price:.8g}</code> · {escape(level.timeframe)}\n\n"
            f"✅ Поточні умови orderflow виконані.\n🕒 {stamp(now_ms)} · Київ\n"
            "🧪 Сигнал для дослідження · не підтвердження виконання ордера")


class TelegramAlerts:
    def __init__(self, config_path='/runtime/telegram.json'):
        self.pending = queue.Queue(maxsize=256)
        path = Path(config_path)
        self.enabled = False
        if not path.exists():
            return
        try:
            config = json.loads(path.read_text())
            self.token = config['token']
            self.chat_id = str(config['chat_id'])
            self.database = path.with_name('telegram-alerts.sqlite')
            self.enabled = bool(self.token and self.chat_id)
        except (OSError, ValueError, KeyError):
            logging.warning('Telegram configuration invalid')
            return
        if self.enabled:
            threading.Thread(target=self._run, daemon=True, name='telegram-alerts').start()

    def enqueue(self, key, text, cooldown_s=0):
        if self.enabled:
            try:
                self.pending.put_nowait((key, text, cooldown_s, time.time()))
            except queue.Full:
                logging.warning('Telegram queue full; alert dropped')

    def _run(self):
        try:
            db = sqlite3.connect(self.database)
            db.execute('CREATE TABLE IF NOT EXISTS sent (key TEXT PRIMARY KEY, ts REAL NOT NULL)')
            while True:
                key, text, cooldown, queued = self.pending.get()
                now = time.time()
                row = db.execute('SELECT ts FROM sent WHERE key=?', (key,)).fetchone()
                if now - queued > 60 or (row and (not cooldown or now - row[0] < cooldown)):
                    continue
                try:
                    response = requests.post(
                        f'https://api.telegram.org/bot{self.token}/sendMessage',
                        json={'chat_id': self.chat_id, 'text': text, 'parse_mode': 'HTML'}, timeout=8,
                    )
                    if response.status_code == 200 and response.json().get('ok'):
                        db.execute('INSERT OR REPLACE INTO sent VALUES (?,?)', (key, now))
                        db.commit()
                    else:
                        logging.warning('Telegram delivery rejected: HTTP %s', response.status_code)
                except (requests.RequestException, ValueError):
                    # Never log exceptions containing the token-bearing URL.
                    logging.warning('Telegram delivery failed')
                time.sleep(1.1)
        except sqlite3.Error:
            logging.warning('Telegram notification storage unavailable')
