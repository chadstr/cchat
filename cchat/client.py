"""Terminal chat client with end-to-end encryption."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import ssl
from datetime import datetime
from dataclasses import dataclass, field
from getpass import getpass
from pathlib import Path
from typing import Dict, List

import websockets
from prompt_toolkit import Application
from prompt_toolkit.application.current import get_app
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame, TextArea

from .crypto import CipherBundle
from .models import ChatMessage, ISO_FORMAT, Reaction, now_iso

CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "cchat" / "config.json"


@dataclass
class ClientState:
    user: str
    cipher: CipherBundle
    messages: List[ChatMessage] = field(default_factory=list)


class ChatUI:
    def __init__(self, state: ClientState, send_callback) -> None:
        self.state = state
        self.send_callback = send_callback
        self.output = TextArea(
            text="", read_only=True, scrollbar=True, wrap_lines=True, focusable=False
        )
        self.input = TextArea(height=5, prompt="> ", multiline=True, wrap_lines=True)
        self.style = Style.from_dict({"frame.border": "ansicyan"})
        self._build_bindings()
        self.app = Application(
            layout=Layout(
                HSplit(
                    [
                        Frame(self.output, title="Chat", style="class:frame"),
                        Frame(self.input, title="Ctrl+J for newline, Enter to send"),
                    ]
                ),
                focused_element=self.input,
            ),
            key_bindings=self.bindings,
            mouse_support=True,
            style=self.style,
            full_screen=True,
        )

    def _build_bindings(self) -> None:
        kb = KeyBindings()

        @kb.add("c-j")
        def _(event) -> None:  # type: ignore[override]
            event.current_buffer.insert_text("\n")

        @kb.add("enter")
        def _(event) -> None:  # type: ignore[override]
            text = event.current_buffer.text.rstrip()
            event.current_buffer.text = ""
            if text:
                self.send_callback(text)

        @kb.add("c-c")
        @kb.add("c-d")
        def _(event) -> None:  # type: ignore[override]
            event.app.exit(result=None)

        @kb.add("c-l")
        def _(event) -> None:  # type: ignore[override]
            self.output.buffer.cursor_position = len(self.output.text)

        self.bindings = kb

    def render_messages(self) -> None:
        lines: List[str] = []
        for msg in self.state.messages:
            reactions = self._format_reactions(msg.reactions)
            lines.append(
                f"[{msg.id}] {msg.user} @ {self._format_timestamp(msg.timestamp)}\n"
                f"{self._decrypt(msg.ciphertext)}{reactions}\n"
            )
        self.output.text = "\n".join(lines)
        self.output.buffer.cursor_position = len(self.output.text)
        self.app.invalidate()

    def _decrypt(self, ciphertext: str) -> str:
        try:
            return self.state.cipher.decrypt_text(ciphertext)
        except ValueError:
            return "*** Unable to decrypt: check your password ***"

    @staticmethod
    def _format_reactions(reactions: List[Reaction]) -> str:
        if not reactions:
            return ""
        summary: Dict[str, List[str]] = {}
        for reaction in reactions:
            summary.setdefault(reaction.emoji, []).append(reaction.user)
        parts = [f"{emoji} x{len(users)} ({', '.join(users)})" for emoji, users in summary.items()]
        return "\n  Reactions: " + ", ".join(parts)

    @staticmethod
    def _format_timestamp(timestamp: str) -> str:
        try:
            parsed = datetime.strptime(timestamp, ISO_FORMAT)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(timestamp)
            except ValueError:
                return timestamp
        return f"{parsed.day} {parsed.strftime('%b %Y, %I:%M%p')}"

    async def run(self) -> None:
        await self.app.run_async()

    def feed_message(self, message: ChatMessage) -> None:
        self.state.messages.append(message)
        self.render_messages()

    def feed_reaction(self, message_id: int, reaction: Reaction) -> None:
        target = next((m for m in self.state.messages if m.id == message_id), None)
        if not target:
            return
        target.reactions.append(reaction)
        self.render_messages()


async def load_username() -> str:
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text())
            if username := data.get("username"):
                return username
        except Exception:
            pass

    username = input("Enter a display name: ").strip()
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps({"username": username}))
    return username


async def run_client(args: argparse.Namespace) -> None:
    ssl_context = None
    if args.server.startswith("wss://"):
        ssl_context = ssl.create_default_context()
        if args.insecure:
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

    async with websockets.connect(args.server, ssl=ssl_context) as websocket:
        await websocket.recv()  # hello
        print("Connected to server. Encryption handshake still local to your password.")

        username = args.user or await load_username()
        password = getpass("Enter shared password (not stored): ")
        cipher = CipherBundle.from_password(password)
        state = ClientState(user=username, cipher=cipher)

        ui = ChatUI(state, send_callback=lambda text: asyncio.create_task(route_command(websocket, state, text)))
        listener_task = asyncio.create_task(listen_server(websocket, state, ui))

        try:
            await ui.run()
        finally:
            listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listener_task


async def route_command(websocket, state: ClientState, text: str) -> None:
    if text.startswith("/react"):
        parts = text.split(maxsplit=2)
        if len(parts) >= 3 and parts[1].isdigit():
            await send_reaction(websocket, state, int(parts[1]), parts[2])
            return
        print("Usage: /react <message_id> <emoji>")
        return
    await send_message(websocket, state, text)


async def send_message(websocket, state: ClientState, text: str) -> None:
    payload = {
        "type": "message",
        "user": state.user,
        "ciphertext": state.cipher.encrypt_text(text),
        "timestamp": now_iso(),
    }
    await websocket.send(json.dumps(payload))


async def send_reaction(websocket, state: ClientState, message_id: int, emoji: str) -> None:
    await websocket.send(
        json.dumps(
            {
                "type": "reaction",
                "message_id": message_id,
                "emoji": emoji,
                "user": state.user,
            }
        )
    )


async def listen_server(websocket, state: ClientState, ui: ChatUI) -> None:
    async for raw in websocket:
        payload = json.loads(raw)
        msg_type = payload.get("type")
        if msg_type == "history":
            for message_payload in payload.get("messages", []):
                ui.feed_message(ChatMessage.from_payload(message_payload))
        elif msg_type == "message":
            ui.feed_message(ChatMessage.from_payload(payload["message"]))
        elif msg_type == "reaction":
            reaction = Reaction(**payload["reaction"])
            ui.feed_reaction(payload["message_id"], reaction)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Connect to a cchat server")
    parser.add_argument("--server", default="wss://localhost:8765", help="WebSocket server URL")
    parser.add_argument("--user", help="Display name (otherwise remembered from config)")
    parser.add_argument("--insecure", action="store_true", help="Skip SSL verification (development only)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(run_client(args))


if __name__ == "__main__":
    main()
