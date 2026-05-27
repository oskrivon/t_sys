# Signal Processing Denoising: Wavelet + Kalman для Big Move Detector

**Статус: CLOSED (2026-05-26)**
**Стратегия: DEAD END (0/8 validation). Kalman velocity: полезный инструмент для direction picking.**

## Гипотеза

Применить классическую фильтрацию сигналов (wavelet denoising, Kalman filter) к ценовым данным перед feature extraction для Big Move Detector. Ожидание: уменьшить шум → улучшить timing и direction prediction.

## Результаты

### Grid Search (первичный, на 48k BTC candles)
- Best config: wl=4, thr=0.8, long-only, vel=0.3, R=1.0, Q=0.01
- PF=1.13, WR=47.7%, total +68% (472 trades)
- 15/27 grid configs прибыльны
- **Оказался ложным — long-only beta на bull market**

### Quant Validation (8 checks) — 0/8 PASS
На последних 240 дней (bearish/sideways):
- PF=0.55, WR=36.2%, total -37%
- Alpha t=-2.32, p(alpha>0) = 0.988 (нет alpha)
- 0/5 месяцев положительных
- MaxDD 34.5%, Sharpe -4.90
- DSR p=0.000

### Ablation Study — что работает, что нет

| Режим | N | WR | PF | Dir Acc | Δ vs baseline |
|---|---|---|---|---|---|
| **Baseline (RSI, L+S)** | 675 | 36.7% | 0.73 | 47.3% | — |
| **+ Wavelet features** | 866 | 35.5% | 0.68 | **42.6%** | **хуже** |
| **Kalman direction (L+S)** | 107 | **46.7%** | **1.25** | **54.2%** | **лучше** |
| Both (L+S) | 132 | 37.1% | 0.78 | 50.8% | mixed |
| Kalman (long-only) | 44 | 38.6% | 0.87 | 54.5% | лучше, но мало |
| Both (LO, tuned best) | 58 | 36.2% | 0.55 | 48.3% | overfitting |

## Почему Kalman работает, а Wavelet нет

### Kalman velocity — online causal filter
- Использует только прошлые данные для оценки в момент t
- Velocity = адаптивно сглаженная первая производная цены
- Однозначный direction signal (vel > 0 = long, < 0 = short)
- RSI — осциллятор: при RSI 60 непонятно, начало тренда или перекупленность
- Direction accuracy: Kalman 54.2% vs RSI 47.3% (+7pp)
- SL rate: Kalman 34.6% vs RSI 48.9% (-14pp)

### Wavelet denoising — batch non-causal filter (ВРЕДЕН)
- `pywt.wavedec/waverec` обрабатывает всю серию целиком
- Threshold рассчитывается по σ всего ряда (включая будущее)
- Denoised close в точке t зависит от t+1, t+2... = **look-ahead bias**
- `noise_level = |raw - denoised| / raw` кодирует расхождение с будущим
- Результат: direction accuracy 42.6% (хуже рандома!) — классический look-ahead leak
- ML использует `noise_level` как 2-й по важности feature — опирается на будущее

### Можно ли починить wavelet?
Causal wavelet (пересчёт окна на каждом баре) возможен, но:
1. ~N раз медленнее
2. Boundary effects на краях окна
3. Для direction picking Kalman velocity уже лучше и проще

## Выводы

1. **Wavelet denoising** в стандартной реализации содержит неявный look-ahead bias. Не использовать для features в walk-forward backtest без causal implementation.

2. **Kalman velocity** — полезный инструмент для direction picking:
   - Лучше RSI на +7pp direction accuracy
   - Снижает SL rate на 14pp
   - Параметры: R=1.0, Q=0.01 (state: [price, velocity])
   - Можно применять в любой стратегии где нужен direction filter

3. **Big Move Detector** как standalone стратегия мёртв — проблема не в фильтрации, а в timing: ML предсказывает "big move скоро", но к моменту входа движение частично произошло.

## Скрипты
- `scripts/research/bigmove_denoised.py` — базовый эксперимент
- `scripts/research/bigmove_denoised_tune.py` — grid search
- `scripts/research/bigmove_denoised_validate.py` — 8-check validation
- `scripts/research/bigmove_filter_ablation.py` — ablation study
