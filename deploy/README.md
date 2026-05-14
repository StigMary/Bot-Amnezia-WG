# Auto-deploy на S1 через GitHub Actions

Workflow: [`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml)

Запускается **только по тегу `v*`** (например `v3.1.0`) или вручную через `workflow_dispatch`.

---

## 1. GitHub Secrets

Settings → Secrets and variables → Actions → **New repository secret**:

| Имя              | Значение                                                      |
|------------------|----------------------------------------------------------------|
| `DEPLOY_HOST`    | IP или домен S1 (например `s1.example.com`)                    |
| `DEPLOY_USER`    | `vpnuser`                                                      |
| `DEPLOY_PORT`    | SSH-порт (необязательно, по умолчанию `22`)                    |
| `DEPLOY_SSH_KEY` | приватный ключ ED25519 в PEM-формате (см. ниже)                |

### Сгенерировать ключ для CI

На локальной машине:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/gha_deploy_key -C "github-actions@bot-amnezia-wg" -N ""
```

Публичную часть добавить в `~vpnuser/.ssh/authorized_keys` на S1:

```bash
ssh root@S1 "echo '$(cat ~/.ssh/gha_deploy_key.pub)' >> /home/vpnuser/.ssh/authorized_keys && chown vpnuser:vpnuser /home/vpnuser/.ssh/authorized_keys && chmod 600 /home/vpnuser/.ssh/authorized_keys"
```

Приватную часть (`cat ~/.ssh/gha_deploy_key`) — целиком (включая `-----BEGIN…END-----`) — вставить в Secret `DEPLOY_SSH_KEY`.

---

## 2. Sudoers на S1

Чтобы `vpnuser` мог рестартить сервис без пароля, на S1 от root:

```bash
echo 'vpnuser ALL=(root) NOPASSWD: /bin/systemctl restart vpn-bot.service, /bin/systemctl status vpn-bot.service, /bin/systemctl is-active vpn-bot.service' \
  | sudo tee /etc/sudoers.d/vpnuser-bot
sudo chmod 440 /etc/sudoers.d/vpnuser-bot
sudo visudo -cf /etc/sudoers.d/vpnuser-bot   # проверка синтаксиса
```

---

## 3. Подготовка репозитория на S1

```bash
sudo -u vpnuser bash <<'EOF'
cd /home/vpnuser/vpn_bot
git remote -v   # должен указывать на https://github.com/StigMary/Bot-Amnezia-WG.git
git fetch --all --tags
EOF
```

Если репозиторий ещё не клонирован — сначала склонируйте, сохранив `.env` и БД отдельно.

---

## 4. Релизный цикл

```bash
# 1. На локальной машине: финализировать CHANGELOG, обновить версию
# 2. Закоммитить и запушить
git push origin main

# 3. Создать тег
git tag -a v3.1.0 -m "Release v3.1.0: venv + systemd hardening + alerts"
git push origin v3.1.0

# 4. GitHub Actions автоматически запустит deploy.yml
# 5. Создать GitHub Release из тега (Releases → Draft a new release)
```

---

## 5. Откат

Workflow содержит **автоматический rollback**: если после рестарта сервис не активен, выполняется `git checkout` на предыдущий коммит и повторный рестарт.

Ручной откат:

```bash
ssh vpnuser@S1
cd /home/vpnuser/vpn_bot
git log --oneline -5
git checkout <prev_commit_or_tag>
.venv/bin/pip install -r requirements.txt
sudo systemctl restart vpn-bot.service
```

---

## 6. Проверка после деплоя

В Actions-логе должно быть `Deploy OK: <hash>` и блок `systemctl status` без ошибок.
При сбое — алерт-скрипт ([`alert@.service`](systemd/alert@.service)) пришлёт уведомление в Telegram.
