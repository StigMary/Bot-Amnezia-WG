# 🛡 VPN Management Bot

Telegram bot for managing a VPN server based on **AmneziaVPN / WireGuard**.  
Allows the administrator to manage clients, subscriptions, and devices directly from Telegram.

---

## ✨ Features

### 👑 For the Administrator
- 📋 View the list of all clients with IP, aliases, and subscription dates
- 💳 Extend subscriptions for clients (including manual date setting)
- 🔗 Silent binding of configs to Telegram accounts (without notifying clients)
- 📤 Issue a second device with **auto-parsing IP** from a `.conf` file
- 🚦 Speed Management (QoS): limit / remove restrictions
- 📊 Morning digest: only important events (expired, expiring soon)
- 🎟 Helpdesk: receive tickets from clients with reply and close buttons

### 👤 For Clients
- 🏠 Personal Cabinet: subscription status, list of devices
- 💳 Payment: payment details + send a photo of the receipt to the administrator
- ➕ Request a second device (limit: 2 configurations per account)
- ✏️ Rename own devices (does not affect administrator's marks)
- 🛠 Connection instructions with fresh links to apps
- 🎧 Support: open a ticket with the ability to write messages and send photos

---

## 🗂 Project Structure

```
vpn_bot/
├── bot.py                  # Entry point, bot initialization
├── config.py               # Settings from .env
├── database.py             # SQLite: schema, migrations, CRUD
├── tasks.py                # Scheduler (morning digest, billing)
├── vpn_engine.py           # WireGuard operations: parsing, QoS
├── admin.py                # (legacy compatibility)
├── handlers/
│   ├── admin.py            # All administrator commands
│   ├── client.py           # Client menu and ticket system
│   ├── decorators.py       # Middleware: rate_limit, admin_only, etc.
│   └── tasks.py            # Task handlers (if extracted)
├── data/                   # ← DO NOT commit (DB, logs, pids)
├── .env                    # ← DO NOT commit (secrets)
├── .env.example            # Environment variables template
├── requirements.txt        # Python dependencies
└── vpn-bot.service         # Systemd unit file for autostart
```

---

## 🚀 Installation and Deployment

### 1. Clone the repository
```bash
git clone https://github.com/YOUR_ACCOUNT/vpn_bot.git
cd vpn_bot
```

### 2. Install dependencies
```bash
pip3 install -r requirements.txt
```

### 3. Configure environment
```bash
cp .env.example .env
nano .env   # Enter bot token, admin_id, etc.
```

### 4. Create data folder
```bash
mkdir -p data
```

### 5. Run as systemd service
```bash
sudo cp vpn-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable vpn-bot
sudo systemctl start vpn-bot
```

### View logs
```bash
journalctl -u vpn-bot -f
```

---

## ⚙️ Environment Variables (`.env`)

| Variable | Description |
|---|---|
| `BOT_TOKEN` | Bot token from @BotFather |
| `ADMIN_ID` | Telegram ID of the administrator |
| `GROUP_ID` | Group ID for client verification |
| `PAYMENT_DETAILS` | Phone number / payment details |
| `PAYMENT_AMOUNT` | Subscription cost (e.g., `200₽`) |
| `DB_FILE` | Path to the SQLite database file |
| `SERVER_NAME` | Server name (for the digest) |

---

## 🔒 Security

- SQL injections are closed via **whitelist of fields** (`ALLOWED_FIELDS`) in `database.py`
- All user aliases pass through `sanitize_alias()` (XSS protection)
- Rate limiting on client commands
- Separation of rights: middleware `admin_only` / `group_member_only`
- Secrets are extracted to `.env` and excluded from the repository

---

## ☕ Support the Project

If you found this project useful, I would appreciate any support! 🙏

| Method | Link |
|---|---|
| 🚀 **Boosty** | [boosty.to/kjznnetx/donate](https://boosty.to/kjznnetx/donate) |
| 💎 **TON** | `UQCGEByMVxefI4hpLvoCOuvaS9EbDPC0d-wPzMCuBql9DFJW` |

[![Boosty](https://img.shields.io/badge/Boosty-Support-orange?style=for-the-badge)](https://boosty.to/kjznnetx/donate)
[![TON](https://img.shields.io/badge/TON-Send-0098EA?style=for-the-badge&logo=telegram)](https://app.tonkeeper.com/transfer/UQCGEByMVxefI4hpLvoCOuvaS9EbDPC0d-wPzMCuBql9DFJW)

---

## 📝 License

Private project. All rights reserved.
