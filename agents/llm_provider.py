"""Multi-LLM abstraction layer.

Supports Google Gemini, Anthropic Claude, and OpenAI GPT via a unified
`generate(system_prompt, user_prompt, provider?) -> str` interface.
"""

import json
from abc import ABC, abstractmethod
from config import Config
from utils.logger import get_logger

log = get_logger("llm_provider")


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------
class LLMProvider(ABC):
    @abstractmethod
    def generate(self, system_prompt: str, user_prompt: str) -> str:
        ...


# ---------------------------------------------------------------------------
# Google Gemini
# ---------------------------------------------------------------------------
class GeminiProvider(LLMProvider):
    def __init__(self):
        import google.generativeai as genai

        genai.configure(api_key=Config.GOOGLE_API_KEY)
        self.model = genai.GenerativeModel(
            "gemini-1.5-pro",
            system_instruction=None,  # set per-call
        )
        log.info("Gemini provider initialized")

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        import google.generativeai as genai

        model = genai.GenerativeModel(
            "gemini-1.5-pro",
            system_instruction=system_prompt,
        )
        response = model.generate_content(user_prompt)
        return response.text


# ---------------------------------------------------------------------------
# Anthropic Claude
# ---------------------------------------------------------------------------
class AnthropicProvider(LLMProvider):
    def __init__(self):
        import anthropic

        self.client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)
        log.info("Anthropic provider initialized")

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        message = self.client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return message.content[0].text


# ---------------------------------------------------------------------------
# OpenAI GPT
# ---------------------------------------------------------------------------
class OpenAIProvider(LLMProvider):
    def __init__(self):
        import openai

        self.client = openai.OpenAI(api_key=Config.OPENAI_API_KEY)
        log.info("OpenAI provider initialized")

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        response = self.client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=4096,
        )
        return response.choices[0].message.content


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
_provider_cache: dict[str, LLMProvider] = {}

PROVIDERS = {
    "gemini": GeminiProvider,
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
}


def get_provider(name: str | None = None) -> LLMProvider:
    """Get (or create + cache) an LLM provider by name."""
    name = name or Config.LLM_PROVIDER
    if name not in PROVIDERS:
        raise ValueError(f"Unknown LLM provider: {name}. Choose from: {list(PROVIDERS.keys())}")
    if name not in _provider_cache:
        _provider_cache[name] = PROVIDERS[name]()
    return _provider_cache[name]


def generate(system_prompt: str, user_prompt: str, provider: str | None = None) -> str:
    """Convenience wrapper: generate text using the specified (or default) provider."""
    return get_provider(provider).generate(system_prompt, user_prompt)


def generate_json(system_prompt: str, user_prompt: str, provider: str | None = None) -> dict:
    """Generate and parse a JSON response. Strips markdown fences if present."""
    raw = generate(system_prompt, user_prompt, provider)
    # Strip ```json ... ``` fencing the LLMs sometimes add
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first line (```json) and last line (```)
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)
    return json.loads(text)
