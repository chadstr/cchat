"""Terminal chat client with end-to-end encryption."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import ssl
import textwrap
from datetime import datetime
from dataclasses import dataclass, field
from getpass import getpass
from pathlib import Path
from typing import Dict, List

import websockets
from prompt_toolkit import Application
from prompt_toolkit.application.current import get_app
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.margins import ScrollbarMargin
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
        self.output_control = FormattedTextControl(
            text=[],
            show_cursor=False,
            get_cursor_position=self._get_cursor_position,
        )
        self.output_window = Window(
            content=self.output_control,
            wrap_lines=True,
            right_margins=[ScrollbarMargin(display_arrows=True)],
            always_hide_cursor=True,
            style="class:output",
        )
        self._last_line_count = 0
        self.input = TextArea(height=5, prompt="> ", multiline=True, wrap_lines=True)
        self.style = Style.from_dict(
            {
                "frame.border": "ansicyan",
                "output": "bg:#1a1b26 fg:#c0caf5",
                "message.meta.left": "fg:#a9b1d6",
                "message.meta.right": "fg:#9aa5ce",
                "message.left": "fg:#7aa2f7",
                "message.right": "fg:#9ece6a",
                "message.reaction.left": "fg:#bb9af7",
                "message.reaction.right": "fg:#f7768e",
            }
        )
        self._build_bindings()
        self.app = Application(
            layout=Layout(
                HSplit(
                    [
                        Frame(self.output_window, title="Chat", style="class:frame"),
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
            self._scroll_to_bottom()

        self.bindings = kb

    def render_messages(self) -> None:
        formatted: List[tuple[str, str]] = []
        width = self._get_content_width()
        line_count = 0
        render_info = self.output_window.render_info
        if render_info:
            visible_height = render_info.window_height
            max_scroll = max(0, self._last_line_count - visible_height)
            was_at_bottom = self.output_window.vertical_scroll >= max_scroll
        else:
            was_at_bottom = True
        for msg in self.state.messages:
            align = "right" if msg.user == self.state.user else "left"
            meta_style = f"class:message.meta.{align}"
            body_style = f"class:message.{align}"
            reaction_style = f"class:message.reaction.{align}"
            header = f"[{msg.id}] {msg.user} @ {self._format_timestamp(msg.timestamp)}"
            for line in self._align_text(header, width, align):
                formatted.append((meta_style, f"{line}\n"))
                line_count += 1
            for line in self._align_text(self._decrypt(msg.ciphertext), width, align):
                formatted.append((body_style, f"{line}\n"))
                line_count += 1
            for reaction_line in self._format_reactions(msg.reactions):
                for line in self._align_text(reaction_line, width, align):
                    formatted.append((reaction_style, f"{line}\n"))
                    line_count += 1
            formatted.append(("", "\n"))
            line_count += 1
        self.output_control.text = FormattedText(formatted)
        self._last_line_count = line_count
        if was_at_bottom:
            self._scroll_to_bottom()
        self.app.invalidate()

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

    @staticmethod
    def _align_text(text: str, width: int, align: str) -> List[str]:
        lines: List[str] = []
        for raw_line in text.splitlines() or [""]:
            wrapped = textwrap.wrap(raw_line, width=width) or [""]
            for line in wrapped:
                lines.append(line.rjust(width) if align == "right" else line)
        return lines

    def _get_content_width(self) -> int:
        try:
            columns = get_app().output.get_size().columns
        except Exception:
            columns = 80
        return max(20, columns - 4)

    def _get_cursor_position(self) -> Point:
        try:
            scroll = self.output_window.vertical_scroll
        except Exception:
            scroll = 0
        return Point(x=0, y=scroll)

    def _scroll_to_bottom(self) -> None:
        render_info = self.output_window.render_info
        if not render_info:
            return
        visible_height = render_info.window_height
        self.output_window.vertical_scroll = max(0, self._last_line_count - visible_height)

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
