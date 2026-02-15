import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # LLM Provider
    LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini")  # gemini | anthropic | openai

    # API Keys
    GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

    # GitHub
    GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
    GITHUB_REPO = os.getenv("GITHUB_REPO", "")

    # Server
    FLASK_PORT = int(os.getenv("FLASK_PORT", "5001"))
    FLASK_DEBUG = os.getenv("FLASK_DEBUG", "true").lower() == "true"
