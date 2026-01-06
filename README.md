# cchat

Terminal-based chat app with end-to-end encryption for small groups (primarily intended for two people). The server
only relays ciphertext; a shared password encrypts and decrypts messages on the
clients. TLS keeps the hop between client and server protected.

## Features
- WebSocket server with optional TLS (supply your own cert + key)
- Client-side encryption using a pre-shared password (never written to disk)
- Terminal UI (Textual): Enter to send, Shift+Enter (or Ctrl+J) for new lines, scrollable history
- Message reactions via `/react <message_id> <emoji>`
- Hover or right-click message bubbles for reactions and editing your own messages
- Right-click in the input box to insert a common emoticon
- Incoming messages while the UI is idle-locked are marked unread immediately

## Getting started

### Install dependencies
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Generate a self-signed certificate (development)
```bash
openssl req -x509 -newkey rsa:4096 -keyout server.key -out server.crt -days 365 -nodes \\
  -subj "/CN=localhost"
```

### Run the server
```bash
python -m cchat.server --host 0.0.0.0 --port 8765 --certfile server.crt --keyfile server.key
```
The server prints a join token on startup (or provide your own with `--join-token`).
To retain message history across restarts, supply a history file path:
```bash
python -m cchat.server --host 0.0.0.0 --port 8765 --certfile server.crt --keyfile server.key \
  --history-file ./data/history.json
```
To only send recent history to new clients, add a window (rounded to the day boundary):
```bash
python -m cchat.server --host 0.0.0.0 --port 8765 --certfile server.crt --keyfile server.key \
  --history-file ./data/history.json --history-window-days 3
```

### Run behind a Cloudflare Tunnel
You can expose the server over a Cloudflare Tunnel (cloudflared).

Option A: Cloudflare terminates TLS (origin is plain WS)
```bash
python -m cchat.server --host 0.0.0.0 --port 8765
cloudflared tunnel --url http://localhost:8765
```
Client:
```bash
python -m cchat.client --server wss://<tunnel-hostname> --join-token <token>
```

Option B: TLS end-to-end (origin is WSS)
```bash
python -m cchat.server --host 0.0.0.0 --port 8765 --certfile server.crt --keyfile server.key
cloudflared tunnel --url https://localhost:8765 --no-tls-verify
```
Client:
```bash
python -m cchat.client --server wss://<tunnel-hostname> --join-token <token>
```
Notes:
- WebSocket traffic is supported automatically by Cloudflare Tunnel.
- Connection logs show direct peer IPs by default; when the tunnel runs on the
  same host (loopback), the server trusts `CF-Connecting-IP` and logs the real
  client IP instead.
- If your tunnel runs on another host, add that host's IP to the trusted proxy
  list in `cchat/server.py` so forwarded headers can be used safely.
- Keep the origin private when Cloudflare terminates TLS at the edge (Option A):
  bind the server to localhost or firewall the port so only the tunnel can reach it.
- If you enable Cloudflare Access, ensure your Access policy allows WebSocket
  traffic and that the client can complete any required authentication flow.
- If you see idle disconnects, check Cloudflare timeout limits and consider
  periodic keepalives from the client to keep the connection active.

Example firewall setup:

```bash
# Allow only the local tunnel and SSH, block direct Internet access to the origin.
sudo ufw allow 22/tcp
sudo ufw deny 8765/tcp
```

### Run the client
```bash
python -m cchat.client --server wss://<host>:8765 --join-token <token>
```
On first run, the client prompts for the server address and join token and stores
them in `~/.config/cchat/config.json`.
For development with a self-signed certificate, keep TLS but skip verification:
```bash
python -m cchat.client --server wss://<host>:8765 --insecure --join-token <token>
```
Debug options:
- `--insecure` skips TLS verification (self-signed certs).
- `--join-token <token>` supplies the join token.
- `--user <name>` overrides the display name and updates the config.
- `--idle-timeout <seconds>` sets the inactivity threshold before messages count as unread (default: 15).
- `--show-message-id` includes message IDs in chat headers for reference.
- `--reset-unlock-phrase` prompts for a new idle lock unlock phrase.
The client workflow:
1. Connects to the server to verify reachability
2. Prompts for your display name (stored in `~/.config/cchat/config.json`)
3. Prompts for a shared salt the first time (stored in `~/.config/cchat/config.json`; random value recommended)
4. Prompts for an unlock phrase + idle lock timeout the first time (stored as a salted hash in `~/.config/cchat/config.json`)
5. Prompts for the shared password (not stored)
6. Opens the chat UI

