from __future__ import annotations

import json
import logging
import re
from typing import Any

from .config import Settings

logger = logging.getLogger(__name__)


def clean_json_text(text: str) -> str:
    """Clean markdown code fences and extraneous text around JSON content."""
    stripped = text.strip()
    # Remove markdown code block if present
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", stripped, re.IGNORECASE)
    if match:
        stripped = match.group(1).strip()
    
    # In case there's still text before the first '{' or '[' and after the last '}' or ']'
    start_brace = stripped.find("{")
    start_bracket = stripped.find("[")
    
    if start_brace != -1 and (start_bracket == -1 or start_brace < start_bracket):
        end_brace = stripped.rfind("}")
        if end_brace != -1:
            stripped = stripped[start_brace : end_brace + 1]
    elif start_bracket != -1:
        end_bracket = stripped.rfind("]")
        if end_bracket != -1:
            stripped = stripped[start_bracket : end_bracket + 1]
            
    return stripped


class LLMClient:
    """Unified LLM Client supporting Google Gemini & OpenAI with 2 fallback tiers.

    Execution order:
      Tier 0 (Primary): e.g. Gemini 2.5 Flash
      Tier 1 (Fallback 1): e.g. Gemini 2.0 Flash
      Tier 2 (Fallback 2): e.g. OpenAI GPT-4o-mini
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._gemini_client = None
        self._openai_client = None

    def _get_gemini_client(self):
        if not self.settings.gemini_api_key:
            raise ValueError("GEMINI_API_KEY is not configured in .env")
        if self._gemini_client is None:
            from google import genai
            self._gemini_client = genai.Client(api_key=self.settings.gemini_api_key)
        return self._gemini_client

    def _get_openai_client(self):
        if not self.settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY is not configured in .env")
        if self._openai_client is None:
            from openai import AsyncOpenAI
            self._openai_client = AsyncOpenAI(
                api_key=self.settings.openai_api_key,
                base_url=self.settings.openai_base_url,
                timeout=self.settings.llm_timeout,
            )
        return self._openai_client

    def _detect_provider(self, model: str) -> str:
        """Infer provider ('gemini' or 'openai') based on model name."""
        lowered = model.lower()
        if "gemini" in lowered:
            return "gemini"
        if any(prefix in lowered for prefix in ("gpt", "o1", "o3", "o4", "text-", "chatgpt")):
            return "openai"
        # Default fallback: check configured keys
        if self.settings.gemini_api_key and not self.settings.openai_api_key:
            return "gemini"
        return "openai"

    def get_tiers(self) -> list[tuple[str, str, str]]:
        """Return list of (tier_name, model_name, provider)."""
        models = [
            ("Primary", self.settings.primary_model),
            ("Fallback-1", self.settings.fallback_model_1),
            ("Fallback-2", self.settings.fallback_model_2),
        ]
        return [
            (tier_name, model, self._detect_provider(model))
            for tier_name, model in models
            if model
        ]

    async def _call_gemini(
        self,
        model: str,
        prompt: str,
        system_prompt: str | None,
        json_mode: bool,
        temperature: float | None,
    ) -> str:
        from google.genai import types

        client = self._get_gemini_client()
        temp = temperature if temperature is not None else self.settings.llm_temperature
        config = types.GenerateContentConfig(
            temperature=temp,
            system_instruction=system_prompt,
            response_mime_type="application/json" if json_mode else None,
        )
        response = await client.aio.models.generate_content(
            model=model,
            contents=prompt,
            config=config,
        )
        return response.text or ""

    async def _call_openai(
        self,
        model: str,
        prompt: str,
        system_prompt: str | None,
        json_mode: bool,
        temperature: float | None,
    ) -> str:
        client = self._get_openai_client()
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        temp = temperature if temperature is not None else self.settings.llm_temperature
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temp,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        response = await client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        return choice.message.content or ""

    async def generate_text(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        temperature: float | None = None,
        json_mode: bool = False,
    ) -> str:
        """Call LLM with automatic 2-tier fallback across providers and models."""
        tiers = self.get_tiers()
        errors: list[str] = []

        for tier_name, model, provider in tiers:
            try:
                logger.info(f"Invoking LLM [{tier_name}]: provider={provider}, model={model}")
                if provider == "gemini":
                    result = await self._call_gemini(
                        model=model,
                        prompt=prompt,
                        system_prompt=system_prompt,
                        json_mode=json_mode,
                        temperature=temperature,
                    )
                elif provider == "openai":
                    result = await self._call_openai(
                        model=model,
                        prompt=prompt,
                        system_prompt=system_prompt,
                        json_mode=json_mode,
                        temperature=temperature,
                    )
                else:
                    raise ValueError(f"Unknown provider: {provider}")

                if result:
                    return result
                raise ValueError(f"Empty response from {model}")

            except Exception as exc:
                err_msg = f"[{tier_name}] provider={provider}, model={model} failed: {exc}"
                logger.warning(err_msg)
                errors.append(err_msg)

        raise RuntimeError(
            "All LLM tiers (Primary and 2 Fallbacks) failed to complete request:\n"
            + "\n".join(f" - {err}" for err in errors)
        )

    async def generate_json(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        """Generate structured JSON response with code fence stripping and validation."""
        json_instruction = (
            "Respond ONLY with a valid JSON object matching the requested specification. "
            "Do NOT include markdown fences or explanatory text."
        )
        effective_system = (
            f"{system_prompt}\n\n{json_instruction}" if system_prompt else json_instruction
        )

        raw_text = await self.generate_text(
            prompt=prompt,
            system_prompt=effective_system,
            temperature=temperature,
            json_mode=True,
        )

        cleaned = clean_json_text(raw_text)
        try:
            parsed = json.loads(cleaned)
            if not isinstance(parsed, dict):
                raise ValueError(f"Expected a JSON object, got {type(parsed).__name__}")
            return parsed
        except json.JSONDecodeError as exc:
            raise ValueError(f"Failed to parse JSON response: {exc}\nRaw text: {raw_text}") from exc
