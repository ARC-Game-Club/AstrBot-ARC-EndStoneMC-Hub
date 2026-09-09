"""Persist chat sessions where /mc activate enabled Minecraft AI tools."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger


class ActivationStore:
    """Stores activated AstrBot session keys for MC tool access."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.store_file = data_dir / "activated_sessions.json"
        self._sessions: dict[str, dict[str, Any]] = {}
        self._session_id_index: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.store_file.exists():
            self.store_file.write_text('{"sessions": {}}', encoding="utf-8")
        try:
            raw = json.loads(self.store_file.read_text(encoding="utf-8"))
            sessions = raw.get("sessions") if isinstance(raw, dict) else {}
            self._sessions = sessions if isinstance(sessions, dict) else {}
        except Exception as error:
            logger.error(f"[弧光EndStone消息中枢] 读取 activated_sessions.json 失败: {error}")
            self._sessions = {}
        self._rebuild_index()
        logger.info(
            f"[弧光EndStone消息中枢] 已加载 {len(self._sessions)} 个已激活 MC 工具会话"
        )

    def _rebuild_index(self) -> None:
        self._session_id_index = {}
        for key, meta in self._sessions.items():
            if not isinstance(meta, dict):
                continue
            sid = str(meta.get("session_id") or "").strip()
            if sid:
                self._session_id_index[sid] = key

    def save(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        payload = {"sessions": self._sessions}
        self.store_file.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def activate(
        self,
        session_key: str,
        *,
        session_id: str = "",
        label: str = "",
        by_admin: str = "",
    ) -> bool:
        """Mark a chat session as activated.

        Args:
            session_key: ``unified_msg_origin`` or equivalent stable key.
            session_id: Raw session / group id shown to admins.
            label: Human-readable label for replies.
            by_admin: Admin user id that ran ``/mc activate``.

        Returns:
            True if newly activated, False if already active.
        """
        key = str(session_key or "").strip()
        if not key:
            return False
        sid = str(session_id or "").strip()
        # Drop stale activations for the same group/session under an old platform id.
        if sid:
            stale_keys = [
                old_key
                for old_key, meta in list(self._sessions.items())
                if old_key != key
                and isinstance(meta, dict)
                and str(meta.get("session_id") or "").strip() == sid
            ]
            for old_key in stale_keys:
                self._sessions.pop(old_key, None)
        existed = key in self._sessions
        self._sessions[key] = {
            "session_id": sid,
            "label": str(label or sid or key).strip(),
            "activated_at": int(time.time()),
            "activated_by": str(by_admin or "").strip(),
        }
        if sid:
            self._session_id_index[sid] = key
        self.save()
        return not existed

    def deactivate(self, session_key: str) -> bool:
        """Remove one activated session.

        Args:
            session_key: ``unified_msg_origin`` to drop.

        Returns:
            True if a session was removed.
        """
        key = str(session_key or "").strip()
        if not key or key not in self._sessions:
            return False
        self._sessions.pop(key, None)
        self._rebuild_index()
        self.save()
        return True

    def migrate_single_platform_session(
        self,
        platform_id: str,
        new_session_key: str,
        *,
        session_id: str = "",
        label: str = "",
    ) -> str | None:
        """If this platform has exactly one activated session, move it to ``new_session_key``.

        Used when QQ Official group openid changes after re-adding the bot.

        Args:
            platform_id: AstrBot platform instance id.
            new_session_key: Current ``unified_msg_origin``.
            session_id: Current group / session id.
            label: Optional display label.

        Returns:
            Old session key when migrated, otherwise None.
        """
        pid = str(platform_id or "").strip()
        new_key = str(new_session_key or "").strip()
        if not pid or not new_key:
            return None
        if new_key in self._sessions:
            return None
        prefix = f"{pid}:"
        same = [
            key
            for key in self._sessions
            if key == pid or key.startswith(prefix)
        ]
        if len(same) != 1:
            return None
        old_key = same[0]
        old_meta = self._sessions.pop(old_key, {}) or {}
        sid = str(session_id or old_meta.get("session_id") or "").strip()
        self._sessions[new_key] = {
            "session_id": sid,
            "label": str(label or sid or new_key).strip(),
            "activated_at": int(time.time()),
            "activated_by": str(old_meta.get("activated_by") or "auto-migrate"),
        }
        self._rebuild_index()
        self.save()
        return old_key

    def is_activated(self, session_key: str) -> bool:
        key = str(session_key or "").strip()
        return bool(key and key in self._sessions)

    def is_activated_by_session_id(self, session_id: str) -> bool:
        sid = str(session_id or "").strip()
        if not sid:
            return False
        key = self._session_id_index.get(sid)
        return bool(key and key in self._sessions)

    def list_sessions(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for key, meta in self._sessions.items():
            if not isinstance(meta, dict):
                continue
            row = dict(meta)
            row["session_key"] = key
            items.append(row)
        items.sort(key=lambda item: int(item.get("activated_at") or 0), reverse=True)
        return items
