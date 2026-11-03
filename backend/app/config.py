from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./data/jobflow.db"
    host: str = "127.0.0.1"
    port: int = 8000
    # --- LLM (swappable provider, Phase 3+) ---
    llm_provider: str = "openai"          # openai | anthropic | ollama | none
    llm_model: str = "gpt-4o-mini"
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    anthropic_api_key: str = ""
    ollama_base_url: str = "http://localhost:11434"
