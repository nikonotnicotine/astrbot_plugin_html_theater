"""Persistent template, profile, play, retention, and backup storage."""

from __future__ import annotations

import base64
import copy
import io
import json
import re
import threading
import time
import unicodedata
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from .html_utils import extract_html_text, sanitize_html

SCHEMA_VERSION = 3
PANEL_THEMES = {"pink-white", "black-white", "blue-white", "gray-white"}
MAX_CUSTOM_PANEL_CSS_BYTES = 128 * 1024
MAX_BACKUP_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_HTML_FILENAME_STEM = 120
_STATE_LOCKS: dict[str, threading.RLock] = {}
_STATE_LOCKS_GUARD = threading.Lock()
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}

DEFAULT_TEMPLATES = [
    {
        "title": "假如是X上的网H博主小剧场",
        "prompt": (
            "{{char}}会想和{{user}}拍摄什么题材影片，粉丝受众都是哪些？"
            "如果要拍一部{{char}}最喜欢的play，{{char}}想用什么标题？"
            "会带什么tag？拍摄内容是什么呢？假如想要以"
            "【“乖巧的小狗喜欢daddy的🍆”】为标题，你会想怎么拍？"
        ),
    },
    {
        "title": "同人小剧场",
        "prompt": (
            "模仿网上火热的同人女风格，允许参考的页面："
            "bilibili混剪、lofter、AO3、微博、论坛。全文不少于2000字。"
        ),
    },
    {
        "title": "小红书小剧场",
        "prompt": (
            "模仿小红书发贴的内容，讨论{{char}}和{{user}}的感情、"
            "八卦等等，全文不少于2000字。"
        ),
    },
]


def unique_title(base_title: str, existing: set[str]) -> str:
    """Return a title that does not overwrite an existing title.

    Args:
        base_title: Preferred title.
        existing: Titles already in use.

    Returns:
        Preferred title or the first available numeric suffix variant.
    """
    title = str(base_title or "").strip()
    if title not in existing:
        return title
    match = re.fullmatch(r"(.*?)(\d+)", title)
    if match:
        root = match.group(1)
        index = int(match.group(2)) + 1
    else:
        root = title
        index = 1
    while f"{root}{index}" in existing:
        index += 1
    return f"{root}{index}"


def normalize_title(value: Any) -> str:
    """Normalize a user-facing title for stable command matching."""
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(normalized.split())


def safe_html_filename(
    title: str,
    play_id: str,
    existing_filenames: set[str] | None = None,
) -> str:
    """Build a human-readable Windows-safe HTML filename."""
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(title or "小剧场"))
    stem = re.sub(r"\s+", " ", stem).strip(" .")
    if not stem:
        stem = "小剧场"
    if stem.upper() in _WINDOWS_RESERVED_NAMES:
        stem = f"_{stem}"
    if len(stem) > MAX_HTML_FILENAME_STEM:
        stem = f"{stem[: MAX_HTML_FILENAME_STEM - 9].rstrip(' .')}-{play_id[:8]}"
    filename = f"{stem}.html"
    existing = {str(item).casefold() for item in (existing_filenames or set())}
    if filename.casefold() in existing:
        filename = f"{stem}-{play_id[:8]}.html"
    return filename


def _shared_state_lock(state_path: Path) -> threading.RLock:
    key = str(state_path.resolve()).casefold()
    with _STATE_LOCKS_GUARD:
        return _STATE_LOCKS.setdefault(key, threading.RLock())


def _redact_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    redacted = copy.deepcopy(snapshot or {})
    redacted.pop("conversation_context", None)
    return redacted


