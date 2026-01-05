# cchat

Terminal-based chat app with end-to-end encryption for two people. The server
only relays ciphertext; a shared password encrypts and decrypts messages on the
clients. TLS keeps the hop between client and server protected.

## Features
- WebSocket server with optional TLS (supply your own cert + key)
- Client-side encryption using a pre-shared password (never written to disk)
- Terminal UI (Textual): Enter to send, Shift+Enter (or Ctrl+J) for new lines, scrollable history
- Message reactions via `/react <message_id> <emoji>`
- Right-click menus for message reactions and input emoticon insertion

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
If you are tunneling with Cloudflare, point the tunnel at the same host/port.
To retain message history across restarts, supply a history file path:
```bash
python -m cchat.server --host 0.0.0.0 --port 8765 --certfile server.crt --keyfile server.key \
  --history-file ./data/history.json
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
The client workflow:
1. Connects to the server to verify reachability
2. Prompts for your display name (stored in `~/.config/cchat/config.json`)
3. Prompts for a shared salt the first time (stored in `~/.config/cchat/config.json`; random value recommended)
4. Prompts for an unlock phrase + idle lock timeout the first time (stored in `~/.config/cchat/config.json`)
5. Prompts for the shared password (not stored)
6. Opens the chat UI

### Sending messages and reactions
- Type a message and press **Enter** to send.
- Press **Shift+Enter** (or **Ctrl+J**) to add a new line without sending.
- Scroll the chat frame with your mouse wheel or PageUp/PageDown.
- React to a message: `/react <message_id> <emoji>` (e.g. `/react 3 😊`).
- Right-click a previous message to pick a reaction from the menu.
- Right-click in the text input to insert a common emoticon.

## Notes on encryption
- Messages and reactions are encrypted client-side with a key derived from the
  shared password.
- The server stores only ciphertext and forwards it; it cannot decrypt content.
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
- Failed join token attempts are rate-limited per IP: 5 failures within 60 seconds.
