# Signal Digest

Signal Digest is a self-hosted news collection and email delivery service. It collects updates from selected AI and financial sources, builds a responsive HTML digest, and sends personalized editions to each subscriber on an independent schedule.

## Features

- Personalized subscriptions: each recipient selects their own sources, delivery time, and reporting window.
- Recipient privacy: every message is delivered separately, so recipients never see other email addresses.
- Multiple encrypted SMTP accounts with a configurable default sender.
- Per-subscriber scheduling with a global pause switch.
- RSS/Atom ingestion, configurable CSS selectors, and dedicated adapters for supported sites.
- Deduplication, original article links, run history, previews, and manual delivery.
- Responsive email design with platform grouping and topic labels.
- Private browser reader pages with reliable platform navigation. Reader links use random tokens, are excluded from search indexing, disable caching, and expire after 90 days.
- Optional OpenAI-compatible summarization.

## Supported source adapters

The project includes adapters for these domains:

- AI media: AI Era, Anthropic, Google DeepMind, Qwen, NVIDIA, and Hugging Face-compatible RSS feeds.
- Financial media: CLS, Sina Finance, Jin10, and other RSS or CSS-selector sources.
- Official economic sources: People's Bank of China, China Securities Regulatory Commission, National Bureau of Statistics of China, and the US Federal Reserve.

These 12 built-in sources are created automatically on first startup. New subscribers choose their own sources in the administration interface.

Website structures change over time. Use the run history to detect source-specific errors and update selectors or adapters when needed.

## Quick start

Requirements:

- Docker Engine
- Docker Compose v2

Create the configuration file:

```bash
cp .env.example .env
```

Set at least these values in `.env`:

```dotenv
APP_SECRET=replace-with-a-long-random-value
ADMIN_USERNAME=admin
ADMIN_PASSWORD=replace-with-a-strong-password
PUBLIC_BASE_URL=http://localhost:8000
```

Generate an application secret with:

```bash
openssl rand -hex 32
```

Start the service:

```bash
docker compose up -d --build
```

Open `http://localhost:8000/digest/` and sign in with the configured administrator credentials.

The Compose file binds to `127.0.0.1:8000` by default. To expose another interface or port:

```dotenv
BIND_ADDRESS=0.0.0.0
APP_PORT=8000
```

## Configuration

### Sender accounts

Add SMTP accounts from the administration interface. Application passwords and authorization codes are encrypted before they are stored in SQLite. The encryption key is derived from `APP_SECRET`; changing that value makes existing sender credentials unreadable.

### Subscribers

Each subscriber has:

- One private recipient address
- An independent daily delivery time
- A reporting window measured in hours or calendar days
- An enabled/disabled state
- An independent set of selected sources

Manual delivery does not consume or suppress the subscriber's scheduled delivery for that day.

### Reader links

Many email clients remove internal HTML anchors. Signal Digest therefore converts platform navigation links into absolute HTTPS links that open a private browser copy of the digest. Set `PUBLIC_BASE_URL` to the externally reachable HTTPS origin in production:

```dotenv
PUBLIC_BASE_URL=https://digest.example.com
```

The browser copy contains digest content and original article URLs, but no recipient address.

### Optional LLM summary

Set `LLM_API_KEY` in `.env`, then configure the model and OpenAI-compatible endpoint in the administration interface. Digests work without an LLM.

## Reverse proxy

An example Nginx configuration is available at [`deploy/nginx.example.conf`](deploy/nginx.example.conf). Replace the sample domain, configure TLS, and set `PUBLIC_BASE_URL` to the resulting public origin.

When deploying behind a proxy, keep the application port bound to localhost unless container networking requires another arrangement.

## Data and backups

Application data is stored in the `digest_data` Docker volume at `/data/digest.db`. Back up the SQLite database before upgrades:

```bash
docker exec signal-digest python -c "import sqlite3; src=sqlite3.connect('/data/digest.db'); dst=sqlite3.connect('/data/backup.db'); src.backup(dst)"
```

Do not commit `.env`, SQLite files, backups, SMTP credentials, API keys, exported subscriber data, or production proxy configuration.

## Source verification

`verify_upgrade.py` runs source checks against an isolated temporary database and does not send email:

```bash
docker exec signal-digest python verify_upgrade.py
```

## Security notes

- Run the application behind HTTPS in production.
- Use a unique, high-entropy `APP_SECRET` and administrator password.
- Restrict access to the administration interface at the network or proxy layer when possible.
- Rotate SMTP authorization codes if they are exposed.
- Reader URLs are bearer links. Treat them as private and avoid forwarding them.

## License

[MIT](LICENSE)
