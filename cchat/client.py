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
from textual.binding import Binding
from textual.events import Key, MouseDown
from textual.widget import Widget
from textual.widgets import Button, Footer, Header, RichLog, TextArea

from .crypto import CipherBundle
from .models import ChatMessage, ISO_FORMAT, Reaction, now_iso

CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "cchat" / "config.json"
COMMON_REACTIONS = ["👍", "❤️", "😂", "🎉", "😮"]


@dataclass
class ClientState:
    user: str
    cipher: CipherBundle
    messages: List[ChatMessage] = field(default_factory=list)


class ChatInput(TextArea):
    BINDINGS = [
        Binding("ctrl+u", "clear_input", "Clear", priority=True),
    ]

    def on_key(self, event: Key) -> None:
        if event.key in {"shift+enter", "ctrl+j"}:
            self.insert("\n")
            event.stop()
            event.prevent_default()
            return
        if event.key == "enter":
            app = self.app
            if isinstance(app, ChatApp):
                app.send_from_input()
                event.stop()
                event.prevent_default()
                return

    def action_clear_input(self) -> None:
        self.text = ""
        self.focus()


class ReactionMenu(Widget):
    DEFAULT_CSS = """
    ReactionMenu {
        background: #24283b;
        border: solid #7aa2f7;
        padding: 0 1;
        height: auto;
        width: auto;
    }
    ReactionMenu Button {
        min-width: 5;
        margin: 0 1;
        background: #1f2335;
        color: #c0caf5;
    }
    ReactionMenu Button:hover {
        background: #3d59a1;
        color: #f2f2f2;
    }
    """

    def __init__(self, message_id: int, emojis: List[str]) -> None:
        super().__init__()
        self.message_id = message_id
        self.emojis = emojis

    def compose(self) -> ComposeResult:
        for emoji in self.emojis:
            yield Button(emoji)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        app = self.app
        if isinstance(app, ChatApp):
            app.send_reaction_from_menu(self.message_id, str(event.button.label))


class ChatLog(RichLog):
    def on_mouse_down(self, event: MouseDown) -> None:
        if event.button != 3:
            return
        app = self.app
        if isinstance(app, ChatApp):
            app.open_reaction_menu(self, event)
            event.stop()


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

    def __init__(self, state: ClientState, send_callback, react_callback) -> None:
        super().__init__()
        self.state = state
        self.send_callback = send_callback
        self.react_callback = react_callback
        self._line_message_map: Dict[int, int] = {}
        self._reaction_menu: ReactionMenu | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield ChatLog(id="chatlog", wrap=True, highlight=False)
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

    def open_reaction_menu(self, log: RichLog, event: MouseDown) -> None:
        line_index = event.y + log.scroll_y
        message_id = self._line_message_map.get(line_index)
        if message_id is None:
            return
        self.dismiss_reaction_menu()
        menu = ReactionMenu(message_id, COMMON_REACTIONS)
        self._reaction_menu = menu
        self.mount(menu)
        menu.styles.offset = (log.region.x + event.x, log.region.y + event.y)

    def dismiss_reaction_menu(self) -> None:
        if self._reaction_menu is None:
            return
        self._reaction_menu.remove()
        self._reaction_menu = None

    def send_reaction_from_menu(self, message_id: int, emoji: str) -> None:
        self.dismiss_reaction_menu()
        asyncio.create_task(self.react_callback(message_id, emoji))

    def render_messages(self) -> None:
        log = self.query_one("#chatlog", RichLog)
        self.dismiss_reaction_menu()
        log.clear()
        self._line_message_map.clear()
        line_index = 0
        for msg in self.state.messages:
            align = "right" if msg.user == self.state.user else "left"
            meta_style = "#9aa5ce" if align == "right" else "#a9b1d6"
            body_style = "#9ece6a" if align == "right" else "#7aa2f7"
            reaction_style = "#f7768e" if align == "right" else "#bb9af7"
            header = Text(
                f"[{msg.id}] {msg.user} @ {self._format_timestamp(msg.timestamp)}",
                style=meta_style,
            )
            body_text = self._decrypt(msg.ciphertext)
            body = Text(body_text, style=body_style)
            log.write(Align(header, align=align))
            log.write(Align(body, align=align))
            self._line_message_map[line_index] = msg.id
            body_line_count = body_text.count("\n") + 1
            for i in range(1, body_line_count + 1):
                self._line_message_map[line_index + i] = msg.id
            line_index += 1 + body_line_count
            for reaction_line in self._format_reactions(msg.reactions):
                log.write(Align(Text(reaction_line, style=reaction_style), align=align))
                self._line_message_map[line_index] = msg.id
                line_index += 1
            log.write(Text(""))
            self._line_message_map[line_index] = msg.id
            line_index += 1
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

        ui = ChatApp(
            state,
            send_callback=lambda text: route_command(websocket, state, text),
            react_callback=lambda message_id, emoji: send_reaction(websocket, state, message_id, emoji),
        )
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
