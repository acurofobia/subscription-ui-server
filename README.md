# Subscription UI Server

Независимый сервер для выдачи VPN-конфигурации и UI-документа приложению с одного
порта. **Python 3.11+**, только стандартная библиотека. Панель управления, база
данных и другие серверы для работы не нужны.

Все данные задаются локальными файлами. Сервер проверяет их структуру и выдаёт
JSON; он не рассчитывает цены, баланс, сроки или статистику. Названия карточек,
кнопок, пояснения и отображаемые значения задаются в UI JSON.

## Быстрый запуск

```sh
git clone https://github.com/acurofobia/subscription-ui-server.git
cd subscription-ui-server
cp .env.example .env
cp config.example.json config.json
cp data/subscription.example.json data/subscription.json
```

Создайте случайный токен в новой локальной `.env`:

```sh
python3 - <<'PY'
from pathlib import Path
import secrets

path = Path('.env')
text = path.read_text().replace(
    'replace_with_a_random_url_safe_token', secrets.token_urlsafe(32)
)
path.write_text(text)
path.chmod(0o600)
PY
```

Замените адрес, UUID и TLS-параметры в `config.json` параметрами своего сервера.
В репозитории находятся **только примеры**: их адреса и ключи не обеспечивают
реальное VPN-подключение.

```sh
make check
make test
make run
```

Если нужный Python установлен под отдельным именем, укажите его явно:

```sh
make PYTHON=python3.13 run
```

Добавьте в приложение ссылку:

```text
http://localhost:8082/subscription/<SUBSCRIPTION_TOKEN>
```

Для Android release используйте HTTPS reverse proxy с доверенным сертификатом
и задайте `PUBLIC_BASE_URL` с тем же origin, что ссылка подписки. Публичный HTTP
приложение не принимает. Для debug APK на подключённом по ADB устройстве:

```sh
adb reverse tcp:8082 tcp:8082
```

Добавьте `http://127.0.0.1:8082/subscription/<SUBSCRIPTION_TOKEN>`.
Loopback HTTP поддерживает нативную выдачу, включая решения по устройствам.
Для debug без нативных решений можно задать `HOST=0.0.0.0` и использовать
локальный IP компьютера, например `http://192.168.1.10:8082/...`.
Android не принимает HTTP по имени `.local`; release требует HTTPS и в LAN.

## Как это работает

1. Приложение запрашивает основную ссылку с токеном в пути.
2. Сервер возвращает `sing_box_url`, `vk_turn_link`, `ui_url` и `ui_token`.
3. Приложение загружает VPN JSON и UI JSON; для UI передаёт Bearer-токен.
4. Клиент применяет VPN-параметры и отображает поддерживаемые блоки UI.

Один экземпляр сервера обслуживает один комплект файлов. Управление
пользователями, удалённое создание VPN-аккаунтов и измерение трафика не входят
в его задачи. Маршруты и DNS остаются под управлением приложения.

| Файл | Назначение |
|---|---|
| `server.py` | HTTP-сервер, CLI, файловые настройки и авторизация |
| `validation.py` | Проверка UI-контракта и ссылок между элементами |
| `provisioning.py` | Проверка нативного VLESS failover и преобразование локального Xray JSON |
| `device_access.py` | Необязательные файловые решения доступа TURN для `device-access-v1` |
| `.env.example` | Пример настроек сервера |
| `config.example.json` | Пример одного VLESS outbound |
| `config.failover.example.json` | Пример четырёх VLESS + REALITY узлов |
| `data/subscription.example.json` | Пример UI-документа |
| `data/device-access.example.json` | Пример правил доступа устройств |
| `tests/` | Тесты с синтетическими данными и временными HTTP-портами |
| [SPECIFICATION.md](SPECIFICATION.md) | Полный UI-контракт версии 1 |

Рабочие `.env`, `config.json`, `config.failover.json` и `data/subscription.json`
исключены из Git. Все примеры используют демонстрационные значения.

## Настройки

Настройки читаются из `.env` рядом с `server.py`, независимо от текущего каталога.
Одноимённые переменные окружения не подменяют значения файла. Относительные пути
считаются от расположения `.env`. Значения с пробелами и `#` заключайте в кавычки.

