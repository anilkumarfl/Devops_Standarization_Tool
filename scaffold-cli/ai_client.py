"""
ai_client.py — unified AI provider abstraction
───────────────────────────────────────────────
Switch providers without changing any caller code.

Set AI_PROVIDER env var (default: claude):
  AI_PROVIDER=claude   → ANTHROPIC_API_KEY  → claude-sonnet-4-6
  AI_PROVIDER=openai   → OPENAI_API_KEY     → gpt-4o
  AI_PROVIDER=gemini   → GOOGLE_API_KEY     → gemini-1.5-pro

Set AI_MODEL env var to override the default model for that provider.

All tools must use Anthropic input_schema format — converted automatically
for OpenAI (function calling) and Gemini (function declarations).
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

import typer

PROVIDER_CONFIG: dict[str, dict] = {
    "claude": {
        "key_env":       "ANTHROPIC_API_KEY",
        "default_model": "claude-sonnet-4-6",
        "package":       "anthropic",
    },
    "openai": {
        "key_env":       "OPENAI_API_KEY",
        "default_model": "gpt-4o",
        "package":       "openai",
    },
    "gemini": {
        "key_env":       "GOOGLE_API_KEY",
        "default_model": "gemini-1.5-pro",
        "package":       "google-generativeai",
    },
    "kimi": {
        "key_env":       "OPENROUTER_API_KEY",
        "default_model": "moonshotai/kimi-k2.7-code",
        "package":       "openai",
        "base_url":      "https://openrouter.ai/api/v1",
    },
}


class AIClient:
    """
    Unified AI client.
      tool_use()  — structured output via tool / function calling
      complete()  — free-text completion
    """

    def __init__(
        self,
        provider: Optional[str] = None,
        model:    Optional[str] = None,
        max_tokens: int = 4096,
    ) -> None:
        self.provider   = (provider or os.environ.get("AI_PROVIDER", "claude")).lower()
        if self.provider not in PROVIDER_CONFIG:
            raise ValueError(
                f"Unknown AI provider '{self.provider}'. "
                f"Valid options: {', '.join(PROVIDER_CONFIG)}"
            )
        cfg             = PROVIDER_CONFIG[self.provider]
        self.model      = model or os.environ.get("AI_MODEL") or cfg["default_model"]
        self.api_key    = os.environ.get(cfg["key_env"])
        self.max_tokens = max_tokens
        self._cfg       = cfg

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def require_key(self) -> bool:
        if not self.api_key:
            typer.secho(
                f"  ! {self._cfg['key_env']} not set — cannot call {self.provider}.\n"
                f"    Set the env var or switch AI_PROVIDER.",
                fg=typer.colors.YELLOW,
            )
            return False
        return True

    # ─────────────────────────────────────────────────────────────────────────
    # Public interface
    # ─────────────────────────────────────────────────────────────────────────

    def tool_use(
        self,
        tools:  list[dict],
        prompt: str,
        system: Optional[str] = None,
        tool_name: Optional[str] = None,
    ) -> Optional[dict]:
        """
        Call AI with tool / function calling.
        Returns the first matching tool's input dict, or None on failure.
        tools must use Anthropic input_schema format.
        """
        if not self.require_key():
            return None
        try:
            if self.provider == "claude":
                return self._claude_tool_use(tools, prompt, system, tool_name)
            if self.provider == "openai":
                return self._openai_tool_use(tools, prompt, system)
            if self.provider == "gemini":
                return self._gemini_tool_use(tools, prompt, system)
            if self.provider == "kimi":
                return self._kimi_tool_use(tools, prompt, system)
        except Exception as exc:
            typer.secho(f"  ! AI tool_use failed ({self.provider}): {exc}", fg=typer.colors.YELLOW)
        return None

    def complete(self, prompt: str, system: Optional[str] = None) -> Optional[str]:
        """Free-text completion. Returns the response string."""
        if not self.require_key():
            return None
        try:
            if self.provider == "claude":
                return self._claude_complete(prompt, system)
            if self.provider == "openai":
                return self._openai_complete(prompt, system)
            if self.provider == "gemini":
                return self._gemini_complete(prompt, system)
            if self.provider == "kimi":
                return self._kimi_complete(prompt, system)
        except Exception as exc:
            typer.secho(f"  ! AI complete failed ({self.provider}): {exc}", fg=typer.colors.YELLOW)
        return None

    # ─────────────────────────────────────────────────────────────────────────
    # Claude
    # ─────────────────────────────────────────────────────────────────────────

    def _claude_tool_use(self, tools, prompt, system, tool_name):
        import anthropic
        client = anthropic.Anthropic(api_key=self.api_key)
        kwargs: dict[str, Any] = dict(
            model       = self.model,
            max_tokens  = self.max_tokens,
            tools       = tools,
            tool_choice = {"type": "any"},
            messages    = [{"role": "user", "content": prompt}],
        )
        if system:
            kwargs["system"] = [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ]
        response = client.messages.create(**kwargs)
        for block in response.content:
            if block.type == "tool_use":
                if tool_name is None or block.name == tool_name:
                    return block.input
        return None

    def _claude_complete(self, prompt, system):
        import anthropic
        client = anthropic.Anthropic(api_key=self.api_key)
        kwargs: dict[str, Any] = dict(
            model      = self.model,
            max_tokens = self.max_tokens,
            messages   = [{"role": "user", "content": prompt}],
        )
        if system:
            kwargs["system"] = system
        response = client.messages.create(**kwargs)
        return response.content[0].text.strip()

    # ─────────────────────────────────────────────────────────────────────────
    # OpenAI
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _anthropic_to_openai_tools(tools: list[dict]) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name":        t["name"],
                    "description": t.get("description", ""),
                    "parameters":  t.get("input_schema", {}),
                },
            }
            for t in tools
        ]

    def _openai_tool_use(self, tools, prompt, system):
        from openai import OpenAI
        client   = OpenAI(api_key=self.api_key)
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        response = client.chat.completions.create(
            model       = self.model,
            tools       = self._anthropic_to_openai_tools(tools),
            tool_choice = "required",
            messages    = messages,
            max_tokens  = self.max_tokens,
        )
        tc = response.choices[0].message.tool_calls
        if tc:
            return json.loads(tc[0].function.arguments)
        return None

    def _openai_complete(self, prompt, system):
        from openai import OpenAI
        client   = OpenAI(api_key=self.api_key)
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        response = client.chat.completions.create(
            model      = self.model,
            messages   = messages,
            max_tokens = self.max_tokens,
        )
        return response.choices[0].message.content.strip()

    # ─────────────────────────────────────────────────────────────────────────
    # Gemini
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _json_schema_to_gemini(schema: dict) -> dict:
        """Recursively convert JSON Schema to Gemini-compatible schema dict."""
        result: dict[str, Any] = {}
        t = schema.get("type", "")
        if t:
            result["type"] = t.upper()
        if "description" in schema:
            result["description"] = schema["description"]
        if "properties" in schema:
            result["properties"] = {
                k: AIClient._json_schema_to_gemini(v)
                for k, v in schema["properties"].items()
            }
        if "items" in schema:
            result["items"] = AIClient._json_schema_to_gemini(schema["items"])
        if "required" in schema:
            result["required"] = schema["required"]
        if "enum" in schema:
            result["enum"] = schema["enum"]
        return result

    def _gemini_tool_use(self, tools, prompt, system):
        import google.generativeai as genai
        genai.configure(api_key=self.api_key)

        fn_decls = []
        for t in tools:
            gemini_schema = self._json_schema_to_gemini(t.get("input_schema", {}))
            fn_decls.append(
                genai.protos.FunctionDeclaration(
                    name        = t["name"],
                    description = t.get("description", ""),
                    parameters  = genai.protos.Schema(**gemini_schema),
                )
            )

        full_prompt = f"{system}\n\n{prompt}" if system else prompt
        model_obj   = genai.GenerativeModel(
            self.model,
            tools=[genai.protos.Tool(function_declarations=fn_decls)],
        )
        response = model_obj.generate_content(full_prompt)
        for candidate in response.candidates:
            for part in candidate.content.parts:
                if hasattr(part, "function_call") and part.function_call:
                    return dict(part.function_call.args)
        return None

    def _gemini_complete(self, prompt, system):
        import google.generativeai as genai
        genai.configure(api_key=self.api_key)
        full_prompt = f"{system}\n\n{prompt}" if system else prompt
        model_obj   = genai.GenerativeModel(self.model)
        response    = model_obj.generate_content(full_prompt)
        return response.text.strip()


    # ─────────────────────────────────────────────────────────────────────────
    # Kimi (Moonshot AI) — OpenAI-compatible API
    # ─────────────────────────────────────────────────────────────────────────

    def _kimi_client(self):
        from openai import OpenAI
        base_url = self._cfg.get("base_url", "https://openrouter.ai/api/v1")
        # OpenRouter requires HTTP-Referer and X-Title headers
        extra_headers = {}
        if "openrouter" in base_url:
            extra_headers = {
                "HTTP-Referer": "https://github.com/devops-scaffold-tool",
                "X-Title":      "DevOps Scaffold Tool",
            }
        return OpenAI(
            api_key         = self.api_key,
            base_url        = base_url,
            default_headers = extra_headers,
        )

    def _kimi_tool_use(self, tools, prompt, system):
        client   = self._kimi_client()
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        response = client.chat.completions.create(
            model       = self.model,
            tools       = self._anthropic_to_openai_tools(tools),
            tool_choice = "required",
            messages    = messages,
            max_tokens  = self.max_tokens,
        )
        tc = response.choices[0].message.tool_calls
        if tc:
            return json.loads(tc[0].function.arguments)
        return None

    def _kimi_complete(self, prompt, system):
        client   = self._kimi_client()
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        response = client.chat.completions.create(
            model      = self.model,
            messages   = messages,
            max_tokens = self.max_tokens,
        )
        return response.choices[0].message.content.strip()


def get_client(
    provider: Optional[str] = None,
    model:    Optional[str] = None,
    max_tokens: int = 4096,
) -> AIClient:
    """Factory — reads AI_PROVIDER env var by default."""
    return AIClient(provider=provider, model=model, max_tokens=max_tokens)


def provider_info() -> str:
    provider = os.environ.get("AI_PROVIDER", "claude").lower()
    cfg      = PROVIDER_CONFIG.get(provider, {})
    model    = os.environ.get("AI_MODEL") or cfg.get("default_model", "unknown")
    key_set  = bool(os.environ.get(cfg.get("key_env", "")))
    status   = "ready" if key_set else f"{cfg.get('key_env','KEY')} not set"
    return f"{provider} / {model}  [{status}]"
