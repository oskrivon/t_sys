# News Event Trading: Strategy/Saylor BTC Sale Case Study

**Дата:** 2026-06-08
**Статус:** NOT ACTIONABLE
**Триггер:** анализ weekend trade #7 (Jun 5-7), гипотеза о связи с продажей Сейлором

## Событие

- **26-31 мая:** Strategy (ex-MicroStrategy) продал 32 BTC за ~$2.5M (avg $77,135)
- **Причина:** оплата дивидендов по привилегированным акциям STRC
- **Первая продажа BTC с декабря 2022** — шок для рынка
- **1 июня (пн):** файлинг опубликован, рынок реагирует
- **Контекст:** 32 BTC из 843,706 = 0.004% holdings. Пыль на балансе.

## Реакция рынка

- BTC: -3.1% в день публикации, -6% за неделю ($65.4k → $61.5k)
- BTC ETF: отток ~$4B за 12 сессий (рекорд consecutive outflow)
- Нарратив: "Saylor capitulated" (хотя продажа для дивидендов, не bearish call)

## Наш weekend trade #7

| Параметр | Значение |
|----------|----------|
| Signal | SHORT (vote_sum=-1, 5/5 predictors OK) |
| Entry | Fri Jun 5 21:05 UTC, $61,850 |
| Sat low | $60,039 (unrealized +1.85%) |
| Sun reversal | BTC → $62,851 |
| Exit | Sun Jun 7 23:05 UTC, $62,851 |
| **PnL** | **-1.62%** |

**Вывод по трейду:** шорт поймал хвост Saylor-движения в субботу (+1.85% unrealized), но к воскресенью рынок решил что 32/843k = ерунда, и развернулся. Основной move ($65k→$61k) произошёл Mon-Thu, до нашего entry.

**Примечание:** exit в 23:05 (старый cron), при exit 12:00 (новый) PnL был бы -1.20% — всё равно loss.

## Можно ли систематизировать?

### Проверенные подходы

| Подход | Проблема |
|--------|----------|
| SEC 8-K мониторинг | Algo-фонды реагируют за секунды, latency неконкурентен |
| On-chain whale tracking | 32 BTC — шум, Strategy могла через OTC |
| News sentiment NLP | Нужен <1min парсер, направление реакции неочевидно |
| ETF flow мониторинг | Данные с задержкой T+1, entry уже поздно |

### Фундаментальные ограничения

1. **N=2** — Strategy продавала BTC дважды (2022, 2026). Нельзя бэктестить.
2. **Unpredictable direction** — рынок мог прочитать как bullish (продали мизер, conviction intact).
3. **Timing** — основной move за минуты/часы после публикации. Retail опаздывает.
4. **Уже есть покрытие** — weekend ensemble ловит sentiment через пятничные предикторы. Этот трейд правильно предсказал SHORT по sentiment, проблема в Sunday reversal.

## Связь с weekend strategy

Weekend ensemble **корректно** уловил bearish sentiment (Saylor sell aftershock → negative Friday в фонде → SHORT signal). Проблема не в сигнале, а в:
- Timing: основной move уже отыгран к пятнице
- Sunday reversal: рынок переоценил значимость

Это подтверждает что weekend strategy работает как **sentiment aggregator**, а не event trader.

## Итог

News event trading в крипте не подходит для нашей инфраструктуры:
- Нужен HFT-level latency для реакции на файлинги
- Sample size единичных событий слишком мал
- Weekend ensemble уже агрегирует macro sentiment, отдельный news trader избыточен
