# Правила запросов Prometheus и Loki

## PromQL

- Сначала определить семантику метрики: counter, gauge, histogram или summary.
- Для counter использовать `rate()` или `increase()`; не строить сырой монотонный counter как рабочий сигнал.
- Выбирать окно `rate` не меньше нескольких scrape interval; предпочтительно использовать `$__rate_interval`.
- Агрегировать только после вычисления rate: `sum by (...) (rate(metric[$__rate_interval]))`.
- Для histogram вычислять квантиль через `histogram_quantile()` по `le` после rate и агрегации.
- Для долей защищаться от деления на ноль только тогда, когда выбранная семантика нуля корректна.
- Не применять `label_replace`, тяжёлые regex и join без необходимости.
- Ограничивать `topk` после осмысленной агрегации.
- Не добавлять pod, instance, path, user ID и другие высококардинальные лейблы в обзорные панели.
- Использовать recording rules для дорогих повторяющихся выражений.

Пример RED:

```promql
sum(rate(http_server_requests_seconds_count{service=~"$service"}[$__rate_interval]))
```

```promql
sum(rate(http_server_requests_seconds_count{service=~"$service",status=~"5.."}[$__rate_interval]))
/
sum(rate(http_server_requests_seconds_count{service=~"$service"}[$__rate_interval]))
```

```promql
histogram_quantile(
  0.95,
  sum by (le) (
    rate(http_server_requests_seconds_bucket{service=~"$service"}[$__rate_interval])
  )
)
```

Имена метрик являются примерами. Перед применением подтвердить их наличие.

## Loki

- Начинать с селектора индексируемых labels; фильтрацию содержимого выполнять после него.
- Не превращать динамические поля вроде request ID или user ID в labels.
- Для обзорных панелей использовать metric queries, а сырые логи оставлять для drill-down.
- Ограничивать объём и диапазон логов, особенно при regex и JSON parsing.
- Использовать `| json`, `| logfmt` или pattern parser только после достаточно узкого селектора.
- Сохранять ссылку из панели ошибки в Explore с тем же временным диапазоном и переменными.

Пример частоты ошибок:

```logql
sum by (service) (
  rate({namespace=~"$namespace", service=~"$service"} |= "ERROR" [$__interval])
)
```

## Проверка запроса

Перед публикацией:

1. Выполнить запрос на коротком диапазоне.
2. Проверить единицу и порядок значения.
3. Посмотреть количество series.
4. Проверить пустой результат и отсутствие datasource.
5. Проверить все варианты переменных, включая `All`.
6. Увеличить диапазон до типичного рабочего и оценить стоимость/время выполнения.
7. Сопоставить результат с независимым сигналом или известным событием.

Не считать синтаксически успешный запрос семантически правильным.
