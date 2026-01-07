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
By default the server binds to `127.0.0.1`; pass `--host 0.0.0.0` to expose it on all interfaces.
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

### Run as a systemd service (auto-start)
Create a unit file at `/etc/systemd/system/cchat.service` (edit paths and user):
```ini
[Unit]
Description=cchat server
After=network.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/path/to/cchat
ExecStart=/path/to/cchat/.venv/bin/python -m cchat.server --port 8765 \
  --history-file ./data/history.json --history-window-days 7 --join-token xxx
Restart=on-failure

[Install]
WantedBy=multi-user.target
```
Using the venv's `python` in `ExecStart` ensures the service runs with the
virtualenv dependencies.
Then enable/start it:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now cchat
sudo systemctl status cchat
```

### Run behind a Cloudflare Tunnel
You can expose the server over a Cloudflare Tunnel (cloudflared).

Option A: Cloudflare terminates TLS (origin is plain WS)
```bash
python -m cchat.server --port 8765
cloudflared tunnel --url http://localhost:8765
```
Client:
```bash
python -m cchat.client --server wss://<tunnel-hostname> --join-token <token>
```

Option B: TLS end-to-end (origin is WSS)
```bash
python -m cchat.server --port 8765 --certfile server.crt --keyfile server.key
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
- If you see idle disconnects, check Cloudflare timeout limits.

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
Process flow notes:
- The server sends a history payload on connect, followed by live events.
- The client decrypts usernames/reactions on receipt and appends the full history list to the local message list before rendering for the first time.
- Message bodies are decrypted on demand and cached for display, so history renders once and subsequent re-renders avoid redundant decrypts.

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
- Reaction emoji removal uses a stable fingerprint (HMAC of username keyed by the
  shared password+salt). This is so the server can match a second tap to the original
  reaction even though encrypted usernames are non-deterministic.

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
- Use `--trusted-proxies` (IP/CIDR list) to honor `CF-Connecting-IP` or
  `X-Forwarded-For` headers from that proxy.
- Trusted proxies default to `127.0.0.1` and `::1`.
- Stable reaction fingerprints make reactions linkable across chats that
  share the same password+salt; they do not reveal usernames without that secret.

## Auth throttling (built-in)
The server can rate-limit failed join token attempts by IP. Defaults:
5 failures within 5 minutes results in a 15 minute ban.

Tune the defaults with:
`--auth-max-failures`, `--auth-failure-window-seconds`, `--auth-ban-seconds`.

If you're behind a proxy, set `--trusted-proxies` so the server uses the real
client IP for throttling. Local tunnels on the same host are already trusted
via `127.0.0.1` and `::1`.
