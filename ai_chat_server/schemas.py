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
    session_id: str | None = None
    user_message_id: str | None = None
    assistant_message_id: str | None = None


class SessionCreate(BaseModel):
    title: str = Field(default="", max_length=200)
    model: str = Field(default="", max_length=100)
    web_search: bool = False


class SessionPatch(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    model: str | None = Field(default=None, max_length=100)
    web_search: bool | None = None
    pinned: bool | None = None


class SessionMessageCreate(BaseModel):
    id: str = Field(max_length=100)
    role: str = Field(pattern="^(user|assistant|system)$")
    content: str = Field(default="", max_length=200000)
    images: list[str] = []
    timestamp: int = 0


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=128)
    age: int | None = Field(default=None, ge=1, le=120)
    birthday: str | None = Field(default=None, max_length=16)
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
    role: str | None = Field(
        default=None,
        pattern="^(super_admin|system_admin|model_admin|user|subscriber)$",
    )
    province: str | None = Field(default=None, max_length=32)
    city: str | None = Field(default=None, max_length=32)
    district: str | None = Field(default=None, max_length=32)
    age: int | None = Field(default=None, ge=1, le=120)
    birthday: str | None = Field(default=None, max_length=16)
    gender: str | None = Field(default=None, pattern="^(male|female|other)$")


class ProfileUpdate(BaseModel):
    username: str | None = Field(default=None, min_length=3, max_length=32)
    avatar: str | None = Field(default=None, max_length=400000)
    birthday: str | None = Field(default=None, max_length=16)
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
    supports_search: bool = True
    enabled: bool = True
    sort_order: int = 0
    is_default: bool = False


class SettingsPayload(BaseModel):
    value: str | None = None
    remark: str | None = None
    enabled: bool | None = None