### Optional: shell alias for the client
You can create a shell alias (or function) that activates the virtualenv and
runs the client in one command. Add this to your `~/.bashrc` or `~/.zshrc`:
```bash
cchat() {
  source /path/to/cchat/.venv/bin/activate
  python -m cchat.client "$@"
}
```
Then reload your shell (`source ~/.bashrc` or `source ~/.zshrc`) and run:
```bash
cchat --server wss://<host>:8765 --join-token <token>
```
`"$@"` forwards any arguments you pass to `cchat` to the client.
If you have already saved the server and join token in `~/.config/cchat/config.json`,
you can just run:
```bash
cchat
```

### Sending messages and reactions
- Type a message and press **Enter** to send.
- Press **Shift+Enter** (or **Ctrl+J**) to add a new line without sending.
- Scroll the chat frame with your mouse wheel or PageUp/PageDown.
- React to a message: `/react <message_id> <emoji>` (e.g. `/react 3 😊`).
- Hover or right-click a previous message to pick a reaction from the menu.
- Hover or right-click your own message to edit it; press Enter to save the edit.
- Right-click in the text input to insert a common emoticon.

## Notes on encryption
- Messages and usernames are encrypted client-side with a key derived from the
  shared password.
- Reaction emoji remain plaintext, but the reacting usernames are encrypted.
- The server stores only ciphertext and forwards it; it cannot decrypt content
  or usernames.
- TLS secures the hop between client and server (recommended in production).

## Debugging

```sh
# Both server and client
source .venv/bin/activate

# Server
python -m cchat.server --host 0.0.0.0 --port 8765 --certfile server.crt --keyfile server.key --history-file ./data/history.json

# Client
python -m cchat.client --server wss://127.0.0.1:8765 --insecure --user user_one --idle-timeout 5 --show-message-id
```

## Security notes
- Join tokens are sent as an `X-Join-Token` header.
- Failed join token attempts are logged by the server (look for `auth failed host=...`).
- If running behind a proxy (for example, Cloudflare Tunnel), logs will show the
  proxy IP unless you explicitly trust and log a forwarded client IP header.
- Connection logs report the direct peer IP by default; when a trusted local
  tunnel is used, logs report the forwarded client IP headers instead.

## Fail2ban setup (optional)
Use fail2ban to ban IPs that repeatedly fail join token checks. This setup
assumes the server logs are written to a file that fail2ban can read.

1) Configure server logging to a file (example using systemd):
```ini
[Service]
ExecStart=/path/to/venv/bin/python -m cchat.server --host 0.0.0.0 --port 8765
StandardOutput=append:/var/log/cchat/server.log
StandardError=append:/var/log/cchat/server.log
```

2) Create a filter at `/etc/fail2ban/filter.d/cchat.conf`:
```ini
[Definition]
failregex = ^.*auth failed host=<HOST>.*$
ignoreregex =
```

3) Create a jail at `/etc/fail2ban/jail.d/cchat.conf`:
```ini
[cchat]
enabled = true
port = 8765
filter = cchat
logpath = /var/log/cchat/server.log
maxretry = 5
findtime = 600
bantime = 3600
```

4) Restart fail2ban and verify:
```bash
sudo systemctl restart fail2ban
sudo fail2ban-client status cchat
```

Notes:
- If you are behind Cloudflare Tunnel on the same host, the server logs the
  real client IP via `CF-Connecting-IP`. If the tunnel runs elsewhere, update
  the trusted proxy list in `cchat/server.py` or fail2ban will only see proxy IPs.
