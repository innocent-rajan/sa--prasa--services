"""Pydantic models for the services API."""
from datetime import datetime

from pydantic import BaseModel, Field


# ---------- Service Status ----------
class ServiceStatusCreate(BaseModel):
    scope_type: str = Field(pattern="^(route|stop|network)$")
    scope_id: int | None = None
    status_type: str = Field(pattern="^(delay|disruption|cancellation)$")
    severity: str = Field(default="minor", pattern="^(minor|major|severe)$")
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    info_status: str = Field(default="manual", pattern="^(live|manual)$")
    effective_from: datetime
    effective_to: datetime | None = None


class ServiceStatusUpdate(BaseModel):
    scope_type: str | None = Field(default=None, pattern="^(route|stop|network)$")
    scope_id: int | None = None
    status_type: str | None = Field(default=None, pattern="^(delay|disruption|cancellation)$")
    severity: str | None = Field(default=None, pattern="^(minor|major|severe)$")
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    info_status: str | None = Field(default=None, pattern="^(live|manual)$")
    effective_from: datetime | None = None
    effective_to: datetime | None = None


class ServiceStatusOut(BaseModel):
    id: int
    scope_type: str
    scope_id: int | None
    status_type: str
    severity: str
    title: str
    description: str
    info_status: str
    effective_from: str
    effective_to: str | None
    created_at: str
    updated_at: str


# ---------- Advisories ----------
class AdvisoryCreate(BaseModel):
    category: str = Field(min_length=1, max_length=50)
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=5000)
    scope_type: str = Field(default="network", pattern="^(route|stop|network)$")
    scope_id: int | None = None
    published_at: datetime
    expires_at: datetime | None = None
    is_active: bool = True


class AdvisoryUpdate(BaseModel):
    category: str | None = Field(default=None, min_length=1, max_length=50)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    body: str | None = Field(default=None, min_length=1, max_length=5000)
    scope_type: str | None = Field(default=None, pattern="^(route|stop|network)$")
    scope_id: int | None = None
    published_at: datetime | None = None
    expires_at: datetime | None = None
    is_active: bool | None = None


class AdvisoryOut(BaseModel):
    id: int
    category: str
    title: str
    body: str
    scope_type: str
    scope_id: int | None
    published_at: str
    expires_at: str | None
    is_active: bool
    created_at: str
    updated_at: str


# ---------- Knowledge Base ----------
class KbArticleCreate(BaseModel):
    slug: str = Field(min_length=1, max_length=120, pattern="^[a-z0-9-]+$")
    category: str = Field(min_length=1, max_length=50)
    language: str = Field(default="en", min_length=2, max_length=5)
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=20000)
    is_published: bool = False


class KbArticleUpdate(BaseModel):
    category: str | None = Field(default=None, min_length=1, max_length=50)
    language: str | None = Field(default=None, min_length=1, max_length=5)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    body: str | None = Field(default=None, min_length=1, max_length=20000)
    is_published: bool | None = None


class KbArticleOut(BaseModel):
    id: int
    slug: str
    category: str
    language: str
    title: str
    body: str
    is_published: bool
    created_at: str
    updated_at: str


# ---------- Notifications ----------
class PreferenceItem(BaseModel):
    topic: str = Field(min_length=1, max_length=50)
    channel: str = Field(min_length=1, max_length=20)
    enabled: bool


class PreferencesPut(BaseModel):
    preferences: list[PreferenceItem] = Field(min_length=1)


class PreferenceOut(BaseModel):
    topic: str
    channel: str
    enabled: bool
    updated_at: str


class TopicOut(BaseModel):
    code: str
    name: str
    description: str


class SendRequest(BaseModel):
    topic: str = Field(min_length=1, max_length=50)
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=2000)


class DispatchResult(BaseModel):
    sent: int
    failed: int


# ---------- Admin ----------
class AuditOut(BaseModel):
    id: int
    actor: str
    action: str
    entity: str
    entity_id: str | None
    payload: str | None
    created_at: str


# ---------- Inquiries ----------
class InquiryCreate(BaseModel):
    category: str = Field(min_length=1, max_length=50,
                          description="Category code, e.g. lost_item | delay_complaint")
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=5000)
    user_key: str | None = Field(default=None, max_length=100,
                                 description="Anonymous device key (optional)")
    contact_name: str | None = Field(default=None, max_length=100)
    contact_email: str | None = Field(default=None, max_length=200)
    contact_phone: str | None = Field(default=None, max_length=30)
    scope_type: str | None = Field(default=None, pattern="^(route|stop|network)$")
    scope_id: int | None = None


class InquiryOut(BaseModel):
    reference: str
    category: str
    status: str
    title: str
    description: str
    scope_type: str | None
    scope_id: int | None
    created_at: str
    updated_at: str


class InquiryStatusOut(BaseModel):
    reference: str
    status: str
    timeline: list[dict]


class InquiryStatusUpdate(BaseModel):
    status: str = Field(pattern="^(in_progress|resolved|closed|rejected)$")
    note: str = Field(default="", max_length=2000)


class InquiryCategoryOut(BaseModel):
    code: str
    name: str
    description: str


class AttachmentOut(BaseModel):
    id: int
    file_name: str
    mime_type: str
    size_bytes: int


# ---------- Feedback ----------
class FeedbackCreate(BaseModel):
    feedback_type: str | None = Field(default=None,
                                      pattern="^(compliment|complaint|suggestion)$")
    rating: int | None = Field(default=None, ge=1, le=5)
    comment: str = Field(default="", max_length=5000)
    user_key: str | None = Field(default=None, max_length=100)
    is_anonymous: bool = False
    scope_type: str | None = Field(default=None, pattern="^(route|stop|network)$")
    scope_id: int | None = None


class FeedbackOut(BaseModel):
    id: int
    feedback_type: str | None
    rating: int | None
    is_anonymous: bool
    scope_type: str | None
    scope_id: int | None
    comment: str
    created_at: str


class FeedbackStatsOut(BaseModel):
    total: int
    by_type: dict[str, int]
    avg_rating: float | None
    rating_count: int