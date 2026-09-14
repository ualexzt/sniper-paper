from types import SimpleNamespace

from sniper_paper.telegram import TelegramAlerts, near_message, signal_message


def test_disabled_without_private_config(tmp_path):
    notifier = TelegramAlerts(tmp_path / 'missing.json')
    notifier.enqueue('x', 'test')
    assert not notifier.enabled
    assert notifier.pending.empty()


def test_messages_distinguish_signal_from_fill_and_escape_html():
    level = SimpleNamespace(price=0.14213, timeframe='1m')
    text = signal_message('ENA<USDT', 'SHORT', 0.14196, level, 'failed_sweep_reclaim', 1789366305000)
    assert 'SELL' in text and 'ENA&lt;USDT' in text
    assert 'не підтвердження виконання' in text
    assert '09:11:45' in text
    event = dict(symbol='ENAUSDT', level_side='HIGH', level_timeframe='1m',
                 level_price=0.14213, observed_price=0.142, occurred_at_ms=1789366305000)
    assert 'Опір' in near_message(event)
