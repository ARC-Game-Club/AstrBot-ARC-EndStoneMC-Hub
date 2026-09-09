"""Persistent admin / super-admin store for ARC message center."""

from __future__ import annotations

import json
from pathlib import Path

from astrbot.api import logger


class AdminStore:
    """Stores mutable hub admins and super admins."""

    def __init__(self, data_dir: Path, *, seed_admins: set[str] | None = None) -> None:
        self.data_dir = data_dir
        self.store_file = data_dir / "admins.json"
        self._admins: set[str] = set()
        self._super_admins: set[str] = set()
        self._load()
        if seed_admins:
            self.seed_defaults(seed_admins)

    def _load(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.store_file.exists():
            self.store_file.write_text(
                json.dumps({"admins": [], "super_admins": []}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        try:
            raw = json.loads(self.store_file.read_text(encoding="utf-8"))
        except Exception as error:
            logger.error(f"[弧光EndStone消息中枢] 读取 admins.json 失败: {error}")
            raw = {}
        admins = raw.get("admins") if isinstance(raw, dict) else []
        supers = raw.get("super_admins") if isinstance(raw, dict) else []
        self._admins = {
            str(item).strip() for item in (admins or []) if str(item).strip()
        }
        self._super_admins = {
            str(item).strip() for item in (supers or []) if str(item).strip()
        }
        self._admins.update(self._super_admins)
        logger.info(
            "[弧光EndStone消息中枢] 已加载管理员 %s 人，超级管理员 %s 人",
            len(self._admins),
            len(self._super_admins),
        )

    def save(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "admins": sorted(self._admins),
            "super_admins": sorted(self._super_admins),
        }
        self.store_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def seed_defaults(self, admins: set[str]) -> bool:
        """Seed config admins into persistent store on first use."""
        normalized = {str(item).strip() for item in admins if str(item).strip()}
        changed = False
        if normalized - self._admins:
            self._admins.update(normalized)
            changed = True
        if not self._super_admins and normalized:
            self._super_admins.update(normalized)
            self._admins.update(normalized)
            changed = True
        if changed:
            self.save()
        return changed

    def seed_super_admins(self, user_ids: set[str]) -> bool:
        """Replace persistent super admins with the non-empty config list.

        Args:
            user_ids: Super-admin ids from plugin config.

        Returns:
            True when the on-disk store was updated.
        """
        normalized = {str(item).strip() for item in user_ids if str(item).strip()}
        if not normalized:
            return False
        if self._super_admins == normalized and normalized <= self._admins:
            return False
        removed = self._super_admins - normalized
        self._super_admins = set(normalized)
        self._admins.update(normalized)
        self._admins.difference_update(removed)
        self.save()
        return True

    def list_admins(self) -> list[str]:
        return sorted(self._admins)

    def list_super_admins(self) -> list[str]:
        return sorted(self._super_admins)

    def is_admin(self, user_id: str) -> bool:
        uid = str(user_id or "").strip()
        return bool(uid and uid in self._admins)

    def is_super_admin(self, user_id: str) -> bool:
        uid = str(user_id or "").strip()
        return bool(uid and uid in self._super_admins)

    def add_admin(self, user_id: str) -> bool:
        uid = str(user_id or "").strip()
        if not uid:
            return False
        existed = uid in self._admins
        self._admins.add(uid)
        if not existed:
            self.save()
        return not existed

    def remove_admin(self, user_id: str) -> tuple[bool, str]:
        uid = str(user_id or "").strip()
        if not uid:
            return False, "ID 为空"
        if uid in self._super_admins:
            return False, "超级管理员不能用 deladmin 移除，请先手动调整 super_admins"
        if uid not in self._admins:
            return False, "该用户当前不是管理员"
        self._admins.discard(uid)
        self.save()
        return True, ""
