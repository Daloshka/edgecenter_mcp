# edgecenter_mcp

MCP-сервер для облачного API [EdgeCenter](https://edgecenter.ru). Даёт
MCP-клиенту — Claude Code, Claude Desktop или любому другому — доступ к вашему
аккаунту EdgeCenter на чтение и управление питанием нод под подтверждение.

Сделан для тех, кто держит флот: один вызов показывает все baremetal- и
виртуальные машины по всем регионам с адресами, статусами и ценой аренды,
другой выдаёт ссылку на VNC-консоль ноды, переставшей отвечать по SSH.

*[English version](README.md)*

## Что умеет

- **Инвентарь флота одним вызовом** — все инстансы во всех регионах, публичные,
  плавающие и приватные адреса сопоставлены друг с другом.
- **Стоимость аренды** — по каждой ноде и итогом по регионам, прямо из API.
- **Возврат в мёртвую ноду** — ссылка на noVNC-консоль без входа в панель.
- **Видимость задач облака** — что происходит с инфраструктурой и чем
  закончились прошлые операции.
- **Запасной ход** — `api_request` достаёт любой эндпоинт, не покрытый
  остальными инструментами.
- **Предохранители** — управление питанием требует явного подтверждения, а
  режим read-only блокирует любые изменения.

## Требования

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) — зависимости объявлены в самом скрипте
  (PEP 723), отдельное окружение создавать не нужно
- Аккаунт EdgeCenter с постоянным API-токеном

## Быстрый старт

```bash
git clone https://github.com/Daloshka/edgecenter_mcp.git ~/Tools/edgecenter_mcp

mkdir -p ~/.config/edgecenter_mcp
cp ~/Tools/edgecenter_mcp/config.example.json ~/.config/edgecenter_mcp/config.json
$EDITOR ~/.config/edgecenter_mcp/config.json     # вставьте свой API-токен
chmod 600 ~/.config/edgecenter_mcp/config.json

# регистрация в Claude Code
claude mcp add edgecenter_mcp -- uv run --script ~/Tools/edgecenter_mcp/server.py
claude mcp get edgecenter_mcp                    # должно быть: Connected
```

Для Claude Desktop добавьте в `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "edgecenter_mcp": {
      "command": "uv",
      "args": ["run", "--script", "/абсолютный/путь/edgecenter_mcp/server.py"]
    }
  }
}
```

Если клиент стартует с урезанным `PATH`, укажите абсолютный путь к `uv`
(посмотреть: `which uv`).

## Как получить API-токен