class TheaterStorage:
    """Manage all global theater state and HTML files."""

    def __init__(
        self,
        data_dir: Path,
        html_dir: Path,
        backup_dir: Path,
    ) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.html_dir = Path(html_dir).resolve()
        self.backup_dir = Path(backup_dir).resolve()
        self.state_path = self.data_dir / "state.json"
        self._lock = _shared_state_lock(self.state_path)
        self._state_mtime_ns = -1
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.html_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self.state = self._load_state()

    @staticmethod
    def _new_state() -> dict[str, Any]:
        now = time.time()
        return {
            "schema_version": SCHEMA_VERSION,
            "templates": [
                {
                    "id": uuid.uuid4().hex,
                    "title": item["title"],
                    "prompt": item["prompt"],
                    "created_at": now,
                    "updated_at": now,
                }
                for item in DEFAULT_TEMPLATES
            ],
            "plays": [],
            "profiles": {},
            "current_play_id": "",
            "panel_preferences": {
                "theme": "blue-white",
                "custom_css": "",
            },
            "revision": 0,
        }

    @staticmethod
    def _normalize_panel_preferences(value: Any) -> dict[str, str]:
        """Normalize persisted panel theme and custom CSS preferences."""
        source = value if isinstance(value, dict) else {}
        theme = str(source.get("theme", "blue-white") or "blue-white").strip()
        if theme not in PANEL_THEMES:
            theme = "blue-white"
        custom_css = str(source.get("custom_css", "") or "")
        if len(custom_css.encode("utf-8")) > MAX_CUSTOM_PANEL_CSS_BYTES:
            custom_css = custom_css.encode("utf-8")[:MAX_CUSTOM_PANEL_CSS_BYTES].decode(
                "utf-8", errors="ignore"
            )
        return {"theme": theme, "custom_css": custom_css}

    @staticmethod
    def _normalize_state(state: dict[str, Any]) -> bool:
        """Upgrade compatible state in place without guessing legacy play names."""
        before = json.dumps(state, ensure_ascii=False, sort_keys=True)
        state["schema_version"] = SCHEMA_VERSION
        state.setdefault("templates", [])
        state.setdefault("plays", [])
        state.setdefault("profiles", {})
        state.setdefault("current_play_id", "")
        state["panel_preferences"] = TheaterStorage._normalize_panel_preferences(
            state.get("panel_preferences")
        )
        state.setdefault("revision", 0)
        if not isinstance(state["templates"], list):
            state["templates"] = []
        if not isinstance(state["plays"], list):
            state["plays"] = []
        if not isinstance(state["profiles"], dict):
            state["profiles"] = {}
        now = time.time()
        for persona_id, profile in list(state["profiles"].items()):
            if not isinstance(profile, dict):
                state["profiles"].pop(persona_id, None)
                continue
            profile.setdefault("persona_id", str(persona_id))
            profile.setdefault("created_at", now)
            profile.setdefault("updated_at", profile["created_at"])
        for play in state["plays"]:
            if not isinstance(play, dict):
                continue
            snapshot = _redact_snapshot(play.get("snapshot"))
            play["snapshot"] = snapshot
            if snapshot.get("persona_id") and not play.get("persona_id"):
                play["persona_id"] = str(snapshot["persona_id"])
        after = json.dumps(state, ensure_ascii=False, sort_keys=True)
        return before != after

    def _load_state(self) -> dict[str, Any]:
        """Load and minimally normalize the persistent state file.

        Returns:
            Loaded state or a new state containing the default templates.
        """
        if not self.state_path.is_file():
            state = self._new_state()
            self._write_state(state)
            return state
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            state = self._new_state()
            self._write_state(state)
            return state
        if not isinstance(state, dict):
            state = self._new_state()
        changed = self._normalize_state(state)
        self.state = state
        if changed:
            self._write_state()
        else:
            self._state_mtime_ns = self.state_path.stat().st_mtime_ns
        return state

    def refresh(self, force: bool = False) -> bool:
        """Reload state when another panel or plugin instance changed it."""
        with self._lock:
            try:
                mtime_ns = self.state_path.stat().st_mtime_ns
            except OSError:
                return False
            if not force and mtime_ns == self._state_mtime_ns:
                return False
            try:
                state = json.loads(self.state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                return False
            if not isinstance(state, dict):
                return False
            changed = self._normalize_state(state)
            self.state = state
            if changed:
                self._write_state()
            else:
                self._state_mtime_ns = mtime_ns
            return True

    def _write_state(self, state: dict[str, Any] | None = None) -> None:
        """Atomically persist state JSON.

        Args:
            state: Optional replacement state. Defaults to the current state.
        """
        payload = state if state is not None else self.state
        payload["schema_version"] = SCHEMA_VERSION
        payload["revision"] = int(payload.get("revision", 0) or 0) + 1
        temporary = self.state_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.state_path)
        self.state = payload
        self._state_mtime_ns = self.state_path.stat().st_mtime_ns

    def public_state(self, query: str = "") -> dict[str, Any]:
        """Return panel-safe state with optional cross-library search.

        Args:
            query: Case-insensitive search term.

        Returns:
            Templates, plays, profiles, and current selection.
        """
        self.refresh()
        term = normalize_title(query).casefold()
        templates = copy.deepcopy(self.state["templates"])
        plays = copy.deepcopy(self.state["plays"])
        if term:
            templates = [
                item
                for item in templates
                if term in str(item.get("title", "")).casefold()
                or term in str(item.get("prompt", "")).casefold()
            ]
            plays = [
                item
                for item in plays
                if term in str(item.get("title", "")).casefold()
                or term in str(item.get("text", "")).casefold()
            ]
        plays.sort(key=lambda item: float(item.get("created_at", 0)), reverse=True)
        return {
            "schema_version": SCHEMA_VERSION,
            "templates": templates,
            "plays": plays,
            "profiles": copy.deepcopy(self.state["profiles"]),
            "current_play_id": self.state.get("current_play_id", ""),
            "panel_preferences": copy.deepcopy(self.state.get("panel_preferences", {})),
            "revision": int(self.state.get("revision", 0) or 0),
        }

    def save_panel_preferences(self, theme: str, custom_css: str) -> dict[str, str]:
        """Persist the panel theme and user-provided CSS."""
        preferences = self._normalize_panel_preferences(
            {"theme": theme, "custom_css": custom_css}
        )
        if str(theme or "").strip() not in PANEL_THEMES:
            raise ValueError("不支持的面板配色。")
        with self._lock:
            self.refresh()
            self.state["panel_preferences"] = preferences
            self._write_state()
            return copy.deepcopy(preferences)

    def get_template(self, title_or_id: str) -> dict[str, Any] | None:
        """Find a template by exact title or ID.

        Args:
            title_or_id: Template title or opaque identifier.

        Returns:
            A copied template record or None.
        """
        self.refresh()
        value = normalize_title(title_or_id)
        for item in self.state["templates"]:
            if item.get("id") == value or normalize_title(item.get("title")) == value:
                return copy.deepcopy(item)
        return None

    def save_template(
        self,
        title: str,
        prompt: str,
        template_id: str = "",
    ) -> dict[str, Any]:
        """Create or update a required-title-and-prompt template.

        Args:
            title: Requested template title.
            prompt: Required theater prompt.
            template_id: Existing template ID for updates.

        Returns:
            Saved template record.

        Raises:
            ValueError: If required fields are missing or an update ID is invalid.
        """
        title = normalize_title(title)
        prompt = str(prompt or "").strip()
        if not title or not prompt:
            raise ValueError("新增或修改小剧场时，标题与提示词都必须填写。")
        with self._lock:
            self.refresh()
            existing = {
                str(item.get("title", ""))
                for item in self.state["templates"]
                if item.get("id") != template_id
            }
            resolved_title = unique_title(title, existing)
            now = time.time()
            if template_id:
                for item in self.state["templates"]:
                    if item.get("id") == template_id:
                        item.update(
                            {
                                "title": resolved_title,
                                "prompt": prompt,
                                "updated_at": now,
                            }
                        )
                        self._write_state()
                        return copy.deepcopy(item)
                raise ValueError("要修改的小剧场模板不存在。")
            item = {
                "id": uuid.uuid4().hex,
                "title": resolved_title,
                "prompt": prompt,
                "created_at": now,
                "updated_at": now,
            }
            self.state["templates"].append(item)
            self._write_state()
            return copy.deepcopy(item)

    def delete_templates(self, template_ids: list[str]) -> int:
        """Delete selected templates.

        Args:
            template_ids: Opaque template IDs.

        Returns:
            Number of deleted templates.
        """
        with self._lock:
            self.refresh()
            selected = {str(item) for item in template_ids}
            before = len(self.state["templates"])
            self.state["templates"] = [
                item
                for item in self.state["templates"]
                if item.get("id") not in selected
            ]
            deleted = before - len(self.state["templates"])
            if deleted:
                self._write_state()
            return deleted

    def save_profile(
        self,
        persona_id: str,
        char_name: str,
        char_prompt: str,
        user_name: str,
        user_prompt: str,
    ) -> dict[str, Any]:
        """Save the panel persona/user override keyed by Persona ID.

        Args:
            persona_id: AstrBot Persona ID.
            char_name: Value used for the char variable.
            char_prompt: Optional replacement persona prompt.
            user_name: Optional value used for the user variable.
            user_prompt: Optional user persona content.

        Returns:
            Saved profile.

        Raises:
            ValueError: If Persona ID is empty.
        """
        persona_id = normalize_title(persona_id)
        if not persona_id:
            raise ValueError("Persona ID 不能为空。")
        with self._lock:
            self.refresh()
            existing = self.state["profiles"].get(persona_id, {})
            now = time.time()
            profile = {
                "persona_id": persona_id,
                "char_name": str(char_name or "").strip(),
                "char_prompt": str(char_prompt or "").strip(),
                "user_name": str(user_name or "").strip(),
                "user_prompt": str(user_prompt or "").strip(),
                "created_at": float(existing.get("created_at", now)),
                "updated_at": now,
            }
            self.state["profiles"][persona_id] = profile
            self._write_state()
            return copy.deepcopy(profile)

    def get_profile(self, persona_id: str) -> dict[str, Any]:
        """Return a configured profile or an empty one for a Persona ID.

        Args:
            persona_id: AstrBot Persona ID.

        Returns:
            Profile dictionary.
        """
        self.refresh()
        profile = self.state["profiles"].get(normalize_title(persona_id), {})
        return copy.deepcopy(profile) if isinstance(profile, dict) else {}

    def delete_profile(self, persona_id: str) -> bool:
        """Delete one page-configured Persona override."""
        key = normalize_title(persona_id)
        if not key:
            raise ValueError("Persona ID 不能为空。")
        with self._lock:
            self.refresh()
            if key not in self.state["profiles"]:
                return False
            del self.state["profiles"][key]
            self._write_state()
            return True

    def get_play(self, play_id: str) -> dict[str, Any] | None:
        """Return one play by opaque ID.

        Args:
            play_id: Play ID.

        Returns:
            Copied play record or None.
        """
        self.refresh()
        for item in self.state["plays"]:
            if item.get("id") == play_id:
                return copy.deepcopy(item)
        return None

    def read_play_html(self, play_id: str) -> str:
        """Read a play without allowing arbitrary path input.

        Args:
            play_id: Opaque play ID.

        Returns:
            Stored HTML text.

        Raises:
            FileNotFoundError: If the record or its HTML file is unavailable.
        """
        record = self.get_play(play_id)
        if record is None:
            raise FileNotFoundError("小剧场不存在。")
        filename = Path(str(record.get("filename", ""))).name
        candidate = (self.html_dir / filename).resolve()
        candidate.relative_to(self.html_dir)
        if not candidate.is_file():
            raise FileNotFoundError("小剧场 HTML 文件不存在。")
        return candidate.read_text(encoding="utf-8")

    def current_play(self) -> dict[str, Any] | None:
        """Return the currently selected play, falling back to the latest."""
        current_id = str(self.state.get("current_play_id", ""))
        current = self.get_play(current_id) if current_id else None
        if current:
            return current
        plays = sorted(
            self.state["plays"],
            key=lambda item: float(item.get("created_at", 0)),
            reverse=True,
        )
        return copy.deepcopy(plays[0]) if plays else None

    def add_play(
        self,
        base_title: str,
        html: str,
        snapshot: dict[str, Any],
        *,
        source_play_id: str = "",
        base_play_id: str = "",
        series_no: int | None = None,
        chapter: int | None = None,
        explicit_title: str = "",
    ) -> dict[str, Any]:
        """Persist a generated or continued play without overwriting.

        Args:
            base_title: Root template title for collision numbering.
            html: Sanitized complete HTML.
            snapshot: Resolved generation request snapshot.
            source_play_id: Optional continuation source.
            series_no: Optional continuation series number.
            chapter: Optional chapter number.
            explicit_title: Precomputed continuation title.

        Returns:
            Saved play record.
        """
        with self._lock:
            self.refresh()
            play_id = uuid.uuid4().hex
            existing_titles = {
                str(item.get("title", "")) for item in self.state["plays"]
            }
            if explicit_title:
                title = unique_title(str(explicit_title), existing_titles)
            else:
                persona_id = normalize_title(snapshot.get("persona_id")) or "default"
                template_title = normalize_title(
                    snapshot.get("template_title") or base_title or "小剧场"
                )
                numbered_base = f"{persona_id}{template_title}"
                pattern = re.compile(rf"^{re.escape(numbered_base)}(\d+)$")
                numbered_titles = existing_titles | {
                    str(item.get("base_title", "")) for item in self.state["plays"]
                }
                used = [
                    int(match.group(1))
                    for item in numbered_titles
                    if (match := pattern.fullmatch(item))
                ]
                title = f"{numbered_base}{max(used, default=0) + 1}"
            existing_filenames = {
                str(item.get("filename", "")) for item in self.state["plays"]
            }
            filename = safe_html_filename(title, play_id, existing_filenames)
            path = self.html_dir / filename
            temporary = path.with_suffix(".html.tmp")
            temporary.write_text(html, encoding="utf-8")
            temporary.replace(path)
            now = time.time()
            lineage_base = next(
                (
                    item
                    for item in self.state["plays"]
                    if str(item.get("id", "")) == str(base_play_id)
                ),
                None,
            )
            lineage_title = (
                str(lineage_base.get("base_title") or lineage_base.get("title"))
                if lineage_base
                else title
            )
            record = {
                "id": play_id,
                "title": title,
                "root_title": str(snapshot.get("root_title") or base_title),
                "template_title": str(snapshot.get("template_title") or base_title),
                "template_prompt": str(snapshot.get("template_prompt", "")),
                "persona_id": str(snapshot.get("persona_id", "")),
                "filename": filename,
                "text": extract_html_text(html),
                "favorite": False,
                "created_at": now,
                "source_play_id": source_play_id,
                "base_play_id": base_play_id or play_id,
                "base_title": lineage_title,
                "series_no": series_no,
                "chapter": chapter,
                "snapshot": _redact_snapshot(snapshot),
            }
            self.state["plays"].append(record)
            self.state["current_play_id"] = play_id
            self._write_state()
            return copy.deepcopy(record)

    def continuation_identity(self, source: dict[str, Any]) -> tuple[str, int, str]:
        """Resolve the linear base play, next chapter, and deterministic title.

        Args:
            source: Selected source play record.

        Returns:
            Base play ID, chapter number, and output title.
        """
        self.refresh()
        source_id = str(source.get("id", ""))
        base_play_id = str(source.get("base_play_id") or source_id)
        base = next(
            (
                item
                for item in self.state["plays"]
                if str(item.get("id", "")) == base_play_id
            ),
            source,
        )
        base_title = str(
            source.get("base_title")
            or base.get("base_title")
            or base.get("title")
            or source.get("title")
            or "小剧场"
        )
        chapters = [
            int(item.get("chapter") or 0)
            for item in self.state["plays"]
            if str(item.get("base_play_id", "")) == base_play_id
            and item.get("chapter") is not None
        ]
        chapter = max(chapters, default=0) + 1
        existing = {str(item.get("title", "")) for item in self.state["plays"]}
        title = f"{base_title}-chapter{chapter}"
        while title in existing:
            chapter += 1
            title = f"{base_title}-chapter{chapter}"
        return base_play_id, chapter, title

    def set_favorite(self, play_id: str, favorite: bool) -> dict[str, Any]:
        """Update one favorite flag.

        Args:
            play_id: Play ID.
            favorite: Desired favorite state.

        Returns:
            Updated play.

        Raises:
            ValueError: If the play is missing.
        """
        with self._lock:
            self.refresh()
            for item in self.state["plays"]:
                if item.get("id") == play_id:
                    item["favorite"] = bool(favorite)
                    self._write_state()
                    return copy.deepcopy(item)
            raise ValueError("小剧场不存在。")

    def select_play(self, play_id: str) -> dict[str, Any]:
        """Select the play served at the public Web root.

        Args:
            play_id: Play ID.

        Returns:
            Selected play.

        Raises:
            ValueError: If the play is missing.
        """
        with self._lock:
            self.refresh()
            play = next(
                (item for item in self.state["plays"] if item.get("id") == play_id),
                None,
            )
            if play is None:
                raise ValueError("小剧场不存在。")
            self.state["current_play_id"] = play_id
            self._write_state()
            return copy.deepcopy(play)

    def delete_plays(self, play_ids: list[str]) -> int:
        """Delete selected plays and their owned HTML files.

        Args:
            play_ids: Opaque play IDs.

        Returns:
            Number of deleted records.
        """
        with self._lock:
            self.refresh()
            selected = {str(item) for item in play_ids}
            removed = [
                item for item in self.state["plays"] if item.get("id") in selected
            ]
            if not removed:
                return 0
            self.state["plays"] = [
                item for item in self.state["plays"] if item.get("id") not in selected
            ]
            if self.state.get("current_play_id") in selected:
                latest = sorted(
                    self.state["plays"],
                    key=lambda item: float(item.get("created_at", 0)),
                    reverse=True,
                )
                self.state["current_play_id"] = latest[0]["id"] if latest else ""
            self._write_state()
            for item in removed:
                path = self.html_dir / Path(str(item.get("filename", ""))).name
                if path.is_file():
                    path.unlink()
            return len(removed)

    def enforce_retention(self, limit: int) -> list[str]:
        """Delete oldest non-favorite plays until the configured limit is met.

        Args:
            limit: Maximum total play count before favorite protection.

        Returns:
            Deleted play IDs.
        """
        with self._lock:
            self.refresh()
            limit = max(1, int(limit or 1))
            removed_ids: list[str] = []
            while len(self.state["plays"]) > limit:
                candidates = sorted(
                    (
                        item
                        for item in self.state["plays"]
                        if not bool(item.get("favorite"))
                    ),
                    key=lambda item: float(item.get("created_at", 0)),
                )
                if not candidates:
                    break
                removed_ids.append(str(candidates[0]["id"]))
                self.delete_plays([str(candidates[0]["id"])])
            return removed_ids

    def export_backup(self, save_server_copy: bool = False) -> bytes:
        """Create a complete page-data ZIP without AstrBot plugin secrets.

        Args:
            save_server_copy: Whether to also persist the ZIP in the backup directory.

        Returns:
            ZIP bytes containing the manifest and all referenced HTML files.
        """
        self.refresh()
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "exported_at": time.time(),
            "templates": copy.deepcopy(self.state["templates"]),
            "plays": [],
            "profiles": copy.deepcopy(self.state["profiles"]),
            "current_play_id": self.state.get("current_play_id", ""),
            "panel_preferences": copy.deepcopy(self.state.get("panel_preferences", {})),
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for play in self.state["plays"]:
                html = self.read_play_html(str(play["id"]))
                backup_name = f"html/{play['id']}.html"
                item = copy.deepcopy(play)
                item["backup_file"] = backup_name
                item.pop("filename", None)
                manifest["plays"].append(item)
                archive.writestr(backup_name, html)
            archive.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )
        payload = buffer.getvalue()
        if save_server_copy:
            timestamp = time.strftime("%Y%m%d-%H%M%S")
            (self.backup_dir / f"html_theater_backup_{timestamp}.zip").write_bytes(
                payload
            )
        return payload

    @staticmethod
    def decode_backup_base64(value: str) -> bytes:
        """Decode a JSON-safe backup upload.

        Args:
            value: Base64-encoded ZIP.

        Returns:
            ZIP bytes.

        Raises:
            ValueError: If the data is invalid.
        """
        try:
            return base64.b64decode(str(value or ""), validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("备份文件不是有效的 Base64 数据。") from exc

    def _validate_backup(self, payload: bytes) -> tuple[dict[str, Any], dict[str, str]]:
        """Validate a ZIP and sanitize every imported HTML document.

        Args:
            payload: Uploaded ZIP bytes.

        Returns:
            Validated manifest and mapping of backup path to sanitized HTML.

        Raises:
            ValueError: If ZIP paths, size, manifest, or HTML are invalid.
        """
        try:
            archive = zipfile.ZipFile(io.BytesIO(payload), "r")
        except zipfile.BadZipFile as exc:
            raise ValueError("导入文件不是有效的小剧场 ZIP 备份。") from exc
        with archive:
            total_size = 0
            for info in archive.infolist():
                name = info.filename
                path = PurePosixPath(name)
                if not name or "\\" in name or path.is_absolute() or ".." in path.parts:
                    raise ValueError("备份中包含不安全的文件路径。")
                total_size += int(info.file_size)
                if total_size > MAX_BACKUP_UNCOMPRESSED_BYTES:
                    raise ValueError("备份解压后的总大小超过 100MB。")
            try:
                manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            except (KeyError, UnicodeDecodeError, ValueError, TypeError) as exc:
                raise ValueError("备份缺少有效的 manifest.json。") from exc
            if not isinstance(manifest, dict) or manifest.get("schema_version") not in {
                1,
                2,
                SCHEMA_VERSION,
            }:
                raise ValueError("备份版本不受支持。")
            if not isinstance(manifest.get("templates"), list) or not isinstance(
                manifest.get("plays"), list
            ):
                raise ValueError("备份目录结构无效。")
            html_files: dict[str, str] = {}
            for play in manifest["plays"]:
                if not isinstance(play, dict):
                    raise ValueError("备份中的小剧场索引无效。")
                backup_file = str(play.get("backup_file", ""))
                if not re.fullmatch(r"html/[a-zA-Z0-9_-]+\.html", backup_file):
                    raise ValueError("备份中的 HTML 引用路径无效。")
                try:
                    raw_html = archive.read(backup_file).decode("utf-8")
                except (KeyError, UnicodeDecodeError) as exc:
                    raise ValueError("备份缺少索引引用的 HTML 文件。") from exc
                try:
                    html_files[backup_file] = sanitize_html(raw_html)
                except ValueError as exc:
                    raise ValueError(f"备份 HTML 无效：{backup_file}") from exc
        return manifest, html_files

    def import_backup(self, payload: bytes, mode: str = "merge") -> dict[str, int]:
        """Import a full page-data backup in merge or replacement mode.

        Args:
            payload: Uploaded ZIP bytes.
            mode: "merge" to preserve current data or "replace" for exact restore.

        Returns:
            Imported template and play counts.

        Raises:
            ValueError: If mode or backup content is invalid.
        """
        if mode not in {"merge", "replace"}:
            raise ValueError("导入模式必须是 merge 或 replace。")
        self.refresh()
        manifest, html_files = self._validate_backup(payload)
        old_play_files = {
            Path(str(item.get("filename", ""))).name for item in self.state["plays"]
        }

        if mode == "replace":
            self.export_backup(save_server_copy=True)
            new_state = {
                "schema_version": SCHEMA_VERSION,
                "templates": [],
                "plays": [],
                "profiles": copy.deepcopy(manifest.get("profiles", {})),
                "current_play_id": "",
                "panel_preferences": self._normalize_panel_preferences(
                    manifest.get("panel_preferences")
                ),
                "revision": 0,
            }
            template_titles: set[str] = set()
            for source in manifest["templates"]:
                title = str(source.get("title", "")).strip()
                prompt = str(source.get("prompt", "")).strip()
                if not title or not prompt:
                    raise ValueError("备份中存在缺少标题或提示词的模板。")
                item = copy.deepcopy(source)
                item["id"] = str(item.get("id") or uuid.uuid4().hex)
                item["title"] = unique_title(title, template_titles)
                template_titles.add(item["title"])
                new_state["templates"].append(item)
            id_map: dict[str, str] = {}
            play_titles: set[str] = set()
            play_filenames: set[str] = set()
            for source in manifest["plays"]:
                old_id = str(source.get("id") or uuid.uuid4().hex)
                new_id = old_id if old_id not in id_map.values() else uuid.uuid4().hex
                id_map[old_id] = new_id
                item = copy.deepcopy(source)
                item.pop("backup_file", None)
                item["id"] = new_id
                item["title"] = unique_title(
                    str(item.get("title") or "小剧场"), play_titles
                )
                play_titles.add(item["title"])
                item["snapshot"] = _redact_snapshot(item.get("snapshot"))
                item["filename"] = safe_html_filename(
                    item["title"], new_id, play_filenames
                )
                play_filenames.add(item["filename"])
                html = html_files[str(source["backup_file"])]
                item["text"] = extract_html_text(html)
                (self.html_dir / item["filename"]).write_text(html, encoding="utf-8")
                new_state["plays"].append(item)
            for item in new_state["plays"]:
                source_id = str(item.get("source_play_id", ""))
                base_id = str(item.get("base_play_id", ""))
                if source_id in id_map:
                    item["source_play_id"] = id_map[source_id]
                if base_id in id_map:
                    item["base_play_id"] = id_map[base_id]
            requested_current = str(manifest.get("current_play_id", ""))
            new_state["current_play_id"] = id_map.get(
                requested_current,
                new_state["plays"][-1]["id"] if new_state["plays"] else "",
            )
            self._normalize_state(new_state)
            self._write_state(new_state)
            referenced = {item["filename"] for item in new_state["plays"]}
            for filename in old_play_files - referenced:
                path = self.html_dir / filename
                if path.is_file():
                    path.unlink()
            return {
                "templates": len(new_state["templates"]),
                "plays": len(new_state["plays"]),
            }

        imported_templates = 0
        template_titles = {
            str(item.get("title", "")) for item in self.state["templates"]
        }
        for source in manifest["templates"]:
            title = str(source.get("title", "")).strip()
            prompt = str(source.get("prompt", "")).strip()
            if not title or not prompt:
                raise ValueError("备份中存在缺少标题或提示词的模板。")
            now = time.time()
            resolved = unique_title(title, template_titles)
            template_titles.add(resolved)
            self.state["templates"].append(
                {
                    "id": uuid.uuid4().hex,
                    "title": resolved,
                    "prompt": prompt,
                    "created_at": float(source.get("created_at", now)),
                    "updated_at": now,
                }
            )
            imported_templates += 1

        for persona_id, profile in manifest.get("profiles", {}).items():
            if persona_id not in self.state["profiles"] and isinstance(profile, dict):
                self.state["profiles"][persona_id] = copy.deepcopy(profile)

        if "panel_preferences" in manifest:
            self.state["panel_preferences"] = self._normalize_panel_preferences(
                manifest.get("panel_preferences")
            )

        play_titles = {str(item.get("title", "")) for item in self.state["plays"]}
        play_filenames = {str(item.get("filename", "")) for item in self.state["plays"]}
        imported_plays = 0
        imported_current = ""
        requested_current = str(manifest.get("current_play_id", ""))
        old_to_new: dict[str, str] = {}
        for source in manifest["plays"]:
            new_id = uuid.uuid4().hex
            old_to_new[str(source.get("id", ""))] = new_id
            title = unique_title(str(source.get("title") or "小剧场"), play_titles)
            play_titles.add(title)
            html = html_files[str(source["backup_file"])]
            filename = safe_html_filename(title, new_id, play_filenames)
            play_filenames.add(filename)
            (self.html_dir / filename).write_text(html, encoding="utf-8")
            item = copy.deepcopy(source)
            item.pop("backup_file", None)
            item.update(
                {
                    "id": new_id,
                    "title": title,
                    "filename": filename,
                    "text": extract_html_text(html),
                    "snapshot": _redact_snapshot(item.get("snapshot")),
                }
            )
            self.state["plays"].append(item)
            imported_plays += 1
            if str(source.get("id", "")) == requested_current:
                imported_current = new_id
        for item in self.state["plays"][-imported_plays:]:
            source_id = str(item.get("source_play_id", ""))
            base_id = str(item.get("base_play_id", ""))
            if source_id in old_to_new:
                item["source_play_id"] = old_to_new[source_id]
            if base_id in old_to_new:
                item["base_play_id"] = old_to_new[base_id]
        if imported_current:
            self.state["current_play_id"] = imported_current
        elif self.state["plays"]:
            self.state["current_play_id"] = self.state["plays"][-1]["id"]
        self._normalize_state(self.state)
        self._write_state()
        return {"templates": imported_templates, "plays": imported_plays}
