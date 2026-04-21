"""Прикладные контракты, которые реализуют сетевые модули.

Модули НЕ обязаны реализовывать всё. Facebook Master сейчас реализует
только `IAccountConnector` / `IAudienceDiscoveryProvider` / `ITemplateProvider`
через адаптеры к существующим сервисам. Reddit Master реализует полный
набор сам.

Важно: это контракты-маркеры на M1 — сигнатуры не финализированы. Их
доведём по мере того, как реальные адаптеры FB и реальные сервисы Reddit
будут готовы (M3/M4/M5). Пока что они служат:
  1) документацией целевой архитектуры;
  2) точкой, вокруг которой группируется код модулей.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


# ── общие value-objects ─────────────────────────────────


@dataclass(frozen=True)
class AccountRef:
    """Сетенезависимая ссылка на connected account."""

    module_id: str
    account_id: int  # primary key из таблицы модуля
    display_label: str


@dataclass(frozen=True)
class SourceRef:
    """Источник аудитории: FB donor / Reddit subreddit / etc."""

    module_id: str
    kind: str  # "donor", "subreddit", "hashtag", ...
    ref: str  # url или name


@dataclass(frozen=True)
class ProspectRef:
    """Найденный человек / user-аккаунт."""

    module_id: str
    kind: str  # "fb_person", "reddit_user"
    ref: str  # url / username
    display_label: str | None = None


@dataclass(frozen=True)
class ActivitySignal:
    """Недавняя видимая активность (post/comment/reaction).

    ВАЖНО: это НЕ online-status. Reddit не даёт online-сигнал, и мы
    сознательно не используем формулировку "был онлайн". Только
    `recent visible activity`.
    """

    prospect: ProspectRef
    kind: str  # "post" | "comment" | "reaction" | ...
    observed_at: datetime
    context_ref: str | None = None  # url поста / треда
    excerpt: str | None = None


# ── account ─────────────────────────────────────────────


class IAccountConnector(ABC):
    """Подключение и здоровье аккаунта (FB / Reddit / …)."""

    @abstractmethod
    def list_accounts(self, organization_id: int) -> list[AccountRef]:
        ...

    @abstractmethod
    def probe_readiness(self, account_ref: AccountRef) -> "ReadinessReport":
        ...

    @abstractmethod
    def capabilities_for(self, account_ref: AccountRef) -> tuple[str, ...]:
        """Capability-id, доступные на конкретном аккаунте прямо сейчас."""


@dataclass(frozen=True)
class ReadinessReport:
    """Сводка готовности аккаунта к работе."""

    account: AccountRef
    overall_ok: bool
    checks: dict[str, str] = field(default_factory=dict)  # name -> status label
    warnings: tuple[str, ...] = ()


# ── audience discovery / activity ───────────────────────


@dataclass(frozen=True)
class SourceQuery:
    module_id: str
    keyword: str | None = None
    topic: str | None = None
    min_size: int | None = None
    max_size: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProspectQuery:
    module_id: str
    sources: tuple[SourceRef, ...] = ()
    keyword: str | None = None
    activity_window_hours: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class IAudienceDiscoveryProvider(ABC):
    @abstractmethod
    def search_sources(self, query: SourceQuery) -> list[SourceRef]: ...

    @abstractmethod
    def search_prospects(self, query: ProspectQuery) -> list[ProspectRef]: ...


class IActivityFeedProvider(ABC):
    @abstractmethod
    def recent_visible_activity(
        self, prospect: ProspectRef, window_hours: int
    ) -> list[ActivitySignal]:
        ...


# ── conversation / content ──────────────────────────────


class ReviewMode(str, Enum):
    MANUAL = "manual"
    SEMI = "semi"
    AUTO = "auto"  # осознанно скрыт из default-UX


@dataclass(frozen=True)
class MessageDraft:
    module_id: str
    account: AccountRef
    prospect: ProspectRef
    body: str
    context_ref: str | None = None  # тред / пост / т.п.
    review_mode: ReviewMode = ReviewMode.MANUAL


@dataclass(frozen=True)
class SendOutcome:
    ok: bool
    reason_code: str | None = None  # "rate_limited" | "chat_restricted" | ...
    notes: str = ""


class IConversationProvider(ABC):
    @abstractmethod
    def list_threads(self, account: AccountRef) -> list[Any]: ...

    @abstractmethod
    def enqueue_send(self, draft: MessageDraft) -> SendOutcome:
        """Поставить в очередь отправку. review_mode диктует, когда реально отправить."""


@dataclass(frozen=True)
class CommentDraft:
    module_id: str
    account: AccountRef
    target_ref: str  # пост / тред
    body: str
    review_mode: ReviewMode = ReviewMode.MANUAL


class IContentInteractionProvider(ABC):
    @abstractmethod
    def enqueue_comment(self, draft: CommentDraft) -> SendOutcome: ...


# ── campaigns / templates / sequences ───────────────────


class ITemplateProvider(ABC):
    @abstractmethod
    def list_templates(self, organization_id: int) -> list[Any]: ...


class ICampaignProvider(ABC):
    @abstractmethod
    def list_campaigns(self, organization_id: int) -> list[Any]: ...


class IAutomationScenarioProvider(ABC):
    @abstractmethod
    def list_sequences(self, organization_id: int) -> list[Any]: ...


# ── compliance / rate-limit / identity ──────────────────


@dataclass(frozen=True)
class ComplianceVerdict:
    allow: bool
    soft_warnings: tuple[str, ...] = ()
    hard_stops: tuple[str, ...] = ()


class IComplianceProvider(ABC):
    @abstractmethod
    def evaluate(self, account: AccountRef, action_kind: str, payload: dict) -> ComplianceVerdict: ...


class IRateLimitProvider(ABC):
    @abstractmethod
    def current_state(self, account: AccountRef) -> dict[str, Any]: ...


class IIdentityResolutionProvider(ABC):
    """Сопоставление prospect в модуле с общей сущностью (например, Person)."""

    @abstractmethod
    def resolve_or_create(
        self, organization_id: int, prospect: ProspectRef
    ) -> int:
        """Вернуть id в общей таблице (Person) или создать запись."""


__all__ = [
    "AccountRef",
    "ActivitySignal",
    "CommentDraft",
    "ComplianceVerdict",
    "IAccountConnector",
    "IActivityFeedProvider",
    "IAudienceDiscoveryProvider",
    "IAutomationScenarioProvider",
    "ICampaignProvider",
    "IComplianceProvider",
    "IContentInteractionProvider",
    "IConversationProvider",
    "IIdentityResolutionProvider",
    "IRateLimitProvider",
    "ITemplateProvider",
    "MessageDraft",
    "ProspectQuery",
    "ProspectRef",
    "ReadinessReport",
    "ReviewMode",
    "SendOutcome",
    "SourceQuery",
    "SourceRef",
]
