"""AstrBot Plugin Page extension APIs for HTML Theater."""

from __future__ import annotations

import time
from typing import Any

from quart import Response, jsonify, request

from astrbot.api import logger

from .generator import TheaterApiError
from .main import PLUGIN_NAME


class TheaterWebApi:
    """Register panel CRUD, continuation, profile, and backup endpoints."""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    def register_routes(self) -> None:
        """Register all routes below AstrBot's authenticated extension prefix."""
        register = self.plugin.context.register_web_api
        routes = [
            ("/state", self.state, ["GET"], "HTML Theater panel state"),
            (
                "/preferences/save",
                self.save_preferences,
                ["POST"],
                "Save panel theme and custom CSS",
            ),
            (
                "/templates/save",
                self.save_template,
                ["POST"],
                "Create or update theater template",
            ),
            (
                "/templates/delete",
                self.delete_templates,
                ["POST"],
                "Delete selected theater templates",
            ),
            (
                "/plays/delete",
                self.delete_plays,
                ["POST"],
                "Delete selected generated plays",
            ),
            (
                "/plays/favorite",
                self.favorite_play,
                ["POST"],
                "Favorite or unfavorite a play",
            ),
            (
                "/plays/select",
                self.select_play,
                ["POST"],
                "Select the public Web play",
            ),
            (
                "/plays/continue",
                self.continue_play,
                ["POST"],
                "Generate a panel-only continuation",
            ),
            (
                "/plays/content/<play_id>",
                self.play_content,
                ["GET"],
                "Read one generated play for sandboxed preview",
            ),
            ("/personas", self.personas, ["GET"], "List AstrBot Personas"),
            (
                "/profiles/save",
                self.save_profile,
                ["POST"],
                "Save theater persona and user override",
            ),
            (
                "/profiles/delete",
                self.delete_profile,
                ["POST"],
                "Delete theater persona and user override",
            ),
            (
                "/backup/export",
                self.export_backup,
                ["GET"],
                "Export complete theater page data",
            ),
            (
                "/backup/import",
                self.import_backup,
                ["POST"],
                "Import complete theater page data",
            ),
        ]
        for route, handler, methods, description in routes:
            register(
                f"/{PLUGIN_NAME}{route}",
                handler,
                methods,
                description,
            )

    @staticmethod
    async def _json() -> dict[str, Any]:
        """Read a JSON object body.

        Returns:
            Parsed dictionary or an empty dictionary.
        """
        payload = await request.get_json(silent=True)
        return payload if isinstance(payload, dict) else {}

    async def state(self) -> Any:
        """Return all panel-safe state, optionally filtered by search query."""
        query = str(request.args.get("q", "") or "")
        payload = self.plugin.storage.public_state(query)
        self.plugin._debug(
            f"panel state revision={payload.get('revision', 0)} "
            f"templates={len(payload['templates'])} plays={len(payload['plays'])}"
        )
        return jsonify({"data": payload})

    async def save_preferences(self) -> Any:
        """Save panel-only theme and custom CSS preferences."""
        try:
            body = await self._json()
            preferences = self.plugin.storage.save_panel_preferences(
                str(body.get("theme", "blue-white")),
                str(body.get("custom_css", "")),
            )
            self.plugin._debug(
                f"panel preferences saved theme={preferences['theme']!r} "
                f"css_bytes={len(preferences['custom_css'].encode('utf-8'))}"
            )
            return jsonify({"data": preferences})
        except ValueError as exc:
            return jsonify({"status": "error", "message": str(exc)}), 400

    async def save_template(self) -> Any:
        """Create or update a required title-and-prompt template."""
        try:
            body = await self._json()
            item = self.plugin.storage.save_template(
                str(body.get("title", "")),
                str(body.get("prompt", "")),
                str(body.get("id", "")),
            )
            self.plugin._debug(f"panel template saved title={item['title']!r}")
            return jsonify({"data": item})
        except ValueError as exc:
            return jsonify({"status": "error", "message": str(exc)}), 400
        except Exception as exc:
            logger.error("[HTML Theater] save template failed", exc_info=True)
            return jsonify({"status": "error", "message": str(exc)}), 500

    async def delete_templates(self) -> Any:
        """Delete selected template IDs."""
        body = await self._json()
        ids = body.get("ids", [])
        if not isinstance(ids, list):
            return jsonify({"status": "error", "message": "ids 必须是列表。"}), 400
        deleted = self.plugin.storage.delete_templates(ids)
        self.plugin._debug(f"panel templates deleted count={deleted}")
        return jsonify({"data": {"deleted": deleted}})

    async def delete_plays(self) -> Any:
        """Delete selected generated plays and update current selection."""
        body = await self._json()
        ids = body.get("ids", [])
        if not isinstance(ids, list):
            return jsonify({"status": "error", "message": "ids 必须是列表。"}), 400
        deleted = self.plugin.storage.delete_plays(ids)
        self.plugin._debug(f"panel plays deleted count={deleted}")
        return jsonify({"data": {"deleted": deleted}})

    async def favorite_play(self) -> Any:
        """Set one favorite flag."""
        try:
            body = await self._json()
            item = self.plugin.storage.set_favorite(
                str(body.get("id", "")),
                bool(body.get("favorite", False)),
            )
            return jsonify({"data": item})
        except ValueError as exc:
            return jsonify({"status": "error", "message": str(exc)}), 404

    async def select_play(self) -> Any:
        """Select the play served from the independent Web root."""
        try:
            body = await self._json()
            item = self.plugin.storage.select_play(str(body.get("id", "")))
            return jsonify({"data": item})
        except ValueError as exc:
            return jsonify({"status": "error", "message": str(exc)}), 404

    async def continue_play(self) -> Any:
        """Generate a continuation without dispatching a QQ/LLM reaction."""
        try:
            body = await self._json()
            item = await self.plugin.generate_continuation_from_panel(
                str(body.get("source_id", "")),
                str(body.get("prompt", "")),
            )
            return jsonify({"data": item})
        except (ValueError, TheaterApiError, OSError) as exc:
            return jsonify({"status": "error", "message": str(exc)}), 400
        except Exception as exc:
            logger.error("[HTML Theater] panel continuation failed", exc_info=True)
            return jsonify({"status": "error", "message": str(exc)}), 500

    async def play_content(self, play_id: str) -> Any:
        """Return sanitized HTML for a sandboxed Plugin Page preview."""
        try:
            content = self.plugin.storage.read_play_html(play_id)
            return jsonify({"html": content})
        except FileNotFoundError as exc:
            return jsonify({"status": "error", "message": str(exc)}), 404

    async def personas(self) -> Any:
        """List known AstrBot Persona IDs and configured profile-only IDs."""
        self.plugin.storage.refresh()
        result: list[dict[str, str]] = []
        seen: set[str] = set()

        def add_persona(persona: Any) -> None:
            if isinstance(persona, dict):
                persona_id = (
                    persona.get("persona_id")
                    or persona.get("name")
                    or persona.get("id")
                )
                display_name = persona.get("name") or persona_id
            else:
                persona_id = (
                    getattr(persona, "persona_id", None)
                    or getattr(persona, "name", None)
                    or getattr(persona, "id", None)
                )
                display_name = getattr(persona, "name", None) or persona_id
            persona_id = str(persona_id or "").strip()
            if not persona_id or persona_id in seen:
                return
            seen.add(persona_id)
            result.append({"id": persona_id, "name": str(display_name or persona_id)})

        manager = getattr(self.plugin.context, "persona_manager", None)
        for source in (
            getattr(manager, "personas_v3", None),
            getattr(manager, "personas", None),
            getattr(manager, "selected_default_persona_v3", None),
            getattr(manager, "selected_default_persona", None),
        ):
            if isinstance(source, list):
                for persona in source:
                    add_persona(persona)
            elif source is not None:
                add_persona(source)
        for persona_id in self.plugin.storage.state.get("profiles", {}):
            add_persona({"persona_id": persona_id, "name": persona_id})
        result.sort(key=lambda item: item["name"].casefold())
        return jsonify({"data": result})

    async def save_profile(self) -> Any:
        """Save page-configured char/user overrides."""
        try:
            body = await self._json()
            profile = self.plugin.storage.save_profile(
                str(body.get("persona_id", "")),
                str(body.get("char_name", "")),
                str(body.get("char_prompt", "")),
                str(body.get("user_name", "")),
                str(body.get("user_prompt", "")),
            )
            self.plugin._debug(f"panel profile saved persona={profile['persona_id']!r}")
            return jsonify({"data": profile})
        except ValueError as exc:
            return jsonify({"status": "error", "message": str(exc)}), 400

    async def delete_profile(self) -> Any:
        """Delete one page-configured Persona override."""
        try:
            body = await self._json()
            persona_id = str(body.get("persona_id", ""))
            deleted = self.plugin.storage.delete_profile(persona_id)
            if not deleted:
                return jsonify({"status": "error", "message": "人设配置不存在。"}), 404
            self.plugin._debug(f"panel profile deleted persona={persona_id!r}")
            return jsonify({"deleted": True})
        except ValueError as exc:
            return jsonify({"status": "error", "message": str(exc)}), 400

    async def export_backup(self) -> Response:
        """Download a complete, secret-free page data ZIP and save a server copy."""
        payload = self.plugin.storage.export_backup(save_server_copy=True)
        self.plugin._debug(f"panel backup exported bytes={len(payload)}")
        filename = time.strftime("html_theater_backup_%Y%m%d-%H%M%S.zip")
        return Response(
            payload,
            mimetype="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    async def import_backup(self) -> Any:
        """Import a multipart or Base64 ZIP in merge or replacement mode."""
        try:
            content_type = str(request.content_type or "")
            is_multipart = content_type.lower().startswith("multipart/form-data")
            if is_multipart:
                form = await request.form
                uploaded = (await request.files).get("file")
                mode = str(form.get("mode", "merge") or "merge")
                confirm = str(form.get("confirm", "") or "")
                if uploaded is None:
                    raise ValueError("备份文件不能为空。")
            else:
                body = await self._json()
                mode = str(body.get("mode", "merge") or "merge")
                confirm = str(body.get("confirm", "") or "")

            if mode == "replace" and confirm != "完整恢复":
                return jsonify(
                    {
                        "status": "error",
                        "message": "完整恢复需要输入确认文本：完整恢复",
                    }
                ), 400
            if is_multipart:
                payload = uploaded.read()
            else:
                payload = self.plugin.storage.decode_backup_base64(
                    str(body.get("content_base64", ""))
                )
            result = self.plugin.storage.import_backup(payload, mode)
            self.plugin.storage.enforce_retention(
                int(self.plugin._config("retention_limit", 6) or 6)
            )
            self.plugin._debug(
                f"panel backup imported mode={mode} "
                f"templates={result['templates']} plays={result['plays']}"
            )
            return jsonify({"data": result})
        except ValueError as exc:
            return jsonify({"status": "error", "message": str(exc)}), 400
        except Exception as exc:
            logger.error("[HTML Theater] backup import failed", exc_info=True)
            return jsonify({"status": "error", "message": str(exc)}), 500
