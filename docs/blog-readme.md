# База знаний SOCMASTER — README

Минимальная инструкция по управлению блогом `/blog`.

## Архитектура

- **Storage:** JSON-файлы в `data/blog/`.
  - `data/blog/categories.json` — список категорий.
  - `data/blog/tags.json` — список тегов.
  - `data/blog/posts/<slug>.json` — статьи.
- **Service:** `backend/services/blog_service.py` — read-only API (filter, search, related).
- **Router:** `backend/routers/blog.py` — публичные handler'ы.
- **Templates:** `templates/blog/list.html` + `templates/blog/post.html`.
- **SEO:** `backend/routers/seo.py` — `/sitemap.xml` динамически включает все опубликованные статьи.

## Создать новую статью

1. Скопировать любой существующий файл в `data/blog/posts/` под новым именем (slug в kebab-case latinized).
2. Заполнить поля:
   - `slug` — должен совпадать с именем файла без `.json`
   - `title` — заголовок (H1, до 70 символов)
   - `meta_title` — для SEO title (до 60 символов)
   - `meta_description` — для SEO meta (до 160 символов)
   - `category` — slug из `categories.json`
   - `tags` — массив slug'ов из `tags.json`
   - `excerpt` — краткое описание (для карточки и OG)
   - `cover_image` — URL обложки (опционально)
   - `author` — обычно `SOCMASTER`
   - `published_at` — ISO дата `YYYY-MM-DD`
   - `status` — `draft` или `published` (только `published` показывается публично)
   - `reading_time_minutes` — int (расчёт: 200 слов в минуту)
   - `audience` — короткое описание целевой аудитории
   - `what_youll_learn` — массив тезисов (5–7 пунктов)
   - `content` — HTML тело статьи (используется `|safe`)
   - `faq` — массив `[{q, a}, ...]`
3. Перезагрузка кэша не нужна — `list_categories` / `list_tags` cached, но статьи читаются заново при каждом запросе. Категории/теги кэшируются — для пере-загрузки рестартни fb-master.

## Опубликовать статью на VPS

```bash
rsync -avz data/blog/posts/<новая-статья>.json root@46.62.230.106:/opt/fb-master/data/blog/posts/
ssh root@46.62.230.106 'chown fbmaster:fbmaster /opt/fb-master/data/blog/posts/<новая-статья>.json'
```

Перезапуск fb-master НЕ нужен (статьи читаются по запросу). Для смены категорий/тегов — нужен restart:
```bash
ssh root@46.62.230.106 'systemctl restart fb-master'
```

## Поменять статус draft → published

Изменить поле `status` в JSON-файле, затем тот же rsync.

## Категории / теги

`data/blog/categories.json` и `data/blog/tags.json` — массивы.
- Slug — kebab-case (для URL `/blog?category=<slug>`).
- После изменения нужен **рестарт fb-master** (lru_cache в blog_service).

## SEO checklist для каждой статьи

- [ ] H1 = `title` (рендерится в шаблоне)
- [ ] meta_title до 60 chars
- [ ] meta_description 120–160 chars
- [ ] Slug на латинице, без подчёркиваний (kebab-case)
- [ ] FAQ (минимум 3 Q&A) — генерирует FAQPage Schema.org
- [ ] Внутренние ссылки на 2–3 другие статьи блога (в `content`)
- [ ] CTA на /buy в середине и в конце (по шаблону `<div class="callout">`)
- [ ] Tags 4–7 шт из `tags.json`
- [ ] Чёткая категория

## Контент-план

Полный план на 90 дней — `docs/blog-content-plan.md`.

## Будущая автоматизация (TODO)

- Скрипт `scripts/generate-daily-article.py` для генерации черновика по pricing tier.
- Cron на VPS для ежедневного draft.
- Минимальная админка `/admin/blog` для смены статусов.
- RSS-feed `/blog/feed.xml`.
- Полнотекстовый поиск по article body (sqlite FTS5 или Postgres tsvector).
