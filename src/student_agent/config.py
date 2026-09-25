from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

TEAM_KEY_PATTERN = re.compile(r"^sk-team-[A-Za-z0-9_-]{16,128}$")


@dataclass(frozen=True)
class Settings:
    competition_api_url: str
    team_api_key: str
    mcp_endpoint: str
    root: Path
    gemini_api_key: str | None = None
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    primary_model: str = "gemini-1.5-flash-8b"
    fallback_model_1: str = "gemma-2-9b-it"
    fallback_model_2: str = "qwen2.5-7b-instruct"
    llm_timeout: float = 60.0
    llm_temperature: float = 0.1

    @classmethod
    def load(cls, root: Path | None = None) -> Settings:
        resolved_root = (root or Path.cwd()).resolve()
        load_dotenv(resolved_root / ".env")
        api_url = os.getenv("COMPETITION_API_URL", "").strip().rstrip("/")
        team_key = os.getenv("COMPETITION_TEAM_API_KEY", "").strip()
        mcp_endpoint = os.getenv("MCP_ENDPOINT", "").strip()
        errors: list[str] = []
        if not api_url.startswith(("http://", "https://")):
            errors.append("COMPETITION_API_URL must be an absolute HTTP(S) URL")
        if not TEAM_KEY_PATTERN.fullmatch(team_key):
            errors.append("COMPETITION_TEAM_API_KEY must use the sk-team-... format")
        if not mcp_endpoint.startswith(("http://", "https://")):
            errors.append("MCP_ENDPOINT must be an absolute HTTP(S) URL")
        if errors:
            raise ValueError("; ".join(errors))

        gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip() or None
        openai_api_key = os.getenv("OPENAI_API_KEY", "").strip() or None
        openai_base_url = os.getenv("OPENAI_BASE_URL", "").strip() or None
        primary_model = os.getenv("PRIMARY_MODEL", "gemini-1.5-flash-8b").strip()
        fallback_model_1 = os.getenv("FALLBACK_MODEL_1", "gemma-2-9b-it").strip()
        fallback_model_2 = os.getenv("FALLBACK_MODEL_2", "qwen2.5-7b-instruct").strip()

        try:
            llm_timeout = float(os.getenv("LLM_TIMEOUT", "60.0").strip())
        except ValueError:
            llm_timeout = 60.0

        try:
            llm_temperature = float(os.getenv("LLM_TEMPERATURE", "0.1").strip())
        except ValueError:
            llm_temperature = 0.1

        return cls(
            competition_api_url=api_url,
            team_api_key=team_key,
            mcp_endpoint=mcp_endpoint,
            root=resolved_root,
            gemini_api_key=gemini_api_key,
            openai_api_key=openai_api_key,
            openai_base_url=openai_base_url,
            primary_model=primary_model,
            fallback_model_1=fallback_model_1,
            fallback_model_2=fallback_model_2,
            llm_timeout=llm_timeout,
            llm_temperature=llm_temperature,
        )
