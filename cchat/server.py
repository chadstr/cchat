"""Async WebSocket chat server.

The server is intentionally simple: it never sees plaintext messages, only
ciphertext blobs supplied by clients. Messages and reactions are broadcast to
all connected clients.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import logging
import secrets
import ssl
from pathlib import Path
from typing import Dict, List, Set

import websockets
from websockets.server import WebSocketServerProtocol

from .models import ChatMessage, Reaction, now_iso, ISO_FORMAT

logger = logging.getLogger("cchat.server")


class ChatServer:
    def __init__(
        self,
        history_path: Path | None = None,
        *,
        join_token: str | None = None,
        history_window_days: int | None = None,
        trusted_proxies: str | None = None,
        auth_max_failures: int = 5,
        auth_failure_window_seconds: int = 300,
        auth_ban_seconds: int = 900,
    ) -> None:
        self._messages: List[ChatMessage] = []
        self._clients: Set[WebSocketServerProtocol] = set()
        self._next_id = 1
        self._history_path = history_path
        self._join_token = join_token
        self._history_window_days = history_window_days
        self._trusted_proxies = self._parse_trusted_proxies(trusted_proxies)
        self._auth_max_failures = max(0, auth_max_failures)
        self._auth_failure_window = timedelta(seconds=max(0, auth_failure_window_seconds))
        self._auth_ban_duration = timedelta(seconds=max(0, auth_ban_seconds))
        self._failed_auth: Dict[str, List[datetime]] = {}
        self._banned_until: Dict[str, datetime] = {}
        self._auth_rate_limit_enabled = (
            self._auth_max_failures > 0
            and self._auth_failure_window.total_seconds() > 0
            and self._auth_ban_duration.total_seconds() > 0
        )
        if self._history_path:
            self._load_history()

    def _load_history(self) -> None:
        if not self._history_path:
            return
        try:
            raw = self._history_path.read_text()
        except FileNotFoundError:
            return
        except OSError as exc:
            print(f"Failed to read history file: {exc}")
            return

        try:
            payload = json.loads(raw)
            messages = payload.get("messages", [])
            self._messages = [ChatMessage.from_payload(item) for item in messages]
            if self._messages:
                max_id = max(message.id for message in self._messages)
                self._next_id = max_id + 1
            else:
                self._next_id = int(payload.get("next_id", 1))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            print(f"Failed to parse history file: {exc}")

    def _save_history(self) -> None:
        if not self._history_path:
            return
        payload = {
            "next_id": self._next_id,
            "messages": [message.to_payload() for message in self._messages],
        }
        try:
            self._history_path.parent.mkdir(parents=True, exist_ok=True)
            self._history_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
        except OSError as exc:
            print(f"Failed to write history file: {exc}")

    async def register(self, websocket: WebSocketServerProtocol) -> None:
        self._clients.add(websocket)
        now = datetime.now().astimezone()
        history_messages = self._filtered_history_messages(now)
        await websocket.send(json.dumps({"type": "hello", "message_count": len(history_messages)}))
        await websocket.send(
            json.dumps(
                {
                    "type": "history",
                    "messages": [message.to_payload() for message in history_messages],
                }
            )
        )
        await self._broadcast_presence()

    async def unregister(self, websocket: WebSocketServerProtocol) -> None:
        self._clients.discard(websocket)
        await self._broadcast_presence()

    async def handler(self, websocket: WebSocketServerProtocol) -> None:
        host = self._client_ip(websocket)
        logger.info("connection attempt host=%s", host or "unknown")
        if host and self._is_banned(host):
            logger.warning("auth blocked host=%s", host)
            await websocket.close(code=1008, reason="too many failed attempts")
            return
        if not self._authorize_connection(websocket):
            if host:
                logger.warning("auth failed host=%s", host)
                if self._record_failed_auth(host):
                    banned_until = self._banned_until.get(host)
                    if banned_until:
                        local_until = banned_until.astimezone().isoformat()
                        logger.warning("auth banned host=%s until=%s", host, local_until)
            else:
                logger.warning("auth failed host=unknown")
            await websocket.close(code=1008, reason="join token required")
            return
        if host:
            self._clear_failed_auth(host)
        await self.register(websocket)
        logger.info("connection accepted host=%s", host or "unknown")
        try:
            async for raw in websocket:
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                msg_type = payload.get("type")
                if msg_type == "message":
                    await self._handle_message(websocket, payload)
                elif msg_type == "edit":
                    await self._handle_edit(payload)
                elif msg_type == "reaction":
                    await self._handle_reaction(payload)
                elif msg_type == "typing":
                    await self._handle_typing(payload)
        finally:
            await self.unregister(websocket)
            logger.info("connection closed host=%s", host or "unknown")

    async def _handle_message(self, websocket: WebSocketServerProtocol, payload: Dict) -> None:
        user = payload.get("user")
        ciphertext = payload.get("ciphertext")
        timestamp = payload.get("timestamp", now_iso())
        if not user or not ciphertext:
            return

        message = ChatMessage(
            id=self._next_id,
            user=user,
            ciphertext=ciphertext,
            timestamp=timestamp,
        )
        self._next_id += 1
        self._messages.append(message)
        self._save_history()
        await self._broadcast({"type": "message", "message": message.to_payload()})

    async def _handle_reaction(self, payload: Dict) -> None:
        message_id = payload.get("message_id")
        emoji = payload.get("emoji")
        user = payload.get("user")
        user_fingerprint = payload.get("user_fingerprint")
        remove = payload.get("remove", False)
        if not (message_id and emoji and user):
            return

        target = next((m for m in self._messages if m.id == message_id), None)
        if not target:
            return

        if remove:
            if user_fingerprint:
                existing = next(
                    (
                        reaction
                        for reaction in target.reactions
                        if reaction.emoji == emoji
                        and reaction.user_fingerprint == user_fingerprint
                    ),
                    None,
                )
            else:
                existing = next(
                    (
                        reaction
                        for reaction in target.reactions
                        if reaction.emoji == emoji and reaction.user == user
                    ),
                    None,
                )
            if not existing:
                return
            target.reactions.remove(existing)
            self._save_history()
            await self._broadcast(
                {
                    "type": "reaction",
                    "message_id": target.id,
                    "reaction": existing.__dict__,
                    "action": "remove",
                }
            )
            return

        reaction = Reaction(
            emoji=emoji,
            user=user,
            timestamp=now_iso(),
            user_fingerprint=user_fingerprint if isinstance(user_fingerprint, str) else None,
        )
        target.reactions.append(reaction)
        self._save_history()
        await self._broadcast(
            {
                "type": "reaction",
                "message_id": target.id,
                "reaction": reaction.__dict__,
                "action": "add",
            }
        )

    async def _handle_edit(self, payload: Dict) -> None:
        message_id = payload.get("message_id")
        ciphertext = payload.get("ciphertext")
        if not (message_id and ciphertext):
            return
        if not isinstance(message_id, int):
            return
        target = next((m for m in self._messages if m.id == message_id), None)
        if not target:
            return
        target.ciphertext = ciphertext
        target.edited = True
        self._save_history()
        await self._broadcast({"type": "edit", "message": target.to_payload()})

    async def _handle_typing(self, payload: Dict) -> None:
        user = payload.get("user")
        typing = payload.get("typing")
        if not isinstance(user, str) or not user:
            return
        await self._broadcast({"type": "typing", "user": user, "typing": bool(typing)})

    async def _broadcast(self, message: Dict) -> None:
        if not self._clients:
            return
        serialized = json.dumps(message)
        await asyncio.gather(*[client.send(serialized) for client in list(self._clients)], return_exceptions=True)

    async def _broadcast_presence(self) -> None:
        await self._broadcast({"type": "presence", "connected_clients": len(self._clients)})

    def _filtered_history_messages(self, now: datetime) -> List[ChatMessage]:
        cutoff = self._history_cutoff(now)
        if cutoff is None:
            return list(self._messages)
        filtered: List[ChatMessage] = []
        for message in self._messages:
            parsed = self._parse_timestamp(message.timestamp)
            if parsed is None:
                filtered.append(message)
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=now.tzinfo)
            if parsed >= cutoff:
                filtered.append(message)
        return filtered

    def _history_cutoff(self, now: datetime) -> datetime | None:
        if self._history_window_days is None:
            return None
        cutoff = now - timedelta(days=self._history_window_days)
        return cutoff.replace(hour=0, minute=0, second=0, microsecond=0)

    @staticmethod
    def _parse_timestamp(timestamp: str) -> datetime | None:
        try:
            return datetime.strptime(timestamp, ISO_FORMAT)
        except ValueError:
            try:
                return datetime.fromisoformat(timestamp)
            except ValueError:
                return None

    def _authorize_connection(self, websocket: WebSocketServerProtocol) -> bool:
        if not self._join_token:
            return True
        token = websocket.request_headers.get("X-Join-Token")
        if not isinstance(token, str) or not token.strip():
            return False
        return secrets.compare_digest(token.strip(), self._join_token)

    def _record_failed_auth(self, host: str) -> bool:
        if not self._auth_rate_limit_enabled:
            return False
        now = datetime.now(tz=timezone.utc)
        cutoff = now - self._auth_failure_window
        failures = self._failed_auth.setdefault(host, [])
        failures[:] = [timestamp for timestamp in failures if timestamp >= cutoff]
        failures.append(now)
        if len(failures) < self._auth_max_failures:
            return False
        self._failed_auth.pop(host, None)
        self._banned_until[host] = now + self._auth_ban_duration
        return True

    def _clear_failed_auth(self, host: str) -> None:
        self._failed_auth.pop(host, None)

    def _is_banned(self, host: str) -> bool:
        if not self._auth_rate_limit_enabled:
            return False
        banned_until = self._banned_until.get(host)
        if not banned_until:
            return False
        now = datetime.now(tz=timezone.utc)
        if now >= banned_until:
            self._banned_until.pop(host, None)
            return False
        return True

    def _client_host(self, websocket: WebSocketServerProtocol) -> str | None:
        address = websocket.remote_address
        if isinstance(address, tuple) and address:
            return str(address[0])
        if isinstance(address, str) and address:
            return address
        return None

    def _client_ip(self, websocket: WebSocketServerProtocol) -> str | None:
        host = self._client_host(websocket)
        if not host:
            return None
        if self._is_trusted_proxy(host):
            forwarded = self._forwarded_client_ip(websocket.request_headers)
            if forwarded:
                return forwarded
        return host

    def _is_trusted_proxy(self, host: str) -> bool:
        try:
            parsed = ipaddress.ip_address(host)
        except ValueError:
            return False
        for proxy in self._trusted_proxies:
            if isinstance(proxy, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
                if parsed in proxy:
                    return True
            elif parsed == proxy:
                return True
        return False

    @staticmethod
    def _parse_trusted_proxies(entries: str | None) -> List[object]:
        proxies: List[object] = []
        for entry in ("127.0.0.1", "::1", *(entries or "").split(",")):
            candidate = entry.strip()
            if not candidate:
                continue
            try:
                if "/" in candidate:
                    proxies.append(ipaddress.ip_network(candidate, strict=False))
                else:
                    proxies.append(ipaddress.ip_address(candidate))
            except ValueError:
                logger.warning("trusted proxy entry ignored: %s", candidate)
        return proxies

    @staticmethod
    def _forwarded_client_ip(headers: websockets.Headers) -> str | None:
        cf_ip = headers.get("CF-Connecting-IP")
        if isinstance(cf_ip, str) and cf_ip.strip():
            return cf_ip.strip()
        xff = headers.get("X-Forwarded-For")
        if isinstance(xff, str) and xff.strip():
            return xff.split(",")[0].strip()
        return None


def build_ssl_context(certfile: Path | None, keyfile: Path | None) -> ssl.SSLContext | None:
    if not certfile or not keyfile:
        return None
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=certfile, keyfile=keyfile)
    return context


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Start the cchat WebSocket server for encrypted relay.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python -m cchat.server
  python -m cchat.server --host 0.0.0.0 --port 8765 --join-token <token>
  python -m cchat.server --certfile server.crt --keyfile server.key
  python -m cchat.server --history-file ./data/history.json
  python -m cchat.server --history-file ./data/history.json --history-window-days 7

Notes:
  - If --join-token is omitted, a token is generated and printed on startup.
  - TLS is enabled only when both --certfile and --keyfile are provided.
  - --history-window-days limits what new clients receive; it does not delete history.
""",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="Port to listen on (default: 8765)")
    parser.add_argument("--certfile", type=Path, help="Path to TLS certificate (PEM)")
    parser.add_argument("--keyfile", type=Path, help="Path to TLS private key (PEM)")
    parser.add_argument(
        "--join-token",
        help="Join token required from clients (generated if omitted)",
    )
    parser.add_argument(
        "--history-file",
        type=Path,
        help="Optional path to JSON history file for message retention",
    )
    parser.add_argument(
        "--history-window-days",
        type=int,
        help="Only send history from the last N days (rounded to day boundary)",
    )
    parser.add_argument(
        "--trusted-proxies",
        help="Comma-separated IPs/CIDRs trusted to supply forwarded client IP headers",
    )
    parser.add_argument(
        "--auth-max-failures",
        type=int,
        default=5,
        help="Failed join token attempts before ban (default: 5)",
    )
    parser.add_argument(
        "--auth-failure-window-seconds",
        type=int,
        default=300,
        help="Window for counting failed join tokens (default: 300)",
    )
    parser.add_argument(
        "--auth-ban-seconds",
        type=int,
        default=900,
        help="Ban duration after too many failures (default: 900)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s cchat.server %(message)s",
    )
    ssl_context = build_ssl_context(args.certfile, args.keyfile)
    join_token = args.join_token or secrets.token_urlsafe(32)
    server = ChatServer(
        history_path=args.history_file,
        join_token=join_token,
        history_window_days=args.history_window_days,
        trusted_proxies=args.trusted_proxies,
        auth_max_failures=args.auth_max_failures,
        auth_failure_window_seconds=args.auth_failure_window_seconds,
        auth_ban_seconds=args.auth_ban_seconds,
    )

    async def run_server() -> None:
        async with websockets.serve(server.handler, args.host, args.port, ssl=ssl_context):
            if args.join_token:
                print("Join token set via --join-token.")
            else:
                print(f"Join token: {join_token}")
            print(f"Server running on {'wss' if ssl_context else 'ws'}://{args.host}:{args.port}")
            await asyncio.Future()  # run forever

    asyncio.run(run_server())


if __name__ == "__main__":
    main()
