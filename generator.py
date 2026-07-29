"""OpenAI-compatible HTML theater generation client."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import aiohttp

from .html_utils import is_complete_html, sanitize_html, strip_markdown_fence


class TheaterApiError(RuntimeError):
    """Raised when the theater API cannot produce a usable HTML document."""


def normalize_api_url(value: str) -> str:
    """Normalize a Base URL into an OpenAI chat-completions endpoint.

    Args:
        value: User-configured API Base URL or complete endpoint.

    Returns:
        Normalized chat-completions endpoint.
    """
    url = str(value or "").strip().rstrip("/")
    lowered = url.lower()
    if lowered.endswith("/chat/completions"):
        return url
    if lowered.endswith("/v1"):
        return url + "/chat/completions"
    return url + "/v1/chat/completions" if url else ""


def extract_response_content(payload: Any) -> str:
    """Extract assistant text from an OpenAI-compatible response payload.

    Args:
        payload: Decoded response JSON.

    Returns:
        Assistant content as text.

    Raises:
        TheaterApiError: If the response does not expose a supported content shape.
    """
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise TheaterApiError("API 响应缺少 choices[0].message.content。") from exc
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    raise TheaterApiError("API 返回了不支持的消息内容格式。")


def build_generation_messages(snapshot: dict[str, Any]) -> list[dict[str, str]]:
    """Build ordered system, style, theater, and persona messages.

    Args:
        snapshot: Resolved generation request snapshot.

    Returns:
        OpenAI-compatible message dictionaries.
    """
    system_parts = [str(snapshot.get("system_prompt", "")).strip()]
    if str(snapshot.get("style_prompt", "")).strip():
        system_parts.append(
            "本次小剧场额外文风要求：\n" + str(snapshot.get("style_prompt", "")).strip()
        )

    persona_lines = [
        f"char 名字：{snapshot.get('char_name', 'Char')}",
        f"user 名字：{snapshot.get('user_name', 'User')}",
    ]
    if str(snapshot.get("persona_prompt", "")).strip():
        persona_lines.append(
            "char 人设内容：\n" + str(snapshot["persona_prompt"]).strip()
        )
    if str(snapshot.get("user_prompt", "")).strip():
        persona_lines.append("user 人设内容：\n" + str(snapshot["user_prompt"]).strip())

    template_content = "\n\n".join(
        [
            f"小剧场类型：{snapshot.get('template_title', '')}",
            "小剧场提示词：\n" + str(snapshot.get("template_prompt", "")).strip(),
        ]
    )
    persona_content = "\n\n".join(
        [
            "人物设定（后置注入）：\n" + "\n\n".join(persona_lines),
            "请只输出一个完整、可独立打开的 HTML 文档。",
        ]
    )
    messages = [
        {"role": "system", "content": "\n\n".join(filter(None, system_parts))},
        {"role": "user", "content": template_content},
    ]
    contexts = snapshot.get("conversation_context", [])
    if isinstance(contexts, list):
        messages.extend(
            {
                "role": str(item["role"]),
                "content": str(item["content"]),
            }
            for item in contexts
            if isinstance(item, dict)
            and item.get("role") in {"user", "assistant"}
            and str(item.get("content", "")).strip()
        )
    messages.append({"role": "user", "content": persona_content})
    return messages


def build_continuation_messages(
    snapshot: dict[str, Any],
    source_html: str,
    continuation_prompt: str,
) -> list[dict[str, str]]:
    """Build a standalone-HTML continuation request.

    Args:
        snapshot: Resolved source generation snapshot.
        source_html: Full HTML of the selected source play.
        continuation_prompt: User-provided continuation instruction.

    Returns:
        OpenAI-compatible message dictionaries.
    """
    messages = build_generation_messages(snapshot)
    persona_message = messages[-1]
    messages = messages[:2]
    messages.append(
        {
            "role": "user",
            "content": "\n\n".join(
                [
                    f"原小剧场类型：{snapshot.get('template_title', '')}",
                    "原小剧场完整 HTML：\n" + source_html,
                    "续写要求：\n" + continuation_prompt.strip(),
                    "请根据原小剧场继续创作，但必须输出一个包含前情衔接的、完整且可独立打开的新 HTML 文档；不要只输出片段。",
                    "人物与文风继续严格遵守系统提示和原始人设。",
                ]
            ),
        }
    )
    messages.append(persona_message)
    return messages


class TheaterGenerator:
    """Call an OpenAI-compatible endpoint and repair blank/truncated responses."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_url: str,
        api_key: str,
        model: str,
        continue_on_empty: bool,
        debug_log: Callable[[str], None] | None = None,
    ) -> None:
        self.session = session
        self.api_url = normalize_api_url(api_url)
        self.api_key = str(api_key or "").strip()
        self.model = str(model or "").strip()
        self.continue_on_empty = bool(continue_on_empty)
        self.debug_log = debug_log

    def validate_config(self) -> None:
        """Validate required API settings before spending work on a request.

        Raises:
            TheaterApiError: If URL, key, or model is missing.
        """
        missing = []
        if not self.api_url:
            missing.append("API URL")
        if not self.api_key:
            missing.append("API 密钥")
        if not self.model:
            missing.append("API 模型")
        if missing:
            raise TheaterApiError("请先配置小剧场" + "、".join(missing) + "。")

    async def _post(self, messages: list[dict[str, str]]) -> str:
        """Post one chat-completions request.

        Args:
            messages: Ordered OpenAI-compatible chat messages.

        Returns:
            Raw assistant content.

        Raises:
            TheaterApiError: On HTTP, JSON, or response-shape errors.
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if self.debug_log:
            self.debug_log(f"API request messages={len(messages)} model={self.model!r}")
        try:
            async with self.session.post(
                self.api_url,
                headers=headers,
                json={"model": self.model, "messages": messages},
            ) as response:
                text = await response.text()
                if response.status < 200 or response.status >= 300:
                    raise TheaterApiError(f"小剧场 API HTTP {response.status}。")
                if self.debug_log:
                    self.debug_log(
                        f"API response status={response.status} bytes={len(text)}"
                    )
        except aiohttp.ClientError as exc:
            raise TheaterApiError(f"小剧场 API 网络请求失败：{exc}") from exc
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise TheaterApiError("小剧场 API 返回的不是有效 JSON。") from exc
        return extract_response_content(payload)

    async def generate(self, messages: list[dict[str, str]]) -> str:
        """Generate and sanitize one complete HTML document.

        Args:
            messages: Initial system and user messages.

        Returns:
            Sanitized standalone HTML.

        Raises:
            TheaterApiError: If the response remains blank or incomplete after
                the configured three rescue attempts.
        """
        self.validate_config()
        working_messages = [dict(item) for item in messages]
        accumulated = ""
        attempts = 1 + (3 if self.continue_on_empty else 0)

        for index in range(attempts):
            if self.debug_log:
                self.debug_log(
                    f"generation attempt={index + 1}/{attempts} accumulated_chars={len(accumulated)}"
                )
            fragment = strip_markdown_fence(await self._post(working_messages))
            if fragment:
                if (
                    accumulated
                    and "<html" in fragment.lower()
                    and is_complete_html(fragment)
                ):
                    accumulated = fragment
                else:
                    accumulated += fragment
            if is_complete_html(accumulated):
                try:
                    return sanitize_html(accumulated)
                except ValueError as exc:
                    raise TheaterApiError(str(exc)) from exc
            if index + 1 >= attempts:
                break
            if accumulated:
                if self.debug_log:
                    self.debug_log("response incomplete; requesting HTML continuation")
                working_messages = [
                    *messages,
                    {"role": "assistant", "content": accumulated},
                    {
                        "role": "user",
                        "content": (
                            "上一次 HTML 在传输中被截断。请从中断处准确续写，"
                            "不要重复已经输出的部分，只输出剩余 HTML，直到 </html>。"
                        ),
                    },
                ]
            else:
                if self.debug_log:
                    self.debug_log("response blank; retrying original request")
                working_messages = [dict(item) for item in messages]

        if not accumulated.strip():
            raise TheaterApiError("小剧场 API 连续返回空内容，已停止重试。")
        raise TheaterApiError("小剧场 HTML 连续三次续写后仍未闭合，未保存残缺文件。")
