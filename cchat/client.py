"""Terminal chat client with end-to-end encryption."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import textwrap
import ssl
from datetime import datetime, timedelta
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
from textual.widgets import Button, Footer, Header, Label, RichLog, TextArea

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
        app = self.app
        if isinstance(app, ChatApp):
            app._mark_activity()
            if not app.connection_ok and event.key != "ctrl+r":
                event.stop()
                event.prevent_default()
                return
        if event.key in {"shift+enter", "ctrl+j"}:
            self.insert("\n")
            event.stop()
            event.prevent_default()
            return
        if event.key == "enter":
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
        layout: horizontal;
        position: absolute;
        layer: overlay;
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
        yield Button("Reply to")
        for emoji in self.emojis:
            yield Button(emoji)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        label = str(event.button.label)
        app = self.app
        if isinstance(app, ChatApp):
            if label == "Reply to":
                app.reply_to_message_from_menu(self.message_id)
            else:
                app.send_reaction_from_menu(self.message_id, label)


class ChatLog(RichLog):
    def on_mouse_down(self, event: MouseDown) -> None:
        app = self.app
        if isinstance(app, ChatApp):
            app._mark_activity()
        if event.button != 3:
            return
        if isinstance(app, ChatApp):
            app.open_reaction_menu(self, event)
            event.stop()

    def on_mouse_scroll_up(self, event) -> None:
        app = self.app
        if isinstance(app, ChatApp):
            app._mark_activity()
            app.update_scroll_state(self)

    def on_mouse_scroll_down(self, event) -> None:
        app = self.app
        if isinstance(app, ChatApp):
            app._mark_activity()
            app.update_scroll_state(self)


class ChatApp(App[None]):
    TITLE = "CChat"
    CSS = """
    Screen {
        background: #1a1b26;
        color: #c0caf5;
    }
    #chatlog {
        padding: 1 2;
        height: 1fr;
    }
    #status {
        height: 2;
        content-align: center middle;
        background: #1f2335;
        color: #9ece6a;
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
        ("ctrl+r", "reconnect", "Reconnect"),
    ]

    def __init__(
        self,
        state: ClientState,
        send_callback,
        react_callback,
        restart_callback,
        idle_timeout_seconds: int,
    ) -> None:
        super().__init__()
        self.state = state
        self.send_callback = send_callback
        self.react_callback = react_callback
        self.restart_callback = restart_callback
        self._idle_timeout = timedelta(seconds=idle_timeout_seconds)
        self._line_message_map: Dict[int, int] = {}
        self._reaction_menu: ReactionMenu | None = None
        self._selected_message_id: int | None = None
        self._user_scrolled_up = False
        self._pending_message_count = 0
        self._pending_start_index: int | None = None
        self._last_activity = datetime.now()
        self._connection_ok = True
        self._reconnect_attempted = False
        self._restart_requested = False

    @property
    def connection_ok(self) -> bool:
        return self._connection_ok

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield ChatLog(id="chatlog", wrap=True, highlight=False)
        yield Label("", id="status")
        yield ChatInput(id="input", show_line_numbers=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#input", TextArea).focus()
        self.render_messages()
        self._update_status_indicator()

    def on_key(self, event: Key) -> None:
        self._mark_activity()

    def on_mouse_down(self, event: MouseDown) -> None:
        self._mark_activity()
        if event.button != 1 or self._reaction_menu is None:
            return
        x = getattr(event, "screen_x", event.x)
        y = getattr(event, "screen_y", event.y)
        region = self._reaction_menu.region
        if not (region.x <= x < region.x + region.width and region.y <= y < region.y + region.height):
            self.dismiss_reaction_menu()

    def action_scroll_end(self) -> None:
        self.query_one("#chatlog", RichLog).scroll_end(animate=False)
        self._user_scrolled_up = False
        self._clear_pending_messages()

    def send_from_input(self) -> None:
        if not self._connection_ok:
            return
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
        self.dismiss_reaction_menu(update=False)
        self._selected_message_id = message_id
        menu = ReactionMenu(message_id, COMMON_REACTIONS)
        self._reaction_menu = menu
        log.auto_scroll = False
        self.mount(menu)
        menu.styles.offset = (log.region.x + event.x, log.region.y + event.y)
        self.call_after_refresh(self._position_reaction_menu, menu, log, event)
        self.render_messages()

    def dismiss_reaction_menu(self, *, update: bool = True) -> None:
        if self._reaction_menu is None and self._selected_message_id is None:
            return
        if self._reaction_menu is not None:
            self._reaction_menu.remove()
        self._reaction_menu = None
        self._selected_message_id = None
        log = self.query_one("#chatlog", RichLog)
        self.update_scroll_state(log)
        if update:
            self.render_messages()

    def _position_reaction_menu(self, menu: ReactionMenu, log: RichLog, event: MouseDown) -> None:
        if menu is not self._reaction_menu:
            return
        screen_width, screen_height = self.screen.size
        menu_width, menu_height = menu.region.size
        desired_x = log.region.x + event.x
        desired_y = log.region.y + event.y
        max_x = max(0, screen_width - menu_width)
        max_y = max(0, screen_height - menu_height)
        clamped_x = max(0, min(desired_x, max_x))
        clamped_y = max(0, min(desired_y, max_y))
        menu.styles.offset = (clamped_x, clamped_y)

    def send_reaction_from_menu(self, message_id: int, emoji: str) -> None:
        self.dismiss_reaction_menu()
        remove = self._has_reaction(message_id, emoji)
        asyncio.create_task(self.react_callback(message_id, emoji, remove))

    def reply_to_message_from_menu(self, message_id: int) -> None:
        self.dismiss_reaction_menu()
        target = next((m for m in self.state.messages if m.id == message_id), None)
        if not target:
            return
        body_text = self._decrypt(target.ciphertext)
        lines = body_text.splitlines() or [""]
        quote_lines = "\n".join(f"| {line}" for line in lines)
        quote_block = f"*replying to: {target.user}*\n{quote_lines}\n\n"
        input_area = self.query_one("#input", TextArea)
        if input_area.text and not input_area.text.endswith("\n"):
            input_area.insert("\n")
        input_area.insert(quote_block)
        input_area.focus()

    def render_messages(self) -> None:
        log = self.query_one("#chatlog", RichLog)
        should_autoscroll = self._should_autoscroll(log)
        log.auto_scroll = should_autoscroll
        log.clear()
        self._line_message_map.clear()
        line_index = 0
        for index, msg in enumerate(self.state.messages):
            if (
                self._pending_message_count > 0
                and self._pending_start_index is not None
                and index == self._pending_start_index
            ):
                line_index = self._render_new_messages_marker(log, line_index)
            align = "right" if msg.user == self.state.user else "left"
            meta_style = "italic #b8b8b8"
            body_style = "#bb9af7" if align == "right" else "#e0af68"
            reaction_style = "#9aa0a6"
            body_text = self._decrypt(msg.ciphertext)
            body_lines = self._format_reply_lines(body_text, body_style)
            header_text = f"[{msg.id}] {msg.user} @ {self._format_timestamp(msg.timestamp)}"
            reaction_lines = self._format_reactions(msg.reactions)
            if msg.id == self._selected_message_id:
                line_index = self._render_selected_message(
                    log=log,
                    align=align,
                    header_text=header_text,
                    body_lines=body_lines,
                    reaction_lines=reaction_lines,
                    meta_style=meta_style,
                    reaction_style=reaction_style,
                    line_index=line_index,
                    message_id=msg.id,
                )
            else:
                header = Text(header_text, style=meta_style)
                body = Text()
                for idx, (line, style) in enumerate(body_lines):
                    if idx:
                        body.append("\n")
                    body.append(line, style=style)
                log.write(Align(header, align=align))
                log.write(Align(body, align=align))
                self._line_message_map[line_index] = msg.id
                body_line_count = body_text.count("\n") + 1
                for i in range(1, body_line_count + 1):
                    self._line_message_map[line_index + i] = msg.id
                line_index += 1 + body_line_count
                for reaction_line in reaction_lines:
                    log.write(Align(Text(reaction_line, style=reaction_style), align=align))
                    self._line_message_map[line_index] = msg.id
                    line_index += 1
                log.write(Text(""))
                self._line_message_map[line_index] = msg.id
                line_index += 1
        if should_autoscroll:
            if self._is_idle():
                log.scroll_end(animate=False)
                self._user_scrolled_up = False
            else:
                self.action_scroll_end()
        self._update_status_indicator()

    def _should_autoscroll(self, log: RichLog) -> bool:
        if self._reaction_menu is not None:
            return False
        if self._selected_message_id is not None:
            return False
        if self._user_scrolled_up:
            return False
        max_scroll_y = getattr(log, "max_scroll_y", 0)
        return log.scroll_y >= max_scroll_y

    def _render_selected_message(
        self,
        *,
        log: RichLog,
        align: str,
        header_text: str,
        body_lines: List[tuple[str, str]],
        reaction_lines: List[str],
        meta_style: str,
        reaction_style: str,
        line_index: int,
        message_id: int,
    ) -> int:
        lines: List[tuple[str, str]] = [(header_text, meta_style)]
        lines.extend(body_lines)
        lines.extend((line, reaction_style) for line in reaction_lines)

        max_line_len = max(len(line) for line, _ in lines) if lines else 1
        max_inner_width = max(1, log.region.width - 4)
        inner_width = min(max_line_len, max_inner_width)

        wrapped_lines: List[tuple[str, str]] = []
        for line, style in lines:
            wrapped = textwrap.wrap(line, width=inner_width) or [""]
            for piece in wrapped:
                wrapped_lines.append((piece, style))

        border_style = "#e0af68"
        highlight_bg = "#28344a"
        top = Text("+" + "-" * (inner_width + 2) + "+", style=border_style)
        log.write(Align(top, align=align))
        self._line_message_map[line_index] = message_id
        line_index += 1
        for line, style in wrapped_lines:
            content = Text()
            content.append("| ", style=border_style)
            content.append(line.ljust(inner_width), style=f"{style} on {highlight_bg}")
            content.append(" |", style=border_style)
            log.write(Align(content, align=align))
            self._line_message_map[line_index] = message_id
            line_index += 1
        bottom = Text("+" + "-" * (inner_width + 2) + "+", style=border_style)
        log.write(Align(bottom, align=align))
        self._line_message_map[line_index] = message_id
        line_index += 1
        log.write(Text(""))
        self._line_message_map[line_index] = message_id
        line_index += 1
        return line_index

    def _format_reply_lines(self, body_text: str, body_style: str) -> List[tuple[str, str]]:
        lines = body_text.splitlines() or [""]
        styled_lines: List[tuple[str, str]] = []
        reply_user_style = body_style
        if lines and lines[0].startswith("*replying to:") and lines[0].endswith("*"):
            header = lines[0].strip("*")
            reply_user = header.split("replying to:", 1)[-1].strip()
            if reply_user:
                reply_user_style = "#bb9af7" if reply_user == self.state.user else "#e0af68"
            styled_lines.append((header, f"italic {reply_user_style}"))
            lines = lines[1:]
        for line in lines:
            if line.startswith("| "):
                styled_lines.append((line, f"italic {reply_user_style}"))
            else:
                styled_lines.append((line, body_style))
        return styled_lines

    def update_scroll_state(self, log: RichLog) -> None:
        max_scroll_y = getattr(log, "max_scroll_y", 0)
        self._user_scrolled_up = log.scroll_y < max_scroll_y
        log.auto_scroll = not self._user_scrolled_up and self._reaction_menu is None
        if not self._user_scrolled_up:
            self._clear_pending_messages()
        self._update_status_indicator()

    def _update_status_indicator(self) -> None:
        label = self.query_one("#status", Label)
        lines: List[tuple[str, str]] = []
        if not self._connection_ok:
            lines.append(("DISCONNECTED", "#f7768e"))
            lines.append(("Press Ctrl+R to reconnect", "#c0caf5"))
        if self._pending_message_count > 0:
            lines.append((f"{self._pending_message_count} new message(s)", "#9ece6a"))
        if not lines:
            label.update("")
            label.display = False
            return
        text = Text()
        for idx, (line, style) in enumerate(lines):
            if idx:
                text.append("\n")
            text.append(line, style=style)
        label.update(text)
        label.display = True

    def _clear_pending_messages(self) -> None:
        self._pending_message_count = 0
        self._pending_start_index = None

    @staticmethod
    def _render_new_messages_marker(log: RichLog, line_index: int) -> int:
        marker_style = "#9aa5ce"
        log.write(Align(Text("New messages", style=marker_style), align="center"))
        line_index += 1
        log.write(Align(Text("-" * 40, style=marker_style), align="center"))
        line_index += 1
        return line_index

    def _mark_activity(self) -> None:
        self._last_activity = datetime.now()
        if not self._user_scrolled_up:
            self._clear_pending_messages()
            self._update_status_indicator()

    def _is_idle(self) -> bool:
        return datetime.now() - self._last_activity >= self._idle_timeout

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
        return [", ".join(parts)]

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
        log = self.query_one("#chatlog", RichLog)
        if message.user != self.state.user and (
            not self._should_autoscroll(log) or self._is_idle()
        ):
            self._pending_message_count += 1
            if self._pending_start_index is None:
                self._pending_start_index = len(self.state.messages)
        self.state.messages.append(message)
        self.render_messages()

    def feed_reaction(self, message_id: int, reaction: Reaction) -> None:
        self.feed_reaction_action(message_id, reaction, action="add")

    def feed_reaction_action(self, message_id: int, reaction: Reaction, *, action: str) -> None:
        target = next((m for m in self.state.messages if m.id == message_id), None)
        if not target:
            return
        if action == "remove":
            target.reactions = [
                existing
                for existing in target.reactions
                if not (existing.emoji == reaction.emoji and existing.user == reaction.user)
            ]
        else:
            target.reactions.append(reaction)
        self.render_messages()

    def set_connection_status(self, connected: bool) -> None:
        if self._connection_ok == connected:
            return
        self._connection_ok = connected
        input_area = self.query_one("#input", TextArea)
        input_area.disabled = not connected
        if connected:
            input_area.focus()
        self._update_status_indicator()

    def action_reconnect(self) -> None:
        if self._connection_ok or self._reconnect_attempted:
            return
        self._reconnect_attempted = True
        self._restart_requested = True
        self.exit()

    @property
    def restart_requested(self) -> bool:
        return self._restart_requested


def _restart_args() -> List[str]:
    spec = globals().get("__spec__")
    if spec and getattr(spec, "name", None):
        return [sys.executable, "-m", spec.name, *sys.argv[1:]]
    return [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]]

    def _has_reaction(self, message_id: int, emoji: str) -> bool:
        target = next((m for m in self.state.messages if m.id == message_id), None)
        if not target:
            return False
        return any(
            reaction.emoji == emoji and reaction.user == self.state.user
            for reaction in target.reactions
        )


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
        restart_args = _restart_args()

        def restart_client() -> None:
            os.execv(restart_args[0], restart_args)

        ui = ChatApp(
            state,
            send_callback=lambda text: route_command(websocket, state, text),
            react_callback=lambda message_id, emoji, remove=False: send_reaction(
                websocket,
                state,
                message_id,
                emoji,
                remove=remove,
            ),
            restart_callback=restart_client,
            idle_timeout_seconds=args.idle_timeout,
        )
        listener_task = asyncio.create_task(listen_server(websocket, state, ui))

        try:
            await ui.run_async()
            if ui.restart_requested:
                restart_client()
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


async def send_reaction(
    websocket,
    state: ClientState,
    message_id: int,
    emoji: str,
    *,
    remove: bool = False,
) -> None:
    await websocket.send(
        json.dumps(
            {
                "type": "reaction",
                "message_id": message_id,
                "emoji": emoji,
                "user": state.user,
                "remove": remove,
            }
        )
    )


async def listen_server(websocket, state: ClientState, ui: ChatApp) -> None:
    try:
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
                action = payload.get("action", "add")
                ui.feed_reaction_action(payload["message_id"], reaction, action=action)
    except websockets.exceptions.ConnectionClosed:
        ui.set_connection_status(False)
    except Exception:
        ui.set_connection_status(False)
        raise
    else:
        ui.set_connection_status(False)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Connect to a cchat server")
    parser.add_argument("--server", default="wss://localhost:8765", help="WebSocket server URL")
    parser.add_argument("--user", help="Display name (otherwise remembered from config)")
    parser.add_argument("--insecure", action="store_true", help="Skip SSL verification (development only)")
    parser.add_argument(
        "--idle-timeout",
        type=int,
        default=15,
        help="Seconds of inactivity before messages count as unread",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(run_client(args))


if __name__ == "__main__":
    main()