| Параметр | Значение по умолчанию / назначение |
|---|---|
| `HOST` | `127.0.0.1`; `0.0.0.0` — доступ через сеть |
| `PORT` | `8082` |
| `SUBSCRIPTION_TOKEN` | Обязательный токен из 16–256 символов `[A-Za-z0-9_-]` |
| `UI_DOCUMENT_PATH` | `data/subscription.json` |
| `SING_BOX_CONFIG_PATH` | Путь к VPN JSON; пустое значение отключает его выдачу |
| `VPN_CONFIG_FORMAT` | `sing-box`, `vless-failover` или `xray`; по умолчанию `sing-box` |
| `VK_TURN_LINK` | Готовая ссылка `wdtt://…` или `vkturnproxy://…`; пусто — режим отключён |
| `PUBLIC_BASE_URL` | Публичный HTTPS-адрес, например `https://vpn.example.org`; пусто — HTTP из проверенного Host |
| `SUBSCRIPTION_PATH_PREFIX` | `/subscription`; можно заменить, например, на `/api/sub` |
| `APP_USER_AGENT_PREFIX` | `VipinApp/`, для согласования нативного формата |
| `APP_SUBSCRIPTION_SIGNING_SECRET` | Необязательный секрет от 32 байт для нативного Bearer-токена |
| `DEVICE_ACCESS_PATH` | Необязательный JSON правил доступа TURN; пусто сохраняет прежнюю выдачу |

`.env`, UI и VPN JSON перечитываются на каждом запросе. После исправления
некорректного файла следующий запрос работает без перезапуска. Сохраняйте файлы
атомарной заменой. После изменения адреса или порта процесс нужно перезапустить;
смена токена требует обновить ссылку в приложении.

`PUBLIC_BASE_URL` должен соответствовать адресу, по которому приложение
обращается к серверу. Допускается внешний префикс, например
`https://example.org/vpn`: reverse proxy должен удалять `/vpn` перед передачей
запроса. Заголовки `X-Forwarded-*` не используются для построения ссылок.

## HTTP API

| Метод и путь по умолчанию | Ответ / авторизация |
|---|---|
| `GET /subscription/<token>` | Дескриптор с адресами VPN и UI; токен в пути |
| `GET /subscription/<token>/config` | VPN JSON; токен в пути |
| `GET /v1/subscription` | UI JSON; `Authorization: Bearer <token>` |
| `GET /subscription/<token>/app/v1/ui` | Нативный UI; Bearer из дескриптора |
| `GET /subscription/<token>/app/v1/config` | Нативный VPN JSON; Bearer из дескриптора |
| `GET /health` | `{"status":"ok"}`; без авторизации |

Для всех путей поддерживается `HEAD` с заголовками GET и без тела.
Неизвестные пути дают 404, другие методы — 405. Файлы проекта напрямую не раздаются.

Пример дескриптора обычного формата:

```json
{
  "sing_box_url": "http://localhost:8082/subscription/<token>/config",
  "vk_turn_link": null,
  "ui_url": "http://localhost:8082/v1/subscription",
  "ui_token": "<token>"
}
```

Для нативной выдачи клиент отправляет `User-Agent: VipinApp/1.0` и
`X-Subscription-Format: vipin-v1`. Для failover дополнительно нужен
`X-Subscription-Capabilities: vless-failover-v1`. Сервер возвращает ссылки на
`/app/v1/*`, заголовок `X-Subscription-Format: vipin-v1` и `Vary` по заголовкам
согласования. Без failover capability VPN URL не рекламируется, а запрос такого
конфига получает 406. Для обычного `sing-box` capability не требуется.

Android release разрешает только HTTPS; debug также разрешает loopback HTTP.
Для HTTP по LAN-адресу сервер использует исходные маршруты и исходный токен;
такую ссылку принимает только debug APK. При включённом `DEVICE_ACCESS_PATH`
нужен нативный формат: используйте HTTPS или loopback HTTP через ADB.

