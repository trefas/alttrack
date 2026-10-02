# alttrack

Инструмент отслеживания жизненного цикла пакетов в репозиториях **Альт Линукс**.

При запуске приложение освежает данные из [ALTRepo API](https://rdb.altlinux.org/api/docs)
(`rdb.altlinux.org` — не требует ключей и не зависит от packages.altlinux.org),
сравнивает их с сохранёнными снимками и записывает события в **журнал**:

- изменение версии/релиза в ветке;
- появление/исчезновение пакета в ветке;
- смена сопровождающего (packager и состав ACL);
- errata и уязвимости (CVE);
- сборки, попавшие в репозиторий;
- **сборочные задания**: появление, смена состояния, сбой, успех, повторная попытка.

Сверх того приложение работает с **продуктами** — именованными образами
дистрибутива (ветка + edition + архитектура), за которыми закреплён продуктолог:

- каталог образов с rdb (`/image/image_info`, 361 образ);
- **массовое добавление**: все srpm образа уходят в отслеживание за секунды
  (маппинг бинарников → srpm одним POST, справочник и baseline — одним запросом);
- **diff-обновление состава**: новые добавляются, выпавшие — на паузу,
  вернувшиеся — возобновляются, с понятным отчётом;
- **сравнение двух списков пакетов** с четырьмя статусами (только слева /
  только справа / версии отличаются / совпадают), группировкой по srpm,
  CSV-выгрузкой и историей.

Есть CRUD списка отслеживаемых пакетов, архивация журнала с полнотекстовым поиском
(FTS5) и два интерфейса — **CLI** и **локальный веб-дашборд**, работающие на одном
сервисном слое.

## Установка

Каталог проекта — `alttrack/` (именно там лежит `pyproject.toml`):

```bash
cd alttrack                  # ← из /home/trefas/Проекты/packages
python3 -m venv .venv
.venv/bin/pip install -e .   # или: .venv/bin/pip install -e ".[dev]" для тестов
```

Команда появляется в `.venv/bin/alttrack`. Запускать её можно двумя способами:

```bash
# 1) без активации — прямой путь (работает всегда)
.venv/bin/alttrack --help

# 2) активировать окружение один раз в сессии — дальше просто alttrack
source .venv/bin/activate
alttrack --help
```

Чтобы `alttrack` был доступен из любого каталога и не зависел от активации:

```bash
# вариант A: ссылка в ~/.local/bin (обычно уже в PATH)
ln -sf "$PWD/.venv/bin/alttrack" ~/.local/bin/alttrack

# вариант B: pipx (если установлен) — изолированная установка
pipx install .
```

Если `pip install -e .` падает с `does not appear to be a Python project` —
вы находитесь вне каталога `alttrack/`, перейдите в него (`cd alttrack`).

## Быстрый старт

```bash
alttrack watch add firefox -b sisyphus -b p11   # добавить пакет (CRUD: Create)
alttrack watch list                             # список                 (Read)
alttrack watch edit firefox --add-branch p10    # скорректировать ветки   (Update)
alttrack watch show firefox                     # состояние по веткам
alttrack watch rm firefox                       # убрать из отслеживания  (Delete)
alttrack watch backfill --all                   # импорт истории сборок (с прогрессом)
alttrack refresh                                # освежить вручную
alttrack log -n 30                              # журнал событий
alttrack search "firefox AND CVE"               # поиск по журналу и архиву
alttrack tasks --active                         # идущие сборки
alttrack task show 434805                       # карточка задания
alttrack archive --older-than 90 --dry-run      # архивация (пробный запуск)
alttrack stats                                  # статистика
alttrack serve                                  # веб-интерфейс + фоновое освежение
```

### Продукты, образы, сравнение

```bash
# 1) продукт = ветка + edition + архитектура
alttrack product create "Образование 11" --branch p11 --edition alt-education

# 2) каталог образов и состав
alttrack image refresh                          # обновить каталог (361 образ)
alttrack image list --branch p11 --edition alt-education
alttrack image preview <uuid> -p 1              # dry-run: что будет добавлено
alttrack image track <uuid> -p 1                # добавить все srpm образа
alttrack image update -p 1 --latest             # diff-обновление состава

# 3) списки и сравнение
alttrack list save-image <uuid> --title "11.2"  # образ как список
alttrack list save-branch p11 --arch x86_64     # состояние ветки как список
alttrack list save-file packages.txt -b p11     # загруженный файл (путь, epoch, .rpm)
alttrack compare run -l 1 -r 2 -t "11.2 ↔ p11"  # отчёт + история
alttrack compare show 1 --status changed        # одна группа; --csv file.csv
alttrack meta sync --branch p11                 # справочник (summary/группа/ACL)
alttrack meta refs                              # архитектуры и группы ПО из rdb
```

По умолчанию массовое добавление включает **только отслеживание версий**
(errata/ACL/задания выключены — это 1 запрос API на пакет при освежении);
переключатели включаются флагами `--errata/--maintainer/--tasks` или в вебе.

## Поведение при первом запуске

База создаётся пустой: нет ни пакетов, ни событий. Дашборд предлагает добавить
первый пакет. При добавлении:

1. имя валидируется по API (можно указывать бинарное имя);
2. ветки выбираются явно; наличие пакета в ветке проверяется — **отсутствие
   даёт предупреждение, но не блокирует** (событие о появлении будет записано);
3. создаётся baseline-снимок и событие `tracking_started`;
4. импортируется история сборок (по умолчанию 50 на ветку, `--no-backfill`
   отключает, `--backfill-limit N` меняет лимит).

Снимок ветки, убранной из отслеживания, **сохраняется**: если вернуть ветку
позже, сравнение пойдёт с прежним состоянием.

## События журнала

| Тип | Значение |
|---|---|
| `tracking_started` | начало отслеживания пакета |
| `version_changed` | новая версия/релиз в ветке |
| `added_to_branch` / `removed_from_branch` | пакет появился / исчез из ветки |
| `maintainer_changed` | сменился packager (при новой сборке) или состав ACL |
| `errata` | новое errata с идентификаторами CVE (исторические не дублируются) |
| `build` | сборка, попавшая в репозиторий (в т.ч. импорт истории) |
| `not_found` | пакет пропал из всех репозиториев (фиксируется один раз) |
| `task_discovered` | на сборочнице появилось задание по пакету |
| `task_state_changed` | переход состояния задания (BUILDING → TESTED → …) |
| `task_failed` | сбой сборки (`FAILED`, `EPERM`) |
| `task_done` | сборка успешно завершена |
| `task_retried` | вырос номер попытки сборки |

Этапы задания (`task-build`, `task-repo-elfsym`, `task-save-repo`) и прогресс по
архитектурам копятся в таблице `task_stages` и показываются на странице задания —
в журнал они не попадают, чтобы не раздувать его.

## Веб-интерфейс

```bash
alttrack serve            # http://127.0.0.1:8300
alttrack serve --port 9000 --no-refresh
```

| Раздел | Дублирует CLI |
|---|---|
| `/packages`, `/packages/new`, `/packages/{id}/edit` | `watch list/add/show/edit/rm` |
| `/journal` (фильтры, пагинация, поиск) | `log`, `search` |
| `/archive` (ручная архивация, очистка) | `archive` |
| `/stats` | `stats` |
| `/tasks`, `/tasks/{id}` | `tasks`, `task show` |
| `/runs` | `runs` |
| `/settings` | `config` (записывает TOML), `meta refs` (справочники архитектур/групп) |
| `/products`, `/products/{id}` (состав, предпросмотр, diff) | `product list/show/create/rm`, `image preview/track/update` |
| `/images` (каталог образов с фильтрами) | `image list/refresh` |
| `/lists` (загрузка файла, drag&drop, вставка, снятие ветки) | `list save-file/save-image/save-branch/rm` |
| `/compare`, `/compare/{id}` (отчёт, CSV, история) | `compare run/show/history/rm` |
| кнопка «Обновить сейчас» | `refresh` |
| кнопка «Импортировать историю сборок» на странице пакета | `watch backfill` |

Массовые операции (добавление образа, diff-обновление, импорт истории,
обновление справочника) выполняются **в фоне**: страница показывает баннер
задачи и сама перезагружается по завершении (`GET /api/jobs` — статус).

**Селектор продукта** в шапке фильтрует журнал, дашборд и сборочные задания по
пакетам выбранного продукта; при единственном продукте он выбран автоматически.

При старте выполняется освежение, дальше — фоновый интервал (`refresh_interval`,
по умолчанию 30 мин). Поскольку сборочные задания опрашиваются в том же прогоне,
задержка обнаружения сбоя сборки равна этому интервалу — уменьшите его в
настройках, если нужно замечать сбои быстрее (задачи стоят одного запроса).

## Конфигурация

Файл `~/.config/alttrack/config.toml` (создаётся по необходимости, доступен из
веб-раздела «Настройки»):

```toml
refresh_interval = 30        # минуты
task_history_days = 7        # горизонт новых сборочных заданий
backfill_limit = 50          # история сборок при добавлении
archive_after_days = 90      # возраст архивации
max_live_rows = 5000         # потолок живого журнала
auto_archive = true
http_timeout = 30.0
http_concurrency = 8
host = "127.0.0.1"
port = 8300
```

Переопределяется переменными окружения `ALTTRACK_DB`, `ALTTRACK_CONFIG`,
`ALTTRACK_REFRESH_INTERVAL`, `ALTTRACK_API_BASE_URL` и др., а также опциями CLI
(`--db`).

## Хранение

`~/.local/share/alttrack/alttrack.db` (SQLite, WAL):

- `tracked_packages` — список отслеживаемых пакетов (ветки, переключатели, заметки);
- `snapshots` — последнее известное состояние по каждой ветке;
- `journal` / `journal_archive` — живой журнал и архив (общий индекс `events_fts`);
- `build_tasks`, `task_packages`, `task_stages` — сборочные задания и их этапы;
- `errata_seen`, `tasks_seen` — дедупликация событий;
- `runs` — история прогонов освежения;
- `products`, `product_packages`, `product_images` — продукты, их состав и
  привязанные образы;
- `image_catalog` — кэш каталога образов (обновляется вручную);
- `package_meta` — справочник srpm (summary, группа, сопровождающий, `pkghash`);
- `lists`, `list_items` — сохранённые списки пакетов;
- `comparisons` — история сравнений (метаданные и снимок статистики; сам отчёт
  пересчитывается на лету).

Архивация переносит события старше `archive_after_days` и подрезает живой журнал
до `max_live_rows`; выполняется автоматически при прогоне и вручную (с
`--dry-run`). Поиск идёт одновременно по живому журналу и архиву.

## Регулярный запуск

```bash
# cron
*/30 * * * *  /path/.venv/bin/alttrack refresh

# systemd user timer
[Timer]
OnBootSec=2min
OnUnitActiveSec=30min
```

Если приложение работает через `alttrack serve`, дополнительный cron не нужен —
освежение выполняется автоматически при старте и по интервалу.

## Запуск в Docker

Сборка и запуск веб-интерфейса (`alttrack serve`) в контейнере:

```bash
docker compose up -d --build   # сборка образа и запуск
docker compose ps              # дождаться статуса healthy
open http://127.0.0.1:8300     # или откройте в браузере
```

Образ: `python:3.13-slim`, установка пакета обычным `pip install .`,
непривилегированный пользователь `app`, порт `8300`.

| Что | Где |
|---|---|
| база SQLite | том `alttrack-data` → `/data/alttrack.db` |
| `config.toml` | том `alttrack-config` → `/config/config.toml` |
| healthcheck | `GET /healthz` |

Конфигурация — через переменные окружения сервиса в `docker-compose.yml`
(значения берутся из `.env`, см. `.env.example`): `ALTTRACK_PORT` (порт хоста),
`ALTTRACK_REFRESH_INTERVAL`, `ALTTRACK_ARCHIVE_AFTER_DAYS`,
`ALTTRACK_HTTP_TIMEOUT`, `ALTTRACK_API_BASE_URL`.

```bash
docker compose logs -f            # журнал
docker compose down               # остановить (тома сохраняются)
docker compose down -v            # остановить и удалить тома (потеря данных)
docker compose exec alttrack alttrack watch list   # CLI внутри контейнера
```

Обновление после изменений в коде: `docker compose up -d --build`.

## Разработка

```bash
pip install -e ".[dev]"
pytest          # 118 тестов: дифф, CRUD, задания, архивация, FTS, парсер,
                # продукты/образы, списки/сравнение, веб
```

Тесты не выходят в сеть — API подменяется фейковым клиентом (`tests/conftest.py`).

## Структура

```
src/alttrack/
  api.py       клиент ALTRepo API (httpx, retry, параллелизм, image/source/repo)
  rpmparse.py  разбор NEVRA из строк файловых списков (пути, epoch, .rpm)
  config.py    TOML/окружение/путь к БД
  db.py        схема SQLite и миграции
  watchlist.py CRUD отслеживаемых пакетов + проверка веток
  refresh.py   снимки → дифф → события, батч ACL, errata, backfill
  tasks.py     сборочные задания, таймлайн этапов
  journal.py   запись событий, фильтры, FTS-поиск, статистика
  archive.py   архивация и очистка
  products.py  продукты: массовое добавление srpm образа, diff-обновление
  images.py    каталог образов (кэш, фильтры, последний release-образ)
  metasync.py  справочник package_meta (sync/ensure/lookup)
  refs.py      справочники rdb: архитектуры и группы ПО (первое заполнение)
  lists.py     сохранённые списки (файл/образ/ветка) + маппинг в srpm
  compare.py   сравнение двух списков, статусы, CSV, история
  cli.py       команды typer/rich
  web/         FastAPI + Jinja2 (дублирует CLI)
```
