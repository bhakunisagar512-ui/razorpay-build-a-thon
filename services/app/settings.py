from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    database_url: str = "postgresql://recovery:recovery@localhost:5432/recovery"
    redis_url: str = "redis://localhost:6379/0"
    temporal_address: str = "localhost:7233"
    temporal_task_queue: str = "recovery-q"
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""
    llm_provider: str = "gemini"
    llm_api_key: str = ""
    llm_model: str = "gemini-2.0-flash"
    llm_triage_enabled: bool = True
    demo_mode: bool = True
    channel_mode: str = "mock"
    holdout_pct: int = 10

settings = Settings()
