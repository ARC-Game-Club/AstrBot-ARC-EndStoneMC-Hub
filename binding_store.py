"""QQ binding store for ARC message center data_rpc.

Playtime / session_count live in ARCCore SQLite (player_basic_info), not here.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger


class BindingStore:
    """Centralized QQ <-> player binding data for MC Hub clients."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.binding_file = data_dir / "data.json"
        self._binding_data: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.binding_file.exists():
            self.binding_file.write_text("{}", encoding="utf-8")
        try:
            raw = json.loads(self.binding_file.read_text(encoding="utf-8"))
            self._binding_data = raw if isinstance(raw, dict) else {}
        except Exception as e:
            logger.error(f"[弧光EndStone消息中枢] 读取 data.json 失败: {e}")
            self._binding_data = {}
        self._normalize()
        logger.info(
            f"[弧光EndStone消息中枢] 已加载绑定数据 {len(self._binding_data)} 条"
        )

    def save(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.binding_file.write_text(
            json.dumps(self._binding_data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _normalize(self) -> None:
        """Ensure binding fields exist; drop obsolete playtime keys from memory view."""
        drop_keys = (
            "total_playtime",
            "session_count",
            "last_join_time",
            "last_quit_time",
        )
        for name, data in list(self._binding_data.items()):
            if not isinstance(data, dict):
                continue
            data.setdefault("name", name)
            for key in drop_keys:
                data.pop(key, None)

    @property
    def binding_data(self) -> dict[str, Any]:
        return self._binding_data

    @staticmethod
    def _now() -> int:
        return int(time.time())

    @staticmethod
    def _max_optional_ts(a, b):
        pool = [x for x in (a, b) if x is not None]
        return max(pool) if pool else None

    @staticmethod
    def _min_optional_ts(a, b):
        pool = [x for x in (a, b) if x is not None]
        return min(pool) if pool else None

    def _get_player_by_xuid(self, xuid: str) -> dict[str, Any]:
        target = str(xuid or "").strip()
        if not target:
            return {}
        for data in self._binding_data.values():
            if isinstance(data, dict) and str(data.get("xuid") or "").strip() == target:
                return data
        return {}

    def find_player_entry(self, player_name: str) -> tuple[str, dict[str, Any]]:
        """Return ``(canonical_name, record)`` with case-insensitive name match."""
        name = str(player_name or "").strip()
        if not name:
            return "", {}
        exact = self._binding_data.get(name)
        if isinstance(exact, dict):
            return name, exact
        lowered = name.lower()
        for key, data in self._binding_data.items():
            if str(key).lower() == lowered and isinstance(data, dict):
                return str(key), data
        return "", {}

    def ensure_player_record(self, player_name: str, player_xuid: str = "") -> str:
        """Create or refresh a player row so group bind can succeed after first login."""
        canonical, data = self.find_player_entry(player_name)
        xuid = str(player_xuid or "").strip()
        if data:
            if xuid and not str(data.get("xuid") or "").strip():
                data["xuid"] = xuid
                self.save()
            elif xuid and str(data.get("xuid") or "").strip() != xuid:
                data["xuid"] = xuid
                self.save()
            return canonical
        name = str(player_name or "").strip()
        if not name:
            return ""
        self._binding_data[name] = {
            "name": name,
            "xuid": xuid,
            "qq": "",
            "bind_time": None,
            "rebind_time": None,
            "unbind_time": None,
            "unbind_by": "",
            "original_qq": "",
            "previous_qq": "",
            "is_banned": False,
            "ban_time": None,
            "ban_by": "",
            "ban_reason": "",
            "unban_time": None,
            "unban_by": "",
        }
        self.save()
        return name

    def is_player_bound(self, player_name: str, player_xuid: str | None = None) -> bool:
        if player_xuid:
            player_data = self._get_player_by_xuid(player_xuid)
            if player_data:
                qq = player_data.get("qq", "")
                return bool(qq and str(qq).strip())
            if player_name in self._binding_data:
                qq = self._binding_data[player_name].get("qq", "")
                return bool(qq and str(qq).strip())
            return False
        if player_name not in self._binding_data:
            return False
        qq = self._binding_data[player_name].get("qq", "")
        return bool(qq and str(qq).strip())

    def get_player_qq(self, player_name: str) -> str:
        _name, data = self.find_player_entry(player_name)
        return str(data.get("qq") or "")

    def resolve_bound_qq(self, player_name: str, player_xuid: str | None = None) -> str:
        """Return the bound QQ number for a player, preferring XUID over name.

        Args:
            player_name: Current game name.
            player_xuid: Player XUID if known.

        Returns:
            Bound QQ as a string, or empty when unbound.
        """
        data: dict[str, Any] = {}
        if player_xuid:
            data = self._get_player_by_xuid(str(player_xuid).strip())
        if not data and player_name:
            maybe = self._binding_data.get(player_name)
            if isinstance(maybe, dict):
                data = maybe
        return str(data.get("qq") or "").strip()

    def resolve_player_binding(self, display_or_name: str) -> tuple[str, str]:
        """Resolve a display label or raw name to (player_name, qq).

        Tries exact key match first, then the longest binding name that is a
        suffix of the display label (titles like ``[传奇]Steve``).

        Args:
            display_or_name: Game display label or raw player name.

        Returns:
            ``(player_name, qq)``; either may be empty when unresolved.
        """
        label = (display_or_name or "").strip()
        if not label:
            return "", ""
        exact = self._binding_data.get(label)
        if isinstance(exact, dict):
            return label, str(exact.get("qq") or "")

        best_name = ""
        for name, data in self._binding_data.items():
            if not isinstance(data, dict) or not name:
                continue
            if label == name or label.endswith(name):
                if len(name) > len(best_name):
                    best_name = name
        if not best_name:
            return "", ""
        data = self._binding_data.get(best_name) or {}
        return best_name, str(data.get("qq") or "")

    def get_qq_player(self, qq_number: str) -> str:
        qq = str(qq_number)
        for name, data in self._binding_data.items():
            if isinstance(data, dict) and str(data.get("qq") or "") == qq:
                return name
        return ""

    def get_qq_player_history(self, qq_number: str) -> list[str]:
        qq = str(qq_number)
        names: list[str] = []
        for name, data in self._binding_data.items():
            if not isinstance(data, dict):
                continue
            if (
                str(data.get("qq") or "") == qq
                or str(data.get("original_qq") or "") == qq
            ):
                names.append(name)
        return names

    def get_player_by_xuid(self, xuid: str) -> dict[str, Any]:
        return self._get_player_by_xuid(xuid)

    def bind_player_qq(
        self, player_name: str, player_xuid: str, qq_number: str
    ) -> bool:
        existing = self.get_qq_player(qq_number)
        if existing and existing != player_name:
            return False
        now = self._now()
        if player_name not in self._binding_data:
            self._binding_data[player_name] = {
                "name": player_name,
                "xuid": player_xuid,
                "qq": str(qq_number),
                "bind_time": now,
                "rebind_time": None,
                "unbind_time": None,
                "unbind_by": "",
                "original_qq": "",
                "previous_qq": "",
                "is_banned": False,
                "ban_time": None,
                "ban_by": "",
                "ban_reason": "",
                "unban_time": None,
                "unban_by": "",
            }
        else:
            data = self._binding_data[player_name]
            prev = str(data.get("qq") or "")
            data["xuid"] = player_xuid
            data["qq"] = str(qq_number)
            if prev and prev != str(qq_number):
                data["previous_qq"] = prev
                data["rebind_time"] = now
            else:
                data["bind_time"] = data.get("bind_time") or now
            data["unbind_time"] = None
            data["unbind_by"] = ""
        self.save()
        return True

    def unbind_player_qq(self, player_name: str, admin_name: str = "system") -> bool:
        """Clear the QQ/openid binding for a game character (case-insensitive name).

        Args:
            player_name: In-game player name (exact or case-insensitive).
            admin_name: Operator label stored on the record.

        Returns:
            True when a bound QQ/openid was cleared.
        """
        canonical, data = self.find_player_entry(player_name)
        if not canonical or not data:
            return False
        if not str(data.get("qq") or "").strip():
            return False
        data["original_qq"] = data.get("original_qq") or data.get("qq") or ""
        data["previous_qq"] = data.get("qq") or ""
        data["qq"] = ""
        data["unbind_time"] = self._now()
        data["unbind_by"] = admin_name
        self.save()
        return True

    def update_player_name(self, old_name: str, new_name: str, xuid: str) -> bool:
        if old_name == new_name:
            return True
        if old_name not in self._binding_data:
            return False
        data = self._binding_data.pop(old_name)
        data["name"] = new_name
        data["xuid"] = xuid
        self._binding_data[new_name] = data
        self.save()
        return True

    def is_player_banned(self, player_name: str) -> bool:
        _name, data = self.find_player_entry(player_name)
        return bool(data.get("is_banned"))

    def ban_player(
        self, player_name: str, admin_name: str = "system", reason: str = ""
    ) -> bool:
        if player_name not in self._binding_data:
            self._binding_data[player_name] = {
                "name": player_name,
                "xuid": "",
                "qq": "",
                "bind_time": None,
                "rebind_time": None,
                "unbind_time": None,
                "unbind_by": "",
                "original_qq": "",
                "previous_qq": "",
                "is_banned": True,
                "ban_time": self._now(),
                "ban_by": admin_name,
                "ban_reason": reason,
                "unban_time": None,
                "unban_by": "",
            }
        else:
            data = self._binding_data[player_name]
            data["is_banned"] = True
            data["ban_time"] = self._now()
            data["ban_by"] = admin_name
            data["ban_reason"] = reason
            data["unban_time"] = None
            data["unban_by"] = ""
        self.save()
        return True

    def unban_player(self, player_name: str, admin_name: str = "system") -> bool:
        if player_name not in self._binding_data:
            return False
        data = self._binding_data[player_name]
        data["is_banned"] = False
        data["unban_time"] = self._now()
        data["unban_by"] = admin_name
        self.save()
        return True

    def get_banned_players(self) -> dict[str, Any]:
        return {
            name: data
            for name, data in self._binding_data.items()
            if isinstance(data, dict) and data.get("is_banned")
        }

    def get_player_binding_history(self, player_name: str) -> dict[str, Any]:
        return dict(self._binding_data.get(player_name) or {})

    def get_complete_player_binding_status(
        self, player_name: str, player_xuid: str | None = None
    ) -> dict[str, Any]:
        data = {}
        if player_xuid:
            data = self._get_player_by_xuid(player_xuid)
        if not data:
            data = dict(self._binding_data.get(player_name) or {})
        return {
            "bound": bool(str(data.get("qq") or "").strip()),
            "qq": str(data.get("qq") or ""),
            "banned": bool(data.get("is_banned")),
            "data": data,
        }

    def merge_legacy_binding_one(
        self, player_name: str, player_data: dict[str, Any]
    ) -> None:
        if not player_name or not isinstance(player_data, dict):
            return
        # Binding-only merge; ignore legacy playtime fields.
        clean = {
            k: v
            for k, v in player_data.items()
            if k
            not in (
                "total_playtime",
                "session_count",
                "last_join_time",
                "last_quit_time",
            )
        }
        if player_name not in self._binding_data:
            clean.setdefault("name", player_name)
            self._binding_data[player_name] = clean
            return
        hub = self._binding_data[player_name]
        self._merge_legacy_into_existing(hub, clean, player_name)

    def merge_legacy_binding_snapshot(self, snapshot: dict[str, Any]) -> None:
        if not isinstance(snapshot, dict):
            return
        for name, data in snapshot.items():
            if isinstance(data, dict):
                self.merge_legacy_binding_one(str(name), data)

    def merge_legacy_binding_persist(self) -> None:
        self.save()

    def _merge_legacy_into_existing(self, hub: dict, inc: dict, name: str) -> None:
        def nz(v) -> bool:
            return v is not None and v != ""

        hub.setdefault("name", name)
        for key in (
            "xuid",
            "qq",
            "original_qq",
            "previous_qq",
            "unbind_by",
            "ban_by",
            "ban_reason",
            "unban_by",
        ):
            if nz(inc.get(key)) and not nz(hub.get(key)):
                hub[key] = inc.get(key)
        for key in (
            "bind_time",
            "rebind_time",
            "unbind_time",
            "ban_time",
            "unban_time",
        ):
            hub[key] = self._max_optional_ts(hub.get(key), inc.get(key))
        if inc.get("is_banned"):
            hub["is_banned"] = True

    def run_action(self, action: str, args: dict[str, Any]) -> Any:
        if action == "get_binding_data":
            return self.binding_data
        if action == "is_player_bound":
            return self.is_player_bound(args["player_name"], args.get("player_xuid"))
        if action == "get_player_qq":
            return self.get_player_qq(args["player_name"])
        if action == "get_qq_player":
            return self.get_qq_player(args["qq_number"])
        if action == "get_qq_player_history":
            return self.get_qq_player_history(args["qq_number"])
        if action == "get_player_by_xuid":
            return self.get_player_by_xuid(args["xuid"])
        if action == "bind_player_qq":
            return self.bind_player_qq(
                args["player_name"], args["player_xuid"], args["qq_number"]
            )
        if action == "unbind_player_qq":
            return self.unbind_player_qq(
                args["player_name"], args.get("admin_name", "system")
            )
        if action == "update_player_name":
            return self.update_player_name(
                args["old_name"], args["new_name"], args["xuid"]
            )
        if action == "is_player_banned":
            return self.is_player_banned(args["player_name"])
        if action == "ban_player":
            return self.ban_player(
                args["player_name"],
                args.get("admin_name", "system"),
                args.get("reason", ""),
            )
        if action == "unban_player":
            return self.unban_player(
                args["player_name"], args.get("admin_name", "system")
            )
        if action == "get_banned_players":
            return self.get_banned_players()
        if action == "get_player_binding_history":
            return self.get_player_binding_history(args["player_name"])
        if action == "get_complete_player_binding_status":
            return self.get_complete_player_binding_status(
                args["player_name"], args.get("player_xuid")
            )
        if action == "merge_legacy_binding_one":
            self.merge_legacy_binding_one(args["player_name"], args["player_data"])
            return True
        if action == "merge_legacy_binding_snapshot":
            self.merge_legacy_binding_snapshot(args["snapshot"])
            return True
        if action == "merge_legacy_binding_persist":
            self.merge_legacy_binding_persist()
            return True
        if action == "save_data":
            self.save()
            return True
        # Playtime moved to ARCCore DB — keep no-op for old MC clients.
        if action in (
            "update_player_join",
            "update_player_quit",
            "start_player_timer",
            "stop_player_timer",
            "get_player_playtime_info",
            "cleanup_timer_system",
        ):
            if action == "get_player_playtime_info":
                return {
                    "session_count": 0,
                    "total_playtime": 0,
                    "is_online": False,
                    "last_join_time": None,
                    "last_quit_time": None,
                }
            return None
        raise ValueError(f"unknown data_rpc action: {action}")
