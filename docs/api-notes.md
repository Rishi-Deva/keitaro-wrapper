# Keitaro Admin API: проверенные факты (шаг 0)

Дата проверки: 2026-11-19. Всё ниже взято из **реальных запросов** к трекеру.
Адрес трекера, ключ и реальные домены здесь не указаны.

**Про документацию.** Локальная документация из админки по пути `admin_api/docs` отдаёт 403
(нужна сессия в админке). Публичная `admin-api.docs.keitaro.io` рендерится через JS и через
fetch не читается. Поэтому названия полей сверены не по документации, а по ответам API
и по успешным и неуспешным POST/PUT/DELETE.

## Общее

- База: `{KEITARO_URL}/admin_api/v1/`, заголовок `Api-Key: <ключ>`, JSON.
- Ошибка валидации: **422**, тело `{"<поле>": ["<текст>"]}`. Пример: `{"alias": ["Alias is required"]}`.
- Ошибка параметра: **400**, тело `{"error": "Param 'type' is required", "stacktrace": null}`.
- Несуществующий или удалённый объект: **404 в виде HTML от nginx, не JSON.** Клиент должен
  обрабатывать ответы без JSON.

## Выбранные ID (лежат в `.env`)

| Переменная | ID | Что это | Почему выбран |
|---|---|---|---|
| `DEFAULT_DOMAIN_ID` | **101** | домен кампаний, state active, SSL | единственный домен в трекере |
| `DEFAULT_GROUP_ID` | **202** | группа кампаний «EXAMPLE_GROUP» | общая тестовая группа; своя группа = лишняя запись в общий трекер |
| `DEFAULT_TRAFFIC_SOURCE_ID` | **303** | источник «EXAMPLE_GROUP» | единственный источник |

## Справочники (GET)

- `GET campaigns` возвращает массив. Ключи: `id, name, alias, type, state, domain_id, domain, group_id, group,
  traffic_source_id, position, token, cost_*, uniqueness_*, cookies_ttl, bind_visitors, parameters, postbacks, notes, created_at, updated_at`.
  Кампании с `state=deleted` в список не попадают.
- `GET offers` возвращает массив. Используем `id, name, state, country, offer_type, action_type`.
  **В трекере сейчас один оффер (id 7001).** Поиск по имени делаем у себя (фильтр по `name`).
- `GET domains`: `id, name, state, network_status, is_ssl, ...`
- `GET groups?type=campaigns`: значение **`campaigns`, во множественном числе**. С `type=campaign`
  приходит `[]`, без `type` приходит 400.
- `GET traffic_sources`: `id, name, state, ...`

## Создание кампании: `POST campaigns`

Проверенное тело, ответ 200:
```json
{"name": "test_<surname>_...", "alias": "test_<surname>_...", "type": "position",
 "state": "active", "domain_id": 101, "group_id": 202, "traffic_source_id": 303}
```
- `alias` **обязателен**: без него приходит 422 `Alias is required`.
- В ответе объект кампании с `id`. Дополнительно в нём `token` и `domain` (URL): их нельзя логировать.

## Потоки

`GET campaigns/{id}/streams` возвращает массив потоков с вложенными `filters`, `offers`, `landings`, `triggers`.

### Поток 1: гео, редирект на google (`POST streams`, ответ 200)
```json
{"campaign_id": 1, "type": "regular", "name": "Geo MX -> Google", "position": 1,
 "state": "active", "schema": "redirect", "action_type": "http",
 "action_payload": "https://google.com", "collect_clicks": true, "filter_or": false,
 "filters": [{"name": "country", "mode": "accept", "payload": ["MX"]}]}
```

### Поток 2: оффер (`POST streams`, ответ 200)
```json
{"campaign_id": 1, "type": "regular", "name": "Rest -> offer", "position": 2,
 "state": "active", "schema": "landings", "action_type": "http", "collect_clicks": true,
 "filter_or": false, "filters": [],
 "offers": [{"offer_id": 7001, "share": 100, "state": "active"}]}
```
- Поток с офферами: `schema: "landings"` и непустой `offers`. Так же устроены уже существующие
  кампании в трекере.
- Элемент `offers[]` в ответе: `{id, stream_id, offer_id, state, share, created_at, updated_at}`.

### Изменение офферов: `PUT streams/{id}`
- **Частичный PUT** с одним `{"offers": [...]}` работает, остальные поля потока не меняются.
- **PUT заменяет список `offers` целиком**: `{"offers": []}` удалил все офферы. Поэтому и добавление,
  и удаление делаем так: читаем текущий список, меняем его у себя, отправляем PUT с полным списком.
- **API не проверяет сумму долей**: PUT с одним оффером и `share: 50` прошёл с ответом 200.
  За сумму 100% отвечает наш код.
- Дробные доли не проверялись (в существующих данных встречаются только целые). Используем целые.

## Удаление кампании: `DELETE campaigns/{id}`
- Ответ 200 с массивом из кампании, у которой `state: "deleted"`. Удаление **мягкое**:
  кампания пропадает из `GET campaigns`, `GET campaigns/{id}` отдаёт 404 (HTML),
  а `GET campaigns/{id}/streams` по-прежнему возвращает потоки.
- Эндпоинт полной очистки не проверялся: документации по нему нет, угадывать не стали.

## Что было записано в трекер на шаге 0

| # | Запрос | Результат |
|---|---|---|
| 1 | POST campaigns без alias | 422, ничего не создано |
| 2 | POST campaigns `test_<surname>_probe1` | 200, id 900002 |
| 3 | POST streams, гео MX → google | 200, stream 800003 |
| 4 | POST streams, оффер 7001 share 100 | 200, stream 800004 |
| 5 | PUT streams/800004, share 50 | 200 (сумма не проверяется) |
| 6 | PUT streams/800004, offers [] | 200 (список заменён) |
| 7 | DELETE campaigns/900002 | 200, state deleted |

Итого создана 1 тестовая кампания из 2 разрешённых, и она уже удалена.
