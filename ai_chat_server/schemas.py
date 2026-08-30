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
    model_key: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    name_en: str = Field(default="", max_length=100)
    provider: str = Field(default="openai", max_length=50)
    free: bool = False
    vision: bool = False
    supports_search: bool = True
    enabled: bool = True
    sort_order: int = Field(default=0, ge=0, le=999999)
    is_default: bool = False

    @field_validator("model_key", "name", "name_en", "provider", mode="before")
    @classmethod
    def _strip_str(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("model_key")
    @classmethod
    def _key_no_whitespace(cls, v):
        if any(c.isspace() for c in v):
            raise ValueError("model_key 不能包含空白字符")
        return v

    @field_validator("name_en")
    @classmethod
    def _name_en_no_chinese(cls, v):
        if v and any("一" <= c <= "鿿" or "㐀" <= c <= "䶿" for c in v):
            raise ValueError("name_en 不能包含中文")
        return v


class SettingsPayload(BaseModel):
    value: str | None = Field(default=None, max_length=4999)
    remark: str | None = Field(default=None, max_length=255)
    enabled: bool | None = None


class SettingLogOut(BaseModel):
    id: int
    setting_key: str
    content: str
    operator: str
    created_at: int


class SettingLogList(BaseModel):
    items: list[SettingLogOut]
    total: int
    page: int
    pageSize: int


class OperationLogOut(BaseModel):
    id: int
    entity: str
    entity_id: int
    content: str
    operator: str
    created_at: int


class OperationLogList(BaseModel):
    items: list[OperationLogOut]
    total: int
    page: int
    pageSize: int


# ============ 通用维表（dim_tables / dim_values） ============


class DimTableOut(BaseModel):
    id: int
    code: str
    name: str
    description: str
    sort_order: int
    value_count: int = 0
    created_at: int | None = None
    updated_at: int | None = None
    updated_by: str = ""


class DimValueOut(BaseModel):
    id: int
    table_id: int
    code: str
    name: str
    name_en: str = ""
    api_key: str = ""
    sort_order: int
    enabled: int
    remark: str
    created_at: int | None = None
    updated_at: int | None = None


class DimValueList(BaseModel):
    items: list[DimValueOut]
    total: int
    page: int
    pageSize: int


class DimTableCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=64)
    description: str = ""


class DimTableUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=64)
    description: str | None = None
    sort_order: int | None = None


class DimValueCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    name: str = Field(min_length=1, max_length=128)
    name_en: str = Field(default="", max_length=128)
    api_key: str = Field(default="", max_length=512)
    sort_order: int = Field(default=0, ge=0, le=999999)
    enabled: bool = True
    remark: str = Field(default="", max_length=255)


class DimValueUpdate(BaseModel):
    # 传 None 表示"不更新该字段"（Pydantic 对 None 跳过 pattern/ge 校验，与 admin.py 语义一致）
    code: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    name: str | None = Field(default=None, min_length=1, max_length=128)
    name_en: str | None = Field(default=None, max_length=128)
    api_key: str | None = Field(default=None, max_length=512)
    sort_order: int | None = Field(default=None, ge=0, le=999999)
    enabled: bool | None = None
    remark: str | None = Field(default=None, max_length=255)