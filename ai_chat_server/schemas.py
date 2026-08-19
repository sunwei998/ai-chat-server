import re

from pydantic import BaseModel, Field, field_validator


class ChatMessage(BaseModel):
    role: str = Field(pattern="^(user|assistant|system)$")
    content: str
    images: list[str] = []


class ChatRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    web_search: bool = False


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=128)
    age: int = Field(ge=1, le=120)
    gender: str = Field(default="", max_length=16)
    province: str = Field(default="", max_length=32)
    city: str = Field(default="", max_length=32)
    district: str = Field(default="", max_length=32)

    @field_validator("password")
    @classmethod
    def _password_strength(cls, v: str) -> str:
        kinds = 0
        if re.search(r"[a-zA-Z]", v):
            kinds += 1
        if re.search(r"[0-9]", v):
            kinds += 1
        if re.search(r"[^a-zA-Z0-9]", v):
            kinds += 1
        if kinds < 2:
            raise ValueError("密码必须包含字母、数字、特殊符号中的至少两类")
        return v


class LoginRequest(BaseModel):
    username: str
    password: str


class UserUpdate(BaseModel):
    is_active: bool | None = None
    role: str | None = Field(default=None, pattern="^(user|admin)$")
    province: str | None = Field(default=None, max_length=32)
    city: str | None = Field(default=None, max_length=32)
    district: str | None = Field(default=None, max_length=32)
    age: int | None = Field(default=None, ge=1, le=120)
    gender: str | None = Field(default=None, pattern="^(male|female|other)$")


class ProfileUpdate(BaseModel):
    username: str | None = Field(default=None, min_length=3, max_length=32)
    avatar: str | None = Field(default=None, max_length=400000)
    age: int | None = Field(default=None, ge=1, le=120)
    gender: str | None = Field(default=None, pattern="^(male|female|other)?$")
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


class SuggestionPayload(BaseModel):
    title_zh: str = Field(min_length=1, max_length=60)
    title_en: str = Field(min_length=1, max_length=60)
    sort_order: int = 0
    enabled: bool = True