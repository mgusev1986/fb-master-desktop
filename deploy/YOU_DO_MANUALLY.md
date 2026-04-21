# Только то, что нужно сделать вам вручную

Остальное на сервере делает скрипт `deploy/bootstrap-server.sh` после того, как код скопирован в `/opt/fb-master`.

**Проще всего:** если проект лежит в **GitHub (или другом git)**, с вашего Mac/Linux выполните  
`export GIT_URL=…` и `./deploy/install-remote.sh root@IP домен email` — см. шапку файла [`deploy/install-remote.sh`](install-remote.sh).

---

## 1) Виртуальный сервер (VPS)

1. У провайдера создайте сервер с **Ubuntu 22.04 или 24.04**. Для Playwright в облаке лучше **от 4 GB RAM**; минимум **2 GB**, если автоматизация только на десктопах.
2. Добавьте **SSH-ключ** (с Mac: `cat ~/.ssh/id_ed25519.pub`).
3. В фаерволе откройте порты **22, 80, 443**.
4. Сохраните **публичный IPv4**.

---

## 2) Домен → IP сервера

1. У регистратора (Namecheap, Cloudflare и т.д.) в DNS добавьте **A-запись** `@` на IPv4 сервера.
2. При необходимости **A** для `www` на тот же IP.
3. Отключите парковку/лишние редиректы у регистратора, если мешают — HTTPS настроит nginx после certbot.

Подождите 5–30 минут.

---

## 3) Supabase (браузер)

1. https://supabase.com — новый проект.
2. **Settings → Database** — строка подключения (pooler / порт **6543** для VPS) → вставите в `.env` как `DATABASE_URL`.
3. **Settings → API** — `SUPABASE_URL`, `SUPABASE_ANON_KEY` в `.env`.
4. **Authentication → URL Configuration → Redirect URLs** добавьте:  
   `https://ВАШ-ДОМЕН/auth/supabase/callback`  
   (и с `www`, если используете www).

---

## 4) NOWPayments (браузер)

1. https://account.nowpayments.io — API Key, IPN Secret.
2. IPN / callback URL: `https://ВАШ-ДОМЕН/webhooks/nowpayments`
3. Значения в `.env` на сервере: см. `.env.example` (`NOWPAYMENTS_*`).

---

## 5) Mac → залить код и запустить bootstrap (одна команда)

**Рекомендуется:** из **корня проекта** FB Master (где лежит `main.py`):

```bash
chmod +x deploy/from-mac.sh
./deploy/from-mac.sh ВАШ_IP_VPS socmaster.pro
```

Когда **DNS уже** указывает на сервер и нужен сразу **HTTPS** (email для Let's Encrypt):

```bash
./deploy/from-mac.sh ВАШ_IP_VPS socmaster.pro ваш@gmail.com
```

Скрипт делает `rsync` в `/opt/fb-master/` и по SSH запускает `deploy/bootstrap-server.sh`. Файл `.env` на сервере **не перезаписывается** (исключён из rsync). Каталог **`data/`** тоже **не синхронизируется** — иначе с Mac могут уехать файлы с владельцем `501:staff`, и сервис `fbmaster` не сможет писать в `data/logs/` (502 от nginx).

**Playwright** на сервере — передайте переменную перед вызовом:

```bash
INSTALL_PLAYWRIGHT=1 ./deploy/from-mac.sh ВАШ_IP socmaster.pro ваш@gmail.com
```

---

### Вариант вручную (две команды)

Если не хотите `from-mac.sh`:

```bash
export VPS_IP="ВАШ_IP_VPS"
rsync -avz --progress \
  --exclude '.git' --exclude '.venv' --exclude 'node_modules' \
  --exclude 'data/' --exclude '__pycache__' --exclude '.env' \
  ./ root@${VPS_IP}:/opt/fb-master/
ssh root@${VPS_IP}
```

На сервере под **root**:

```bash
cd /opt/fb-master
chmod +x deploy/bootstrap-server.sh
./deploy/bootstrap-server.sh socmaster.pro
# или с HTTPS:
./deploy/bootstrap-server.sh socmaster.pro ваш@gmail.com
INSTALL_PLAYWRIGHT=1 ./deploy/bootstrap-server.sh socmaster.pro ваш@gmail.com
```

---

## 6) Обязательно отредактировать `.env` на сервере

```bash
nano /opt/fb-master/.env
```

Минимум: `SECRET_KEY`, `APP_BASE_URL`, `OAUTH_REDIRECT_BASE`, `DATABASE_URL`, `SUPABASE_*`, `FB_MASTER_OWNER_EMAILS`, при необходимости `NOWPAYMENTS_*`. Черновик: `deploy/vps-supabase.env.example`.

Перезапуск:

```bash
systemctl restart fb-master
```

---

## 7) Проверка

- Браузер: `https://ваш-домен` — вход в кабинет.
- **Платформа → Ключи** — создать ключ, затем `FB_MASTER_ACCESS_KEY_REQUIRED=true` в `.env`.

Если что-то не стартует: `journalctl -u fb-master -n 80 --no-pager`
