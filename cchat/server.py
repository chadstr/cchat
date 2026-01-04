"""Async WebSocket chat server.

The server is intentionally simple: it never sees plaintext messages, only
ciphertext blobs supplied by clients. Messages and reactions are broadcast to
all connected clients.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import ssl
from pathlib import Path
from typing import Dict, List, Set

import websockets
from websockets.server import WebSocketServerProtocol

from .models import ChatMessage, Reaction, now_iso


class ChatServer:
    def __init__(self, history_path: Path | None = None) -> None:
        self._messages: List[ChatMessage] = []
        self._clients: Set[WebSocketServerProtocol] = set()
        self._next_id = 1
        self._history_path = history_path
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
        await websocket.send(json.dumps({"type": "hello", "message_count": len(self._messages)}))
        await websocket.send(
            json.dumps(
                {
                    "type": "history",
                    "messages": [message.to_payload() for message in self._messages],
                }
            )
        )
        await self._broadcast_presence()

    async def unregister(self, websocket: WebSocketServerProtocol) -> None:
        self._clients.discard(websocket)
        await self._broadcast_presence()

    async def handler(self, websocket: WebSocketServerProtocol) -> None:
        await self.register(websocket)
        try:
            async for raw in websocket:
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                msg_type = payload.get("type")
                if msg_type == "message":
                    await self._handle_message(websocket, payload)
                elif msg_type == "reaction":
                    await self._handle_reaction(payload)
        finally:
            await self.unregister(websocket)

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
        remove = payload.get("remove", False)
        if not (message_id and emoji and user):
            return

        target = next((m for m in self._messages if m.id == message_id), None)
        if not target:
            return

        if remove:
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

        reaction = Reaction(emoji=emoji, user=user, timestamp=now_iso())
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

    async def _broadcast(self, message: Dict) -> None:
        if not self._clients:
            return
        serialized = json.dumps(message)
        await asyncio.gather(*[client.send(serialized) for client in list(self._clients)], return_exceptions=True)

    async def _broadcast_presence(self) -> None:
        await self._broadcast({"type": "presence", "connected_clients": len(self._clients)})


def build_ssl_context(certfile: Path | None, keyfile: Path | None) -> ssl.SSLContext | None:
    if not certfile or not keyfile:
        return None
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=certfile, keyfile=keyfile)
    return context


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start the cchat WebSocket server")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8765, help="Port to listen on (default: 8765)")
    parser.add_argument("--certfile", type=Path, help="Path to TLS certificate (PEM)")
    parser.add_argument("--keyfile", type=Path, help="Path to TLS private key (PEM)")
    parser.add_argument(
        "--history-file",
        type=Path,
        help="Optional path to JSON history file for message retention",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    ssl_context = build_ssl_context(args.certfile, args.keyfile)
    server = ChatServer(history_path=args.history_file)

    async def run_server() -> None:
        async with websockets.serve(server.handler, args.host, args.port, ssl=ssl_context):
            print(f"Server running on {'wss' if ssl_context else 'ws'}://{args.host}:{args.port}")
            await asyncio.Future()  # run forever

    asyncio.run(run_server())


if __name__ == "__main__":
    main()
