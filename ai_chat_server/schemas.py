from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: str = Field(pattern="^(user|assistant|system)$")
    content: str
    images: list[str] = []


class ChatRequest(BaseModel):
    model: str
    messages: list[ChatMessage]


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=6, max_length=128)
    province: str = Field(default="", max_length=32)
    city: str = Field(default="", max_length=32)
    district: str = Field(default="", max_length=32)


class LoginRequest(BaseModel):
    username: str
    password: str


class UserUpdate(BaseModel):
    is_active: bool | None = None
    role: str | None = Field(default=None, pattern="^(user|admin)$")
    province: str | None = Field(default=None, max_length=32)
    city: str | None = Field(default=None, max_length=32)
    district: str | None = Field(default=None, max_length=32)


class ResetPasswordRequest(BaseModel):
    password: str = Field(min_length=6, max_length=128)


class ModelPayload(BaseModel):
    model_key: str
    name: str
    provider: str = "openai"
    free: bool = False
    vision: bool = False
    enabled: bool = True
    sort_order: int = 0


class SettingsPayload(BaseModel):
    value: str