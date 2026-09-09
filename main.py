"""AstrBot 弧光 EndStone 消息中枢插件。"""

from __future__ import annotations

import asyncio
import ipaddress
import re

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.event.filter import CustomFilter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType

from .activation_store import ActivationStore
from .admin_store import AdminStore
from .binding_store import BindingStore
from .hub_server import (
    ArcHubServer,
    extract_event_raw_text,
    is_known_arc_command,
    is_mc_activate_command,
    normalize_mc_arc_raw_message,
    strip_mc_command_prefix,
)
from .mc_ai_event import McAiMessageEvent

PLUGIN_NAME = "astrbot_plugin_endstone_arc"
ARC_GUARD_PLUGIN = "astrbot_plugin_arc_guard"
_HUB_DISPLAY = "弧光EndStone消息中枢"
_cq_pattern = re.compile(r"\[CQ:(\w+)([^\]]*)\]")
_MC_AI_IDENTITY_HINT = (
    "当前是 Minecraft 游戏内对话，没有 QQ 群号。"
    "发送者 ID：该玩家若已绑定 QQ 则为 QQ 号（可与 QQ 侧记忆对上同一个人），"
    "未绑定则使用 XUID。昵称只是当前游戏名。"
    "执行游戏指令时，在玩家发来这条消息的那台 Minecraft 服务器上执行。"
    "玩家问在线人数、谁在线、TPS、服务器信息或要你执行游戏指令时，"
    "必须调用对应工具查询或执行，禁止凭空编造数字或名单。"
    "问路标、出生点、公共传送点、公共领地时优先看系统提示里的地标清单；"
    "需要最新列表时调用 mc_landmarks（只读）。"
    "银行：查自己余额用 mc_economy（sub_action=query）；"
    "已绑定用户可用 transfer 从自己账户给别人发红包（amount 每人金额；targets 收款人或 to_online=true 发给在线玩家）。"
    "查他人或 change 加减钱仅管理员。"
    "领地查询用 mc_land，传送到 Home/Warp/坐标用 mc_arc_tp；"
    "禁止用 mc_run_command 代替这些弧光核心工具。"
    "要把玩家关进监狱、释放或查看在押名单时，必须调用 mc_jail_player / "
    "mc_release_player / mc_list_prisoners，不要用 mc_run_command 去跑 /jail。"
    "管理员帮别人绑定或解绑 QQ 与游戏角色时，必须调用 mc_qq_binding"
    "（sub_action=bind/unbind/query），不要编造绑定状态；force=true 可强制改绑。"
    "qq 参数是平台用户 ID：可为传统数字 QQ，或 QQ 官方机器人的 member_openid 字符串；"
    "用户已 @对方时 qq 应留空让工具自动解析，禁止把群名片当 qq。"
    "查询玩家位置、近期行为、打了谁、被谁打、坐标附近发生过什么时，必须调用 "
    "mc_skyeye_player / mc_skyeye_combat / mc_skyeye_location，禁止编造。"
    "天眼不要求玩家在线。不知道在哪台服时 server 必须留空，工具会搜索全部已连接服务器。"
    "minutes 必须按用户说的查询时长自己换算成分钟数再传入，例如一天=1440、一小时=60；用户没说时长才用 30。"
    "查询模拟美股玩家盈亏排行时必须调用 mc_stock_leaderboard；"
    "查某只股票现价/走势必须调用 mc_stock_quote（AAPL/TSLA/BTC-USD 等），禁止编造行情。"
    "需要玩家连接 IP 时必须调用 mc_player_ip，从返回的 ip= 字段取出原始 IP；"
    "要根据 IP 查归属地/基站级精确定位时必须再调用 mc_ip_locate（传入该 IP），禁止编造位置；"
    "不填 player_name 则查当前对话玩家，助手只能查自己。"
    "优先调用 mc_run_command 执行其它指令；只有工具不可用时，才在可见回复里使用 "
    "[execution_command:实际游戏指令] 标记。"
    "effect 只能用于药水效果，例如 effect Steve slowness 20 0 true。"
    "劈闪电必须用 execute at 玩家名 run summon lightning_bolt ~ ~ ~ ，"
    "禁止 effect 玩家名 summon（summon 不是药水效果）。"
)
_QQ_MC_TOOL_HINT = (
    "当前对话已通过 /mc activate 接入弧光 Minecraft 中枢。"
    "查询在线、TPS、服务器信息或执行游戏指令时必须调用对应工具，禁止编造。"
    "权限由工具根据本条消息发起者 ID 自动校验（插件管理员/超级管理员、或群主/群管、"
    "或已绑定自救）；禁止先调用群身份/群成员查询工具判断是否管理员，也禁止向用户索要权限证明。"
    "下面会写明「当前消息发起者」的 Minecraft 工具权限档位；有管理员档位时直接调用管理类工具即可。"
    "无权限时工具会返回错误，不要自行推断「不是群管就不能用」。"
    "问路标/出生点/公共传送点/公共领地用 mc_landmarks（只读，已激活即可）。"
    "已绑定游戏角色的用户可调用 mc_economy（sub_action=query）查询本人余额；"
    "也可 transfer 从自己账户发红包/转账（amount 每人；targets 或 to_online=true）；"
    "查询他人或 change 加减钱需管理员档位（工具自动拦）。"
    "领地用 mc_land，传送 Home/Warp/坐标用 mc_arc_tp；"
    "不要用 mc_run_command 代替这些弧光核心工具。"
    "关押玩家用 mc_jail_player，释放用 mc_release_player，查看在押用 mc_list_prisoners。"
    "查玩家位置/近期行为用 mc_skyeye_player，查打架用 mc_skyeye_combat，查坐标附近用 mc_skyeye_location。"
    "天眼不要求玩家在线。不知道人在哪台服时，server 必须留空（会搜全部已连接服务器），不要猜服名。"
    "调用天眼时必须自己把用户说的时长换算成分钟写入 minutes，例如一天=1440、一小时=60。"
    "查询模拟美股玩家盈亏排行用 mc_stock_leaderboard；查股价/走势用 mc_stock_quote。"
    "需要玩家连接 IP 时调用 mc_player_ip，把返回的 ip= 原样取出；"
    "查 IP 归属地/基站级精确定位必须再调用 mc_ip_locate，禁止编造位置。"
    "改世界/查在线等其它工具在多开服时仍须填写 server。"
    "有多台 Minecraft 服务器时，除天眼外先调用 mc_list_servers，再填写 server"
    "（名称、编号或别名），不要猜测。"
    "mc_landmarks / mc_stock_leaderboard / mc_stock_quote / mc_player_ip / mc_ip_locate 只读、已激活即可。"
    "已绑定游戏角色的 QQ 用户可在求助时使用 tp / effect / spawnpoint 等自救指令，且仅限本人绑定角色；"
    "未绑定用户无权执行改世界类指令，需先 /mc 绑定 <玩家名>。"
    "帮别人绑定/解绑 QQ 与游戏角色时，必须调用 mc_qq_binding"
    "（sub_action=bind/unbind/query），不要让用户自己猜指令；force=true 可强制改绑。"
    "qq 可为数字 QQ 或 member_openid；消息里已 @对方时 qq 留空即可，不要传群名片。"
    "effect 只能用于药水效果。劈闪电必须用 execute at 玩家名 run summon lightning_bolt ~ ~ ~。"
)

_SKYEYE_FANOUT_ACTIONS = frozenset(
    {"skyeye_player", "skyeye_combat", "skyeye_location"}
)
_SKYEYE_EMPTY_MARKERS = (
    "天眼里也没有记录",
    "没有记录",
    "无记录",
    "暂无记录",
    "没有找到",
    "无匹配",
    "本服未安装",
    "版本过旧",
    "玩家名为空",
    "坐标无效",
)


class McArcCommandFilter(CustomFilter):
    """Intercept ``/mc ...`` before LLM wake, regardless of message type."""

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        if event.get_extra("mc_ai_event"):
            return False
        raw = extract_event_raw_text(event)
        return normalize_mc_arc_raw_message(raw) is not None