Если задан секрет подписи, нативный `ui_token` — base64url без padding от
HMAC-SHA256(secret, `vipin-v1:<SUBSCRIPTION_TOKEN>`). Без секрета он равен
`SUBSCRIPTION_TOKEN`. Смена секрета инвалидирует прежние нативные токены;
исходный `/v1/subscription` продолжает принимать токен подписки.

### Доступ Android-устройств через JSON

Android отправляет capability `device-access-v1` и стабильный для установки
идентификатор в заголовке `x-hwid`. Чтобы сервер передавал решения доступа:

```sh
cp data/device-access.example.json data/device-access.json
```

В `.env` задайте `DEVICE_ACCESS_PATH=./data/device-access.json`. Файл имеет вид:

```json
{
  "schemaVersion": 1,
  "default": {"state": "allowed", "revision": 1},
  "devices": {
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": {
      "state": "device_revoked", "revision": 2
    }
  }
}
```

В `devices` укажите реальный `x-hwid`; правило для него заменяет `default`.
Допустимы `allowed`, `not_allowed`, `device_revoked`, `device_limit_reached`.
`revision` — целое от 0 до 9223372036854775807. При каждом изменении решения
увеличивайте ревизию для этого устройства, в том числе при возврате к `default`:
Android сохраняет запрет и игнорирует более старое разрешение. Равные ревизии
с разными состояниями отклоняются клиентом.

Дескриптор дополняется `device_access: {schemaVersion, hwid, state, revision}`.
При запрете `vk_turn_link` равен `null`; VLESS и авторский UI не меняются.
Android применяет запрет до загрузки UI и снимает его только после получения
более нового `allowed` и полного корректного комплекта с TURN-ссылкой.

Правила перечитываются на каждом запросе дескриптора, без перезапуска.
`--check` и запуск сервера также проверяют файл. Ошибка правил даёт 503 без
выдачи TURN; после исправления следующий запрос работает. Файл ограничен 1 MiB.
Запросы дескриптора без нативного `device-access-v1` получают 406, без одного
корректного `x-hwid` — 400. Поэтому включайте правила для совместимых клиентов.
Без `DEVICE_ACCESS_PATH` прежняя выдача остаётся доступной.

Сервер не регистрирует устройства автоматически и не рассчитывает их лимит:
состояния задаются вручную. HWID передаёт сам клиент; эти правила не заменяют
авторизацию VPN-шлюза и не отзывают уже выданные ключи на удалённом сервере.
Изменения применяются в приложении при обновлении подписки; автоматическая
проверка Android выполняется не чаще раза в час, ручное обновление доступно сразу.

Все ответы имеют `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`
и JSON Content-Type. Ошибки: 400 — запрос или Host; 401 — Bearer;
404 — путь/токен/отключённый конфиг; 405 — метод; 406 — формат/capability;
503 — некорректные настройки или данные. `/health` проверяет процесс,
а не готовность конфигурации. Access log отключён, ошибки не раскрывают файлы
или credentials. UI ограничен 1 MiB, VPN — 8 MiB.

## VPN-конфигурация

### Один VLESS outbound

`VPN_CONFIG_FORMAT=sing-box` принимает outbound либо объект с `outbounds`.
Клиент выбирает VLESS с тегом `R3-N1`, а если его нет — единственный VLESS.
Неоднозначный выбор или зависимость через `detour` не поддерживаются клиентом.

### Четыре узла с failover

```sh
cp config.failover.example.json config.failover.json
```

В `.env`:

```dotenv
SING_BOX_CONFIG_PATH=./config.failover.json
VPN_CONFIG_FORMAT=vless-failover
```

Замените синтетические адреса, UUID и REALITY-ключи. `failover.primary` задаёт
основной узел, `backups` — три резервных в порядке приоритета. Теги должны
соответствовать четырём уникальным `outbounds`. Нативный формат требует TLS,
uTLS и REALITY; UUID — в каноническом виде с дефисами.

Политика текущего клиента: `selector=vk-wg`, проверка каждые 10 секунд,
таймаут 5 секунд, переключение после двух ошибок, возврат после трёх успешных
проверок. Переключение выполняет приложение, сервер лишь передаёт политику.

Для временно недоступного режима допустим ответ:

