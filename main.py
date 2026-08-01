"""AstrBot HTML theater plugin entry point."""

import asyncio
import json
import logging
import random
import re
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.star.filter.command import GreedyStr

from .generator import (
    TheaterApiError,
    TheaterGenerator,
    build_continuation_messages,
    build_generation_messages,
)
from .html_utils import extract_html_text, extract_image_urls, normalize_image_urls
from .storage import TheaterStorage, normalize_title
from .web_server import TheaterWebServer

PLUGIN_NAME = "astrbot_plugin_html_theater"
DEFAULT_LLM_INJECTION_PROMPT = (
    "[系统提示]这是你的真实经历与内容，请根据你当前人设，直接自然回应用户。"
)
REQUESTED_BACKUP_DIR = Path(r"F:\qq\AstrBotLauncher-0.3.0\给gpt进行备份的")
THEATER_COMMAND_PATTERN = re.compile(
    r"^\s*/?(?:生成小剧场|临时小剧场|小剧场目录|小剧场|生成随机小剧场|查看小剧场)(?:\s|$)"
)


@register(
    PLUGIN_NAME,
    "nikonotnicotine",
    "生成、保存和展示 HTML 小剧场，并提供模板、续写、人设与备份面板。",
    "1.3.2",
)
class HtmlTheaterPlugin(Star):
    """Generate HTML theaters with a separate API and react in the active persona."""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.context = context
        self.config = config
        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        configured_html = str(config.get("html_save_path", "") or "").strip()
        if configured_html:
            candidate = Path(configured_html)
            self.html_dir = (
                candidate if candidate.is_absolute() else self.data_dir / candidate
            )
        else:
            self.html_dir = self.data_dir / "html"
        backup_dir = REQUESTED_BACKUP_DIR
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            backup_dir = self.data_dir / "backups"
        self.storage = TheaterStorage(self.data_dir, self.html_dir, backup_dir)
        self.http_session: aiohttp.ClientSession | None = None
        self.web_server: TheaterWebServer | None = None
        self.web_retry_task: asyncio.Task | None = None
        self.generation_lock = asyncio.Lock()
        self.last_requests: dict[str, dict[str, Any]] = {}
        self.pending_reactions: dict[str, dict[str, Any]] = {}
        from .web_api import TheaterWebApi

        TheaterWebApi(self).register_routes()

    @staticmethod
    def _value(source: Any, key: str, default: Any = None) -> Any:
        if isinstance(source, dict):
            return source.get(key, default)
        return getattr(source, key, default)

    def _config(self, key: str, default: Any) -> Any:
        return self.config.get(key, default) if hasattr(self.config, "get") else default

    def _debug(self, message: str) -> None:
        """Write plugin-local diagnostics only when explicitly enabled."""
        if bool(self._config("debug_enabled", False)):
            record = logger.makeRecord(
                logger.name,
                logging.DEBUG,
                __file__,
                0,
                "[HTML Theater] %s",
                (message,),
                None,
            )
            logger.handle(record)

    def _new_web_server(self) -> TheaterWebServer:
        """Build an independent Web server from current plugin config."""
        return TheaterWebServer(
            self.storage,
            str(self._config("web_host", "127.0.0.1")),
            int(self._config("web_port", 7315) or 7315),
            str(self._config("web_password", "") or ""),
            self._debug,
        )

    async def _retry_web_server_start(self) -> None:
        """Retry after hot reload releases the old plugin's listening socket."""
        for attempt in range(1, 4):
            await asyncio.sleep(2)
            candidate = self._new_web_server()
            try:
                await candidate.start()
            except OSError as exc:
                self._debug(f"Web retry attempt={attempt}/3 failed error={exc!s}")
                continue
            self.web_server = candidate
            logger.info(
                "[HTML Theater] Web server started after hot-reload retry at %s:%s",
                self._config("web_host", "127.0.0.1"),
                self._config("web_port", 7315),
            )
            return
        logger.error(
            "[HTML Theater] Web server still cannot bind at %s:%s after 3 retries",
            self._config("web_host", "127.0.0.1"),
            self._config("web_port", 7315),
        )

    async def initialize(self) -> None:
        """Initialize the shared HTTP session and optional independent Web server."""
        if self.http_session is None or self.http_session.closed:
            self.http_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=300)
            )
        if bool(self._config("web_enabled", False)):
            candidate = self._new_web_server()
            try:
                await candidate.start()
            except OSError as exc:
                self.web_server = None
                error_code = getattr(exc, "winerror", None) or getattr(
                    exc, "errno", None
                )
                if error_code == 10048:
                    logger.warning(
                        "[HTML Theater] Web port is held during hot reload; "
                        "retrying in background"
                    )
                    self.web_retry_task = asyncio.create_task(
                        self._retry_web_server_start()
                    )
                else:
                    logger.error(
                        "[HTML Theater] Web server failed to start at %s:%s: %s",
                        self._config("web_host", "127.0.0.1"),
                        self._config("web_port", 7315),
                        exc,
                    )
            else:
                self.web_server = candidate
        logger.info(
            "[HTML Theater] initialized | context=%s reaction=%s web=%s",
            bool(self._config("inject_conversation_context", False)),
            bool(self._config("inject_after_generation", False)),
            bool(self.web_server),
        )

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        """Log that the plugin has completed AstrBot startup loading."""
        logger.info(
            "[HTML Theater] loaded | templates=%s plays=%s web=%s",
            len(self.storage.state["templates"]),
            len(self.storage.state["plays"]),
            bool(self.web_server),
        )

    async def terminate(self) -> None:
        """Release the independent server and API client session."""
        if self.web_retry_task is not None:
            self.web_retry_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.web_retry_task
            self.web_retry_task = None
        if self.web_server is not None:
            await self.web_server.stop()
            self.web_server = None
        if self.http_session is not None and not self.http_session.closed:
            await self.http_session.close()
        self.http_session = None
        self.pending_reactions.clear()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Return an initialized HTTP session.

        Returns:
            Shared aiohttp client session.
        """
        if self.http_session is None or self.http_session.closed:
            self.http_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=300)
            )
        return self.http_session

    async def _generator(self) -> TheaterGenerator:
        """Build a generator from the latest mutable plugin configuration.

        Returns:
            Configured theater API client.
        """
        return TheaterGenerator(
            await self._ensure_session(),
            str(self._config("api_url", "") or ""),
            str(self._config("api_key", "") or ""),
            str(self._config("model", "") or ""),
            bool(self._config("continue_on_empty", True)),
            self._debug,
        )

    def _whitelist_error(self, event: AstrMessageEvent) -> str:
        """Return an access error or an empty string for an allowed QQ sender.

        Args:
            event: Current AstrBot message event.

        Returns:
            User-facing error string, or an empty string when allowed.
        """
        configured = self._config("allowed_qq_ids", [])
        if isinstance(configured, str):
            raw_ids = configured.replace("，", ",").split(",")
        elif isinstance(configured, list):
            raw_ids = configured
        else:
            raw_ids = []
        allowed = {str(item).strip() for item in raw_ids if str(item).strip().isdigit()}
        if not allowed:
            return "小剧场插件尚未配置 QQ 白名单，请联系管理员。"
        sender_id = str(event.get_sender_id() or "").strip()
        if sender_id not in allowed:
            return "你不在小剧场插件的 QQ 白名单中，无法使用该指令。"
        return ""

    @staticmethod
    def _event_key(event: AstrMessageEvent) -> str:
        """Build a stable key for the generated reaction request.

        Args:
            event: Current message event.

        Returns:
            Event-scoped key.
        """
        message_obj = getattr(event, "message_obj", None)
        message_id = str(getattr(message_obj, "message_id", "") or "")
        return (
            message_id
            or f"{event.unified_msg_origin}:{event.get_sender_id()}:{id(event)}"
        )

    async def _conversation_and_persona(
        self, event: AstrMessageEvent
    ) -> tuple[Any, str, str, str, list[dict[str, Any]]]:
        """Resolve the active conversation and its selected AstrBot Persona.

        Args:
            event: Current message event.

        Returns:
            Conversation, Persona ID, Persona name, prompt, and begin dialogs.
        """
        conversation = None
        manager = getattr(self.context, "conversation_manager", None)
        if manager is not None:
            try:
                cid = await manager.get_curr_conversation_id(event.unified_msg_origin)
                if cid:
                    conversation = await manager.get_conversation(
                        event.unified_msg_origin, str(cid)
                    )
            except Exception as exc:
                logger.warning("[HTML Theater] unable to resolve conversation: %s", exc)

        conversation_persona_id = (
            str(getattr(conversation, "persona_id", "") or "") if conversation else ""
        )
        persona = None
        resolved_id = conversation_persona_id
        persona_manager = getattr(self.context, "persona_manager", None)
        if persona_manager is not None:
            try:
                provider_settings: dict[str, Any] = {}
                cfg = self.context.get_config(umo=event.unified_msg_origin)
                if hasattr(cfg, "get"):
                    provider_settings = cfg.get("provider_settings", {}) or {}
                if hasattr(persona_manager, "resolve_selected_persona"):
                    (
                        resolved_id,
                        persona,
                        _,
                        _,
                    ) = await persona_manager.resolve_selected_persona(
                        umo=event.unified_msg_origin,
                        conversation_persona_id=conversation_persona_id or None,
                        platform_name=event.get_platform_name(),
                        provider_settings=provider_settings,
                    )
                if (
                    persona is None
                    and conversation_persona_id
                    and hasattr(persona_manager, "get_persona_v3_by_id")
                ):
                    persona = persona_manager.get_persona_v3_by_id(
                        conversation_persona_id
                    )
            except Exception as exc:
                logger.warning("[HTML Theater] unable to resolve persona: %s", exc)

        persona_id = str(
            resolved_id
            or conversation_persona_id
            or self._value(persona, "persona_id", "")
            or self._value(persona, "name", "")
            or "default"
        )
        persona_name = str(
            self._value(persona, "name", "")
            or self._value(persona, "persona_id", "")
            or persona_id
        )
        persona_prompt = str(
            self._value(persona, "prompt", "")
            or self._value(persona, "system_prompt", "")
            or ""
        )
        begin_dialogs = self._value(persona, "_begin_dialogs_processed", [])
        if not isinstance(begin_dialogs, list):
            begin_dialogs = []
        return (
            conversation,
            persona_id,
            persona_name,
            persona_prompt,
            list(begin_dialogs),
        )

    @staticmethod
    def _conversation_history(conversation: Any) -> list[dict[str, Any]]:
        """Normalize a Conversation history field into a mutable list.

        Args:
            conversation: AstrBot Conversation object.

        Returns:
            Parsed history list.
        """
        if conversation is None:
            return []
        history = getattr(conversation, "history", [])
        if isinstance(history, list):
            return list(history)
        try:
            parsed = json.loads(str(history or "[]"))
        except (TypeError, ValueError):
            return []
        return list(parsed) if isinstance(parsed, list) else []

    @staticmethod
    def _message_text(content: Any) -> str:
        """Extract plain text from a stored AstrBot message payload."""
        if isinstance(content, str):
            return content.strip()
        if not isinstance(content, list):
            return ""
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif item.get("type") in {
                "text",
                "input_text",
                "output_text",
            } and isinstance(item.get("content"), str):
                parts.append(item["content"])
        return "\n".join(parts).strip()

    def _generation_context(self, conversation: Any) -> list[dict[str, str]]:
        """Return the latest 20 ordinary chat messages for HTML generation."""
        if not bool(self._config("inject_conversation_context", False)):
            return []
        contexts: list[dict[str, str]] = []
        for item in self._conversation_history(conversation):
            if not isinstance(item, dict):
                continue
            role = str(item.get("role", ""))
            if role not in {"user", "assistant"}:
                continue
            content = self._message_text(item.get("content"))
            if not content or (
                role == "user" and THEATER_COMMAND_PATTERN.match(content)
            ):
                continue
            contexts.append({"role": role, "content": content})
        selected = contexts[-20:]
        self._debug(f"conversation context selected messages={len(selected)}")
        return selected

    async def _recent_memory_context(
        self,
        event: AstrMessageEvent,
        persona_id: str,
    ) -> str:
        """Read recent records from the optional Romantic Memory plugin.

        Args:
            event: Current command event.
            persona_id: Persona selected for the current conversation.

        Returns:
            Formatted recent memory context, or an empty string when unavailable.
        """
        if not bool(self._config("inject_memory_and_diary", False)):
            return ""
        try:
            get_registered_star = getattr(self.context, "get_registered_star", None)
            metadata = (
                get_registered_star("astrbot_plugin_romantic_memory")
                if callable(get_registered_star)
                else None
            )
            memory_plugin = getattr(metadata, "star_cls", None)
            if memory_plugin is None:
                return ""

            config = getattr(memory_plugin, "config", {})
            keep_days = int(config.get("context_keep_limit", 5) or 0)
            if keep_days == 0:
                return ""
            sessions = getattr(memory_plugin, "sessions", None)
            store = getattr(memory_plugin, "store", None)
            if sessions is None or store is None:
                return ""

            session_id = sessions.key(str(event.unified_msg_origin))
            records = await asyncio.to_thread(
                store.list_records,
                session_id,
                persona_id,
            )
            cutoff = time.time() - keep_days * 86400 if keep_days > 0 else None
            recent_records: list[dict[str, Any]] = []
            for record in records:
                content = str(record.get("content", "") or "").strip()
                if not content:
                    continue
                if cutoff is not None:
                    try:
                        timestamp = float(record.get("timestamp", 0) or 0)
                    except (TypeError, ValueError):
                        continue
                    if timestamp < cutoff:
                        continue
                recent_records.append(record)

            if not recent_records:
                return ""
            recent_records.sort(
                key=lambda item: float(item.get("timestamp", 0) or 0),
                reverse=True,
            )
            lines = [
                "- {} | {}".format(
                    str(item.get("date", "unknown date") or "unknown date"),
                    str(item.get("content", "") or "").strip(),
                )
                for item in recent_records
            ]
            self._debug(
                "recent romantic memory selected "
                f"session={session_id!r} persona={persona_id!r} "
                f"keep_days={keep_days} records={len(lines)}"
            )
            return "Recent relationship memories (facts only):\n" + "\n".join(lines)
        except Exception as exc:
            logger.warning("[HTML Theater] recent memory lookup skipped: %s", exc)
            return ""

    async def build_snapshot(
        self,
        event: AstrMessageEvent,
        template: dict[str, Any],
    ) -> tuple[dict[str, Any], Any, list[dict[str, Any]]]:
        """Resolve variables and persona overrides into a retry-safe snapshot.

        Args:
            event: Current command event.
            template: Selected template record.

        Returns:
            Snapshot, Conversation, and persona begin dialogs.
        """
        (
            conversation,
            persona_id,
            persona_name,
            persona_prompt,
            begin_dialogs,
        ) = await self._conversation_and_persona(event)
        profile = self.storage.get_profile(persona_id)
        char_name = str(profile.get("char_name") or persona_name or persona_id)
        user_name = str(
            profile.get("user_name")
            or event.get_sender_name()
            or event.get_sender_id()
            or "User"
        )
        replacements = {"{{char}}": char_name, "{{user}}": user_name}

        def resolve(value: Any) -> str:
            text = str(value or "")
            for marker, replacement in replacements.items():
                text = text.replace(marker, replacement)
            return text

        template_prompt = resolve(template["prompt"])
        snapshot = {
            "root_title": str(template["title"]),
            "template_title": str(template["title"]),
            "template_prompt": template_prompt,
            "image_urls": extract_image_urls(template_prompt),
            "system_prompt": str(
                self._config("theater_system_prompt", "") or ""
            ).strip(),
            "style_prompt": str(self._config("style_prompt", "") or "").strip(),
            "persona_id": persona_id,
            "char_name": char_name,
            "persona_prompt": resolve(
                profile.get("char_prompt") or persona_prompt
            ).strip(),
            "user_name": user_name,
            "user_prompt": resolve(profile.get("user_prompt", "")).strip(),
            "conversation_context": self._generation_context(conversation),
            "memory_context": await self._recent_memory_context(event, persona_id),
        }
        self._debug(
            "snapshot resolved "
            f"persona={persona_id!r} template={template['title']!r} "
            f"profile_override={bool(profile.get('char_prompt'))}"
        )
        return snapshot, conversation, begin_dialogs

    async def generate_snapshot(
        self,
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        """Generate and persist one normal or retry play.

        Args:
            snapshot: Retry-safe resolved request.

        Returns:
            Saved play record.
        """
        async with self.generation_lock:
            generator = await self._generator()
            html = await generator.generate(
                build_generation_messages(snapshot),
                snapshot.get("image_urls", []),
            )
            play = self.storage.add_play(
                str(snapshot.get("root_title") or "小剧场"),
                html,
                snapshot,
            )
            self.storage.enforce_retention(int(self._config("retention_limit", 6) or 6))
            logger.info(
                "[HTML Theater] generated | persona=%s template=%s title=%s",
                snapshot.get("persona_id", "default"),
                snapshot.get("template_title", ""),
                play["title"],
            )
            return play

    async def generate_continuation_from_panel(
        self,
        source_play_id: str,
        continuation_prompt: str,
    ) -> dict[str, Any]:
        """Generate a panel-only continuation without any chat injection.

        Args:
            source_play_id: Selected source play ID.
            continuation_prompt: Required continuation instruction.

        Returns:
            Saved continuation play.

        Raises:
            ValueError: If source or prompt is missing.
        """
        continuation_prompt = str(continuation_prompt or "").strip()
        if not continuation_prompt:
            raise ValueError("续写提示词不能为空。")
        async with self.generation_lock:
            source = self.storage.get_play(str(source_play_id or ""))
            if source is None:
                raise ValueError("要续写的小剧场不存在。")
            source_html = self.storage.read_play_html(str(source["id"]))
            snapshot = dict(source.get("snapshot") or {})
            snapshot.pop("conversation_context", None)
            if not snapshot:
                snapshot = {
                    "root_title": source.get("root_title") or source.get("title"),
                    "template_title": source.get("template_title")
                    or source.get("title"),
                    "template_prompt": source.get("template_prompt", ""),
                    "image_urls": extract_image_urls(source.get("template_prompt", "")),
                    "system_prompt": str(
                        self._config("theater_system_prompt", "") or ""
                    ),
                    "style_prompt": str(self._config("style_prompt", "") or ""),
                    "char_name": "Char",
                    "user_name": "User",
                    "persona_prompt": "",
                    "user_prompt": "",
                    "persona_id": str(source.get("persona_id") or "default"),
                }
            snapshot.setdefault(
                "persona_id", str(source.get("persona_id") or "default")
            )
            source_template_prompt = str(
                snapshot.get("template_prompt") or source.get("template_prompt") or ""
            )
            inherited_image_urls = normalize_image_urls(snapshot.get("image_urls", []))
            if not inherited_image_urls:
                inherited_image_urls = extract_image_urls(source_template_prompt)
            snapshot["image_urls"] = normalize_image_urls(
                [*inherited_image_urls, *extract_image_urls(continuation_prompt)]
            )
            snapshot["continuation_prompt"] = continuation_prompt
            generator = await self._generator()
            html = await generator.generate(
                build_continuation_messages(
                    snapshot,
                    source_html,
                    continuation_prompt,
                ),
                snapshot.get("image_urls", []),
            )
            base_play_id, chapter, title = self.storage.continuation_identity(source)
            play = self.storage.add_play(
                str(snapshot.get("root_title") or source.get("title") or "小剧场"),
                html,
                snapshot,
                source_play_id=str(source["id"]),
                base_play_id=base_play_id,
                chapter=chapter,
                explicit_title=title,
            )
            self.storage.enforce_retention(int(self._config("retention_limit", 6) or 6))
            logger.info(
                "[HTML Theater] continued | source=%s title=%s chapter=%s",
                source["title"],
                play["title"],
                chapter,
            )
            return play

    def _success_message(self, play: dict[str, Any]) -> str:
        """Build the chat success notice and access hint.

        Args:
            play: Newly saved play.

        Returns:
            User-facing result text.
        """
        lines = [
            f"小剧场《{play['title']}》已生成。",
            f"HTML：{self.html_dir / play['filename']}",
        ]
        if bool(self._config("web_enabled", False)) and self.web_server is not None:
            host = str(self._config("web_host", "127.0.0.1"))
            port = int(self._config("web_port", 7315) or 7315)
            if host in {"0.0.0.0", "::"}:
                lines.append(
                    f"Web 已监听 {host}:{port}，请使用服务器 IP 或反向代理域名访问。"
                )
                lines.append(f"成品路径：/plays/{play['id']}")
            else:
                lines.append(f"Web：http://{host}:{port}/")
                lines.append(f"本成品：http://{host}:{port}/plays/{play['id']}")
        elif bool(self._config("web_enabled", False)):
            lines.append("Web 服务未成功启动，请检查端口占用和 AstrBot 日志。")
        return "\n".join(lines)

    def _reaction_request(
        self,
        event: AstrMessageEvent,
        play: dict[str, Any],
        snapshot: dict[str, Any],
        conversation: Any,
        begin_dialogs: list[dict[str, Any]],
    ) -> Any:
        """Create a one-shot current-provider reaction request.

        Args:
            event: Triggering chat event.
            play: Generated play.
            snapshot: Resolved persona and template snapshot.
            conversation: Active AstrBot Conversation.
            begin_dialogs: Selected persona begin dialogs.

        Returns:
            ProviderRequest yielded into the normal AstrBot LLM pipeline.
        """
        reaction_source = str(play.get("text", "") or "").strip()
        try:
            reaction_source = self.storage.read_play_html(str(play.get("id", "") or ""))
        except (FileNotFoundError, OSError, UnicodeError, ValueError):
            pass
        reaction_text = extract_html_text(reaction_source)
        if not reaction_text:
            reaction_text = extract_html_text(str(play.get("text", "") or ""))
        reaction_parts: list[str] = []
        injection_prompt = str(
            self._config("llm_injection_prompt", DEFAULT_LLM_INJECTION_PROMPT) or ""
        ).strip()
        if injection_prompt:
            reaction_parts.append(injection_prompt)
        if bool(self._config("inject_theater_prompt_after_generation", True)):
            template_prompt = str(snapshot.get("template_prompt", "")).strip()
            if template_prompt:
                reaction_parts.extend(["[小剧场提示词]", template_prompt])
        reaction_parts.extend(["[小剧场正文]", reaction_text])
        reaction_prompt = "\n\n".join(reaction_parts)
        system_parts: list[str] = []
        if str(snapshot.get("persona_prompt", "")).strip():
            system_parts.append(
                "# Persona Instructions\n\n" + str(snapshot["persona_prompt"]).strip()
            )
        if str(snapshot.get("user_prompt", "")).strip():
            system_parts.append(
                "# User Persona\n\n" + str(snapshot["user_prompt"]).strip()
            )
        contexts = [
            *begin_dialogs,
            *self._conversation_history(conversation),
        ]
        event_key = self._event_key(event)
        self.pending_reactions[event_key] = {
            "umo": str(event.unified_msg_origin),
            "conversation_id": str(getattr(conversation, "cid", "") or ""),
            "prompt": reaction_prompt,
        }
        return event.request_llm(
            prompt=reaction_prompt,
            contexts=contexts,
            system_prompt="\n\n".join(system_parts),
        )

    async def _run_template_command(
        self,
        event: AstrMessageEvent,
        template: dict[str, Any],
    ):
        """Run generation and optionally yield a one-shot persona reaction.

        Args:
            event: Triggering command event.
            template: Selected theater template.

        Yields:
            Success notice and optional ProviderRequest.
        """
        snapshot, conversation, begin_dialogs = await self.build_snapshot(
            event, template
        )
        try:
            play = await self.generate_snapshot(snapshot)
        except (TheaterApiError, ValueError, OSError) as exc:
            yield event.plain_result(f"小剧场生成失败：{exc}")
            return
        self.last_requests[str(event.unified_msg_origin)] = copy_snapshot = dict(
            snapshot
        )
        yield event.plain_result(self._success_message(play))
        if bool(self._config("inject_after_generation", False)):
            await asyncio.sleep(5)
            yield self._reaction_request(
                event,
                play,
                copy_snapshot,
                conversation,
                begin_dialogs,
            )

    @filter.command("小剧场目录")
    async def list_theater_directory(self, event: AstrMessageEvent):
        """Return the current template directory to the triggering chat."""
        if error := self._whitelist_error(event):
            yield event.plain_result(error)
            return
        self.storage.refresh()
        templates = list(self.storage.state.get("templates", []))
        self._debug(f"directory requested available={len(templates)}")
        if not templates:
            yield event.plain_result("当前小剧场目录为空。")
            return
        lines = ["当前小剧场列表："]
        lines.extend(
            f"{index}. {item.get('title', '未命名小剧场')}"
            for index, item in enumerate(templates, 1)
        )
        yield event.plain_result("\n".join(lines))

    @filter.command("生成小剧场", alias={"小剧场"})
    async def generate_theater(
        self,
        event: AstrMessageEvent,
        template_title: GreedyStr,
    ):
        """Generate a selected template, or reroll with `/小剧场 重试`."""
        if error := self._whitelist_error(event):
            yield event.plain_result(error)
            return
        template_title = normalize_title(template_title)
        if template_title == "重试":
            snapshot = self.last_requests.get(str(event.unified_msg_origin))
            if snapshot is None:
                yield event.plain_result("当前会话还没有可重试的小剧场。")
                return
            try:
                play = await self.generate_snapshot(dict(snapshot))
            except (TheaterApiError, ValueError, OSError) as exc:
                yield event.plain_result(f"小剧场重试失败：{exc}")
                return
            yield event.plain_result(self._success_message(play))
            if bool(self._config("inject_after_generation", False)):
                await asyncio.sleep(5)
                (
                    conversation,
                    _,
                    _,
                    _,
                    begin_dialogs,
                ) = await self._conversation_and_persona(event)
                yield self._reaction_request(
                    event,
                    play,
                    dict(snapshot),
                    conversation,
                    begin_dialogs,
                )
            return
        self.storage.refresh()
        self._debug(
            f"template lookup requested={template_title!r} "
            f"available={len(self.storage.state.get('templates', []))}"
        )
        template = self.storage.get_template(template_title)
        if template is None:
            yield event.plain_result(f"未找到小剧场模板：{template_title}")
            return
        async for result in self._run_template_command(event, template):
            yield result

    @filter.command("生成随机小剧场")
    async def generate_random_theater(self, event: AstrMessageEvent):
        """Randomly select one existing template and generate it."""
        if error := self._whitelist_error(event):
            yield event.plain_result(error)
            return
        self.storage.refresh()
        templates = list(self.storage.state.get("templates", []))
        if not templates:
            yield event.plain_result("小剧场模板目录为空，请先在插件页面新增模板。")
            return
        template = random.choice(templates)
        async for result in self._run_template_command(event, dict(template)):
            yield result

    @filter.command("临时小剧场")
    async def generate_temporary_theater(
        self,
        event: AstrMessageEvent,
        temporary_prompt: GreedyStr,
    ):
        """Generate from a one-shot prompt without saving a template."""
        if error := self._whitelist_error(event):
            yield event.plain_result(error)
            return
        prompt = str(temporary_prompt or "").strip()
        if not prompt:
            yield event.plain_result("用法：/临时小剧场 <提示词>")
            return
        template = {"title": "临时小剧场", "prompt": prompt}
        async for result in self._run_template_command(event, template):
            yield result

    @filter.command("查看小剧场")
    async def view_theater_prompt(
        self,
        event: AstrMessageEvent,
        template_title: GreedyStr,
    ):
        """Show a template prompt without exposing generated HTML."""
        if error := self._whitelist_error(event):
            yield event.plain_result(error)
            return
        requested_title = normalize_title(template_title)
        self.storage.refresh()
        self._debug(
            f"view template requested={requested_title!r} "
            f"available={len(self.storage.state.get('templates', []))}"
        )
        template = self.storage.get_template(requested_title)
        if template is None:
            yield event.plain_result(f"未找到小剧场模板：{requested_title}")
            return
        yield event.plain_result(f"【{template['title']}】\n{template['prompt']}")

    @filter.on_llm_response()
    async def save_reaction_history(
        self,
        event: AstrMessageEvent,
        response: LLMResponse,
    ) -> None:
        """Persist custom-context theater reactions into the triggering conversation."""
        pending = self.pending_reactions.pop(self._event_key(event), None)
        if pending is None:
            return
        completion = str(getattr(response, "completion_text", "") or "").strip()
        conversation_id = str(pending.get("conversation_id", ""))
        if not completion or not conversation_id:
            return
        manager = getattr(self.context, "conversation_manager", None)
        if manager is None:
            return
        try:
            conversation = await manager.get_conversation(
                str(pending["umo"]), conversation_id
            )
            history = self._conversation_history(conversation)
            history.extend(
                [
                    {"role": "user", "content": str(pending["prompt"])},
                    {"role": "assistant", "content": completion},
                ]
            )
            await manager.update_conversation(
                str(pending["umo"]),
                conversation_id,
                history=history,
            )
        except Exception as exc:
            logger.warning("[HTML Theater] unable to persist reaction history: %s", exc)