class EndstoneArcMessageCenter(Star):
    """弧光 EndStone 消息中枢：WebSocket Hub + QQ 桥接。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self._hub: ArcHubServer | None = None
        self._binding_store: BindingStore | None = None
        self._activation_store: ActivationStore | None = None
        self._admin_store: AdminStore | None = None
        self._group_umo: dict[str, str] = {}
        self._platform_id: str = self._normalize_platform_id(
            self.config.get("platform_id")
        )
        self._start_task: asyncio.Task | None = None

    @staticmethod
    def _normalize_platform_id(value: object) -> str:
        """Treat empty / ``default`` as unset so the plugin auto-learns.

        Args:
            value: Raw config value.

        Returns:
            Concrete platform id, or empty string for auto-learn.
        """
        text = str(value or "").strip()
        if not text or text.lower() == "default":
            return ""
        return text

    async def initialize(self):
        """Start Hub after plugin load."""
        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self._binding_store = BindingStore(data_dir)
        self._activation_store = ActivationStore(data_dir)
        self._admin_store = AdminStore(data_dir, seed_admins=self._config_admin_ids())
        self._admin_store.seed_super_admins(self._config_super_admin_ids())
        self._sync_admins_to_config()
        self._hydrate_umos_from_config()
        self._start_task = asyncio.create_task(self._start_hub())

    async def terminate(self):
        """Stop Hub on unload/reload."""
        if self._start_task and not self._start_task.done():
            self._start_task.cancel()
            try:
                await self._start_task
            except asyncio.CancelledError:
                pass
        if self._hub:
            await self._hub.stop()
            self._hub = None

    def _hydrate_umos_from_config(self) -> None:
        platform_id = self._platform_id.strip()
        if not platform_id:
            return
        for gid in self._target_group_ids():
            umo = str(MessageSession(platform_id, MessageType.GROUP_MESSAGE, str(gid)))
            self._group_umo.setdefault(str(gid), umo)

    def _target_group_ids(self) -> set[str]:
        raw = self.config.get("target_groups") or []
        ids: set[str] = set()
        for item in raw:
            text = str(item).strip()
            if text:
                ids.add(text)
        return ids

    def _is_group_allowed(self, group_id: str | None) -> bool:
        """Whether ``group_id`` is allowed by the configured whitelist.

        Empty ``target_groups`` means no restriction (legacy behavior).

        Args:
            group_id: Platform group id or QQ official group openid.

        Returns:
            True when interaction / forward / broadcast is allowed.
        """
        gid = str(group_id or "").strip()
        if not gid:
            return False
        targets = self._target_group_ids()
        if not targets:
            return True
        return gid in targets

    def _session_id_from_umo(self, umo: str) -> str:
        """Extract session/group id from a unified message origin.

        Args:
            umo: ``platform:MessageType:session_id`` string.

        Returns:
            Trailing session id segment, or the original string when malformed.
        """
        text = str(umo or "").strip()
        if not text:
            return ""
        if ":" not in text:
            return text
        return text.rsplit(":", 1)[-1].strip()

    def _group_names(self) -> dict[str, str]:
        raw = self.config.get("group_names") or {}
        if not isinstance(raw, dict):
            return {}
        return {str(k): str(v) for k, v in raw.items() if str(v).strip()}

    def _config_admin_ids(self) -> set[str]:
        raw = self.config.get("admins") or []
        return {str(a).strip() for a in raw if str(a).strip()}

    def _config_super_admin_ids(self) -> set[str]:
        raw = self.config.get("super_admins") or []
        return {str(a).strip() for a in raw if str(a).strip()}

    def _admin_ids(self) -> set[str]:
        store = self._admin_store
        if store is not None:
            return set(store.list_admins())
        return self._config_admin_ids()

    def _super_admin_ids(self) -> set[str]:
        store = self._admin_store
        if store is not None:
            return set(store.list_super_admins())
        supers = self._config_super_admin_ids()
        return supers or self._config_admin_ids()

    @staticmethod
    def _sender_id(event: AstrMessageEvent) -> str:
        """Return the platform sender id, including non-string user_id values.

        Args:
            event: AstrBot message event.

        Returns:
            Stripped sender id, or an empty string when unavailable.
        """
        uid = event.get_sender_id()
        if uid is not None and str(uid).strip():
            return str(uid).strip()
        sender = getattr(getattr(event, "message_obj", None), "sender", None)
        raw = getattr(sender, "user_id", None) if sender else None
        if raw is None or not str(raw).strip():
            return ""
        return str(raw).strip()

    @staticmethod
    def _id_in_set(user_id: object, pool: set[str]) -> bool:
        """Match a platform user id against a stored admin set.

        Args:
            user_id: Candidate sender / mention id.
            pool: Stored admin ids.

        Returns:
            True when the id is in the set, ignoring surrounding space and hex case.
        """
        uid = str(user_id or "").strip()
        if not uid:
            return False
        if uid in pool:
            return True
        lowered = uid.lower()
        return any(item.lower() == lowered for item in pool)

    def _is_hub_admin(self, event: AstrMessageEvent) -> bool:
        return self._id_in_set(self._sender_id(event), self._admin_ids())

    def _is_super_admin(self, event: AstrMessageEvent) -> bool:
        return self._id_in_set(self._sender_id(event), self._super_admin_ids())

    def _refresh_hub_admins(self) -> None:
        if self._hub is not None:
            self._hub.hub_admins = set(self._admin_ids())

    def _sync_admins_to_config(self) -> None:
        """Write the live admin store back to plugin config for the WebUI.

        Returns:
            None.
        """
        store = self._admin_store
        config = self.config
        if store is None or not hasattr(config, "save_config"):
            return
        supers = store.list_super_admins()
        super_set = set(supers)
        admins = [item for item in store.list_admins() if item not in super_set]
        current_admins = [
            str(item).strip()
            for item in (config.get("admins") or [])
            if str(item).strip()
        ]
        current_supers = [
            str(item).strip()
            for item in (config.get("super_admins") or [])
            if str(item).strip()
        ]
        if current_admins == admins and current_supers == supers:
            return
        config["admins"] = admins
        config["super_admins"] = supers
        try:
            config.save_config()
        except Exception as error:
            logger.warning(f"[{_HUB_DISPLAY}] 同步管理员到插件配置失败: {error}")

    async def _reply_to_event(self, event: AstrMessageEvent, text: str) -> None:
        try:
            await event.send(MessageEventResult().message(text))
            return
        except Exception as error:
            logger.debug(
                f"[{_HUB_DISPLAY}] event.send 失败，改用 context.send_message: {error}"
            )
        umo = str(event.unified_msg_origin or "").strip()
        if not umo:
            logger.warning(f"[{_HUB_DISPLAY}] 无法回复：缺少 unified_msg_origin")
            return
        try:
            await self.context.send_message(umo, MessageEventResult().message(text))
        except Exception as error:
            logger.warning(f"[{_HUB_DISPLAY}] 回复消息失败: {error}")

    def _is_tool_session_activated(self, event: AstrMessageEvent) -> bool:
        store = self._activation_store
        if store is None:
            return False
        gid = str(event.get_group_id() or event.get_session_id() or "").strip()
        if event.get_group_id() and not self._is_group_allowed(gid):
            return False
        umo = str(event.unified_msg_origin or "").strip()
        if umo and store.is_activated(umo):
            return True
        if umo and ":" in umo:
            umo_sid = umo.rsplit(":", 1)[-1].strip()
            if umo_sid and store.is_activated_by_session_id(umo_sid):
                return True
        for sid in (
            str(event.get_session_id() or "").strip(),
            str(event.get_group_id() or "").strip(),
        ):
            if sid and store.is_activated_by_session_id(sid):
                return True
        return False

    async def _handle_mc_activate(self, event: AstrMessageEvent) -> None:
        if not self._is_hub_admin(event):
            await self._reply_to_event(
                event,
                f"[{self.config.get('hub_server_name') or _HUB_DISPLAY}]\n"
                "❌ 仅插件管理员可使用 /mc activate。\n"
                "请先由超级管理员把你的账号加入管理员列表。",
            )
            return

        session_key = str(event.unified_msg_origin or "").strip()
        session_id = str(event.get_session_id() or event.get_group_id() or "").strip()
        if not session_key:
            await self._reply_to_event(
                event,
                f"[{self.config.get('hub_server_name') or _HUB_DISPLAY}]\n"
                "❌ 无法识别当前会话，请稍后再试。",
            )
            return
        if not self._is_group_allowed(session_id):
            await self._reply_to_event(
                event,
                f"[{self.config.get('hub_server_name') or _HUB_DISPLAY}]\n"
                "❌ 当前群不在白名单 target_groups 中，无法激活。\n"
                f"当前群 ID: {session_id or '(空)'}",
            )
            return

        self._remember_umo(event)
        label = session_id or session_key
        store = self._activation_store
        if store is None:
            await self._reply_to_event(event, f"[{_HUB_DISPLAY}] 激活存储未就绪。")
            return
        added = store.activate(
            session_key,
            session_id=session_id,
            label=label,
            by_admin=self._sender_id(event),
        )
        logger.info(
            f"[{_HUB_DISPLAY}] /mc activate session_key={session_key} "
            f"session_id={session_id} added={added}"
        )
        hub_name = self.config.get("hub_server_name") or _HUB_DISPLAY
        if added:
            text = (
                f"[{hub_name}]\n"
                "✅ 已在本会话激活 Minecraft AI 工具。\n"
                f"会话 ID: {label}\n"
                "此后本对话中的 AI 可调用 mc_list_players、mc_run_command 等工具。"
            )
        else:
            text = f"[{hub_name}]\nℹ️ 本会话已处于激活状态。\n会话 ID: {label}"
        await self._reply_to_event(event, text)

    @staticmethod
    def _parse_mc_local_command(mc_raw: str) -> tuple[str, list[str]]:
        normalized = strip_mc_command_prefix(mc_raw) or mc_raw
        parts = str(normalized or "").strip().split()
        if not parts:
            return "", []
        head = parts[0].lstrip("/").lower()
        return head, parts[1:]

    def _ignored_mention_ids(self, event: AstrMessageEvent) -> set[str]:
        """Ids that must not be treated as addadmin/deladmin targets.

        Args:
            event: Current message event.

        Returns:
            Bot self id plus reserved mention tokens.
        """
        ignored = {"all", "everyone", "qq_official", "unknown_selfid"}
        self_id = str(event.get_self_id() or "").strip()
        if self_id:
            ignored.add(self_id)
            ignored.add(self_id.lower())
        return ignored

    def _extract_mentioned_targets(
        self, event: AstrMessageEvent, raw_text: str
    ) -> list[tuple[str, str]]:
        """Extract @ / reply targets as ``(raw_id, hint_name)``.

        NapCat ``at.qq`` may be a tiny id, not the sender QQ used by permission
        checks. The hint name (group card) is kept so we can resolve the real id.

        Args:
            event: AstrBot message event.
            raw_text: Original message text, used for CQ codes.

        Returns:
            Unique targets, @mentions first, then replied-to sender.
        """
        targets: list[tuple[str, str]] = []
        ignored = self._ignored_mention_ids(event)
        reply_targets: list[tuple[str, str]] = []

        def _add(
            value: object,
            hint: object = "",
            bucket: list[tuple[str, str]] | None = None,
        ) -> None:
            text = str(value or "").strip()
            if not text or text in ignored or text.lower() in ignored:
                return
            label = str(hint or "").strip()
            dest = targets if bucket is None else bucket
            for index, (existing, old_hint) in enumerate(dest):
                if existing != text:
                    continue
                if label and len(label) > len(old_hint):
                    dest[index] = (text, label)
                return
            dest.append((text, label))

        try:
            raw_obj = getattr(getattr(event, "message_obj", None), "raw_message", None)
            raw_data = (
                getattr(raw_obj, "raw_data", None) if raw_obj is not None else None
            )
            if isinstance(raw_obj, dict):
                segs = raw_obj.get("message")
                if isinstance(segs, list):
                    for seg in segs:
                        if (
                            not isinstance(seg, dict)
                            or str(seg.get("type") or "") != "at"
                        ):
                            continue
                        data = (
                            seg.get("data") if isinstance(seg.get("data"), dict) else {}
                        )
                        _add(
                            data.get("uin") or data.get("user_id") or data.get("qq"),
                            data.get("name") or data.get("display") or "",
                        )
                mentions = raw_obj.get("mentions")
                if raw_data is None:
                    raw_data = raw_obj
            else:
                mentions = (
                    getattr(raw_obj, "mentions", None) if raw_obj is not None else None
                )
            mention_lists = [mentions]
            if isinstance(raw_data, dict):
                mention_lists.append(raw_data.get("mentions"))
            for mention_list in mention_lists:
                for mention in mention_list or []:
                    if isinstance(mention, dict):
                        if mention.get("is_you") or mention.get("bot"):
                            continue
                        _add(
                            mention.get("member_openid")
                            or mention.get("user_openid")
                            or mention.get("uin")
                            or mention.get("id"),
                            mention.get("username") or mention.get("name") or "",
                        )
                        continue
                    if (
                        getattr(mention, "is_you", False) is True
                        or getattr(mention, "bot", False) is True
                    ):
                        continue
                    _add(
                        getattr(mention, "member_openid", None)
                        or getattr(mention, "user_openid", None)
                        or getattr(mention, "id", None),
                        getattr(mention, "username", None)
                        or getattr(mention, "name", None)
                        or "",
                    )
        except Exception:
            pass
        try:
            for comp in event.get_messages():
                type_name = type(comp).__name__.lower()
                if type_name == "reply":
                    _add(
                        getattr(comp, "sender_id", None) or getattr(comp, "qq", None),
                        getattr(comp, "sender_nickname", None) or "",
                        reply_targets,
                    )
                    continue
                if "at" not in type_name:
                    continue
                _add(
                    getattr(comp, "qq", None) or getattr(comp, "target", None),
                    getattr(comp, "name", None) or "",
                )
        except Exception:
            pass
        for match in re.finditer(r"\[CQ:at,([^\]]*)\]", raw_text or ""):
            params = match.group(1)
            qq_match = re.search(r"(?:uin|user_id|qq)=([^,\]]+)", params)
            name_match = re.search(r"name=([^,\]]+)", params)
            if qq_match:
                _add(qq_match.group(1), name_match.group(1) if name_match else "")
        for source in (
            raw_text,
            getattr(event, "message_str", "") or "",
        ):
            for match in re.finditer(r"<@!?([A-Za-z0-9_-]+)>", str(source or "")):
                _add(match.group(1))
        if not targets:
            for item in reply_targets:
                _add(item[0], item[1])
        return targets

    def _nickname_from_at_hint(self, hint: str, raw_id: str) -> str:
        """Return a usable group card from an At label.

        Args:
            hint: At.name or CQ name, e.g. ``QQ用户123(快跑你Buldapider来了)``.
            raw_id: Mention id reported by the adapter.

        Returns:
            Bare group card / nickname, or empty string.
        """
        text = str(hint or "").strip()
        if not text:
            return ""
        match = re.search(
            rf"QQ用户{re.escape(str(raw_id))}\s*[（(](.+?)[）)]\s*$",
            text,
        )
        if match:
            return match.group(1).strip()
        match = re.search(r"QQ用户\S*\s*[（(](.+?)[）)]\s*$", text)
        if match:
            return match.group(1).strip()
        if text.startswith("QQ用户") or text in {"all", "全体成员"}:
            return ""
        return text

    async def _resolve_admin_target(
        self, event: AstrMessageEvent, raw_id: str, hint: str
    ) -> str:
        """Map an @ mention to the id used by ``get_sender_id()``.

        Args:
            event: Current group event.
            raw_id: Id taken from At/CQ (may be NapCat tiny id).
            hint: Display name / group card from the mention.

        Returns:
            Resolved platform user id. Falls back to ``raw_id`` when lookup fails.
        """
        uid = str(raw_id or "").strip()
        nick = self._nickname_from_at_hint(hint, uid)
        bot = getattr(event, "bot", None)
        gid = str(event.get_group_id() or "").strip()
        if bot is None or not gid or not gid.isdigit():
            return uid

        routing = {}
        self_id = getattr(getattr(event, "message_obj", None), "self_id", None)
        if self_id:
            routing["self_id"] = self_id

        real_from_info = ""
        if uid.isdigit():
            try:
                info = await bot.call_action(
                    "get_group_member_info",
                    group_id=int(gid),
                    user_id=int(uid),
                    no_cache=True,
                    **routing,
                )
            except Exception as error:
                logger.debug(
                    f"[{_HUB_DISPLAY}] get_group_member_info({uid}) 失败: {error}"
                )
                info = None
            if isinstance(info, dict):
                real_from_info = str(
                    info.get("user_id") or info.get("uin") or ""
                ).strip()
                if not nick:
                    nick = str(info.get("card") or info.get("nickname") or "").strip()

        real_from_name = ""
        if nick:
            try:
                members = await bot.call_action(
                    "get_group_member_list",
                    group_id=int(gid),
                    **routing,
                )
            except Exception as error:
                logger.debug(f"[{_HUB_DISPLAY}] get_group_member_list 失败: {error}")
                members = None
            if isinstance(members, list):
                matches: list[str] = []
                for member in members:
                    if not isinstance(member, dict):
                        continue
                    names = {
                        str(member.get("card") or "").strip(),
                        str(member.get("nickname") or "").strip(),
                    }
                    if nick not in names:
                        continue
                    member_id = str(
                        member.get("user_id") or member.get("uin") or ""
                    ).strip()
                    if member_id and member_id not in matches:
                        matches.append(member_id)
                if len(matches) == 1:
                    real_from_name = matches[0]
                elif len(matches) > 1:
                    logger.warning(
                        f"[{_HUB_DISPLAY}] 群名片 {nick} 匹配到多个成员: {matches}"
                    )

        resolved = real_from_name or real_from_info or uid
        if resolved != uid:
            logger.info(
                f"[{_HUB_DISPLAY}] 解析 @ 目标 {uid} ({nick or hint}) -> {resolved}"
            )
        return resolved

    def _log_admin_mention_dump(
        self, event: AstrMessageEvent, mc_raw: str, raw_text: str
    ) -> None:
        """Log raw @mention payload so addadmin ID mismatches can be diagnosed.

        Args:
            event: Current command event.
            mc_raw: Normalized ``/mc ...`` command text.
            raw_text: Text from ``extract_event_raw_text``.
        """
        lines: list[str] = [
            f"cmd={mc_raw!r}",
            f"platform_name={event.get_platform_name()!r} platform_id={event.get_platform_id()!r}",
            f"sender_id={self._sender_id(event)!r} self_id={event.get_self_id()!r} group_id={event.get_group_id()!r}",
            f"message_str={getattr(event, 'message_str', '')!r}",
            f"raw_text={raw_text!r}",
            f"ignored={sorted(self._ignored_mention_ids(event))}",
        ]
        try:
            comps: list[str] = []
            for comp in event.get_messages():
                comps.append(
                    "type={type} qq={qq!r} target={target!r} name={name!r} "
                    "sender_id={sender_id!r} text={text!r}".format(
                        type=type(comp).__name__,
                        qq=getattr(comp, "qq", None),
                        target=getattr(comp, "target", None),
                        name=getattr(comp, "name", None),
                        sender_id=getattr(comp, "sender_id", None),
                        text=getattr(comp, "text", None),
                    )
                )
            lines.append("components=" + (" | ".join(comps) if comps else "(empty)"))
        except Exception as error:
            lines.append(f"components_error={error!r}")

        raw_obj = getattr(getattr(event, "message_obj", None), "raw_message", None)
        lines.append(f"raw_type={type(raw_obj).__name__}")
        if isinstance(raw_obj, dict):
            lines.append(f"raw_message_field={raw_obj.get('raw_message')!r}")
            segs = raw_obj.get("message")
            if isinstance(segs, list):
                dumped: list[str] = []
                for seg in segs:
                    if not isinstance(seg, dict):
                        dumped.append(repr(seg))
                        continue
                    dumped.append(f"type={seg.get('type')!r} data={seg.get('data')!r}")
                lines.append(
                    "raw_segments=" + (" | ".join(dumped) if dumped else "(empty)")
                )
            mentions = raw_obj.get("mentions")
            if mentions:
                lines.append(f"raw_mentions={mentions!r}")
        elif raw_obj is not None:
            try:
                lines.append(f"raw_repr={raw_obj!r}"[:2000])
            except Exception as error:
                lines.append(f"raw_repr_error={error!r}")
            mentions = getattr(raw_obj, "mentions", None)
            if mentions:
                dumped_mentions: list[str] = []
                for mention in mentions:
                    if isinstance(mention, dict):
                        dumped_mentions.append(repr(mention))
                        continue
                    dumped_mentions.append(
                        "id={id!r} member_openid={member_openid!r} user_openid={user_openid!r} "
                        "username={username!r} is_you={is_you!r} bot={bot!r}".format(
                            id=getattr(mention, "id", None),
                            member_openid=getattr(mention, "member_openid", None),
                            user_openid=getattr(mention, "user_openid", None),
                            username=getattr(mention, "username", None)
                            or getattr(mention, "name", None),
                            is_you=getattr(mention, "is_you", None),
                            bot=getattr(mention, "bot", None),
                        )
                    )
                lines.append("raw_mentions=" + " | ".join(dumped_mentions))

        logger.info("[%s] addadmin dump\n%s", _HUB_DISPLAY, "\n".join(lines))

    async def _handle_mc_admin_command(
        self, event: AstrMessageEvent, mc_raw: str
    ) -> bool:
        head, args = self._parse_mc_local_command(mc_raw)
        if head not in {"addadmin", "deladmin", "admins"}:
            return False

        hub_name = self.config.get("hub_server_name") or _HUB_DISPLAY
        if head == "admins":
            admins = sorted(self._admin_ids())
            supers = sorted(self._super_admin_ids())
            lines = [
                f"[{hub_name}]",
                f"超级管理员: {len(supers)} 人",
                *[f"• {x}" for x in supers],
            ]
            lines.append(f"管理员: {len(admins)} 人")
            lines.extend(f"• {x}" for x in admins if x not in supers)
            await self._reply_to_event(event, "\n".join(lines))
            return True

        if not self._is_super_admin(event):
            await self._reply_to_event(
                event,
                f"[{hub_name}]\n❌ 仅超级管理员可任免管理员。",
            )
            return True

        store = self._admin_store
        if store is None:
            await self._reply_to_event(event, f"[{hub_name}]\n❌ 管理员存储未就绪。")
            return True

        raw_text = extract_event_raw_text(event)
        self._log_admin_mention_dump(event, mc_raw, raw_text)
        mentioned = self._extract_mentioned_targets(event, raw_text)
        logger.info(
            "[%s] addadmin parsed mentioned=%s args=%s",
            _HUB_DISPLAY,
            mentioned,
            args,
        )
        raw_id = ""
        hint = ""
        if mentioned:
            raw_id, hint = mentioned[0]
        elif args:
            candidate = str(args[0]).strip().lstrip("@")
            wrapped = re.fullmatch(r"<@!?([A-Za-z0-9_-]+)>", candidate)
            if wrapped:
                candidate = wrapped.group(1)
            ignored = self._ignored_mention_ids(event)
            if (
                candidate
                and candidate not in ignored
                and candidate.lower() not in ignored
            ):
                raw_id, hint = candidate, candidate
        if not raw_id:
            usage = (
                "/mc addadmin @对方 或回复对方消息后发送 /mc addadmin"
                if head == "addadmin"
                else "/mc deladmin @对方"
            )
            await self._reply_to_event(
                event, f"[{hub_name}]\n❌ 请指定目标用户。\n用法：{usage}"
            )
            return True

        target = await self._resolve_admin_target(event, raw_id, hint)
        nick = self._nickname_from_at_hint(hint, raw_id)
        if not (
            target.isdigit()
            or (
                len(target) >= 20
                and all(ch in "0123456789abcdefABCDEF" for ch in target)
            )
        ):
            await self._reply_to_event(
                event,
                f"[{hub_name}]\n❌ 无法把 @{nick or raw_id} 解析成对方的账号 ID。\n"
                "请回复对方的一条消息后再发送 /mc addadmin。",
            )
            return True
        label = f"{target}" + (f"（{nick}）" if nick and nick != target else "")
        if target != raw_id and raw_id.isdigit():
            label += f"\n已将 @ 段 ID {raw_id} 解析为对方发言时使用的账号 ID。"
        logger.info(
            f"[{_HUB_DISPLAY}] /mc {head} raw={raw_id} hint={hint!r} resolved={target}"
        )
        if head == "addadmin":
            added = store.add_admin(target)
            self._refresh_hub_admins()
            self._sync_admins_to_config()
            text = (
                f"[{hub_name}]\n✅ 已添加管理员：{label}\n发送 /mc admins 可查看当前列表。"
                if added
                else f"[{hub_name}]\nℹ️ 该用户已是管理员：{label}\n发送 /mc admins 可查看当前列表。"
            )
            await self._reply_to_event(event, text)
            return True

        removed, reason = store.remove_admin(target)
        if removed:
            self._refresh_hub_admins()
            self._sync_admins_to_config()
            await self._reply_to_event(
                event, f"[{hub_name}]\n✅ 已移除管理员：{target}"
            )
        else:
            await self._reply_to_event(event, f"[{hub_name}]\n❌ 移除失败：{reason}")
        return True

    def _server_aliases(self) -> dict[str, str]:
        raw = self.config.get("server_aliases") or {}
        if not isinstance(raw, dict):
            return {}
        aliases: dict[str, str] = {}
        for key, value in raw.items():
            alias = str(key or "").strip()
            target = str(value or "").strip()
            if alias and target:
                aliases[alias.lower()] = target
        return aliases

    async def _start_hub(self) -> None:
        assert self._binding_store is not None
        self._hub = ArcHubServer(
            host=str(self.config.get("ws_host") or "0.0.0.0"),
            port=int(self.config.get("ws_port") or 19136),
            token=str(self.config.get("auth_token") or ""),
            hub_server_name=str(self.config.get("hub_server_name") or _HUB_DISPLAY),
            binding_store=self._binding_store,
            broadcast_qq=self._broadcast_to_activated_sessions,
            reply_qq=self._reply_to_session,
            set_group_card=self._set_group_card_all_targets,
            mute_qq=self._mute_qq_all_targets,
            get_arc_guard_api=self._get_arc_guard_api,
            group_names=self._group_names(),
            hub_admins=sorted(self._admin_ids()),
            sync_group_card=bool(self.config.get("sync_group_card", True)),
        )
        self._hub.process_ai_chat = self._process_ai_chat
        try:
            self._hub.ai_chat_timeout = float(self.config.get("ai_chat_timeout") or 180)
        except (TypeError, ValueError):
            self._hub.ai_chat_timeout = 180.0
        await self._hub.start()
        if bool(self.config.get("startup_announce", True)):
            # Delay slightly so platform adapters are ready.
            await asyncio.sleep(2)
            try:
                await self._broadcast_to_activated_sessions(
                    f"[{self.config.get('hub_server_name') or _HUB_DISPLAY}]\n"
                    f"[{_HUB_DISPLAY}] 已启动"
                )
            except Exception as e:
                logger.warning(f"[{_HUB_DISPLAY}] 启动播报失败: {e}")

    def _get_arc_guard_api(self):
        """Resolve Arc Guard cross-plugin API when the plugin is active.

        Returns:
            Arc Guard ``get_api()`` result, or None if unavailable.
        """
        try:
            meta = self.context.get_registered_star(ARC_GUARD_PLUGIN)
        except Exception as e:
            logger.debug(f"[{_HUB_DISPLAY}] 查找弧光护卫失败: {e}")
            return None
        if not meta or not getattr(meta, "activated", False):
            return None
        star = getattr(meta, "star_cls", None)
        if star is None:
            return None
        get_api = getattr(star, "get_api", None)
        if not callable(get_api):
            return None
        try:
            return get_api()
        except Exception as e:
            logger.warning(f"[{_HUB_DISPLAY}] 调用弧光护卫 get_api 失败: {e}")
            return None

    async def _mute_qq_all_targets(self, user_id: str, seconds: int) -> bool:
        """Mute a QQ user in every configured target group (Arc Guard accumulate).

        Prefers ``astrbot_plugin_arc_guard`` ``mute_user_in_group`` so duration
        is remaining + added. Uses ``respect_whitelist=True`` (normal Arc Guard
        punishment). Falls back to raw ``set_group_ban`` if Arc Guard is
        unavailable.

        Args:
            user_id: QQ user id.
            seconds: Extra mute seconds to accumulate (or set on fallback).

        Returns:
            True if at least one group ban succeeded.
        """
        platform_id = (self._platform_id or "").strip()
        if not platform_id:
            logger.warning(f"[{_HUB_DISPLAY}] 禁言失败：未学习到 platform_id")
            return False
        platform = self.context.get_platform_inst(platform_id)
        if platform is None:
            logger.warning(f"[{_HUB_DISPLAY}] 禁言失败：找不到平台 {platform_id}")
            return False
        bot = platform.get_client()
        if bot is None:
            logger.warning(f"[{_HUB_DISPLAY}] 禁言失败：平台无 client")
            return False

        uid = str(user_id or "").strip()
        duration = max(1, int(seconds))
        api = self._get_arc_guard_api()
        mute_in_group = getattr(api, "mute_user_in_group", None) if api else None
        ok_any = False

        for gid in self._target_group_ids():
            if callable(mute_in_group):
                try:
                    result = await mute_in_group(
                        bot,
                        str(gid),
                        uid,
                        duration,
                        respect_whitelist=True,
                    )
                    if result.get("ok"):
                        ok_any = True
                        logger.info(
                            f"[{_HUB_DISPLAY}] 弧光护卫累加禁言 group={gid} user={uid} "
                            f"remaining={result.get('remaining_before')}s "
                            f"+ add={result.get('added')}s "
                            f"-> total={result.get('total')}s"
                        )
                    else:
                        logger.warning(
                            f"[{_HUB_DISPLAY}] 弧光护卫禁言未成功 group={gid} "
                            f"user={uid}: {result.get('error') or 'unknown'}"
                        )
                except Exception as e:
                    logger.warning(
                        f"[{_HUB_DISPLAY}] 弧光护卫禁言异常 group={gid} user={uid}: {e}"
                    )
                continue

            try:
                await bot.call_action(
                    "set_group_ban",
                    group_id=int(gid),
                    user_id=int(uid),
                    duration=duration,
                )
                ok_any = True
            except Exception as e:
                logger.warning(
                    f"[{_HUB_DISPLAY}] 禁言失败 group={gid} user={user_id}: {e}"
                )
        return ok_any

    async def _set_group_card_all_targets(self, user_id: int, card: str) -> None:
        """Set QQ group card via aiocqhttp for every configured target group."""
        platform_id = (self._platform_id or "").strip()
        if not platform_id:
            logger.warning(f"[{_HUB_DISPLAY}] 改群名片失败：未学习到 platform_id")
            return
        platform = self.context.get_platform_inst(platform_id)
        if platform is None:
            logger.warning(f"[{_HUB_DISPLAY}] 改群名片失败：找不到平台 {platform_id}")
            return
        bot = platform.get_client()
        if bot is None:
            logger.warning(f"[{_HUB_DISPLAY}] 改群名片失败：平台无 client")
            return
        for gid in self._target_group_ids():
            try:
                await bot.call_action(
                    "set_group_card",
                    group_id=int(gid),
                    user_id=int(user_id),
                    card=str(card),
                )
            except Exception as e:
                logger.warning(
                    f"[{_HUB_DISPLAY}] 改群名片失败 group={gid} user={user_id}: {e}"
                )

    async def _send_message_to_umo(
        self, umo: str, text: str, *, label: str = ""
    ) -> None:
        umo = str(umo or "").strip()
        if not umo:
            return
        try:
            ok = await self.context.send_message(
                umo, MessageEventResult().message(text)
            )
            if not ok:
                logger.warning(
                    f"[{_HUB_DISPLAY}] 发送失败（未找到平台）: {label or umo}"
                )
        except Exception as error:
            err_text = str(error)
            logger.error(f"[{_HUB_DISPLAY}] 发送到 {label or umo} 异常: {error}")
            # QQ Official: group openid expired / bot left group / group recreated.
            if any(
                token in err_text
                for token in ("群已注销", "11255", "40011028", "请求的资源不存在")
            ):
                store = self._activation_store
                if store is not None and store.deactivate(umo):
                    logger.warning(
                        f"[{_HUB_DISPLAY}] 已移除失效激活会话 {umo}；"
                        "请在当前群重新发送 /mc activate"
                    )

    def _resolve_session_umo(self, target: str) -> str | None:
        key = str(target or "").strip()
        if not key:
            return None
        remembered = self._group_umo.get(key)
        if remembered:
            return remembered
        store = self._activation_store
        if store is not None and store.is_activated(key):
            return key
        if store is not None:
            for item in store.list_sessions():
                session_key = str(item.get("session_key") or "").strip()
                session_id = str(item.get("session_id") or "").strip()
                if key in {session_key, session_id} and session_key:
                    return session_key
        return None

    async def _broadcast_to_activated_sessions(self, text: str) -> None:
        """Broadcast MC→QQ text to whitelist groups (preferred) or activated sessions.

        When ``target_groups`` is set, only those group ids receive the message.
        Activated sessions outside the whitelist are skipped.
        """
        targets = self._target_group_ids()
        seen: set[str] = set()

        if targets:
            for gid in targets:
                umo = self._resolve_umo(gid)
                if not umo or umo in seen:
                    continue
                seen.add(umo)
                await self._send_message_to_umo(umo, text, label=str(gid))
            if seen:
                return
            logger.debug(
                f"[{_HUB_DISPLAY}] 白名单群尚无 unified_msg_origin，"
                "请先在白名单群内发一条消息或 /mc activate"
            )
            return

        store = self._activation_store
        if store is None:
            return
        sessions = store.list_sessions()
        if not sessions:
            logger.debug(f"[{_HUB_DISPLAY}] 无已激活会话，跳过广播")
            return
        for item in sessions:
            umo = str(item.get("session_key") or "").strip()
            if not umo or umo in seen:
                continue
            sid = str(item.get("session_id") or self._session_id_from_umo(umo)).strip()
            if not self._is_group_allowed(sid):
                continue
            seen.add(umo)
            label = str(item.get("label") or sid or umo)
            await self._send_message_to_umo(umo, text, label=label)

    async def _reply_to_session(self, target: str, text: str) -> None:
        umo = self._resolve_session_umo(target)
        if not umo:
            logger.warning(
                f"[{_HUB_DISPLAY}] 无法回复会话 {target}：未找到 unified_msg_origin"
            )
            return
        await self._send_message_to_umo(umo, text, label=str(target))

    async def _send_to_all_target_groups(self, text: str) -> None:
        """Legacy broadcast to configured target_groups (admin / mute helpers)."""
        groups = self._target_group_ids()
        if not groups:
            logger.warning("[弧光EndStone消息中枢] 未配置 target_groups，无法发往 QQ")
            return
        for gid in groups:
            umo = self._resolve_umo(gid)
            if not umo:
                logger.warning(
                    f"[弧光EndStone消息中枢] 群 {gid} 尚无 unified_msg_origin，"
                    "请先在该群发一条消息或配置 platform_id"
                )
                continue
            await self._send_message_to_umo(umo, text, label=str(gid))

    def _resolve_umo(self, group_id: str | int) -> str | None:
        gid = str(group_id)
        if gid in self._group_umo:
            return self._group_umo[gid]
        platform_id = self._platform_id.strip()
        if not platform_id:
            return None
        umo = str(MessageSession(platform_id, MessageType.GROUP_MESSAGE, gid))
        self._group_umo[gid] = umo
        return umo

    def _remember_umo(self, event: AstrMessageEvent) -> None:
        umo = str(event.unified_msg_origin or "").strip()
        if not umo:
            return
        if not self._platform_id:
            learned = str(event.get_platform_id() or "").strip()
            if learned and learned.lower() != "default":
                self._platform_id = learned
        self._group_umo[umo] = umo
        for key in (event.get_group_id(), event.get_session_id()):
            if key:
                self._group_umo[str(key)] = umo
        self._maybe_migrate_activation(event)

    def _maybe_migrate_activation(self, event: AstrMessageEvent) -> None:
        """Move a single per-platform activation when QQ group openid changes.

        Args:
            event: Current inbound group/private event.
        """
        store = self._activation_store
        if store is None:
            return
        umo = str(event.unified_msg_origin or "").strip()
        sid = str(event.get_session_id() or event.get_group_id() or "").strip()
        platform_id = str(event.get_platform_id() or self._platform_id or "").strip()
        if not umo or not platform_id:
            return
        if store.is_activated(umo) or (sid and store.is_activated_by_session_id(sid)):
            return
        if not self._is_group_allowed(sid):
            return
        old_key = store.migrate_single_platform_session(
            platform_id,
            umo,
            session_id=sid,
            label=sid or umo,
        )
        if old_key:
            logger.warning(
                f"[{_HUB_DISPLAY}] 检测到群会话 ID 变更，已迁移激活会话："
                f"{old_key} -> {umo}"
            )

    def _resolve_display_name(self, event: AstrMessageEvent) -> str:
        assert self._binding_store is not None
        user_id = self._sender_id(event)
        bound = self._binding_store.get_qq_player(user_id) if user_id else ""
        if bound:
            return bound
        name = (event.get_sender_name() or "").strip()
        return name or user_id or "未知"

    def _extract_forward_text(self, event: AstrMessageEvent) -> str:
        text = (event.message_str or "").strip()
        if text:
            return self._truncate(self._replace_cq_codes(text), 150)

        parts: list[str] = []
        try:
            for comp in event.get_messages():
                type_name = type(comp).__name__.lower()
                if hasattr(comp, "text") and getattr(comp, "text"):
                    parts.append(str(getattr(comp, "text")))
                elif "image" in type_name:
                    parts.append("[图片]")
                elif "record" in type_name or "voice" in type_name:
                    parts.append("[语音]")
                elif "video" in type_name:
                    parts.append("[视频]")
                elif "at" in type_name:
                    qq = getattr(comp, "qq", None) or getattr(comp, "target", "")
                    parts.append(f"@{qq}" if qq else "[@]")
                elif "face" in type_name:
                    parts.append("[表情]")
                elif "reply" in type_name:
                    parts.append("[回复]")
        except Exception:
            pass
        joined = "".join(parts).strip()
        return self._truncate(joined, 150) if joined else ""

    @staticmethod
    def _replace_cq_codes(text: str) -> str:
        def _repl(match: re.Match[str]) -> str:
            cq_type = match.group(1)
            params = match.group(2) or ""
            if cq_type == "image":
                return "[图片]"
            if cq_type == "video":
                return "[视频]"
            if cq_type == "record":
                return "[语音]"
            if cq_type == "face":
                return "[表情]"
            if cq_type == "at":
                if "qq=all" in params:
                    return "@全体成员"
                m = re.search(r"qq=(\d+)", params)
                return f"@{m.group(1)}" if m else "[@]"
            if cq_type == "reply":
                return "[回复]"
            return f"[{cq_type}]"

        return _cq_pattern.sub(_repl, text)

    @staticmethod
    def _truncate(text: str, max_length: int) -> str:
        if len(text) <= max_length:
            return text
        return text[: max_length - 1] + "…"

    @filter.custom_filter(McArcCommandFilter, priority=1000)
    async def on_mc_arc_hub_command(self, event: AstrMessageEvent):
        """Handle /mc ... locally before LLM chat consumes the message."""
        raw = extract_event_raw_text(event)
        mc_raw = normalize_mc_arc_raw_message(raw)
        if mc_raw is None:
            return

        gid = str(event.get_group_id() or event.get_session_id() or "").strip()
        if event.get_group_id() and not self._is_group_allowed(gid):
            return

        self._remember_umo(event)

        if is_mc_activate_command(mc_raw):
            await self._handle_mc_activate(event)
            event.stop_event()
            return

        if await self._handle_mc_admin_command(event, mc_raw):
            event.stop_event()
            return

        if not self._hub:
            await self._reply_to_event(event, f"{_HUB_DISPLAY}尚未启动")
            event.stop_event()
            return

        if not is_known_arc_command(mc_raw):
            await self._reply_to_event(
                event,
                f"[{self.config.get('hub_server_name') or _HUB_DISPLAY}]\n"
                "未知 /mc 指令。发送 /mc help 查看帮助。",
            )
            event.stop_event()
            return

        display_name = self._resolve_display_name(event)
        await self._hub.push_command_forward(
            raw_message=mc_raw,
            user_id=self._sender_id(event),
            display_name=display_name,
            group_id=gid,
            session_key=str(event.unified_msg_origin or ""),
            sender_role=self._qq_sender_role(event),
        )
        event.stop_event()

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """Forward whitelist-group QQ messages/commands to connected MC servers."""
        if event.get_extra("mc_ai_event"):
            return
        if not self._hub:
            return

        gid = event.get_group_id()
        if not gid:
            return

        if not self._is_group_allowed(str(gid)):
            return

        self._remember_umo(event)

        raw = extract_event_raw_text(event)

        if normalize_mc_arc_raw_message(raw) is not None:
            return

        display_name = self._resolve_display_name(event)

        if raw.startswith("/"):
            return

        if not bool(self.config.get("forward_qq_chat", True)):
            return

        text = self._extract_forward_text(event)
        if not text or text == "[空消息]":
            return

        await self._hub.push_qq_chat(display_name, text, gid)

    @filter.command("弧光状态", alias={"arcstatus", "arc_hub_status"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_hub_status(self, event: AstrMessageEvent):
        """Show connected MC servers."""
        if not self._hub:
            yield event.plain_result(f"{_HUB_DISPLAY}尚未启动")
            return
        names = self._hub.connected_server_names()
        catalog = self._hub.get_server_catalog()
        lines = [
            f"{_HUB_DISPLAY}状态",
            f"监听: {self.config.get('ws_host')}:{self.config.get('ws_port')}",
            f"已连接子服: {len(names)}",
        ]
        for item in catalog:
            mark = "✅" if item["name"] in names else "·"
            lines.append(f"{mark} [{item['id']}] {item['name']}")
        if not catalog:
            lines.append("（暂无注册记录）")
        activated = (
            self._activation_store.list_sessions() if self._activation_store else []
        )
        lines.append(f"已激活 MC 工具会话: {len(activated)}")
        for item in activated[:5]:
            lines.append(
                f"• {item.get('label') or item.get('session_id') or item.get('session_key')}"
            )
        if len(activated) > 5:
            lines.append(f"… 另有 {len(activated) - 5} 个")
        admins = sorted(self._admin_ids())
        supers = sorted(self._super_admin_ids())
        lines.append(f"超级管理员: {len(supers)}")
        lines.append(f"管理员: {len(admins)}")
        yield event.plain_result("\n".join(lines))

    def _qq_tool_permission_status_text(self, event: AstrMessageEvent) -> str:
        """Describe the current QQ caller's MC tool permission for the LLM.

        Args:
            event: Current AstrBot event (external / QQ side).

        Returns:
            One short paragraph the model can trust without calling identity tools.
        """
        uid = self._sender_id(event)
        level = self._resolve_tool_permission_level(event)
        if self._is_super_admin(event):
            title = "代理服主（插件超级管理员）"
            can_admin = True
        elif self._id_in_set(uid, self._admin_ids()):
            title = "管理员（插件管理员列表）"
            can_admin = True
        elif self._qq_can_run_command(event):
            role = self._qq_sender_role(event)
            title = f"管理员（QQ 群身份 {role or 'owner/admin'}）"
            can_admin = True
        elif self._qq_bound_player_name(event):
            bound = self._qq_bound_player_name(event)
            title = f"助手（已绑定角色 {bound}，仅自救/查自己）"
            can_admin = False
        else:
            title = "普通（只读工具；改世界需先 /mc 绑定或由管理员操作）"
            can_admin = False
        admin_note = (
            "可直接调用入狱/天眼/改银行/领地/弧光传送/mc_run_command 等管理类工具。"
            if can_admin
            else "管理类工具若被调用会由后端拒绝；不要先查群管身份再决定是否调用。"
        )
        return (
            f"当前消息发起者 ID={uid or '未知'}；Minecraft 工具权限={title}"
            f"（permission_level={level}）。{admin_note}"
            "QQ 群名片/群身份元数据不能替代本条权限判定。"
        )

    @filter.on_llm_request()
    async def on_llm_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """Append MC extra system instructions without replacing AstrBot persona."""
        if event.get_extra("mc_ai_event"):
            parts: list[str] = [_MC_AI_IDENTITY_HINT]
            extra = event.get_extra("mc_ai_extra_system")
            if extra:
                text = str(extra).strip()
                if text:
                    parts.append(text)
        elif self._is_external_mc_tool_session(event):
            parts = [_QQ_MC_TOOL_HINT, self._qq_tool_permission_status_text(event)]
            listing = self._format_ai_helper_listing()
            if listing:
                parts.append("已连接的 Minecraft 服务器：\n" + listing)
        else:
            return
        prefix = (req.system_prompt or "").rstrip()
        block = "# Minecraft Server Extra Instructions\n\n" + "\n\n".join(parts)
        req.system_prompt = f"{prefix}\n\n{block}\n" if prefix else f"{block}\n"

    def _is_external_mc_tool_session(self, event: AstrMessageEvent) -> bool:
        """Return True when this AstrBot session may use Minecraft AI tools."""
        if event.get_extra("mc_ai_event"):
            return False
        return self._is_tool_session_activated(event)

    def _qq_sender_role(self, event: AstrMessageEvent) -> str:
        try:
            raw_obj = event.message_obj.raw_message if event.message_obj else None
            if isinstance(raw_obj, dict):
                return str(
                    (raw_obj.get("sender") or {}).get("role") or "member"
                ).lower()
        except Exception:
            pass
        return "member"

    def _qq_bound_player_name(self, event: AstrMessageEvent) -> str:
        """Return the bound in-game player name for a QQ sender, if any."""
        assert self._binding_store is not None
        user_id = self._sender_id(event)
        if not user_id:
            return ""
        return str(self._binding_store.get_qq_player(user_id) or "").strip()

    def _qq_can_run_command(self, event: AstrMessageEvent) -> bool:
        """Whether this caller may run world-changing MC tools.

        Hub admins always can. In a QQ group with a known member role, only
        owner/admin can. Sessions without a group id rely on hub admin list.
        """
        uid = self._sender_id(event)
        if self._id_in_set(uid, self._admin_ids()):
            return True
        gid = str(event.get_group_id() or "").strip()
        if not gid:
            return False
        return self._qq_sender_role(event) in {"owner", "admin"}

    def _resolve_tool_permission_level(self, event: AstrMessageEvent) -> str:
        """Map MC / QQ caller to AI Helper three-tier permission_level."""
        if event.get_extra("mc_ai_event"):
            level = str(event.get_extra("mc_ai_permission_level") or "").strip()
            if level:
                return level
            if bool(event.get_extra("mc_ai_is_op")):
                return "admin"
            return "assistant"
        if self._is_super_admin(event):
            return "proxy_owner"
        if self._qq_can_run_command(event):
            return "admin"
        if self._qq_bound_player_name(event):
            return "assistant"
        return "assistant"

    def _format_ai_helper_listing(self) -> str:
        if not self._hub:
            return ""
        helpers = self._hub.list_ai_helper_game_names()
        if not helpers:
            return ""
        catalog_ids = {
            str(item["name"]): int(item["id"])
            for item in self._hub.get_server_catalog()
        }
        lines: list[str] = []
        for name in helpers:
            sid = catalog_ids.get(name)
            if sid is not None:
                lines.append(f"[{sid}] {name}")
            else:
                lines.append(name)
        return "\n".join(lines)

    def _resolve_tool_server(
        self, event: AstrMessageEvent, server_hint: str
    ) -> tuple[str, str]:
        """Pick the AI Helper game server for a tool call.

        Args:
            event: Current AstrBot event.
            server_hint: Optional name, catalog id, or alias.

        Returns:
            ``(server_name, error)``. Exactly one side is non-empty.
        """
        hint = str(server_hint or "").strip()
        if event.get_extra("mc_ai_event"):
            origin = str(event.get_extra("mc_ai_server") or "").strip()
            if origin:
                return origin, ""
            return "", "无法确定 Minecraft 服务器名称。"
        if not self._hub:
            return "", "弧光消息中枢尚未启动。"
        helpers = self._hub.list_ai_helper_game_names()
        if not helpers:
            return "", "当前没有 Minecraft AI Helper 在线。"
        if not hint:
            if len(helpers) == 1:
                return helpers[0], ""
            listing = self._format_ai_helper_listing() or "\n".join(helpers)
            return (
                "",
                "当前连了多台 Minecraft 服务器，请填写 server（名称、编号或别名）。已连接：\n"
                + listing,
            )

        aliases = self._server_aliases()
        mapped = aliases.get(hint.lower())
        if mapped:
            hint = mapped

        try:
            sid = int(hint)
        except ValueError:
            sid = None
        if sid is not None:
            for item in self._hub.get_server_catalog():
                if int(item["id"]) == sid:
                    name = str(item["name"])
                    if self._hub.find_ai_helper_ws(name) is not None:
                        return name, ""
                    return "", f"服务器 [{name}] 的 AI Helper 未连接"

        lowered = hint.lower()
        exact = [name for name in helpers if name.lower() == lowered]
        if len(exact) == 1:
            return exact[0], ""
        partial = [name for name in helpers if lowered in name.lower()]
        if len(partial) == 1:
            return partial[0], ""
        if len(partial) > 1:
            return "", "server 匹配到多台服，请改用更完整的名称或编号：\n" + "\n".join(
                partial
            )
        listing = self._format_ai_helper_listing() or "\n".join(helpers)
        return "", f"找不到服务器 [{hint}]。已连接：\n{listing}"

    @staticmethod
    def _parse_duration_minutes(raw: str, default: int = 30) -> int:
        """Parse minutes from numbers or Chinese duration phrases such as 一天."""
        text = str(raw or "").strip().lower().replace(" ", "")
        if not text:
            return default
        named = {
            "一天": 1440,
            "1天": 1440,
            "24小时": 1440,
            "24h": 1440,
            "半天": 720,
            "12小时": 720,
            "12h": 720,
            "一小时": 60,
            "1小时": 60,
            "1h": 60,
            "一周": 10080,
            "7天": 10080,
        }
        if text in named:
            return max(1, min(named[text], 10080))
        match = re.fullmatch(r"(\d+)(天|日)", text)
        if match:
            return max(1, min(int(match.group(1)) * 1440, 10080))
        match = re.fullmatch(r"(\d+)(小时|时|h)", text)
        if match:
            return max(1, min(int(match.group(1)) * 60, 10080))
        match = re.fullmatch(r"(\d+)(分钟|分|m|min)?", text)
        if match:
            return max(1, min(int(match.group(1)), 10080))
        try:
            return max(1, min(int(text), 10080))
        except ValueError:
            return default

    @staticmethod
    def _skyeye_result_looks_empty(text: str) -> bool:
        blob = str(text or "").strip()
        if not blob:
            return True
        if any(
            token in blob
            for token in (
                "BlockBreak",
                "BlockPlace",
                "ActorDamage",
                "PlayerDeath",
                "当前在线",
                "最近一次天眼",
            )
        ):
            return False
        return any(marker in blob for marker in _SKYEYE_EMPTY_MARKERS)

    async def _call_one_mc_ai_tool(
        self,
        server_name: str,
        action: str,
        payload: dict,
        *,
        timeout: float = 20,
    ) -> tuple[str, str]:
        """Call one helper and return ``(text, error)``."""
        assert self._hub is not None
        try:
            resp = await self._hub.call_ai_tool(
                server_name, action, payload, timeout=timeout
            )
        except Exception as error:
            logger.warning(
                f"[{_HUB_DISPLAY}] MC 工具 {action} 在 [{server_name}] 失败: {error}"
            )
            return "", f"调用 Minecraft 工具失败: {error}"
        if not isinstance(resp, dict):
            return "", "Minecraft 工具返回格式异常"
        if not resp.get("ok"):
            return "", str(resp.get("error") or "Minecraft 工具执行失败")
        return str(resp.get("text") or "").strip() or "（无返回）", ""

    async def _call_mc_ai_tool_all_helpers(
        self,
        action: str,
        payload: dict,
        *,
        timeout: float = 30,
    ) -> str:
        """Query every connected AI Helper and keep servers that have records."""
        if not self._hub:
            return "弧光消息中枢尚未启动。"
        helpers = self._hub.list_ai_helper_game_names()
        if not helpers:
            return "当前没有 Minecraft AI Helper 在线。"
        if len(helpers) == 1:
            text, error = await self._call_one_mc_ai_tool(
                helpers[0], action, payload, timeout=timeout
            )
            return error or text

        async def _one(name: str) -> tuple[str, str, str]:
            text, error = await self._call_one_mc_ai_tool(
                name, action, payload, timeout=timeout
            )
            return name, text, error

        rows = await asyncio.gather(*[_one(name) for name in helpers])
        hits: list[str] = []
        misses: list[str] = []
        for name, text, error in rows:
            if error:
                misses.append(f"[{name}] {error}")
                continue
            if self._skyeye_result_looks_empty(text):
                first = text.splitlines()[0] if text else "无记录"
                misses.append(f"[{name}] {first}")
                continue
            hits.append(f"[{name}]\n{text}")
        if hits:
            return "\n\n".join(hits)
        detail = "\n".join(misses) if misses else "（无返回）"
        return "所有已连接服务器均未查到有效天眼记录。\n" + detail

    async def _call_mc_ai_tool(
        self,
        event: AstrMessageEvent,
        action: str,
        args: dict | None = None,
        *,
        server: str = "",
        require_admin: bool = False,
        require_bound_or_admin: bool = False,
    ) -> str:
        """Run a Minecraft AI Helper tool for MC chat or any AstrBot session.

        Args:
            event: Current AstrBot event.
            action: Helper action name.
            args: Extra arguments for the action.
            server: External-side server hint; ignored for in-game origin server.
            require_admin: If True, identifiable QQ group callers must be hub
                admin / group owner / admin. Sessions without a group id are allowed.
            require_bound_or_admin: If True, QQ callers must be hub/group admin
                or have a bound game character for limited self-help commands.
        """
        is_mc = bool(event.get_extra("mc_ai_event"))
        if not self._hub:
            return "弧光消息中枢尚未启动。"
        if not is_mc and not self._is_tool_session_activated(event):
            return (
                "该会话尚未激活 Minecraft 工具。"
                "请让插件管理员在本对话发送 /mc activate。"
            )
        if require_bound_or_admin and not is_mc:
            bound_name = self._qq_bound_player_name(event)
            if self._qq_can_run_command(event):
                pass
            elif bound_name:
                pass
            else:
                return (
                    "没有权限：请先使用 /mc 绑定 <玩家名> 绑定游戏角色后再求助执行指令。"
                    "（未绑定用户不能调用改世界类工具，与普通人尝试使用管理指令相同。）"
                )
        elif require_admin and not is_mc and not self._qq_can_run_command(event):
            return "没有权限：执行游戏指令仅限插件管理员、群主和群管理员。"

        payload = dict(args or {})
        if action in _SKYEYE_FANOUT_ACTIONS and "minutes" in payload:
            payload["minutes"] = str(
                self._parse_duration_minutes(str(payload.get("minutes") or ""))
            )
        permission_level = self._resolve_tool_permission_level(event)
        payload.setdefault("permission_level", permission_level)
        if is_mc:
            caller_name = str(event.get_extra("mc_ai_player_name") or "").strip()
            caller_xuid = str(event.get_extra("mc_ai_xuid") or "").strip()
            payload.setdefault("is_op", bool(event.get_extra("mc_ai_is_op")))
            payload.setdefault("player_name", caller_name)
            payload.setdefault("player_xuid", caller_xuid)
            payload["caller_player_name"] = caller_name
            payload["caller_xuid"] = caller_xuid
        else:
            bound_name = self._qq_bound_player_name(event)
            is_admin = self._qq_can_run_command(event)
            payload.setdefault("is_op", is_admin)
            if bound_name:
                payload.setdefault("bound_player_name", bound_name)
            if require_bound_or_admin:
                payload.setdefault(
                    "is_bound_self_help", bool(bound_name) and not is_admin
                )
            payload.setdefault(
                "player_name",
                bound_name
                or self._sender_id(event)
                or str(event.get_sender_name() or ""),
            )
            payload.setdefault("player_xuid", "")
            payload["caller_player_name"] = bound_name
            payload["caller_xuid"] = ""

        timeout = 30.0 if action in _SKYEYE_FANOUT_ACTIONS else 20.0
        if action in _SKYEYE_FANOUT_ACTIONS and not str(server or "").strip():
            return await self._call_mc_ai_tool_all_helpers(
                action, payload, timeout=timeout
            )

        server_name, error = self._resolve_tool_server(event, server)
        if error:
            if action in _SKYEYE_FANOUT_ACTIONS:
                return await self._call_mc_ai_tool_all_helpers(
                    action, payload, timeout=timeout
                )
            return error
        text, call_error = await self._call_one_mc_ai_tool(
            server_name, action, payload, timeout=timeout
        )
        if action in _SKYEYE_FANOUT_ACTIONS and (
            call_error or self._skyeye_result_looks_empty(text)
        ):
            fallback = await self._call_mc_ai_tool_all_helpers(
                action, payload, timeout=timeout
            )
            if fallback and not self._skyeye_result_looks_empty(fallback):
                return fallback
            if fallback:
                return fallback
        if call_error:
            return call_error
        if not is_mc and len(self._hub.list_ai_helper_game_names()) > 1:
            return f"[{server_name}]\n{text}"
        return text

    @filter.llm_tool(name="mc_list_servers")
    async def mc_list_servers(self, event: AstrMessageEvent) -> str:
        """列出当前已连接、可执行工具的 Minecraft 服务器名称与编号。多开服时先调用再填写其它工具的 server。"""
        if not self._hub:
            return "弧光消息中枢尚未启动。"
        if not event.get_extra("mc_ai_event") and not self._is_tool_session_activated(
            event
        ):
            return (
                "该会话尚未激活 Minecraft 工具。"
                "请让插件管理员在本对话发送 /mc activate。"
            )
        listing = self._format_ai_helper_listing()
        if not listing:
            return "当前没有 Minecraft AI Helper 在线。"
        return "已连接的 Minecraft 服务器：\n" + listing

    @filter.llm_tool(name="mc_list_players")
    async def mc_list_players(self, event: AstrMessageEvent, server: str = "") -> str:
        """查询指定 Minecraft 服务器在线玩家名单与人数。玩家问起谁在线、有没有某某、在线人数时必须调用，禁止编造。

        Args:
            server(string): 目标服务器名称、编号或别名；游戏内可留空；QQ 多开服必须填
        """
        return await self._call_mc_ai_tool(event, "list", server=server)

    @filter.llm_tool(name="mc_get_tps")
    async def mc_get_tps(self, event: AstrMessageEvent, server: str = "") -> str:
        """查询指定 Minecraft 服务器 TPS / MSPT 等性能数据。玩家问起卡不卡、TPS、延迟时必须调用，禁止编造。

        Args:
            server(string): 目标服务器名称、编号或别名；游戏内可留空；QQ 多开服必须填
        """
        return await self._call_mc_ai_tool(event, "tps", server=server)

    @filter.llm_tool(name="mc_server_info")
    async def mc_server_info(self, event: AstrMessageEvent, server: str = "") -> str:
        """查询 Minecraft 服务器信息（含 CPU/内存/GPU；同物理机多开会合并且只显示一次硬件）。不要编造。

        Args:
            server(string): 目标服务器名称、编号或别名；留空则查询全部已连接子服并按物理机合并
        """
        is_mc = bool(event.get_extra("mc_ai_event"))
        if not self._hub:
            return "弧光消息中枢尚未启动。"
        if not is_mc and not self._is_tool_session_activated(event):
            return (
                "该会话尚未激活 Minecraft 工具。"
                "请让插件管理员在本对话发送 /mc activate。"
            )

        hint = str(server or "").strip()
        if is_mc and not hint:
            origin = str(event.get_extra("mc_ai_server") or "").strip()
            names = [origin] if origin else None
            if names:
                return await self._hub.collect_and_format_server_info(
                    server_names=names
                )

        names, error = self._hub.resolve_info_server_names(
            server_hint=hint,
            aliases=self._server_aliases(),
        )
        if error:
            return error
        return await self._hub.collect_and_format_server_info(server_names=names)

    @filter.llm_tool(name="mc_run_command")
    async def mc_run_command(
        self, event: AstrMessageEvent, command: str, server: str = ""
    ) -> str:
        """在指定 Minecraft 服务器控制台执行一条游戏指令。禁止 stop、kill；gamemode 仅 OP/管理员明确要求时可用。需要真实改游戏世界或给效果时调用。QQ 群已绑定用户可在求助时对本人角色使用 tp / effect / spawnpoint 等自救指令；未绑定用户无权执行。劈闪电用 execute at 玩家名 run summon lightning_bolt ~ ~ ~，不要写成 effect 玩家名 summon。

        Args:
            command(string): 不含斜杠的游戏指令。给效果如 effect Steve night_vision 30 0 true；劈闪电如 execute at Steve run summon lightning_bolt ~ ~ ~
            server(string): 目标服务器名称、编号或别名；游戏内可留空；QQ 多开服必须填
        """
        command_line = str(command or "").strip()
        if not command_line:
            return "指令为空"
        return await self._call_mc_ai_tool(
            event,
            "cmd",
            {"command": command_line},
            server=server,
            require_bound_or_admin=True,
        )

    @filter.llm_tool(name="mc_landmarks")
    async def mc_landmarks(self, event: AstrMessageEvent, server: str = "") -> str:
        """查询本服公开地标：出生点、公共传送点(Warp)、公共领地/功能区。问路标、功能建筑、出生点时调用；只读，已激活即可。系统提示若已有地标清单可优先引用，需要最新数据再调本工具。

        Args:
            server(string): 目标服务器名称、编号或别名；游戏内可留空；QQ 多开服必须填
        """
        return await self._call_mc_ai_tool(event, "landmarks", server=server)

    @filter.llm_tool(name="mc_economy")
    async def mc_economy(
        self,
        event: AstrMessageEvent,
        player_name: str = "",
        sub_action: str = "query",
        delta: str = "",
        amount: str = "",
        xuid: str = "",
        targets: str = "",
        to_online: str = "",
        server: str = "",
    ) -> str:
        """弧光银行。query 查余额（已绑定可查自己）；transfer 从自己账户发红包/转账；change 加减钱仅管理员。发红包时 amount 为每人金额；targets 填收款人（逗号分隔），给当前在线玩家发则 to_online=true。

        Args:
            player_name(string): query/change 的玩家名；transfer 时可空
            sub_action(string): query / transfer / change
            delta(string): change 时的变动金额，正加负减
            amount(string): transfer 每人金额，或 change 的 delta
            xuid(string): 玩家 XUID；可与 player_name 二选一
            targets(string): transfer 收款人，逗号分隔；发全员红包可填 online
            to_online(string): true 时发给当前服在线且非自己的玩家
            server(string): 目标服务器名称、编号或别名；游戏内可留空；QQ 多开服必须填。银行数据跨服共通，任意已连接 Helper 均可
        """
        name = str(player_name or "").strip()
        xuid_val = str(xuid or "").strip()
        action = str(sub_action or "query").strip().lower() or "query"
        is_mc = bool(event.get_extra("mc_ai_event"))
        is_transfer = action in (
            "transfer",
            "pay",
            "send",
            "hongbao",
            "redpack",
            "红包",
        )
        mutating_admin = action in ("change", "adjust", "add", "remove")
        if not is_mc:
            bound_name = self._qq_bound_player_name(event)
            is_admin = self._qq_can_run_command(event)
            if mutating_admin and not is_admin:
                return "没有权限：加减银行余额仅限插件管理员、群主和群管理员。"
            if is_transfer:
                if not is_admin and not bound_name:
                    return (
                        "没有权限：请先使用 /mc 绑定 <玩家名> 后再用自己的余额发红包。"
                    )
            elif action in ("query", "get", "balance", "") and not is_admin:
                if not bound_name:
                    return "没有权限：请先使用 /mc 绑定 <玩家名> 后再查询自己的余额。"
                if name and name.lower() != bound_name.lower():
                    return "没有权限：已绑定用户只能查询自己绑定角色的余额。"
                name = bound_name
                xuid_val = ""
        if not is_transfer and not name and not xuid_val:
            if is_mc:
                name = str(event.get_extra("mc_ai_player_name") or "").strip()
                xuid_val = str(event.get_extra("mc_ai_xuid") or "").strip()
            if not name and not xuid_val:
                return "需要 player_name 或 xuid"
        payload: dict = {
            "player_name": name,
            "xuid": xuid_val,
            "sub_action": action,
            "targets": str(targets or "").strip(),
            "to_online": str(to_online or "").strip(),
        }
        delta_text = str(delta or "").strip() or str(amount or "").strip()
        if delta_text:
            payload["delta"] = delta_text
            payload["amount"] = delta_text
        return await self._call_mc_ai_tool(
            event,
            "economy",
            payload,
            server=server,
            require_admin=mutating_admin,
            require_bound_or_admin=is_transfer or not mutating_admin,
        )

    @filter.llm_tool(name="mc_land")
    async def mc_land(
        self,
        event: AstrMessageEvent,
        player_name: str = "",
        sub_action: str = "list",
        land_id: str = "",
        dimension: str = "",
        x: str = "",
        y: str = "",
        z: str = "",
        xuid: str = "",
        server: str = "",
    ) -> str:
        """弧光领地查询。list=某人领地列表；info=按 land_id 详情；at=某坐标所在领地。不要用 mc_run_command。仅管理员。

        Args:
            player_name(string): list 时的玩家名；可与 xuid 二选一
            sub_action(string): list / info / at
            land_id(string): info 时的领地 ID
            dimension(string): at 时的维度，可空
            x(string): at 时的 X
            y(string): at 时的 Y
            z(string): at 时的 Z
            xuid(string): 玩家 XUID；可与 player_name 二选一
            server(string): 目标服务器名称、编号或别名；游戏内可留空；QQ 多开服必须填
        """
        action = str(sub_action or "list").strip().lower() or "list"
        return await self._call_mc_ai_tool(
            event,
            "land",
            {
                "player_name": str(player_name or "").strip(),
                "xuid": str(xuid or "").strip(),
                "sub_action": action,
                "land_id": str(land_id or "").strip(),
                "dimension": str(dimension or "").strip(),
                "x": str(x or "").strip(),
                "y": str(y or "").strip(),
                "z": str(z or "").strip(),
            },
            server=server,
            require_admin=True,
        )

    @filter.llm_tool(name="mc_arc_tp")
    async def mc_arc_tp(
        self,
        event: AstrMessageEvent,
        player_name: str,
        sub_action: str,
        home_name: str = "",
        warp_name: str = "",
        name: str = "",
        dimension: str = "",
        x: str = "",
        y: str = "",
        z: str = "",
        server: str = "",
    ) -> str:
        """弧光传送：把在线玩家送到 Home / Warp / 坐标。送公共传送点用 warp；送家用 home；送坐标用 pos。不要用 mc_run_command 代替。仅管理员。

        Args:
            player_name(string): 要传送的在线玩家名
            sub_action(string): home / warp / pos
            home_name(string): home 时的家名；可空表示默认家
            warp_name(string): warp 时的公共传送点名
            name(string): 可代替 home_name 或 warp_name
            dimension(string): pos 时的维度，可空
            x(string): pos 时的 X
            y(string): pos 时的 Y
            z(string): pos 时的 Z
            server(string): 目标服务器名称、编号或别名；游戏内可留空；QQ 多开服必须填
        """
        pname = str(player_name or "").strip()
        if not pname:
            return "玩家名为空"
        action = str(sub_action or "").strip().lower()
        if action not in {"home", "warp", "pos"}:
            return "sub_action 须为 home / warp / pos"
        return await self._call_mc_ai_tool(
            event,
            "arc_tp",
            {
                "player_name": pname,
                "sub_action": action,
                "home_name": str(home_name or "").strip(),
                "warp_name": str(warp_name or "").strip(),
                "name": str(name or "").strip(),
                "dimension": str(dimension or "").strip(),
                "x": str(x or "").strip(),
                "y": str(y or "").strip(),
                "z": str(z or "").strip(),
            },
            server=server,
            require_admin=True,
        )

    @filter.llm_tool(name="mc_jail_player")
    async def mc_jail_player(
        self,
        event: AstrMessageEvent,
        player_name: str,
        minutes: str = "",
        reason: str = "",
        server: str = "",
    ) -> str:
        """把指定玩家关进监狱（一键入狱）。玩家要求关人、入狱、坐牢、禁闭时必须调用，不要用 mc_run_command 执行 /jail。时长单位为分钟，可填 -1 或 无期；不填则用服务器默认时长。

        Args:
            player_name(string): 要关押的游戏内玩家名
            minutes(string): 刑期分钟数，或 -1/life/无期；留空则用默认一键入狱时长
            reason(string): 入狱原因，写入监狱插件；可留空
            server(string): 目标服务器名称、编号或别名；游戏内可留空；QQ 多开服必须填
        """
        name = str(player_name or "").strip()
        if not name:
            return "玩家名为空"
        return await self._call_mc_ai_tool(
            event,
            "jail",
            {
                "player_name": name,
                "minutes": str(minutes or "").strip(),
                "reason": str(reason or "").strip(),
            },
            server=server,
            require_admin=True,
        )

    @filter.llm_tool(name="mc_release_player")
    async def mc_release_player(
        self, event: AstrMessageEvent, player_name: str, server: str = ""
    ) -> str:
        """释放监狱中的指定玩家。玩家要求放人、出狱、释放时调用。

        Args:
            player_name(string): 要释放的游戏内玩家名
            server(string): 目标服务器名称、编号或别名；游戏内可留空；QQ 多开服必须填
        """
        name = str(player_name or "").strip()
        if not name:
            return "玩家名为空"
        return await self._call_mc_ai_tool(
            event,
            "release",
            {"player_name": name},
            server=server,
            require_admin=True,
        )

    @filter.llm_tool(name="mc_list_prisoners")
    async def mc_list_prisoners(self, event: AstrMessageEvent, server: str = "") -> str:
        """查询指定 Minecraft 服务器当前在押玩家名单。问起谁在坐牢、监狱里有谁时必须调用，禁止编造。

        Args:
            server(string): 目标服务器名称、编号或别名；游戏内可留空；QQ 多开服必须填
        """
        return await self._call_mc_ai_tool(event, "prisoners", server=server)

    @filter.llm_tool(name="mc_skyeye_player")
    async def mc_skyeye_player(
        self,
        event: AstrMessageEvent,
        player_name: str,
        minutes: str = "30",
        action: str = "",
        server: str = "",
    ) -> str:
        """查询天眼：指定玩家现在在哪、是否在领地内，以及近期做过什么（破坏/放置/交互/进出服等）。不要求该玩家在线。问起某人在哪、刚干了什么、一天内干了什么时必须调用，禁止编造。仅管理员。

        Args:
            player_name(string): 游戏内玩家名
            minutes(string): 由你根据用户要求换算后的回溯分钟数。用户说一天则传 1440，一小时传 60，未说明可省略（默认 30）
            action(string): 可选，限定行为类型，如 BlockBreak / BlockPlace / ActorDamage / PlayerDeath
            server(string): 可留空以搜索全部已连接服务器；仅在明确只要某一台时才填写
        """
        name = str(player_name or "").strip()
        if not name:
            return "玩家名为空"
        return await self._call_mc_ai_tool(
            event,
            "skyeye_player",
            {
                "player_name": name,
                "minutes": str(minutes or "30").strip(),
                "action": str(action or "").strip(),
            },
            server=server,
            require_admin=True,
        )

    @filter.llm_tool(name="mc_skyeye_combat")
    async def mc_skyeye_combat(
        self,
        event: AstrMessageEvent,
        player_name: str,
        minutes: str = "30",
        server: str = "",
    ) -> str:
        """查询天眼战斗记录：该玩家打了谁、被谁打了、死亡。不要求该玩家在线。问起打架、被打、击杀时必须调用，禁止编造。仅管理员。

        Args:
            player_name(string): 游戏内玩家名
            minutes(string): 由你根据用户要求换算后的回溯分钟数。用户说一天则传 1440，一小时传 60，未说明可省略（默认 30）
            server(string): 可留空以搜索全部已连接服务器；仅在明确只要某一台时才填写
        """
        name = str(player_name or "").strip()
        if not name:
            return "玩家名为空"
        return await self._call_mc_ai_tool(
            event,
            "skyeye_combat",
            {"player_name": name, "minutes": str(minutes or "30").strip()},
            server=server,
            require_admin=True,
        )

    @filter.llm_tool(name="mc_skyeye_location")
    async def mc_skyeye_location(
        self,
        event: AstrMessageEvent,
        x: str,
        y: str,
        z: str,
        radius: str = "8",
        dimension: str = "",
        minutes: str = "30",
        server: str = "",
    ) -> str:
        """查询天眼：某坐标附近谁做过什么、是否在领地内。问起这块地发生过什么、谁挖了这里时必须调用。仅管理员。

        Args:
            x(string): X 坐标
            y(string): Y 坐标
            z(string): Z 坐标
            radius(string): 半径格数，默认 8
            dimension(string): 维度，如 minecraft:overworld；可留空表示不限
            minutes(string): 由你根据用户要求换算后的回溯分钟数。用户说一天则传 1440，一小时传 60，未说明可省略（默认 30）
            server(string): 可留空以搜索全部已连接服务器；仅在明确只要某一台时才填写
        """
        return await self._call_mc_ai_tool(
            event,
            "skyeye_location",
            {
                "x": str(x or "").strip(),
                "y": str(y or "").strip(),
                "z": str(z or "").strip(),
                "radius": str(radius or "8").strip(),
                "dimension": str(dimension or "").strip(),
                "minutes": str(minutes or "30").strip(),
            },
            server=server,
            require_admin=True,
        )

    @filter.llm_tool(name="mc_stock_leaderboard")
    async def mc_stock_leaderboard(
        self,
        event: AstrMessageEvent,
        mode: str = "relative",
        top: str = "5",
        bottom: str = "5",
        player_name: str = "",
        server: str = "",
    ) -> str:
        """查询服务器模拟美股插件的玩家盈亏排行榜（收益率/绝对盈亏）。问谁赚最多、亏最多、股票排行、某玩家盈亏时必须调用，禁止编造。只读，已激活即可。需目标服安装 UpsAndDowns。

        Args:
            mode(string): relative=收益率（默认）；absolute=绝对盈亏金额
            top(string): 前 N 名，默认 5
            bottom(string): 倒数 N 名，默认 5
            player_name(string): 可选；只查该玩家名次与盈亏
            server(string): 目标服务器名称、编号或别名；游戏内可留空；QQ 多开服必须填（通常填主服）
        """
        return await self._call_mc_ai_tool(
            event,
            "stock_leaderboard",
            {
                "mode": str(mode or "relative").strip(),
                "top": str(top or "5").strip(),
                "bottom": str(bottom or "5").strip(),
                "player_name": str(player_name or "").strip(),
            },
            server=server,
        )

    @filter.llm_tool(name="mc_stock_quote")
    async def mc_stock_quote(
        self,
        event: AstrMessageEvent,
        symbol: str,
        period: str = "day",
        server: str = "",
    ) -> str:
        """查询单只股票或加密货币现价/走势（如 AAPL、TSLA、BTC-USD）。问股价、涨跌、走势时必须调用，禁止编造。只读，已激活即可。需目标服安装 UpsAndDowns。

        Args:
            symbol(string): 股票代码，如 AAPL
            period(string): price=仅现价；minute/day/month=走势，默认 day
            server(string): 目标服务器名称、编号或别名；游戏内可留空；QQ 多开服必须填（通常填主服）
        """
        code = str(symbol or "").strip()
        if not code:
            return "股票代码为空"
        return await self._call_mc_ai_tool(
            event,
            "stock_quote",
            {
                "symbol": code,
                "period": str(period or "day").strip(),
            },
            server=server,
        )

    @filter.llm_tool(name="mc_player_ip")
    async def mc_player_ip(
        self,
        event: AstrMessageEvent,
        player_name: str = "",
        server: str = "",
    ) -> str:
        """查询本服在线玩家的连接 IP（原始地址）。需要天气问候或 IP 精确定位时先调用本工具，从返回的 ip= 字段取出 IP，再原样传给 mc_ip_locate。禁止编造 IP。不填 player_name 则查当前对话玩家；助手只能查自己，管理员可查任意在线玩家。只读，已激活即可。

        Args:
            player_name(string): 可选；目标玩家名，默认当前对话/绑定玩家
            server(string): 目标服务器名称、编号或别名；游戏内可留空；QQ 多开服必须填
        """
        return await self._call_mc_ai_tool(
            event,
            "player_ip",
            {
                "player_name": str(player_name or "").strip(),
            },
            server=server,
        )

    @filter.llm_tool(name="mc_ip_locate")
    async def mc_ip_locate(self, event: AstrMessageEvent, ip: str) -> str:
        """根据 IP 查询基站级/高精度归属地（国家省市县区、运营商、经纬度等）。玩家连接 IP 须先用 mc_player_ip 取得 ip= 再原样传入本工具；也可直接传入任意公网 IP。结果为 IP 库估算（移动网络常标为基站/WiFi），不是 GPS。禁止编造位置。只读，已激活即可。

        Args:
            ip(string): 要查询的 IPv4/IPv6；可带 ip= 前缀，工具会自动剥离
        """
        is_mc = bool(event.get_extra("mc_ai_event"))
        if not is_mc and not self._is_tool_session_activated(event):
            return (
                "该会话尚未激活 Minecraft 工具。"
                "请让插件管理员在本对话发送 /mc activate。"
            )

        raw = str(ip or "").strip()
        if raw.lower().startswith("ip="):
            raw = raw[3:].strip()
        # Model may paste a whole mc_player_ip line; keep the first IP-looking token.
        match = re.search(
            r"(?:(?:\d{1,3}\.){3}\d{1,3}|[0-9a-fA-F:]{2,})",
            raw,
        )
        if match:
            raw = match.group(0)
        if not raw:
            return "IP 为空：请先调用 mc_player_ip 取得 ip=，或直接传入公网 IP。"

        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            return f"无效 IP：{raw}"
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
        ):
            return f"ip={raw}\n无法定位：该地址为内网/保留地址，无公网归属地。"

        timeout = aiohttp.ClientTimeout(total=8)
        errors: list[str] = []
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers={"User-Agent": "AstrBot-EndstoneArc/1.7.9"},
        ) as session:
            # Primary: ip9 — Chinese ISP data; mobile often marked as 基站WiFi.
            try:
                async with session.get(
                    "https://ip9.com.cn/get",
                    params={"ip": raw},
                ) as resp:
                    if resp.status == 200:
                        payload = await resp.json(content_type=None)
                        data = (
                            payload.get("data") if isinstance(payload, dict) else None
                        )
                        if (
                            isinstance(payload, dict)
                            and int(payload.get("ret") or 0) == 200
                            and isinstance(data, dict)
                        ):
                            country = str(data.get("country") or "").strip()
                            prov = str(data.get("prov") or "").strip()
                            city = str(data.get("city") or "").strip()
                            area = str(data.get("area") or "").strip()
                            isp = str(data.get("isp") or "").strip()
                            lng = str(data.get("lng") or "").strip()
                            lat = str(data.get("lat") or "").strip()
                            post = str(data.get("post_code") or "").strip()
                            area_code = str(data.get("area_code") or "").strip()
                            big = str(data.get("big_area") or "").strip()
                            lines = [
                                f"ip={raw}",
                                f"国家={country or '未知'}",
                                f"省={prov or '未知'}",
                                f"市={city or '未知'}",
                                f"区县={area or '未知'}",
                                f"运营商={isp or '未知'}",
                            ]
                            if lng and lat:
                                lines.append(f"经纬度={lng},{lat}")
                            if post:
                                lines.append(f"邮编={post}")
                            if area_code:
                                lines.append(f"区号={area_code}")
                            if big:
                                lines.append(f"大区={big}")
                            precision = (
                                "基站/移动网络级（IP 库估算）"
                                if "基站" in isp
                                else "区县级（IP 库估算）"
                            )
                            lines.append(f"精度={precision}")
                            lines.append("说明=非 GPS 实测，供天气问候/大致位置参考")
                            lines.append("来源=ip9")
                            return "\n".join(lines)
                        errors.append(f"ip9 返回异常: {payload!r}"[:160])
                    else:
                        errors.append(f"ip9 HTTP {resp.status}")
            except Exception as exc:
                errors.append(f"ip9: {exc}")

            # Fallback: ip-api
            try:
                async with session.get(
                    f"http://ip-api.com/json/{raw}",
                    params={
                        "lang": "zh-CN",
                        "fields": (
                            "status,message,country,regionName,city,district,"
                            "lat,lon,isp,org,as,mobile,proxy,hosting,query"
                        ),
                    },
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        if isinstance(data, dict) and data.get("status") == "success":
                            isp = str(data.get("isp") or data.get("org") or "").strip()
                            mobile = bool(data.get("mobile"))
                            lines = [
                                f"ip={raw}",
                                f"国家={str(data.get('country') or '未知').strip()}",
                                f"省={str(data.get('regionName') or '未知').strip()}",
                                f"市={str(data.get('city') or '未知').strip()}",
                                f"区县={str(data.get('district') or '未知').strip()}",
                                f"运营商={isp or '未知'}",
                            ]
                            lat, lon = data.get("lat"), data.get("lon")
                            if lat is not None and lon is not None:
                                lines.append(f"经纬度={lon},{lat}")
                            flags = []
                            if mobile:
                                flags.append("移动网络/基站侧")
                            if data.get("proxy"):
                                flags.append("代理")
                            if data.get("hosting"):
                                flags.append("机房/托管")
                            if flags:
                                lines.append(f"网络类型={','.join(flags)}")
                            lines.append(
                                "精度="
                                + (
                                    "基站/移动网络级（IP 库估算）"
                                    if mobile
                                    else "城市级（IP 库估算）"
                                )
                            )
                            lines.append("说明=非 GPS 实测，供天气问候/大致位置参考")
                            lines.append("来源=ip-api")
                            return "\n".join(lines)
                        msg = data.get("message") if isinstance(data, dict) else None
                        errors.append(f"ip-api: {msg or data!r}"[:160])
                    else:
                        errors.append(f"ip-api HTTP {resp.status}")
            except Exception as exc:
                errors.append(f"ip-api: {exc}")

        detail = "；".join(errors) if errors else "未知错误"
        logger.warning(f"[{_HUB_DISPLAY}] IP 定位失败 ip={raw}: {detail}")
        return f"查询 IP 定位失败：{detail}"

    def _can_manage_qq_binding(self, event: AstrMessageEvent) -> bool:
        """Whether the caller may bind/unbind QQ for others (admin / OP only)."""
        if event.get_extra("mc_ai_event"):
            if bool(event.get_extra("mc_ai_is_op")):
                return True
            level = str(event.get_extra("mc_ai_permission_level") or "").strip().lower()
            return level in {
                "admin",
                "管理员",
                "proxy_owner",
                "代理服主",
                "owner",
                "服主",
            }
        return self._qq_can_run_command(event)

    def _admin_label_for_binding(self, event: AstrMessageEvent) -> str:
        if event.get_extra("mc_ai_event"):
            name = str(event.get_extra("mc_ai_player_name") or "").strip()
            return f"mc:{name}" if name else "mc:op"
        uid = self._sender_id(event)
        return f"qq:{uid}" if uid else "admin"

    @staticmethod
    def _is_valid_platform_user_id(user_id: str) -> bool:
        """Accept classic QQ digits or QQ Official / other platform string ids."""
        text = str(user_id or "").strip()
        if not text or any(ch.isspace() for ch in text):
            return False
        if text.isdigit():
            return 5 <= len(text) <= 11
        # member_openid / user_openid / similar opaque ids
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", text):
            return False
        # Must contain at least one letter or underscore (not a short pure digit miss)
        return bool(re.search(r"[A-Za-z_]", text))

    async def _resolve_binding_user_id(
        self, event: AstrMessageEvent, qq_hint: str
    ) -> tuple[str, str]:
        """Resolve platform user id from tool arg and/or @ mentions in the event.

        Returns:
            ``(user_id, error)``. Exactly one side is non-empty.
        """
        hint = str(qq_hint or "").strip()
        # Strip common wrappers the model may copy from prompts.
        if hint.startswith("<@") and hint.endswith(">"):
            hint = hint[2:-1].strip()
        if hint.startswith("@"):
            hint = hint[1:].strip()

        if self._is_valid_platform_user_id(hint):
            return hint, ""

        raw_text = extract_event_raw_text(event)
        mentioned = self._extract_mentioned_targets(event, raw_text)
        resolved_mentions: list[tuple[str, str]] = []
        for raw_id, name_hint in mentioned:
            resolved = await self._resolve_admin_target(event, raw_id, name_hint)
            resolved = str(resolved or raw_id).strip()
            if self._is_valid_platform_user_id(resolved):
                resolved_mentions.append((resolved, str(name_hint or "").strip()))

        if not hint and len(resolved_mentions) == 1:
            return resolved_mentions[0][0], ""

        if hint and resolved_mentions:
            for resolved, name_hint in resolved_mentions:
                if hint in {resolved, name_hint}:
                    return resolved, ""
                if name_hint and (hint in name_hint or name_hint in hint):
                    return resolved, ""

        if hint:
            # Treat as group card / nickname and try member-list resolve.
            looked_up = await self._resolve_admin_target(event, "", hint)
            looked_up = str(looked_up or "").strip()
            if self._is_valid_platform_user_id(looked_up):
                return looked_up, ""
            return (
                "",
                f"「{hint}」不是有效的平台用户 ID。"
                "请传入传统 QQ 号（5～11 位数字）、QQ 官方机器人的 member_openid，"
                "或在本条消息里 @对方后留空 qq 参数。",
            )

        if len(resolved_mentions) > 1:
            listing = "、".join(f"{name or uid}" for uid, name in resolved_mentions)
            return "", f"消息里 @ 了多人（{listing}），请只 @ 一位或显式传入 qq。"
        return (
            "",
            "请提供 qq（平台用户 ID），或在本条消息里 @对方。",
        )

    def _query_qq_binding_text(self, player_name: str = "", qq: str = "") -> str:
        assert self._binding_store is not None
        store = self._binding_store
        name = str(player_name or "").strip()
        qq_str = str(qq or "").strip()
        if name:
            canonical, data = store.find_player_entry(name)
            if not data:
                return f"中枢绑定记录中没有玩家「{name}」（可用 bind 经弧光核心解析后写入）"
            bound = str(data.get("qq") or "").strip()
            xuid = str(data.get("xuid") or "").strip()
            if bound:
                return f"玩家 {canonical} 已绑定 QQ {bound}" + (
                    f"，XUID {xuid}" if xuid else ""
                )
            return f"玩家 {canonical} 存在记录但未绑定 QQ" + (
                f"（XUID {xuid}）" if xuid else ""
            )
        if qq_str:
            bound_name = store.get_qq_player(qq_str)
            if bound_name:
                return f"QQ {qq_str} 已绑定游戏角色「{bound_name}」"
            hist = store.get_qq_player_history(qq_str)
            if hist:
                return f"QQ {qq_str} 当前未绑定；历史关联角色：" + "、".join(hist)
            return f"QQ {qq_str} 无绑定记录"
        return "请提供 player_name 或 qq"

    async def _admin_bind_qq(
        self,
        *,
        event: AstrMessageEvent,
        player_name: str,
        qq: str,
        force: bool,
        admin_label: str,
    ) -> str:
        assert self._hub is not None and self._binding_store is not None
        store = self._binding_store
        name = str(player_name or "").strip()
        if not name:
            return "绑定需要提供 player_name（游戏名）"
        qq_str, resolve_error = await self._resolve_binding_user_id(event, qq)
        if resolve_error:
            return resolve_error

        if store.is_player_banned(name):
            return f"玩家 {name} 已被封禁，无法绑定"

        existing_for_qq = store.get_qq_player(qq_str)
        if existing_for_qq and existing_for_qq.lower() != name.lower():
            if not force:
                return (
                    f"平台用户 {qq_str} 已绑定角色「{existing_for_qq}」。"
                    "若要改绑请设 force=true，或先对该角色 unbind。"
                )
            store.unbind_player_qq(existing_for_qq, admin_name=admin_label)

        canonical, stored = store.find_player_entry(name)
        player_xuid = str((stored or {}).get("xuid") or "").strip()
        if not player_xuid:
            resolved_name, resolved_xuid = await self._hub._resolve_player_via_core(
                name
            )
            if not resolved_xuid:
                return (
                    f"服务器记录中找不到名为「{name}」的玩家。"
                    "请确认该玩家至少登录过一次游戏。"
                )
            canonical = store.ensure_player_record(resolved_name, resolved_xuid)
            player_xuid = resolved_xuid
        elif not canonical:
            canonical = store.ensure_player_record(name, player_xuid)

        target = canonical or name
        bound_qq = str(store.get_player_qq(target) or "").strip()
        if bound_qq:
            if bound_qq == qq_str:
                return f"平台用户 {qq_str} 已与「{target}」绑定，无需重复操作"
            if not force:
                return (
                    f"角色「{target}」已绑定平台用户 {bound_qq}。"
                    "若要改绑请设 force=true，或先 unbind。"
                )
            store.unbind_player_qq(target, admin_name=admin_label)

        if not store.bind_player_qq(target, player_xuid, qq_str):
            return "绑定失败，请稍后重试"
        # 传统数字 QQ 才改群名片；openid 等字符串 ID 不能 int()
        if self._hub.sync_group_card and self._hub.set_group_card and qq_str.isdigit():
            try:
                await self._hub.set_group_card(int(qq_str), target)
            except Exception as error:
                logger.warning(f"[{_HUB_DISPLAY}] 绑定工具改群名片失败: {error}")
        return (
            f"已将平台用户 {qq_str} 绑定到游戏角色「{target}」（操作者 {admin_label}）"
        )

    async def _admin_unbind_qq(
        self,
        *,
        event: AstrMessageEvent,
        player_name: str = "",
        qq: str = "",
        admin_label: str,
    ) -> str:
        assert self._binding_store is not None
        store = self._binding_store
        name = str(player_name or "").strip()
        qq_hint = str(qq or "").strip()
        if not name and not qq_hint:
            # Allow @-only unbind
            qq_str, resolve_error = await self._resolve_binding_user_id(event, "")
            if resolve_error:
                return (
                    resolve_error or "解绑需要提供 player_name、平台用户 ID，或 @ 对方"
                )
            name = store.get_qq_player(qq_str)
            if not name:
                return f"平台用户 {qq_str} 当前没有绑定的游戏角色"
        elif not name and qq_hint:
            qq_str, resolve_error = await self._resolve_binding_user_id(event, qq_hint)
            if resolve_error:
                return resolve_error
            name = store.get_qq_player(qq_str)
            if not name:
                return f"平台用户 {qq_str} 当前没有绑定的游戏角色"
        canonical, data = store.find_player_entry(name)
        if not data:
            return f"找不到玩家「{name}」的绑定记录"
        bound = str(data.get("qq") or "").strip()
        if not bound:
            return f"玩家「{canonical}」当前未绑定平台用户"
        if not store.unbind_player_qq(canonical, admin_name=admin_label):
            return "解绑失败，请稍后重试"
        return f"已解除「{canonical}」与平台用户 {bound} 的绑定（操作者 {admin_label}）"

    @filter.llm_tool(name="mc_qq_binding")
    async def mc_qq_binding(
        self,
        event: AstrMessageEvent,
        sub_action: str = "query",
        player_name: str = "",
        qq: str = "",
        force: str = "",
    ) -> str:
        """管理平台用户 ID 与游戏角色绑定。管理员可帮别人绑定/解绑/查询。qq 可为传统 QQ 号或 QQ 官方机器人 member_openid；消息里已 @ 对方时可把 qq 留空，工具自动读取，不要把群名片当 qq。bind 需 player_name；unbind/query 填 player_name 或 qq/@ 其一。force=true 可强制改绑。

        Args:
            sub_action(string): query / bind / unbind
            player_name(string): 游戏内玩家名；bind 必填；unbind/query 可与 qq 二选一
            qq(string): 平台用户 ID（数字 QQ 或 openid）；已 @ 对方时可空；不要填群名片
            force(string): bind 时 true 表示强制改绑（先解旧绑再绑新）
        """
        is_mc = bool(event.get_extra("mc_ai_event"))
        if not self._hub or self._binding_store is None:
            return "弧光消息中枢尚未启动。"
        if not is_mc and not self._is_tool_session_activated(event):
            return (
                "该会话尚未激活 Minecraft 工具。"
                "请让插件管理员在本对话发送 /mc activate。"
            )
        action = str(sub_action or "query").strip().lower() or "query"
        if action in ("get", "lookup", "status", "who"):
            action = "query"
        if action in ("bindqq", "绑定"):
            action = "bind"
        if action in ("unbindqq", "解绑", "unbind"):
            action = "unbind"

        if action != "query" and not self._can_manage_qq_binding(event):
            return "没有权限：绑定/解绑他人仅限插件管理员、群主/群管，或游戏内 OP。"

        if action == "query":
            name = str(player_name or "").strip()
            qq_hint = str(qq or "").strip()
            qq_str = ""
            if qq_hint or not name:
                qq_str, _err = await self._resolve_binding_user_id(event, qq_hint)
                if not qq_str and not name:
                    return _err or "请提供 player_name、平台用户 ID，或 @ 对方"
            return self._query_qq_binding_text(player_name=name, qq=qq_str)

        admin_label = self._admin_label_for_binding(event)
        if action == "bind":
            force_flag = str(force or "").strip().lower() in {
                "1",
                "true",
                "yes",
                "y",
                "强制",
            }
            return await self._admin_bind_qq(
                event=event,
                player_name=player_name,
                qq=qq,
                force=force_flag,
                admin_label=admin_label,
            )
        if action == "unbind":
            return await self._admin_unbind_qq(
                event=event,
                player_name=player_name,
                qq=qq,
                admin_label=admin_label,
            )
        return "sub_action 须为 query / bind / unbind"

    async def _process_ai_chat(self, data: dict) -> dict:
        """Run one Minecraft player message through AstrBot's conversation pipeline.

        Args:
            data: Hub ``ai_chat`` payload.

        Returns:
            ``{"ok": True, "reply": "..."}`` or ``{"ok": False, "error": "..."}``.
        """
        player_name = str(data.get("player_name") or "player").strip() or "player"
        player_xuid = str(data.get("player_xuid") or data.get("xuid") or "").strip()
        server_name = str(data.get("server_name") or "mc").strip() or "mc"
        content = str(data.get("content") or "").strip()
        extra_system = str(data.get("extra_system_prompt") or "").strip()
        channel = str(data.get("channel") or "public").strip() or "public"
        is_op = bool(data.get("is_op", False))
        permission_level = str(data.get("permission_level") or "").strip()
        if not content:
            return {"ok": False, "error": "空消息"}

        bound_qq = ""
        if self._binding_store is not None:
            bound_qq = self._binding_store.resolve_bound_qq(player_name, player_xuid)
        sender_id = bound_qq or player_xuid or f"name_{player_name}"

        # Label shows requester identity (not AI capability ceiling).
        if permission_level in {"代理服主", "proxy_owner", "owner", "服主"}:
            status = "代理服主"
        elif permission_level in {"管理员", "admin"} or is_op:
            status = "管理员"
        elif permission_level in {"助手", "assistant"}:
            # Effective assistant tier: non-OP players after ceiling clamp.
            status = "普通玩家"
        else:
            status = "OP玩家" if is_op else "普通玩家"
        channel_label = "GUI私聊" if channel == "gui" else "公开聊天"
        user_text = f"{player_name}({status})[{channel_label}]: {content}"

        event = McAiMessageEvent(
            player_name=player_name,
            player_xuid=player_xuid,
            sender_id=sender_id,
            server_name=server_name,
            message=user_text,
            extra_system_prompt=extra_system,
            is_op=is_op,
            permission_level=permission_level,
            channel=channel,
            bound_qq=bound_qq,
        )
        await self.context.get_event_queue().put(event)
        try:
            timeout = float(getattr(self._hub, "ai_chat_timeout", 180) or 180)
            reply = await asyncio.wait_for(event.wait_reply(), timeout=timeout)
        except asyncio.TimeoutError:
            return {"ok": False, "error": "AstrBot 对话超时"}
        except Exception as e:
            logger.error(f"[{_HUB_DISPLAY}] MC AI 对话失败: {e}")
            return {"ok": False, "error": str(e)}

        reply_text = str(reply or "").strip()
        reply_text = await self._filter_mc_ai_reply(event, reply_text)
        if not reply_text:
            return {"ok": False, "error": "AstrBot 未返回文本"}
        return {"ok": True, "reply": reply_text}

    async def _filter_mc_ai_reply(
        self, event: McAiMessageEvent, reply_text: str
    ) -> str:
        """Intercept forbidden AI text and punish the triggering player.

        Sender id is bound QQ, otherwise XUID. Punishment matches MC chat
        keyword hits (jail / QQ mute / kill / warning). Whitelist players are
        not punished, but the original AI text is still replaced.

        Args:
            event: Synthetic MC AI event for this turn.
            reply_text: Assistant text collected from the AstrBot pipeline.

        Returns:
            Warning text when blocked, otherwise the original reply.
        """
        if not reply_text:
            return reply_text
        api = self._get_arc_guard_api()
        blocked = bool(event.get_extra("arc_guard_ai_blocked"))
        hits = int(event.get_extra("arc_guard_ai_hits") or 0)
        mute_seconds = int(event.get_extra("arc_guard_ai_mute_seconds") or 0)
        warning = str(event.get_extra("arc_guard_ai_reply") or "").strip()
        if (not blocked) and api is not None:
            try:
                hits = int(api.count_forbidden(reply_text) or 0)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[{_HUB_DISPLAY}] 弧光护卫检测 AI 回复失败: {exc}")
                hits = 0
            if hits > 0:
                blocked = True
                mute_seconds = int(api.mute_seconds_for_hits(hits))
                warning = str(api.format_reply(hits) or "").strip()
        if not blocked or hits <= 0:
            return reply_text

        warning = warning or reply_text
        bound_qq = str(event.get_extra("mc_ai_qq") or "").strip()
        skip_punish = False
        if bound_qq and api is not None:
            try:
                skip_punish = bool(api.is_whitelisted(bound_qq))
            except Exception:
                skip_punish = False
        if skip_punish:
            logger.info(
                "[%s] 拦截弧光天星违禁回复（白名单不处罚） server=%s player=%s qq=%s hits=%s",
                _HUB_DISPLAY,
                event.get_extra("mc_ai_server"),
                event.get_extra("mc_ai_player_name"),
                bound_qq,
                hits,
            )
            return warning

        if self._hub is not None:
            await self._hub.apply_forbidden_player_hit(
                origin_server=str(event.get_extra("mc_ai_server") or ""),
                player_name=str(event.get_extra("mc_ai_player_name") or ""),
                bound_qq=bound_qq,
                hits=hits,
                mute_seconds=mute_seconds,
                reply=warning,
                reason="MC AI回复违规",
            )
        return warning
