"""WebSocket Hub compatible with EndstoneMC-ARC-QQ-Sync-Plugin HubClient."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
from websockets.asyncio.server import ServerConnection, serve

from astrbot.api import logger

from .binding_store import BindingStore

HUB_MAX_NUMERIC_SERVER_ID = 9999
# Wait for all targeted servers' command_result before merging into one QQ
# message; flushes with partial results after this timeout.
COMMAND_RESULT_TIMEOUT = 10.0
_mc_format_code_pattern = re.compile("\u00a7.")

# QQ 群内由弧光 EndStone 消息中枢识别的指令（群内须 /mc 前缀）
# 例如 /mc help、/mc cmd stop；剥前缀后以内形 /help、/cmd 下发 QQ Sync
ARC_GROUP_COMMANDS = frozenset(
    {
        "help",
        "servers",
        "admins",
        "addadmin",
        "deladmin",
        "list",
        "tps",
        "info",
        "cmd",
        "who",
        "ban",
        "unban",
        "banlist",
        "unbindqq",
        "reload",
        "重启",
        "绑定",
    }
)


def strip_minecraft_format_codes(text: str) -> str:
    """Remove Minecraft section format codes from text.

    Args:
        text: Raw game text that may contain section signs.

    Returns:
        Clean text without format codes.
    """
    if not text:
        return text
    return _mc_format_code_pattern.sub("", text)


def format_merged_server_info(payloads: list[dict[str, Any]]) -> str:
    """Merge per-server info payloads, deduplicating hardware by host_id.

    Args:
        payloads: Successful ``server_info`` results from QQ Sync.

    Returns:
        Single QQ-facing text with one hardware block per physical host.
    """
    if not payloads:
        return "ℹ️ 未能获取任何服务器信息。"

    groups: dict[str, list[dict[str, Any]]] = {}
    for item in payloads:
        system = item.get("system") if isinstance(item.get("system"), dict) else {}
        host_id = (
            str(system.get("host_id") or "").strip()
            or f"solo:{item.get('server_name')}"
        )
        groups.setdefault(host_id, []).append(item)

    lines: list[str] = ["ℹ️ 服务器信息"]
    for host_id, members in groups.items():
        system = (
            members[0].get("system")
            if isinstance(members[0].get("system"), dict)
            else {}
        )
        names = [str(m.get("server_name") or "?") for m in members]
        hostname = str(system.get("hostname") or host_id)
        lines.append("")
        lines.append(f"🖥️ 物理机 {hostname}（{'、'.join(names)}）")
        lines.append(f"• 操作系统: {system.get('os') or '?'}")

        cpu = system.get("cpu") if isinstance(system.get("cpu"), dict) else {}
        model = str(cpu.get("model") or "未知")
        if len(model) > 50:
            model = model[:50] + "..."
        lines.append(f"• CPU型号: {model}")
        if cpu.get("max_freq_ghz"):
            freq = f"• CPU主频: {float(cpu['max_freq_ghz']):.2f}GHz"
            if cpu.get("current_freq_ghz"):
                freq += f" (当前: {float(cpu['current_freq_ghz']):.2f}GHz)"
            lines.append(freq)
        lines.append(
            f"• CPU核心: {cpu.get('physical_cores') or '?'}核"
            f"{cpu.get('logical_cores') or '?'}线程"
        )
        lines.append(f"• CPU使用率: {float(cpu.get('usage_percent') or 0):.1f}%")

        mem = system.get("memory") if isinstance(system.get("memory"), dict) else {}
        lines.append(
            f"• 内存: {float(mem.get('used_gb') or 0):.1f}GB / "
            f"{float(mem.get('total_gb') or 0):.1f}GB "
            f"({float(mem.get('percent') or 0):.1f}%)"
        )

        gpus = system.get("gpus") if isinstance(system.get("gpus"), list) else []
        if gpus:
            for idx, gpu in enumerate(gpus, start=1):
                if not isinstance(gpu, dict):
                    continue
                label = f"GPU{idx}" if len(gpus) > 1 else "GPU"
                name = str(gpu.get("name") or "未知")
                mem_mb = gpu.get("memory_mb")
                if mem_mb is not None:
                    lines.append(f"• {label}: {name} ({float(mem_mb):.0f}MB)")
                else:
                    lines.append(f"• {label}: {name}")
        else:
            lines.append("• GPU: 未检测到")

        for disk in system.get("disks") or []:
            if not isinstance(disk, dict) or "error" in disk:
                continue
            lines.append(
                f"• 硬盘({disk.get('device')}): "
                f"{float(disk.get('used_gb') or 0):.1f}GB / "
                f"{float(disk.get('total_gb') or 0):.1f}GB "
                f"({float(disk.get('percent') or 0):.1f}%)"
            )

        for m in members:
            lines.append(
                f"• [{m.get('server_name') or '?'}] "
                f"MC {m.get('minecraft_version') or '?'} / "
                f"Endstone {m.get('endstone_version') or '?'} / "
                f"在线 {m.get('online_players', 0)}/{m.get('max_players', '?')} / "
                f"运行 {m.get('uptime_str') or '?'}"
            )

    return "\n".join(lines)


def strip_mc_command_prefix(raw_message: str) -> str | None:
    """Strip required /mc prefix into internal /command form.

    Args:
        raw_message: Raw group text.

    Returns:
        Internal command starting with / (e.g. /help), or None if not /mc.
    """
    s = (raw_message or "").strip()
    if len(s) < 3 or not s.lower().startswith("/mc"):
        return None
    rest = s[3:]
    if not rest:
        return "/help"
    if not rest[0].isspace():
        # Require a space after /mc (avoid /mchelp colliding with other bots).
        return None
    rest = rest.lstrip()
    if not rest:
        return "/help"
    if rest.startswith("/"):
        return rest
    return f"/{rest}"


def extract_event_raw_text(event) -> str:
    """Return the best-effort original user text for a platform message.

    Prefer adapter ``raw_message`` so ``/mc`` survives wake_prefix stripping.
    """
    try:
        message_obj = getattr(event, "message_obj", None)
        raw_obj = getattr(message_obj, "raw_message", None) if message_obj else None
        if isinstance(raw_obj, dict):
            raw_message = str(raw_obj.get("raw_message") or "").strip()
            if raw_message:
                return raw_message
        elif raw_obj is not None:
            raw_message = str(getattr(raw_obj, "raw_message", "") or "").strip()
            if raw_message:
                return raw_message
    except Exception:
        pass
    return str(getattr(event, "message_str", "") or "").strip()


def normalize_mc_arc_raw_message(raw_message: str) -> str | None:
    """Detect ``/mc ...`` even when wake_prefix already removed the leading ``/``.

    Returns:
        Canonical ``/mc ...`` text, or None if this is not an ARC MC command.
    """
    text = (raw_message or "").strip()
    if not text:
        return None
    if strip_mc_command_prefix(text) is not None:
        return text
    lowered = text.lower()
    if lowered == "mc":
        return "/mc"
    if lowered.startswith("mc") and (len(text) == 2 or text[2].isspace()):
        rest = text[2:].lstrip()
        if rest.startswith("/"):
            rest = rest.lstrip("/").lstrip()
        if not rest:
            return "/mc"
        return f"/mc {rest}"
    return None


def parse_hub_command_routing(raw_message: str) -> tuple[str, int | None]:
    """Parse optional numeric server id from a QQ group command.

    Args:
        raw_message: Raw group command text (/mc ... or internal /...).

    Returns:
        Tuple of (command line without server id, target server id or None).
    """
    normalized = strip_mc_command_prefix(raw_message)
    s = normalized if normalized is not None else (raw_message or "").strip()
    if not s.startswith("/"):
        return s, None

    parts = s.split()
    if not parts:
        return s, None

    head = parts[0]
    cmd = head[1:].lower() if head.startswith("/") else head.lower()
    args = parts[1:]

    def _valid_sid(token: str) -> int | None:
        if not token.isdigit():
            return None
        sid = int(token)
        if 1 <= sid <= HUB_MAX_NUMERIC_SERVER_ID:
            return sid
        return None

    if cmd == "cmd":
        if args and (sid := _valid_sid(args[0])) is not None:
            return f"/cmd {' '.join(args[1:])}".strip(), sid
        return s, None

    if cmd == "who":
        if len(args) >= 2 and (sid := _valid_sid(args[-1])) is not None:
            return f"/who {' '.join(args[:-1])}".strip(), sid
        return s, None

    query_cmds = {"list", "tps", "info", "banlist", "help", "reload", "servers"}
    if cmd in query_cmds and args and (sid := _valid_sid(args[-1])) is not None:
        return f"{head} {' '.join(args[:-1])}".strip(), sid

    return s, None


def is_known_arc_command(raw_message: str) -> bool:
    """Return whether a QQ message is an ARC MC group command.

    Args:
        raw_message: Raw group text; must start with /mc.

    Returns:
        True if the leading command is handled by the message center / MC plugin.
    """
    mc_raw = normalize_mc_arc_raw_message(raw_message)
    if mc_raw is None:
        return False
    normalized = strip_mc_command_prefix(mc_raw)
    if normalized is None:
        return False
    head = normalized.split(None, 1)[0][1:]
    if not head:
        return False
    # ASCII commands are matched case-insensitively; Chinese stay as-is.
    key = head.lower() if head.isascii() else head
    return key in ARC_GROUP_COMMANDS


def is_mc_activate_command(raw_message: str) -> bool:
    """Return whether the message is ``/mc activate`` (handled locally, not MC)."""
    mc_raw = normalize_mc_arc_raw_message(raw_message)
    if mc_raw is None:
        return False
    normalized = strip_mc_command_prefix(mc_raw)
    if normalized is None:
        return False
    head = normalized.split(None, 1)[0][1:].lower()
    return head == "activate"


SendQQBroadcastCallback = Callable[[str], Awaitable[None]]
SendQQReplyCallback = Callable[[str, str], Awaitable[None]]
MuteQQCallback = Callable[[str, int], Awaitable[bool]]
GetArcGuardApiCallback = Callable[[], Any]


class ArcHubServer:
    """ARC message center Hub: accepts MC HubClient connections."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        token: str,
        hub_server_name: str,
        binding_store: BindingStore,
        broadcast_qq: SendQQBroadcastCallback,
        reply_qq: SendQQReplyCallback,
        set_group_card: Callable[[int, str], Awaitable[None]] | None = None,
        mute_qq: MuteQQCallback | None = None,
        get_arc_guard_api: GetArcGuardApiCallback | None = None,
        group_names: dict[str, str] | None = None,
        hub_admins: list[str] | None = None,
        sync_group_card: bool = True,
    ):
        self.host = host
        self.port = int(port)
        self.token = token or ""
        self.hub_server_name = hub_server_name or "弧光EndStone消息中枢"
        self.binding_store = binding_store
        self.broadcast_qq = broadcast_qq
        self.reply_qq = reply_qq
        self.set_group_card = set_group_card
        self.mute_qq = mute_qq
        self.get_arc_guard_api = get_arc_guard_api
        self.group_names = group_names or {}
        self.hub_admins = {str(a) for a in (hub_admins or []) if str(a).strip()}
        self.sync_group_card = bool(sync_group_card)

        self.connected_servers: dict[str, ServerConnection] = {}
        self.ws_to_server: dict[ServerConnection, str] = {}
        self.service_clients: dict[ServerConnection, dict[str, str]] = {}
        self._server_numeric_id_by_name: dict[str, int] = {}
        self._next_server_numeric_id = 1
        self._running = False
        self._server = None
        self._serve_task: asyncio.Task | None = None
        self.process_ai_chat: (
            Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None
        ) = None
        self.ai_chat_timeout = 180.0
        self._pending_ai_tool: dict[str, tuple[ServerConnection, asyncio.Future]] = {}
        self._pending_core_rpc: dict[str, tuple[ServerConnection, asyncio.Future]] = {}
        # request_id -> aggregation state for merged command_forward replies.
        self._pending_command_replies: dict[str, dict[str, Any]] = {}

    def _ensure_server_numeric_id(self, server_name: str) -> int:
        if server_name not in self._server_numeric_id_by_name:
            self._server_numeric_id_by_name[server_name] = self._next_server_numeric_id
            self._next_server_numeric_id += 1
        return self._server_numeric_id_by_name[server_name]

    def get_server_catalog(self) -> list[dict[str, Any]]:
        items = [
            {"id": sid, "name": name}
            for name, sid in self._server_numeric_id_by_name.items()
        ]
        items.sort(key=lambda x: x["id"])
        return items

    def connected_server_names(self) -> list[str]:
        return list(self.connected_servers.keys())

    async def start(self) -> None:
        """Start the WebSocket Hub listener."""
        if self._running:
            return
        self._running = True
        logger.info(
            f"[弧光EndStone消息中枢] 启动 WebSocket Hub ws://{self.host}:{self.port}"
        )
        self._serve_task = asyncio.create_task(self._run_ws_server())

    async def stop(self) -> None:
        """Stop Hub and close all MC connections."""
        self._fail_pending_ai_tools("弧光消息中枢已停止")
        self._fail_pending_core_rpcs("弧光消息中枢已停止")
        for pending in self._pending_command_replies.values():
            timer = pending.get("timer")
            if timer:
                timer.cancel()
        self._pending_command_replies.clear()
        self._running = False
        for ws in list(self.connected_servers.values()):
            try:
                await ws.close()
            except Exception:
                pass
        self.connected_servers.clear()
        self.ws_to_server.clear()
        self.service_clients.clear()
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            try:
                await server.wait_closed()
            except Exception:
                pass
        if self._serve_task is not None:
            try:
                await self._serve_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            self._serve_task = None
        logger.info("[弧光EndStone消息中枢] Hub 已停止")

    async def _run_ws_server(self) -> None:
        try:
            async with serve(
                self._handle_server_connection,
                self.host,
                self.port,
                ping_interval=20,
                ping_timeout=10,
            ) as server:
                self._server = server
                logger.info(
                    f"[弧光EndStone消息中枢] Hub 已监听 ws://{self.host}:{self.port}"
                )
                await server.wait_closed()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[弧光EndStone消息中枢] Hub 启动失败: {e}")
            self._running = False
        finally:
            self._server = None

    async def _handle_server_connection(self, websocket: ServerConnection) -> None:
        server_name = None
        try:
            try:
                raw = await asyncio.wait_for(websocket.recv(), timeout=15)
                data = json.loads(raw)
            except asyncio.TimeoutError:
                logger.warning("[弧光EndStone消息中枢] 连接超时，未收到注册消息")
                await websocket.close()
                return
            except json.JSONDecodeError:
                logger.warning("[弧光EndStone消息中枢] 收到无效注册消息")
                await websocket.close()
                return

            if data.get("type") != "register":
                logger.warning(
                    f"[弧光EndStone消息中枢] 期望 register，收到: {data.get('type')}"
                )
                await websocket.close()
                return

            if self.token and data.get("token") != self.token:
                logger.warning(
                    f"[弧光EndStone消息中枢] Token 验证失败: {data.get('server_name')}"
                )
                await websocket.close(1008, "认证失败")
                return

            server_name = str(data.get("server_name") or "未知服务器")
            role = str(data.get("role") or "server").strip().lower() or "server"
            is_service = role in {"ai_helper", "service"}

            if is_service:
                await self._bind_service_client(websocket, server_name, role)
                logger.info(
                    f"[弧光EndStone消息中枢] 服务客户端 [{server_name}] role={role} 已连接"
                )
                welcome = self._build_welcome_payload(my_server_id=None)
                await websocket.send(json.dumps(welcome, ensure_ascii=False))
            else:
                if server_name in self.connected_servers:
                    old_ws = self.connected_servers[server_name]
                    try:
                        await old_ws.close()
                    except Exception:
                        pass
                    self.ws_to_server.pop(old_ws, None)

                self.connected_servers[server_name] = websocket
                self.ws_to_server[websocket] = server_name
                remote_id = self._ensure_server_numeric_id(server_name)
                logger.info(
                    f"[弧光EndStone消息中枢] 子服 [{server_name}] 已连接（编号 {remote_id}）"
                )

                welcome = self._build_welcome_payload(my_server_id=remote_id)
                await websocket.send(json.dumps(welcome, ensure_ascii=False))

                # Hub WS connect is authoritative (force-killed MC may never send server_start).
                await self._broadcast_to_others(
                    server_name,
                    {
                        "type": "cross_server_event",
                        "from_server": server_name,
                        "event": "server_connected",
                    },
                )
                await self.broadcast_qq(f"[{server_name}]\n服务器已启动！")

            async for message in websocket:
                try:
                    msg_data = json.loads(message)
                    await self._handle_server_message(server_name, websocket, msg_data)
                except json.JSONDecodeError:
                    logger.warning(f"[弧光EndStone消息中枢] [{server_name}] 无效 JSON")

        except websockets.exceptions.ConnectionClosed:
            if server_name:
                kind = "服务客户端" if websocket in self.service_clients else "子服"
                logger.info(f"[弧光EndStone消息中枢] {kind} [{server_name}] 断开连接")
        except Exception as e:
            logger.error(f"[弧光EndStone消息中枢] 处理子服连接出错: {e}")
        finally:
            was_service = websocket in self.service_clients
            self.service_clients.pop(websocket, None)
            self.ws_to_server.pop(websocket, None)
            if was_service:
                self._fail_pending_ai_tools("AI Helper 连接已断开", websocket)
            if server_name and not was_service:
                self._fail_pending_core_rpcs(f"子服 [{server_name}] 已断开", websocket)
                self.connected_servers.pop(server_name, None)
                # Hub WS drop is authoritative (force-killed MC may never send server_stop).
                await self._broadcast_to_others(
                    server_name,
                    {
                        "type": "cross_server_event",
                        "from_server": server_name,
                        "event": "server_disconnected",
                    },
                )
                try:
                    await self.broadcast_qq(f"[{server_name}]\n服务器已停止！")
                except Exception:
                    pass

    async def _handle_server_message(
        self, server_name: str, ws: ServerConnection, data: dict
    ) -> None:
        msg_type = data.get("type")
        if msg_type == "game_event":
            await self._handle_game_event(server_name, data)
        elif msg_type == "api_send":
            text = data.get("text", "")
            if text:
                await self.broadcast_qq(strip_minecraft_format_codes(str(text)))
        elif msg_type == "command_result":
            await self._handle_command_result(server_name, data)
        elif msg_type == "ping":
            await ws.send(json.dumps({"type": "pong"}))
        elif msg_type == "data_rpc":
            await self._handle_data_rpc(ws, data)
        elif msg_type == "set_group_card":
            if self.sync_group_card and self.set_group_card:
                try:
                    uid = int(data.get("user_id"))
                    card = str(data.get("card") or "")
                    if card:
                        await self.set_group_card(uid, card)
                except Exception as e:
                    logger.warning(f"[弧光EndStone消息中枢] 改群名片失败: {e}")
        elif msg_type == "restart_vote_online_list_response":
            # Restart vote is handled on MC side via command_forward; ignore here.
            pass
        elif msg_type == "ai_chat":
            asyncio.create_task(self._handle_ai_chat(server_name, ws, data))
        elif msg_type == "ai_tool_response":
            rid = str(data.get("request_id") or "")
            pending = self._pending_ai_tool.get(rid)
            if pending:
                _ws, fut = pending
                if fut and not fut.done():
                    fut.set_result(data)
        elif msg_type == "core_rpc_response":
            rid = str(data.get("request_id") or "")
            pending = self._pending_core_rpc.get(rid)
            if pending:
                _ws, fut = pending
                if fut and not fut.done():
                    fut.set_result(data)
        else:
            logger.debug(f"[弧光EndStone消息中枢] [{server_name}] 未知类型: {msg_type}")

    def _build_welcome_payload(self, my_server_id: int | None) -> dict[str, Any]:
        """Build hub_welcome JSON for a newly registered client.

        Args:
            my_server_id: Numeric id for game servers; None for service clients.

        Returns:
            Welcome payload including ``ai_chat`` capability flag.
        """
        payload: dict[str, Any] = {
            "type": "hub_welcome",
            "connected_servers": list(self.connected_servers.keys()),
            "hub_server_name": self.hub_server_name,
            "server_catalog": self.get_server_catalog(),
            "sync_group_card": self.sync_group_card,
            "hub_admins": sorted(self.hub_admins),
            "features": ["ai_chat", "ai_tools"],
            "ai_chat": True,
        }
        if my_server_id is not None:
            payload["my_server_id"] = my_server_id
        return payload

    async def _bind_service_client(
        self, websocket: ServerConnection, server_name: str, role: str
    ) -> None:
        """Register a non-game Hub client (e.g. AI Helper) without fan-out.

        Args:
            websocket: Client WebSocket.
            server_name: Client-chosen name (should not collide with game servers).
            role: Service role such as ``ai_helper``.
        """
        for old_ws, meta in list(self.service_clients.items()):
            if meta.get("name") == server_name:
                self.service_clients.pop(old_ws, None)
                logger.warning(
                    "[弧光EndStone消息中枢] 服务连接 [%s] 已被新连接替换（多开服请使用互不相同的 server_name）",
                    server_name,
                )
                try:
                    await old_ws.close()
                except Exception:
                    pass
                break
        self.service_clients[websocket] = {"name": server_name, "role": role}

    def _fail_pending_ai_tools(
        self, reason: str, websocket: ServerConnection | None = None
    ) -> None:
        """Wake pending MC tool RPCs when a helper connection drops.

        Args:
            reason: Error text stored on unfinished futures.
            websocket: If set, only fail RPCs waiting on this helper.
        """
        exc = ConnectionError(reason)
        for rid, (ws, fut) in list(self._pending_ai_tool.items()):
            if websocket is not None and ws is not websocket:
                continue
            if not fut.done():
                fut.set_exception(exc)
            self._pending_ai_tool.pop(rid, None)

    def _fail_pending_core_rpcs(
        self, reason: str, websocket: ServerConnection | None = None
    ) -> None:
        """Wake pending QQ Sync core_rpc calls when a game server drops."""
        exc = ConnectionError(reason)
        for rid, (ws, fut) in list(self._pending_core_rpc.items()):
            if websocket is not None and ws is not websocket:
                continue
            if not fut.done():
                fut.set_exception(exc)
            self._pending_core_rpc.pop(rid, None)

    async def call_core_rpc(
        self,
        game_server: str,
        action: str,
        args: dict[str, Any] | None = None,
        *,
        timeout: float = 12,
    ) -> dict[str, Any]:
        """Ask QQ Sync on a game server to run an arc_core lookup (not AI Helper).

        Args:
            game_server: Connected Minecraft server name.
            action: e.g. ``player_basic_info``.
            args: Extra arguments for the action.
            timeout: Seconds to wait for the reply.

        Returns:
            ``core_rpc_response`` payload from QQ Sync.
        """
        ws = self.connected_servers.get(str(game_server or "").strip())
        if ws is None:
            raise RuntimeError(f"服务器 [{game_server}] 的群服互通未连接")
        request_id = str(uuid.uuid4())
        fut = asyncio.get_running_loop().create_future()
        self._pending_core_rpc[request_id] = (ws, fut)
        await ws.send(
            json.dumps(
                {
                    "type": "core_rpc",
                    "request_id": request_id,
                    "action": str(action),
                    "args": args or {},
                },
                ensure_ascii=False,
            )
        )
        try:
            resp = await asyncio.wait_for(fut, timeout=max(5.0, float(timeout)))
        except asyncio.TimeoutError as e:
            raise TimeoutError(f"core_rpc 超时: {action}") from e
        finally:
            self._pending_core_rpc.pop(request_id, None)
        if not isinstance(resp, dict):
            raise RuntimeError("core_rpc 返回格式异常")
        return resp

    def find_ai_helper_ws(self, game_server: str) -> ServerConnection | None:
        """Find the AI Helper WebSocket for a Minecraft server name.

        Args:
            game_server: Game server display name.

        Returns:
            Connected helper WebSocket, or None.
        """
        target = str(game_server or "").strip()
        if not target:
            return None
        expected = f"{target}#ai-helper"
        for ws, meta in list(self.service_clients.items()):
            if str(meta.get("role") or "") != "ai_helper":
                continue
            name = str(meta.get("name") or "")
            if name == expected or name == target or name.split("#", 1)[0] == target:
                return ws
        return None

    def list_ai_helper_game_names(self) -> list[str]:
        """Return connected AI Helper game-server names without the ``#ai-helper`` suffix.

        Returns:
            Unique game server names currently registered as ``ai_helper``.
        """
        names: list[str] = []
        seen: set[str] = set()
        for meta in list(self.service_clients.values()):
            if str(meta.get("role") or "") != "ai_helper":
                continue
            raw = str(meta.get("name") or "").strip()
            game = raw.split("#", 1)[0].strip() if raw else ""
            if game and game not in seen:
                seen.add(game)
                names.append(game)
        names.sort()
        return names

    async def call_ai_tool(
        self,
        game_server: str,
        action: str,
        args: dict[str, Any] | None = None,
        *,
        timeout: float = 20,
    ) -> dict[str, Any]:
        """Ask the MC AI Helper to run a server tool and return its JSON result.

        Args:
            game_server: Origin Minecraft server name.
            action: Tool action such as ``list`` / ``tps`` / ``info`` / ``cmd``.
            args: Extra arguments for the action.
            timeout: Seconds to wait for the helper reply.

        Returns:
            Helper ``ai_tool_response`` payload.

        Raises:
            RuntimeError: When the helper is not connected.
            TimeoutError: When the helper does not reply in time.
        """
        ws = self.find_ai_helper_ws(game_server)
        if ws is None:
            raise RuntimeError(f"服务器 [{game_server}] 的 AI Helper 未连接")
        request_id = str(uuid.uuid4())
        fut = asyncio.get_running_loop().create_future()
        self._pending_ai_tool[request_id] = (ws, fut)
        # Keep hub-authenticated identity on the message root. Helper ignores
        # permission_level / is_op inside args (model-forgeable) and only trusts
        # these top-level fields from the hub WebSocket.
        payload_args = dict(args or {})
        hub_level = payload_args.pop("permission_level", None)
        hub_is_op = payload_args.pop("is_op", None)
        message: dict[str, Any] = {
            "type": "ai_tool",
            "request_id": request_id,
            "action": str(action),
            "args": payload_args,
        }
        if hub_level not in (None, ""):
            message["permission_level"] = hub_level
        if hub_is_op is not None:
            message["is_op"] = bool(hub_is_op)
        await ws.send(json.dumps(message, ensure_ascii=False))
        try:
            resp = await asyncio.wait_for(fut, timeout=max(5.0, float(timeout)))
        except asyncio.TimeoutError as e:
            raise TimeoutError(f"AI 工具超时: {action}") from e
        finally:
            self._pending_ai_tool.pop(request_id, None)
        if not isinstance(resp, dict):
            raise RuntimeError("AI 工具返回格式异常")
        return resp

    async def _resolve_player_via_core(self, player_name: str) -> tuple[str, str]:
        """Ask QQ Sync (群服互通) to resolve a name via arc_core player APIs.

        Binding must not go through AI Helper. Cross-server sync lives inside
        arc_core (``player_basic_info`` / shared DB); QQ Sync only calls the API.
        """
        servers = self.connected_server_names()
        if not servers:
            logger.warning("[弧光EndStone消息中枢] 解析玩家失败: 无群服互通子服在线")
            return "", ""
        last_error = ""
        for game in servers:
            try:
                resp = await self.call_core_rpc(
                    game,
                    "player_basic_info",
                    {"player_name": player_name},
                    timeout=12,
                )
            except Exception as error:
                last_error = str(error)
                continue
            if not isinstance(resp, dict):
                last_error = "core_rpc 返回格式异常"
                continue
            if resp.get("ok"):
                result = (
                    resp.get("result") if isinstance(resp.get("result"), dict) else resp
                )
                name = str(result.get("player_name") or player_name).strip()
                xuid = str(result.get("xuid") or "").strip()
                if name and xuid:
                    return name, xuid
            err = str(resp.get("error") or "").strip()
            if err:
                last_error = err
        logger.warning(
            f"[弧光EndStone消息中枢] 解析玩家 {player_name} 失败: "
            f"{last_error or '所有子服均无记录'}"
        )
        return "", ""

    async def _handle_ai_chat(
        self, server_name: str, ws: ServerConnection, data: dict
    ) -> None:
        """Handle an MC AI chat RPC and reply on the same WebSocket.

        Args:
            server_name: Registered client name (game server or ``name#ai-helper``).
            ws: Source WebSocket.
            data: Incoming ``ai_chat`` payload.
        """
        request_id = data.get("request_id")
        if not self.process_ai_chat:
            await ws.send(
                json.dumps(
                    {
                        "type": "ai_chat_response",
                        "request_id": request_id,
                        "ok": False,
                        "error": "弧光消息中枢未就绪，无法进行 AstrBot 对话",
                    },
                    ensure_ascii=False,
                )
            )
            return

        payload = dict(data)
        if not str(payload.get("server_name") or "").strip():
            payload["server_name"] = str(server_name).split("#", 1)[0]
        try:
            result = await self.process_ai_chat(payload)
            if not isinstance(result, dict):
                result = {"ok": False, "error": "AstrBot 返回格式异常"}
        except Exception as e:
            logger.error(f"[弧光EndStone消息中枢] ai_chat 失败: {e}")
            result = {"ok": False, "error": str(e)}

        await ws.send(
            json.dumps(
                {
                    "type": "ai_chat_response",
                    "request_id": request_id,
                    "ok": bool(result.get("ok")),
                    "reply": result.get("reply") or "",
                    "error": result.get("error") or "",
                },
                ensure_ascii=False,
            )
        )

    async def _handle_data_rpc(self, ws: ServerConnection, data: dict) -> None:
        request_id = data.get("request_id")
        action = data.get("action")
        args = data.get("args") or {}
        try:
            result = self.binding_store.run_action(str(action), args)
            await ws.send(
                json.dumps(
                    {
                        "type": "data_rpc_response",
                        "request_id": request_id,
                        "ok": True,
                        "result": result,
                    },
                    ensure_ascii=False,
                )
            )
        except Exception as e:
            logger.error(f"[弧光EndStone消息中枢] data_rpc {action} 失败: {e}")
            await ws.send(
                json.dumps(
                    {
                        "type": "data_rpc_response",
                        "request_id": request_id,
                        "ok": False,
                        "error": str(e),
                    },
                    ensure_ascii=False,
                )
            )

    async def _handle_game_event(self, server_name: str, data: dict) -> None:
        event = data.get("event")
        player = strip_minecraft_format_codes(str(data.get("player", "")))
        message = strip_minecraft_format_codes(str(data.get("message", "")))
        session_count = data.get("session_count")
        playtime_str = data.get("playtime_str", "")
        origin = str(data.get("server_name") or server_name)
        raw_player_name = strip_minecraft_format_codes(
            str(data.get("raw_player_name") or "")
        ).strip()

        # Chat: optional Arc Guard before QQ / cross-server fan-out.
        if str(event or "") == "chat":
            blocked = await self._maybe_block_forbidden_chat(
                origin_server=server_name,
                display_player=player,
                raw_player_name=raw_player_name,
                message=message,
            )
            if blocked:
                return

        qq_msg = self._format_qq_message(
            origin,
            str(event or ""),
            player,
            message,
            session_count,
            str(playtime_str),
        )
        if qq_msg:
            await self.broadcast_qq(qq_msg)

        # server_start/stop still go to QQ above; cross-server connection status
        # uses hub WS server_connected/disconnected (reliable on force-kill).
        if str(event or "") in ("server_start", "server_stop"):
            return

        await self._broadcast_to_others(
            server_name,
            {
                "type": "cross_server_event",
                "from_server": origin,
                "event": event,
                "player": player,
                "message": message,
            },
        )

    async def _punish_mc_chat_with_jail(
        self, origin_server: str, player_name: str, minutes: int, reason: str
    ) -> tuple[bool, str]:
        """Try replacing QQ mute with prison time on the origin server."""
        if not player_name.strip():
            return False, "玩家名为空"
        try:
            resp = await self.call_ai_tool(
                origin_server,
                "jail",
                {
                    "player_name": player_name,
                    "minutes": str(max(1, int(minutes))),
                    "reason": reason,
                    "is_op": True,
                },
                timeout=20,
            )
        except Exception as e:
            return False, str(e)
        if not isinstance(resp, dict):
            return False, "监狱工具返回格式异常"
        if not resp.get("ok"):
            return False, str(resp.get("error") or "关押失败")
        text = str(resp.get("text") or "").strip() or "关押成功"
        return True, text

    async def _maybe_block_forbidden_chat(
        self,
        *,
        origin_server: str,
        display_player: str,
        raw_player_name: str,
        message: str,
    ) -> bool:
        """Run Arc Guard on MC chat; punish and block forward when hit.

        Args:
            origin_server: Connected Hub server name that sent the event.
            display_player: Display label (may include title).
            raw_player_name: Bare player name when provided by MC.
            message: Chat text.

        Returns:
            True if the chat must not be forwarded to QQ / other servers.
        """
        if not self.get_arc_guard_api:
            return False
        try:
            api = self.get_arc_guard_api()
        except Exception as e:
            logger.warning(f"[弧光EndStone消息中枢] 获取弧光护卫 API 失败: {e}")
            return False
        if api is None:
            return False

        player_name = raw_player_name
        bound_qq = ""
        if player_name:
            bound_qq = self.binding_store.get_player_qq(player_name)
        if not player_name or not bound_qq:
            resolved_name, resolved_qq = self.binding_store.resolve_player_binding(
                display_player
            )
            if resolved_name:
                player_name = player_name or resolved_name
            if resolved_qq:
                bound_qq = resolved_qq
        if not player_name:
            player_name = display_player.strip()

        try:
            result = api.check(message, user_id=bound_qq or None)
        except Exception as e:
            logger.warning(f"[弧光EndStone消息中枢] 弧光护卫检测失败: {e}")
            return False

        if not result.get("should_punish"):
            return False

        hits = int(result.get("hits") or 1)
        mute_seconds = int(result.get("mute_seconds") or 60)
        reply = str(result.get("reply") or "").strip()
        logger.info(
            "[弧光EndStone消息中枢] 弧光护卫命中 chat server=%s player=%s qq=%s hits=%s",
            origin_server,
            player_name,
            bound_qq or "-",
            hits,
        )
        await self.apply_forbidden_player_hit(
            origin_server=origin_server,
            player_name=player_name,
            bound_qq=bound_qq,
            hits=hits,
            mute_seconds=mute_seconds,
            reply=reply,
            reason="MC聊天违规",
        )
        return True

    async def apply_forbidden_player_hit(
        self,
        *,
        origin_server: str,
        player_name: str,
        bound_qq: str,
        hits: int,
        mute_seconds: int,
        reply: str,
        reason: str,
    ) -> str:
        """Apply the same Arc Guard punishment used for MC chat keyword hits.

        Jail when available, otherwise QQ mute; always kill + in-game warning
        and QQ notice.

        Args:
            origin_server: Game server name to punish on.
            player_name: Minecraft player name.
            bound_qq: Bound QQ id, or empty if unbound.
            hits: Forbidden-sequence hit count.
            mute_seconds: Mute / jail duration in seconds.
            reply: Warning text from Arc Guard.
            reason: Jail reason string.

        Returns:
            In-game warning text.
        """
        minutes = max(1, (int(mute_seconds) + 59) // 60)
        jailed = False
        jail_result = ""
        name = str(player_name or "").strip()
        if name:
            jailed, jail_result = await self._punish_mc_chat_with_jail(
                origin_server, name, minutes, reason
            )
            if jailed:
                logger.info(
                    "[弧光EndStone消息中枢] 弧光护卫改为监狱处罚 server=%s player=%s minutes=%s",
                    origin_server,
                    name,
                    minutes,
                )
            else:
                logger.info(
                    "[弧光EndStone消息中枢] 本服未使用监狱处罚，回退群禁言 server=%s player=%s reason=%s",
                    origin_server,
                    name,
                    jail_result or "-",
                )

        if (not jailed) and bound_qq and self.mute_qq:
            try:
                ok = await self.mute_qq(bound_qq, mute_seconds)
                logger.info(
                    "[弧光EndStone消息中枢] 弧光护卫禁言 qq=%s %ss ok=%s",
                    bound_qq,
                    mute_seconds,
                    ok,
                )
            except Exception as e:
                logger.error(
                    f"[弧光EndStone消息中枢] 弧光护卫禁言失败 qq={bound_qq}: {e}"
                )

        if jailed:
            warn_game = reply or f"竟敢辱骂至高无上的ENMO，关进监狱{minutes}分钟！"
        else:
            warn_game = reply or f"竟敢辱骂至高无上的ENMO，枪毙{minutes}分钟！"
        kill_name = name.replace('"', "").strip()
        if " " in kill_name:
            kill_arg = f'"{kill_name}"'
        else:
            kill_arg = kill_name
        warn_text = f"[弧光护卫] {kill_name}: {warn_game}"
        tellraw_json = json.dumps(
            {"rawtext": [{"text": warn_text}]},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        commands = [f"tellraw @a {tellraw_json}"]
        if kill_arg:
            commands.insert(0, f"kill {kill_arg}")
        await self._dispatch_silent_console_commands(origin_server, commands)

        notice = f"[{origin_server}]\n[弧光护卫] {name}: {warn_game}"
        if jailed and jail_result:
            notice += f"\n[jail] {jail_result}"
        try:
            await self.broadcast_qq(notice)
        except Exception as e:
            logger.warning(f"[弧光EndStone消息中枢] 弧光护卫群警告发送失败: {e}")
        logger.info(
            "[弧光EndStone消息中枢] 弧光护卫处罚完成 kind=%s server=%s player=%s qq=%s hits=%s",
            reason,
            origin_server,
            name,
            bound_qq or "-",
            hits,
        )
        return warn_game

    async def _dispatch_silent_console_commands(
        self, server_name: str, commands: list[str]
    ) -> None:
        """Run console commands on one MC server without echoing results to QQ.

        Args:
            server_name: Connected server name.
            commands: Console command lines without leading slash.
        """
        target_sid = self._server_numeric_id_by_name.get(server_name)
        for cmd in commands:
            line = (cmd or "").strip()
            if not line:
                continue
            await self.broadcast_to_all(
                {
                    "type": "command_forward",
                    "raw_message": f"/cmd {line}",
                    "command_line": f"/cmd {line}",
                    "target_server_id": target_sid,
                    "user_id": "0",
                    "display_name": "弧光护卫",
                    "group_id": "0",
                    "sender_role": "admin",
                    "is_config_admin": True,
                    "silent": True,
                }
            )

    def _format_qq_message(
        self,
        server_name: str,
        event: str,
        player: str,
        message: str,
        session_count=None,
        playtime_str: str = "",
    ) -> str:
        prefix = f"[{server_name}]\n"
        if event == "chat":
            return f"{prefix}💬 {player}: {message}"
        if event == "join":
            if session_count is None:
                return f"{prefix}🟢 {player} 加入游戏"
            if int(session_count) <= 1:
                return f"{prefix}🌟 {player} 首次进入服务器！"
            return f"{prefix}🟢 {player} 加入游戏 (第{session_count}次游戏)"
        if event == "quit":
            if playtime_str:
                return f"{prefix}🔴 {player} 离开游戏 (总游戏时长: {playtime_str})"
            return f"{prefix}🔴 {player} 离开游戏"
        if event == "death":
            # Prefer full death line from MC (often already includes 💀 / 玩家…).
            body = (message or "").strip()
            if not body:
                body = f"{player} 死了" if player else "有玩家死了"
            if body.startswith("💀"):
                return f"{prefix}{body}"
            return f"{prefix}💀 {body}"
        if event == "custom":
            return f"{prefix}{message}" if message else ""
        # server_start/stop: QQ announce uses hub WS connected/disconnected instead
        # (force-killed MC may never emit server_stop).
        if event in ("server_start", "server_stop"):
            return ""
        return f"{prefix}{message}" if message else ""

    def format_group_help_text(self) -> str:
        """QQ-facing help: commands use the /mc prefix required by this hub."""
        return "\n".join(
            [
                f"[{self.hub_server_name}] 群指令（均以 /mc 开头）：",
                "/mc help — 显示本帮助",
                "/mc servers — 查看已连接子服编号",
                "/mc admins — 查看管理员与超级管理员",
                "/mc addadmin @对方 — 超级管理员添加管理员（也可回复对方消息）",
                "/mc deladmin @对方 — 超级管理员移除管理员",
                "/mc list [编号] — 在线玩家",
                "/mc tps [编号] — TPS / MSPT",
                "/mc info [编号] — 服务器信息（CPU/内存/GPU，同物理机合并）",
                "/mc 绑定 <玩家名> — 绑定 QQ 到游戏角色",
                "/mc unbindqq <玩家名|QQ|openid> — 插件管理员解除绑定",
                "/mc 重启 — 重启投票（须在线）",
                "/mc cmd [编号] <控制台命令> — 群管理可用",
                "/mc who <玩家名|QQ> [编号] — 插件管理员",
                "/mc ban|unban|banlist|reload — 插件管理员",
                "/mc activate — 插件管理员在本会话激活 Minecraft AI 工具",
                "",
                "说明：中枢会剥掉 /mc 后再发给各 MC 子服；省略编号则所有子服执行。",
                "绑定 / unbindqq 由中枢本地处理，不依赖子服在线。",
                "AI 工具需先 /mc activate；会话 ID 可为非数字字符串。",
            ]
        )

    async def _broadcast_to_others(self, from_server: str, data: dict) -> None:
        msg = json.dumps(data, ensure_ascii=False)
        for name, ws in list(self.connected_servers.items()):
            if name == from_server:
                continue
            try:
                await ws.send(msg)
            except Exception:
                pass

    async def broadcast_to_all(self, data: dict) -> None:
        """Broadcast a Hub payload to every connected MC server.

        Args:
            data: JSON-serializable Hub message.
        """
        msg = json.dumps(data, ensure_ascii=False)
        for ws in list(self.connected_servers.values()):
            try:
                await ws.send(msg)
            except Exception:
                pass

    async def push_qq_chat(
        self, display_name: str, message: str, group_id: str | int
    ) -> None:
        """Push a QQ group chat message to all MC servers.

        Args:
            display_name: Sender display name in game.
            message: Cleaned chat text.
            group_id: Source QQ group id.
        """
        group_name = self.group_names.get(str(group_id), "")
        await self.broadcast_to_all(
            {
                "type": "qq_message",
                "display_name": display_name,
                "message": message,
                "group_name": group_name,
            }
        )

    async def push_command_forward(
        self,
        *,
        raw_message: str,
        user_id: str | int,
        display_name: str,
        group_id: str | int,
        session_key: str = "",
        sender_role: str = "member",
    ) -> None:
        """Forward a QQ group command to connected MC servers.

        Args:
            raw_message: Original command text.
            user_id: QQ user id.
            display_name: Bound player name or nickname.
            group_id: Source group / session id for replies.
            session_key: ``unified_msg_origin`` of the command source.
            sender_role: OneBot sender role.
        """
        reply_target = str(session_key or group_id or "").strip()
        # Normalize /mc ... -> /... before routing / forwarding to MC.
        internal = strip_mc_command_prefix(raw_message)
        forward_message = internal if internal is not None else raw_message.strip()

        if forward_message == "/servers":
            catalog = self.get_server_catalog()
            if not self.connected_servers:
                await self.reply_qq(
                    reply_target,
                    f"[{self.hub_server_name}]\n当前没有已连接的 Minecraft 子服。",
                )
                return
            lines = [f"[{self.hub_server_name}]\n当前子服列表:"]
            for item in catalog:
                online = "✅" if item["name"] in self.connected_servers else "·"
                lines.append(f"[{item['id']}] {item['name']} {online}")
            await self.reply_qq(reply_target, "\n".join(lines))
            return

        # Hub answers /mc help so QQ users see /mc-prefixed usage; MC keeps /help.
        if forward_message == "/help":
            await self.reply_qq(reply_target, self.format_group_help_text())
            return

        bind_head = forward_message.strip().split(None, 1)[0].lstrip("/")
        if bind_head == "绑定":
            await self._handle_qq_bind(
                reply_target, user_id, forward_message.strip().split()[1:]
            )
            return
        if bind_head.lower() == "unbindqq":
            await self._handle_qq_unbind(
                reply_target,
                user_id,
                display_name,
                forward_message.strip().split()[1:],
            )
            return

        eff_line, route_sid = parse_hub_command_routing(forward_message)
        if route_sid is not None:
            mc_ids = {
                c["id"]
                for c in self.get_server_catalog()
                if c["name"] in self.connected_servers
            }
            if mc_ids and route_sid not in mc_ids:
                await self.reply_qq(
                    reply_target,
                    f"[{self.hub_server_name}]\n"
                    f"❌ 无编号 {route_sid} 的服务器。\n"
                    f"💡 发送 /mc servers 查看当前子服列表",
                )
                return

        if not self.connected_servers:
            await self.reply_qq(
                reply_target,
                f"[{self.hub_server_name}]\n❌ 当前没有已连接的 Minecraft 子服。",
            )
            return

        info_head = eff_line.strip().split(None, 1)[0].lstrip("/").lower()
        if info_head == "info":
            text = await self.collect_and_format_server_info(route_sid=route_sid)
            await self.reply_qq(reply_target, f"[{self.hub_server_name}]\n{text}")
            return

        uid = str(user_id or "").strip()
        is_admin = uid in self.hub_admins or (
            bool(uid) and any(item.lower() == uid.lower() for item in self.hub_admins)
        )
        # Register aggregation so replies from all targeted servers merge into
        # one QQ message (requires QQ Sync >= 1.0.8 command_result).
        request_id = uuid.uuid4().hex
        if route_sid is not None:
            forward_targets = [
                str(item["name"])
                for item in self.get_server_catalog()
                if int(item["id"]) == route_sid
                and str(item["name"]) in self.connected_servers
            ]
        else:
            forward_targets = list(self.connected_servers.keys())
        if forward_targets:
            self._pending_command_replies[request_id] = {
                "targets": forward_targets,
                "responded": set(),
                "replies": {},
                "reply_target": reply_target,
                "timer": asyncio.create_task(
                    self._expire_command_replies(request_id, COMMAND_RESULT_TIMEOUT)
                ),
            }
        await self.broadcast_to_all(
            {
                "type": "command_forward",
                "raw_message": forward_message,
                "command_line": eff_line,
                "target_server_id": route_sid,
                "request_id": request_id,
                "user_id": user_id,
                "display_name": display_name,
                "group_id": group_id,
                "sender_role": sender_role,
                "is_config_admin": is_admin,
            }
        )

    async def _expire_command_replies(self, request_id: str, delay: float) -> None:
        """Flush aggregated command replies after the timeout, partial or not.

        Args:
            request_id: Pending aggregation to flush.
            delay: Seconds to wait for remaining servers.
        """
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        await self._flush_command_replies(request_id)

    async def _handle_command_result(self, server_name: str, data: dict) -> None:
        """Collect one server's command_result and merge when all have replied.

        Args:
            server_name: Server that executed the forwarded command.
            data: Payload with ``request_id`` and ``replies`` text list.
        """
        rid = str(data.get("request_id") or "")
        pending = self._pending_command_replies.get(rid)
        if pending is None or server_name not in pending["targets"]:
            return
        raw_replies = data.get("replies")
        if isinstance(raw_replies, list):
            cleaned = [
                strip_minecraft_format_codes(str(text))
                for text in raw_replies
                if str(text or "").strip()
            ]
            if cleaned:
                pending["replies"].setdefault(server_name, []).extend(cleaned)
        pending["responded"].add(server_name)
        if pending["responded"] >= set(pending["targets"]):
            await self._flush_command_replies(rid)

    async def _flush_command_replies(self, request_id: str) -> None:
        """Send all collected command replies as a single QQ message.

        Args:
            request_id: Pending aggregation to flush.
        """
        pending = self._pending_command_replies.pop(request_id, None)
        if pending is None:
            return
        timer = pending.get("timer")
        # Cancel only from another task; a self-cancel would raise
        # CancelledError at the next await and drop the merged reply.
        if timer and timer is not asyncio.current_task():
            timer.cancel()
        sections: list[str] = []
        for name in pending["targets"]:
            replies = pending["replies"].get(name)
            if not replies:
                continue
            body = "\n".join(replies)
            # QQ Sync replies already carry a "[server]" line; only label
            # sections whose text lacks it.
            if len(pending["targets"]) > 1 and not body.startswith(f"[{name}]"):
                body = f"[{name}]\n{body}"
            sections.append(body)
        if not sections:
            return
        await self.reply_qq(pending["reply_target"], "\n\n".join(sections))

    def resolve_info_server_names(
        self,
        *,
        route_sid: int | None = None,
        server_hint: str = "",
        aliases: dict[str, str] | None = None,
    ) -> tuple[list[str], str]:
        """Resolve QQ Sync game servers for ``/mc info`` / ``mc_server_info``.

        Args:
            route_sid: Optional catalog numeric id from ``/mc info <id>``.
            server_hint: Optional name, id, or alias string.
            aliases: Lowercase alias map from plugin config.

        Returns:
            ``(server_names, error)``. Exactly one side is non-empty.
        """
        connected = self.connected_server_names()
        if not connected:
            return [], "当前没有已连接的 Minecraft 子服。"

        if route_sid is not None:
            for item in self.get_server_catalog():
                if int(item["id"]) == int(route_sid):
                    name = str(item["name"])
                    if name in self.connected_servers:
                        return [name], ""
                    return [], f"服务器 [{name}] 的群服互通未连接"
            return [], f"无编号 {route_sid} 的服务器。"

        hint = str(server_hint or "").strip()
        if not hint:
            return connected, ""

        alias_map = aliases or {}
        mapped = alias_map.get(hint.lower())
        if mapped:
            hint = mapped

        try:
            sid = int(hint)
        except ValueError:
            sid = None
        if sid is not None:
            for item in self.get_server_catalog():
                if int(item["id"]) == sid:
                    name = str(item["name"])
                    if name in self.connected_servers:
                        return [name], ""
                    return [], f"服务器 [{name}] 的群服互通未连接"
            return [], f"无编号 {sid} 的服务器。"

        lowered = hint.lower()
        exact = [name for name in connected if name.lower() == lowered]
        if len(exact) == 1:
            return exact, ""
        partial = [name for name in connected if lowered in name.lower()]
        if len(partial) == 1:
            return partial, ""
        if len(partial) > 1:
            return [], "server 匹配到多台服，请改用更完整的名称或编号：\n" + "\n".join(
                partial
            )
        return [], f"未找到服务器「{hint}」。已连接：\n" + "\n".join(connected)

    async def collect_and_format_server_info(
        self,
        *,
        route_sid: int | None = None,
        server_names: list[str] | None = None,
        timeout: float = 12,
    ) -> str:
        """Fetch ``server_info`` from QQ Sync and merge by physical host.

        Args:
            route_sid: Optional catalog id filter.
            server_names: Explicit target names; overrides route_sid when set.
            timeout: Per-server core_rpc timeout seconds.

        Returns:
            Merged QQ-facing text.
        """
        if server_names is not None:
            targets = [str(n).strip() for n in server_names if str(n).strip()]
        else:
            targets, error = self.resolve_info_server_names(route_sid=route_sid)
            if error:
                return error

        if not targets:
            return "当前没有可查询的 Minecraft 子服。"

        async def _one(name: str) -> tuple[str, dict[str, Any] | None, str]:
            try:
                resp = await self.call_core_rpc(
                    name, "server_info", {}, timeout=timeout
                )
            except Exception as exc:
                return name, None, str(exc)
            if not isinstance(resp, dict) or not resp.get("ok"):
                return (
                    name,
                    None,
                    str(
                        (resp or {}).get("error")
                        if isinstance(resp, dict)
                        else "返回异常"
                    ),
                )
            result = resp.get("result")
            if not isinstance(result, dict) or not result.get("ok"):
                return (
                    name,
                    None,
                    str(
                        (result or {}).get("error")
                        if isinstance(result, dict)
                        else "无有效数据"
                    ),
                )
            return name, result, ""

        rows = await asyncio.gather(*[_one(name) for name in targets])
        payloads: list[dict[str, Any]] = []
        errors: list[str] = []
        for name, payload, err in rows:
            if payload is not None:
                payloads.append(payload)
            else:
                errors.append(f"[{name}] {err}")

        if not payloads:
            detail = "\n".join(errors) if errors else "（无返回）"
            return (
                "未能获取服务器信息（需 QQ Sync ≥ 1.0.6 的 server_info）。\n" + detail
            )

        text = format_merged_server_info(payloads)
        if errors:
            text += "\n\n部分子服失败：\n" + "\n".join(errors)
        return text

    async def _handle_qq_unbind(
        self,
        reply_target: str,
        user_id: str | int,
        display_name: str,
        args: list[str],
    ) -> None:
        """Clear a QQ/openid ↔ player binding on the hub (no MC round-trip).

        Args:
            reply_target: Session / group id for ``reply_qq``.
            user_id: Command sender platform id.
            display_name: Sender display label stored as ``unbind_by``.
            args: Tokens after ``unbindqq``; expect one player name / QQ / openid.
        """
        prefix = f"[{self.hub_server_name}]"
        uid = str(user_id or "").strip()
        is_admin = uid in self.hub_admins or (
            bool(uid) and any(item.lower() == uid.lower() for item in self.hub_admins)
        )
        if not is_admin:
            await self.reply_qq(
                reply_target,
                f"{prefix}\n❌ 仅插件管理员可使用 /mc unbindqq。",
            )
            return
        if len(args) != 1 or not str(args[0]).strip():
            await self.reply_qq(
                reply_target,
                f"{prefix}\n❌ 命令格式错误\n"
                "💡 正确用法：/mc unbindqq <游戏内玩家名|QQ|openid>\n"
                "💡 例如：/mc unbindqq DEVILENMO",
            )
            return

        search = str(args[0]).strip()
        store = self.binding_store
        admin_label = str(display_name or uid or "admin").strip() or "admin"

        canonical = ""
        data: dict[str, Any] = {}
        # Prefer player-name match; fall back to current/history QQ/openid.
        canonical, data = store.find_player_entry(search)
        if not data:
            by_qq = str(store.get_qq_player(search) or "").strip()
            if by_qq:
                canonical, data = store.find_player_entry(by_qq)
        if not data:
            # Historical bind lookup (same as QQ Sync who/unban path).
            for name, row in store.binding_data.items():
                if not isinstance(row, dict):
                    continue
                for key in ("qq", "previous_qq", "original_qq"):
                    if str(row.get(key) or "").strip() == search:
                        canonical, data = str(name), row
                        break
                if data:
                    break

        if not data:
            await self.reply_qq(
                reply_target,
                f"{prefix}\n❌ 未找到「{search}」对应的绑定记录",
            )
            return

        bound = str(data.get("qq") or "").strip()
        if not bound:
            await self.reply_qq(
                reply_target,
                f"{prefix}\n❌ 玩家「{canonical}」当前未绑定平台用户",
            )
            return

        if not store.unbind_player_qq(canonical, admin_name=admin_label):
            await self.reply_qq(
                reply_target,
                f"{prefix}\n❌ 解绑失败，请稍后重试",
            )
            return

        await self.reply_qq(
            reply_target,
            f"{prefix}\n✅ 已解除「{canonical}」与平台用户 {bound} 的绑定"
            f"（操作者 {admin_label}）",
        )

    async def _handle_qq_bind(
        self,
        reply_target: str,
        user_id: str | int,
        args: list[str],
    ) -> None:
        """Bind the sender QQ to a game character without forwarding to MC.

        Group /绑定 used to be broadcast to every sub-server. Each server then
        blocked the Hub WebSocket loop on synchronous data_rpc, ping timed out,
        and all connections dropped.
        """
        prefix = f"[{self.hub_server_name}]"
        if len(args) != 1 or not str(args[0]).strip():
            await self.reply_qq(
                reply_target,
                f"{prefix}\n❌ 命令格式错误\n💡 正确用法：/mc 绑定 <游戏内玩家名>\n"
                "💡 例如：/mc 绑定 DEVILENMO",
            )
            return

        target_player_name = str(args[0]).strip()
        qq_str = str(user_id).strip()
        store = self.binding_store

        if store.is_player_banned(target_player_name):
            await self.reply_qq(
                reply_target,
                f"{prefix}\n❌ 玩家 {target_player_name} 已被封禁，无法绑定QQ",
            )
            return

        existing_for_qq = store.get_qq_player(qq_str)
        if existing_for_qq and existing_for_qq.lower() != target_player_name.lower():
            await self.reply_qq(
                reply_target,
                f"{prefix}\n❌ 您的QQ已绑定游戏角色「{existing_for_qq}」\n"
                "💡 如需改绑请先联系管理员解绑，避免恶意占用多个角色",
            )
            return

        canonical, stored = store.find_player_entry(target_player_name)
        player_xuid = str((stored or {}).get("xuid") or "").strip()
        if not player_xuid:
            resolved_name, resolved_xuid = await self._resolve_player_via_core(
                target_player_name
            )
            if not resolved_xuid:
                await self.reply_qq(
                    reply_target,
                    f"{prefix}\n❌ 服务器记录中找不到名为「{target_player_name}」的玩家\n"
                    "💡 请先在游戏中至少登录一次，再于群内绑定",
                )
                return
            canonical = store.ensure_player_record(resolved_name, resolved_xuid)
            player_xuid = resolved_xuid
        elif not canonical:
            canonical = store.ensure_player_record(target_player_name, player_xuid)

        target_player_name = canonical or target_player_name

        if store.is_player_bound(target_player_name, player_xuid):
            bound_qq = str(store.get_player_qq(target_player_name) or "")
            if bound_qq == qq_str:
                await self.reply_qq(
                    reply_target,
                    f"{prefix}\n✅ 您的QQ已与游戏角色「{target_player_name}」绑定，无需重复操作",
                )
            else:
                await self.reply_qq(
                    reply_target,
                    f"{prefix}\n❌ 游戏角色「{target_player_name}」已绑定其他QQ\n"
                    f"💡 该角色当前绑定QQ: {bound_qq}\n"
                    "💡 若需更换绑定请联系管理员，请勿抢绑他人账号",
                )
            return

        if not store.bind_player_qq(target_player_name, player_xuid, qq_str):
            await self.reply_qq(
                reply_target,
                f"{prefix}\n❌ 绑定失败，请稍后重试或联系管理员",
            )
            return

        await self.reply_qq(
            reply_target,
            f"{prefix}\n✅ 玩家 {target_player_name} 已成功绑定QQ！",
        )
        if self.sync_group_card and self.set_group_card:
            try:
                await self.set_group_card(int(qq_str), target_player_name)
            except Exception as error:
                logger.warning(f"[弧光EndStone消息中枢] 绑定后改群名片失败: {error}")
