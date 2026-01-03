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
from rich.align import Align
from rich.text import Text
from textual.app import App, ComposeResult
from textual.events import Key
from textual.widgets import Footer, Header, RichLog, TextArea

from .crypto import CipherBundle
from .models import ChatMessage, ISO_FORMAT, Reaction, now_iso

CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "cchat" / "config.json"


@dataclass
class ClientState:
    user: str
    cipher: CipherBundle
    messages: List[ChatMessage] = field(default_factory=list)


class ChatInput(TextArea):
    def on_key(self, event: Key) -> None:
        if event.key == "enter":
            app = self.app
            if isinstance(app, ChatApp):
                app.send_from_input()
                event.stop()
                event.prevent_default()
                return
        if event.key in {"shift+enter", "ctrl+j"}:
            self.insert("\n")
            event.stop()
            event.prevent_default()


class ChatApp(App[None]):
    CSS = """
    Screen {
        background: #1a1b26;
        color: #c0caf5;
    }
    #chatlog {
        padding: 1 2;
        height: 1fr;
    }
    #input {
        height: 6;
        border: tall #7aa2f7;
        padding: 0 1;
    }
    """

    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
        ("ctrl+d", "quit", "Quit"),
        ("ctrl+l", "scroll_end", "Bottom"),
    ]

    def __init__(self, state: ClientState, send_callback) -> None:
        super().__init__()
        self.state = state
        self.send_callback = send_callback

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield RichLog(id="chatlog", wrap=True, highlight=False)
        yield ChatInput(id="input", show_line_numbers=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#input", TextArea).focus()
        self.render_messages()

    def action_scroll_end(self) -> None:
        self.query_one("#chatlog", RichLog).scroll_end(animate=False)

    def send_from_input(self) -> None:
        input_area = self.query_one("#input", TextArea)
        text = input_area.text.rstrip()
        input_area.text = ""
        if text:
            asyncio.create_task(self.send_callback(text))

    def render_messages(self) -> None:
        log = self.query_one("#chatlog", RichLog)
        log.clear()
        for msg in self.state.messages:
            align = "right" if msg.user == self.state.user else "left"
            meta_style = "#9aa5ce" if align == "right" else "#a9b1d6"
            body_style = "#9ece6a" if align == "right" else "#7aa2f7"
            reaction_style = "#f7768e" if align == "right" else "#bb9af7"
            header = Text(
                f"[{msg.id}] {msg.user} @ {self._format_timestamp(msg.timestamp)}",
                style=meta_style,
            )
            body = Text(self._decrypt(msg.ciphertext), style=body_style)
            log.write(Align(header, align=align))
            log.write(Align(body, align=align))
            for reaction_line in self._format_reactions(msg.reactions):
                log.write(Align(Text(reaction_line, style=reaction_style), align=align))
            log.write(Text(""))
        self.action_scroll_end()

    def _decrypt(self, ciphertext: str) -> str:
        try:
            return self.state.cipher.decrypt_text(ciphertext)
        except ValueError:
            return "*** Unable to decrypt: check your password ***"

    @staticmethod
    def _format_reactions(reactions: List[Reaction]) -> List[str]:
        if not reactions:
            return []
        summary: Dict[str, List[str]] = {}
        for reaction in reactions:
            summary.setdefault(reaction.emoji, []).append(reaction.user)
        parts = [f"{emoji} x{len(users)} ({', '.join(users)})" for emoji, users in summary.items()]
        return ["Reactions: " + ", ".join(parts)]

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
    save_username(username)
    return username


def save_username(username: str) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps({"username": username}))


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

        if args.user:
            username = args.user
            save_username(username)
        else:
            username = await load_username()
        password = getpass("Enter shared password (not stored): ")
        cipher = CipherBundle.from_password(password)
        state = ClientState(user=username, cipher=cipher)

        ui = ChatApp(state, send_callback=lambda text: route_command(websocket, state, text))
        listener_task = asyncio.create_task(listen_server(websocket, state, ui))

        try:
            await ui.run_async()
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


async def listen_server(websocket, state: ClientState, ui: ChatApp) -> None:
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