```json
{
  "schemaVersion": 1,
  "kind": "vless-failover",
  "state": "unavailable",
  "reason": "traffic_limited",
  "outbounds": [],
  "failover": null
}
```

Для `restricted`/`unavailable` узлы должны быть пустыми, failover — null,
причина — `not_allowed` или `traffic_limited`. Для `available` причина — null.
Это фиксированные значения протокола клиента, а не расчёт на сервере.

### Локальный Xray JSON

Укажите его путь в `SING_BOX_CONFIG_PATH` и `VPN_CONFIG_FORMAT=xray`.
Адаптер следует цепочке `proxy` → loopback → balancer → fallback, извлекая
четыре VLESS TCP/RAW + REALITY узла. Порядок элементов в файле не влияет на
приоритет; неоднозначные графы, циклы и другие транспорты отклоняются.
Удалённая панель для этого не нужна.

## UI текущего клиента

Редактируйте `data/subscription.json`. В строке `valueRow` можно показывать любое
число или текст: пример использует «Количество серверов: 17». Сервер не придаёт
значениям финансового смысла и не подставляет данные по ID.

Расположение и поведение существующего клиента сохранены:

| ID / тип | Отображение |
|---|---|
| `block01` / `card` | Карточка на главном экране и в профиле; её кнопки — только в профиле |
| `block02` / `valueRow` | Строка в профиле: `title`, `subtitle`, `text`, кнопки |
| `block03` / `actionSheet` | Модальное окно по действию `openBlock` |
| `block04` / `selectionList` | `option01` — VLESS, `option02` — VK TURN, `option03` пока не поддерживается |
| `block05` / `menu` | Пункты профиля; перед `item03` клиент вставляет свои логи и настройки |
| `block06` / `progress` | `title`, `subtitle` и индикатор из `value`, `maximum`, `unit`, `period`, `resetAt` |

Типы известных ID проверяются как в клиенте. Неизвестные ID допустимы в JSON,
но текущий интерфейс их не размещает. Порядок блоков на экранах задаётся клиентом.
Все тексты — обычные строки; ссылки и действия задаются в JSON.

Встроенные «Подписка», «Обновить», «Изменить», вкладки и оформление остаются
в приложении. Индикатор сам форматирует байты в GB, проценты, период и дату;
его `text` не отображается. Кнопки вариантов показываются только при
`restricted` и известном транспорте; `stateText` для `option03` заменяется
на «В разработке». Пример не содержит настроек для этих неуправляемых элементов.

Сервер возвращает оба UI endpoint без подстановок. Их содержимое не зависит
от наличия VPN-файла или транспортной ссылки. Клиент дополнительно проверяет
готовность транспорта и хранит последний полный комплект данных при ошибке
обновления. Полный формат полей и действий — в [SPECIFICATION.md](SPECIFICATION.md).

## CLI и тесты

```sh
python3 server.py --check
python3 server.py --env /path/to/.env
python3 server.py --data /path/to/ui.json --check
python3 server.py --host 127.0.0.1 --port 8090
python3 -m unittest discover -s tests -v
curl --fail http://localhost:8082/health
```

`--check` проверяет токен, UI, включённый VPN-конфиг и правила доступа без открытия порта.
`--data` задаётся относительно текущего каталога. Остановка — Ctrl+C.

Тесты используют только синтетические данные и временные локальные порты.
Если рядом находится `android-client`, доступны JDK 17+, Android SDK и кеш
Gradle-зависимостей, четыре дополнительных теста компилируют текущий слой данных
и загружают подписку настоящим `SubscriptionRepository`. Проверяются raw VLESS,
failover, подписанный Bearer, TURN без VLESS, запрет при ошибке UI, устаревшее
разрешение и восстановление доступа. Исходники Android не изменяются; используются
только синтетические файлы, in-memory storage и loopback HTTP. Другой путь к клиенту
можно задать через `ANDROID_CLIENT_PATH`. Gradle запускается с `--offline`.

Если доступен Xcode и рядом находится проект клиента `sing-prototype`, три
дополнительных теста компилируют его настоящий Swift-загрузчик и проверяют
загрузку с этого сервера. Без клиентского проекта эти проверки пропускаются;
сервер и остальные тесты полностью самостоятельны.
