from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from student_agent.config import Settings
from student_agent.llm import LLMClient, clean_json_text


def test_clean_json_text() -> None:
    # Plain JSON
    assert clean_json_text('{"key": "value"}') == '{"key": "value"}'

    # JSON inside markdown fences
    markdown = '```json\n{\n  "action": "refund"\n}\n```'
    assert clean_json_text(markdown) == '{\n  "action": "refund"\n}'

    # JSON with conversational text before and after
    text = 'Here is the result:\n```json\n{"status": "ok"}\n```\nHope that helps!'
    assert clean_json_text(text) == '{"status": "ok"}'

    # Raw array with text
    array_text = 'Analysis list:\n["item1", "item2"]\nEnd.'
    assert clean_json_text(array_text) == '["item1", "item2"]'


def test_llm_client_tier_detection() -> None:
    settings = Settings(
        competition_api_url="http://localhost:8081",
        team_api_key="sk-team-test_api_key_123456",
        mcp_endpoint="http://localhost:8001/mcp",
        root=Path("."),
        gemini_api_key="fake-gemini-key",
        openai_api_key="fake-openai-key",
        primary_model="gemini-1.5-flash-8b",
        fallback_model_1="gemma-2-9b-it",
        fallback_model_2="qwen2.5-7b-instruct",
    )
    client = LLMClient(settings)
    tiers = client.get_tiers()

    assert len(tiers) == 3
    assert tiers[0] == ("Primary", "gemini-1.5-flash-8b", "gemini")
    assert tiers[1] == ("Fallback-1", "gemma-2-9b-it", "gemini")
    assert tiers[2] == ("Fallback-2", "qwen2.5-7b-instruct", "openai")


@pytest.mark.anyio
async def test_llm_client_fallback_to_tier_1() -> None:
    settings = Settings(
        competition_api_url="http://localhost:8081",
        team_api_key="sk-team-test_api_key_123456",
        mcp_endpoint="http://localhost:8001/mcp",
        root=Path("."),
        gemini_api_key="fake-gemini-key",
        openai_api_key="fake-openai-key",
        primary_model="gemini-2.5-flash",
        fallback_model_1="gemini-2.0-flash",
        fallback_model_2="gpt-4o-mini",
    )
    client = LLMClient(settings)

    # Simulate Primary failing, Fallback-1 succeeding
    async def mock_call_gemini(model, prompt, system_prompt, json_mode, temperature):
        if model == "gemini-2.5-flash":
            raise RuntimeError("Primary rate limited / 429")
        if model == "gemini-2.0-flash":
            return '{"status": "recovered_by_tier_1"}'
        raise ValueError(f"Unexpected model {model}")

    with patch.object(client, "_call_gemini", side_effect=mock_call_gemini):
        result = await client.generate_json("Test prompt")
        assert result == {"status": "recovered_by_tier_1"}


@pytest.mark.anyio
async def test_llm_client_fallback_to_tier_2_openai() -> None:
    settings = Settings(
        competition_api_url="http://localhost:8081",
        team_api_key="sk-team-test_api_key_123456",
        mcp_endpoint="http://localhost:8001/mcp",
        root=Path("."),
        gemini_api_key="fake-gemini-key",
        openai_api_key="fake-openai-key",
        primary_model="gemini-2.5-flash",
        fallback_model_1="gemini-2.0-flash",
        fallback_model_2="gpt-4o-mini",
    )
    client = LLMClient(settings)

    mock_gemini = patch.object(
        client, "_call_gemini", side_effect=RuntimeError("Gemini unavailable")
    )
    mock_openai = patch.object(
        client, "_call_openai", new=AsyncMock(return_value='{"status": "openai_success"}')
    )
    with mock_gemini, mock_openai:
        result = await client.generate_json("Test prompt")
        assert result == {"status": "openai_success"}


@pytest.mark.anyio
async def test_llm_client_fallback_on_invalid_json() -> None:
    settings = Settings(
        competition_api_url="http://localhost:8081",
        team_api_key="sk-team-test_api_key_123456",
        mcp_endpoint="http://localhost:8001/mcp",
        root=Path("."),
        gemini_api_key="fake-gemini-key",
        openai_api_key="fake-openai-key",
        primary_model="gemini-2.5-flash",
        fallback_model_1="gemini-2.0-flash",
        fallback_model_2="gpt-4o-mini",
    )
    client = LLMClient(settings)

    # Primary returns broken JSON, Fallback 1 returns valid JSON
    async def mock_call_gemini(model, prompt, system_prompt, json_mode, temperature):
        if model == "gemini-2.5-flash":
            return "This is not json at all {broken"
        if model == "gemini-2.0-flash":
            return '{"status": "valid_json_recovered"}'
        raise ValueError(f"Unexpected model {model}")

    with patch.object(client, "_call_gemini", side_effect=mock_call_gemini):
        result = await client.generate_json("Test prompt")
        assert result == {"status": "valid_json_recovered"}


@pytest.mark.anyio
async def test_llm_client_all_tiers_fail() -> None:
    settings = Settings(
        competition_api_url="http://localhost:8081",
        team_api_key="sk-team-test_api_key_123456",
        mcp_endpoint="http://localhost:8001/mcp",
        root=Path("."),
        gemini_api_key="fake-gemini-key",
        openai_api_key="fake-openai-key",
        primary_model="gemini-2.5-flash",
        fallback_model_1="gemini-2.0-flash",
        fallback_model_2="gpt-4o-mini",
    )
    client = LLMClient(settings)

    mock_gemini = patch.object(client, "_call_gemini", side_effect=RuntimeError("Gemini error"))
    mock_openai = patch.object(client, "_call_openai", side_effect=RuntimeError("OpenAI error"))
    with mock_gemini, mock_openai, pytest.raises(RuntimeError, match="All LLM tiers"):
        await client.generate_text("Test prompt")
