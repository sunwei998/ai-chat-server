from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局配置，从 server/.env 读取（pydantic-settings 大小写不敏感）。"""

    siliconflow_api_key: str = ""
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    jwt_secret: str = "dev-secret-change-me"
    jwt_expire_days: int = 7
    admin_username: str = "admin"
    admin_password: str = "admin123"
    db_path: str = "data/chat.db"
    cors_origins: str = "*"
    request_timeout: float = 300.0
    login_rate_per_minute: int = 10
    register_rate_per_hour: int = 5
    chat_rate_per_minute: int = 30

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()