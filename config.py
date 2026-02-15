import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Pydantic AI model string — e.g. "anthropic:claude-sonnet-4-20250514", "openai:gpt-4o", "google-gla:gemini-2.0-flash"
    AI_MODEL = os.getenv("AI_MODEL", "anthropic:claude-sonnet-4-20250514")

    # API Keys (Pydantic AI reads these from env automatically, but we keep them for reference)
    GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

    # GitHub
    GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
    GITHUB_REPO = os.getenv("GITHUB_REPO", "")

    # Server
    PORT = int(os.getenv("PORT", "5001"))
    DEBUG = os.getenv("DEBUG", "true").lower() == "true"

    # Repo cloning
    CLONE_BASE_DIR = os.getenv("CLONE_BASE_DIR", "/tmp/autoduty-repos")

    # Pipeline retry settings
    MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))

    # Sandbox budget — max number of sandbox runs per incident
    MAX_SANDBOX_RUNS = int(os.getenv("MAX_SANDBOX_RUNS", "5"))
