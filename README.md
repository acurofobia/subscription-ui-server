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

Для телефона в той же сети задайте `HOST=0.0.0.0` и замените `localhost` на
LAN-адрес компьютера или его доступное в сети имя. На macOS имя можно узнать
командой `scutil --get LocalHostName`, затем использовать `<имя>.local`.
Для публичного размещения настройте HTTPS reverse proxy и `PUBLIC_BASE_URL`.

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
| `.env.example` | Пример настроек сервера |
| `config.example.json` | Пример одного VLESS outbound |
| `config.failover.example.json` | Пример четырёх VLESS + REALITY узлов |
| `data/subscription.example.json` | Пример UI-документа |
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

Текущий клиент разрешает нативные токены по HTTPS или loopback HTTP. Для HTTP
по LAN-адресу или имени компьютера сервер автоматически использует исходные
маршруты и исходный токен: пересборка клиента не требуется.

Если задан секрет подписи, нативный `ui_token` — base64url без padding от
HMAC-SHA256(secret, `vipin-v1:<SUBSCRIPTION_TOKEN>`). Без секрета он равен
`SUBSCRIPTION_TOKEN`. Смена секрета инвалидирует прежние нативные токены;
исходный `/v1/subscription` продолжает принимать токен подписки.

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

`--check` проверяет токен, UI и включённый VPN-конфиг без открытия порта.
`--data` задаётся относительно текущего каталога. Остановка — Ctrl+C.

Тесты используют только синтетические данные и временные локальные порты.
Если доступен Xcode и рядом находится проект клиента `sing-prototype`, три
дополнительных теста компилируют его настоящий Swift-загрузчик и проверяют
загрузку с этого сервера. Без клиентского проекта эти проверки пропускаются;
сервер и остальные тесты полностью самостоятельны.