**Через панель.** Войдите в [EdgeCenter](https://edgecenter.ru), профиль →
**API-токены** → создать. Выберите бессрочный, если сервер должен работать без
вашего участия.

**Или через API,** взяв JWT из DevTools как стартовый доступ:

```bash
curl -X POST "https://api.edgecenter.ru/iam/clients/<CLIENT_ID>/tokens" \
  -H "Authorization: Bearer <JWT_ИЗ_DEVTOOLS>" \
  -H "Content-Type: application/json" \
  -d '{"name":"mcp","description":"MCP server","exp_date":null,
       "client_user":{"role":{"id":1,"name":"Administrators"}}}'
```

`exp_date: null` — токен бессрочный. Список токенов покажет инструмент
`api_tokens()`, отзыв — `DELETE /iam/clients/<CLIENT_ID>/tokens/<TOKEN_ID>`.

Схему авторизации сервер выбирает сам: постоянный токен уходит как
`Authorization: APIKey <token>`, браузерный JWT — как `Bearer <token>`.
Постоянный токен, отправленный как `Bearer`, даёт ошибку «jwt decode failed» —
на этом легко потерять час.

> **Осторожно с `$` в шелле.** Токены EdgeCenter выглядят как `12345$abcdef…`.
> В двойных кавычках шелл съедает всё после `$` как имя переменной, поэтому
> `curl -H "Authorization: APIKey $ТОКЕН_ЦЕЛИКОМ"` отправляет обрезанный токен и
> вы получаете `401` на всё подряд. Используйте одинарные кавычки или читайте
> токен из файла конфигурации.

## Конфигурация

Переменные окружения приоритетнее файла. Файл ищется по пути
`$EDGECENTER_MCP_CONFIG`, затем `~/.config/edgecenter_mcp/config.json`.

| Ключ конфига | Переменная | Значение |
|---|---|---|
| `api_token` | `EDGECENTER_API_TOKEN` | постоянный токен или JWT — обязателен |
| `base_url` | `EDGECENTER_BASE_URL` | по умолчанию `https://api.edgecenter.ru` |
| `project_id` | `EDGECENTER_PROJECT_ID` | необязателен; если проектов несколько, берётся первый — укажите явно, чтобы выбрать другой (список даёт `whoami()`) |
| `client_id` | `EDGECENTER_CLIENT_ID` | необязателен, берётся из `/iam/users/me` |
| `readonly` | `EDGECENTER_MCP_READONLY=1` | отклонять любые изменения |

## Инструменты

Только чтение — безопасны в любой момент:

| Инструмент | Что делает |
|---|---|
| `whoami()` | аккаунт, клиент, подключённые услуги, проекты, регионы |
| `regions()` | регионы с флагами baremetal/KVM |
| `servers()` | инвентарь флота; `with_floating_ips=True` подтягивает плавающие и приватные адреса |
| `costs()` | цена каждой ноды и итог по регионам |
| `fleet_health()` | кто не ACTIVE, кто завис в task_state, какие задачи идут |
| `server(query)` | детали ноды: железо, интерфейсы, VLAN sub-ports, цена |
| `console(query)` | одноразовая ссылка на noVNC-консоль |
| `metrics(query)` | метрики CPU/сети/диска (только KVM, у baremetal пусто) |
| `tasks()` / `task(id)` | история задач облака и результат одной задачи |
| `network(region_id)` | сети, подсети, плавающие IP, security groups, роутеры |
| `ssh_keys()` | SSH-ключи по региону или по всем сразу |
| `images(region_id)` | доступные образы ОС, baremetal или виртуальные |
| `api_tokens()` | выпущенные API-токены и дата последнего использования |

Изменяющие — отклоняются без `confirm=True` и всегда в режиме read-only:

| Инструмент | Что делает |
|---|---|
| `server_action(query, action, confirm)` | `start`, `stop`, `reboot`, `powercycle`, `suspend`, `resume` |
| `api_request(path, method, params, body, confirm)` | любой эндпоинт; `{project}` в пути подставляется |

Везде, где принимается `query`, подойдёт имя ноды, её UUID (или префикс) либо
любой её адрес — публичный, плавающий или приватный. Регистр не важен.

Два параметра встречаются у многих инструментов: `refresh=True` обходит кэш
(инвентарь живёт 60 секунд, список регионов — час), а `raw=True` возвращает
сырой JSON вместо таблицы — удобно, когда вывод нужно обрабатывать, а не читать.

## Модель безопасности

- У читающих инструментов проставлен `readOnlyHint`, у изменяющих —
  `destructiveHint`, поэтому клиент может показывать их по-разному.
- `server_action` и изменяющие вызовы `api_request` возвращают ошибку с
  объяснением, пока не передан `confirm=True`. Это не даёт модели перезагрузить
  прод по собственной инициативе.
- `EDGECENTER_MCP_READONLY=1` отклоняет изменения даже с `confirm=True` —
  разумно включить для всего, что работает без присмотра.
- `powercycle` обесточивает машину без корректного выключения. Он существует для
  нод, которые уже не отвечают, и это написано прямо в описании инструмента.

## Карта API

Проверено живыми запросами. Списки приходят в конверте `{count, results}`.

```
GET  /iam/users/me · /iam/clients/me · /iam/clients/{client}/tokens
GET  /cloud/v1/projects · /cloud/v1/regions
GET  /cloud/v1/bminstances/{project}/{region}      # baremetal, все поля
GET  /cloud/v1/instances/{project}/{region}        # только KVM
GET  /cloud/v1/instances/{project}/{region}/{id}   # работает и для baremetal
GET  …/{id}/interfaces · …/{id}/ports · …/{id}/get_console
GET  /cloud/v1/price_info/{project}/{region}/instances/{id}
POST …/{id}/metrics            {"time_interval": 6, "time_unit": "hour"}
POST …/{id}/{action}           start|stop|reboot|powercycle|suspend|resume
POST …/{id}/attach_interface · …/{id}/detach_interface · …/{id}/put_into_servergroup
GET  /cloud/v1/tasks?project_id=…&region_id=…&limit=… · /cloud/v1/tasks/{id}
GET  /cloud/v1/{networks|subnets|routers|floatingips|securitygroups|keypairs
      |servergroups|volumes|snapshots|images|bmimages|flavors|bmflavors
      |loadbalancers}/{project}/{region}
```

Маршрутов, которых **нет**, хотя они напрашиваются или встречаются в
документации:

```
POST /cloud/v1/instances/{p}/{r}/{id}/action        # схемы «action в теле» тут нет
POST /cloud/v1/bminstances/{p}/{r}/{id}/reboot      # у bminstances действий нет вообще
     …/reboot_hard · …/power-cycle · …/pause · …/rebuild · …/rescue · …/resize
GET  /cloud/v1/quotas* · /cloud/v1/client_quotas/*  # квот в API нет
GET  /billing/v1/* · /iam/clients/{id}/{balance,invoices}
GET  /cloud/v1/price_info/{p}/{r}/{floatingips|volumes|networks}/{id}
```

Квоты, баланс, счета и скидки живут только в панели. `price_info` отдаёт прайс
инстанса, а не выставленный счёт.

### Грабли

- **Baremetal не виден в листинге `/instances`** — там `count: 0`, ноды лежат в
  `/bminstances`. При этом получение одной ноды и все действия идут через
  `/instances/{project}/{region}/{id}` с тем же id.
- **Как проверить маршрут, не выполняя действие**: POST на несуществующий UUID.
  Пустой `404` — маршрута нет; JSON `{"exception_class": "NotFoundError"}` —
  маршрут есть, не нашёлся только инстанс. Список действий выше составлен так,
  ничего не перезагружая.
- **У ноды бывает два публичных адреса** — из записи инстанса и плавающий IP,
  привязанный к приватному адресу VLAN sub-port. `servers()` показывает первый,
  `servers(with_floating_ips=True)` сопоставляет оба.
- **`OPTIONS` на `/cloud/*` всегда отвечает `204`** без схемы, поэтому для
  разведки бесполезен. Под `/iam/*` он отдаёт полное описание полей.
- **`bmflavors?include_prices=true` возвращает `null`** для baremetal, хотя у
  виртуальных `g1-*` цены настоящие. Реальная цена baremetal — только поштучно
  через `price_info`.
- **Метрики baremetal всегда пустые** — EdgeCenter собирает их только для KVM.
- **`status: ACTIVE` — мнение оркестратора.** Нода бывает ACTIVE и при этом
  недоступна; это подсказка, а не проверка здоровья.

## Разработка

```bash
uv run --script server.py                 # запуск сервера на stdio (Ctrl-C)
uv run --script examples/smoke_test.py    # список инструментов и проверка чтения
uvx ruff check .                          # линтер
```

`examples/smoke_test.py` ходит в ваш реальный аккаунт только на чтение и
проверяет, что изменяющие инструменты отказываются работать без подтверждения.

## Совместимость

API EdgeCenter происходит из той же кодовой базы, что и Gcore, поэтому большая
часть путей совпадает. Укажите другой `base_url`, чтобы попробовать — всё
остальное определяется во время работы.

## Лицензия

MIT — см. [LICENSE](LICENSE).
