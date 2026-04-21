"""
Funding Rate Mean Reversion Research

Hypothesis: When funding rate reaches extreme values, it tends to revert to the mean.
Trade the reversion: SHORT when rate extremely positive, LONG when extremely negative.

Results (2026-04-21, Bybit, 18 coins, ~67 days each):
  - At 1bp threshold, 24h horizon: +63.8bp avg (N=1104, 53.6% WR) — noisy
  - At 2bp threshold, 24h horizon: +165.5bp avg (N=210, 53.3% WR) — best risk/reward
  - At 3bp threshold, 24h horizon: +79.4bp avg (N=105, 53.3% WR, Sharpe 4.76)
  - At 5bp threshold: too few trades (N=28), negative returns

VERDICT: Signal exists at 24h horizon but heavily skewed by a few coins (ORDI).
         Per-coin results are inconsistent — some coins show strong mean reversion,
         others show momentum (rate stays extreme). Not robust enough to trade blind.
         MaxDD is enormous relative to avg return.

Usage:
    ssh root@<SERVER_HOST> "cd /root/trading && python3 scripts/research/funding_mean_reversion.py"
"""
from __future__ import annotations

import time
import numpy as np
import ccxt
from datetime import datetime, timezone
from collections import defaultdict


def main():
    bybit = ccxt.bybit({
        'apiKey': '<BYBIT_API_KEY>',
        'secret': '<BYBIT_API_SECRET>',
    })

    COINS = [
        'BTC', 'ETH', 'SOL', 'DOGE', 'XRP', 'WIF', 'PEOPLE', 'MEME',
        '1000BONK', 'LINK', 'ADA', 'AVAX', 'SUI', 'ARB', 'APT', 'SEI',
        'TIA', 'ORDI',
    ]
    THRESHOLDS = [1.0, 1.5, 2.0, 3.0, 5.0]  # bps
    HORIZONS = {'1h': 1, '4h': 4, '8h': 8, '24h': 24}
    FEE_BPS = 11  # round-trip

    results: dict = defaultdict(lambda: defaultdict(list))

    for coin in COINS:
        sym = f'{coin}/USDT:USDT'
        print(f'Fetching {coin}...', flush=True)

        try:
            # Fetch funding rate history (latest 200 records ~67 days)
            all_fr = bybit.fetch_funding_rate_history(sym, limit=200)
            time.sleep(0.15)
            if not all_fr:
                continue

            # Fetch 1h candles covering the funding period + 24h buffer
            fr_start = all_fr[0]['timestamp']
            fr_end = all_fr[-1]['timestamp']
            all_candles: list = []
            s = fr_start
            end_need = fr_end + 24 * 3600000
            for _ in range(15):
                candles = bybit.fetch_ohlcv(sym, '1h', since=s, limit=1000)
                if not candles:
                    break
                all_candles.extend(candles)
                if candles[-1][0] >= end_need or len(candles) < 1000:
                    break
                s = candles[-1][0] + 1
                time.sleep(0.15)

            if not all_candles:
                continue

            price_by_ts = {
                (c[0] // 3600000) * 3600000: c[4] for c in all_candles
            }

            rates = np.array([x['fundingRate'] * 10000 for x in all_fr])
            extreme = int(np.sum(np.abs(rates) > 1.0))
            print(f'  {len(all_fr)} FR, {len(all_candles)} candles, '
                  f'range [{rates.min():.1f}, {rates.max():.1f}]bp, >1bp: {extreme}')

            for fr_rec in all_fr:
                rate_bps = fr_rec['fundingRate'] * 10000
                hour_ts = (fr_rec['timestamp'] // 3600000) * 3600000
                entry_price = price_by_ts.get(hour_ts)
                if not entry_price:
                    continue

                for thr in THRESHOLDS:
                    if abs(rate_bps) < thr:
                        continue
                    direction = -1 if rate_bps > 0 else 1

                    for h_name, h_hours in HORIZONS.items():
                        exit_ts = hour_ts + h_hours * 3600000
                        exit_price = price_by_ts.get(exit_ts)
                        if not exit_price:
                            continue
                        ret_bps = direction * (exit_price - entry_price) / entry_price * 10000
                        net_ret = ret_bps - FEE_BPS
                        results[(thr, h_name)]['returns'].append(net_ret)
                        results[(thr, h_name)]['coin'].append(coin)

        except Exception as e:
            print(f'  ERROR: {e}')
            time.sleep(1)

    # --- Results ---
    print(f'\n{"=" * 90}')
    print('FUNDING RATE MEAN REVERSION — RESULTS (net of 11bps fees)')
    print(f'{"=" * 90}')
    print(f'{"Threshold":>10} {"Horizon":>8} {"Trades":>7} {"WinRate":>8} '
          f'{"AvgRet":>9} {"Median":>8} {"MaxDD":>8} {"Sharpe":>8}')
    print('-' * 90)

    for thr in THRESHOLDS:
        for h_name in HORIZONS:
            rets = results[(thr, h_name)]['returns']
            if not rets:
                print(f'{thr:>7.1f}bp {h_name:>8} {"N/A":>7}')
                continue
            r = np.array(rets)
            n = len(r)
            wr = np.mean(r > 0) * 100
            avg = np.mean(r)
            med = np.median(r)
            cum = np.cumsum(r)
            mdd = np.max(np.maximum.accumulate(cum) - cum)
            std = np.std(r)
            sharpe = avg / std * np.sqrt(252 * 3) if std > 0 else 0
            print(f'{thr:>7.1f}bp {h_name:>8} {n:>7} {wr:>6.1f}% '
                  f'{avg:>7.1f}bp {med:>7.1f}bp {mdd:>6.0f}bp {sharpe:>7.2f}')
        print()

    # Per-coin
    print('\nPer-coin breakdown (>2bp threshold, 8h horizon):')
    print(f'{"Coin":>10} {"Trades":>7} {"WinRate":>8} {"AvgRet":>9}')
    print('-' * 40)
    coin_rets: dict = defaultdict(list)
    key = (2.0, '8h')
    if key in results:
        for ret, c in zip(results[key]['returns'], results[key]['coin']):
            coin_rets[c].append(ret)
        for c in COINS:
            if c in coin_rets:
                cr = np.array(coin_rets[c])
                print(f'{c:>10} {len(cr):>7} {np.mean(cr > 0) * 100:>6.1f}% '
                      f'{np.mean(cr):>7.1f}bp')


if __name__ == '__main__':
    main()
