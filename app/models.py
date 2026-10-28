"""Pydantic schemas shared by the pipeline, API, exporter and UI."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

ComponentStatus = Literal["yes", "no", "not_stated"]
MatchLevel = Literal["same", "near", "partial", "rejected"]
SellerType = Literal["DMC", "Tour operator", "OTA", "Marketplace", "Retailer", "Other"]

STATUSES = (
    "uploaded", "extracting", "awaiting_review", "queued",
    "searching", "verifying", "matching", "completed", "failed",
)


class Component(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    category: str = "other"  # hotel | meal | transfer | tour | attraction | activity | other
    required: bool = True
    match_rule: Optional[str] = None  # one line: what a competitor page must show to count as the SAME service

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        return " ".join(v.split())


class ReferencePackage(BaseModel):
    title: str = ""
    destination: str = ""
    country: Optional[str] = None
    cities: list[str] = Field(default_factory=list)
    nights: Optional[int] = Field(default=None, ge=0, le=60)
    days: Optional[int] = Field(default=None, ge=0, le=61)
    hotel_star: Optional[int] = Field(default=None, ge=1, le=5)
    hotel_names: list[str] = Field(default_factory=list)
    price_amount: Optional[float] = Field(default=None, ge=0)
    price_currency: str = "USD"
    price_basis: Optional[str] = "per person"
    operator: Optional[str] = None
    transfers: Optional[str] = None  # SIC / private / None
    flights_included: Optional[bool] = False
    visa_included: Optional[bool] = None
    meals_summary: Optional[str] = None
    group_size: Optional[int] = None
    travel_month: Optional[str] = None
    cancellation_policy: Optional[str] = None
    exclusions: list[str] = Field(default_factory=list)
    availability_range: Optional[str] = None
    components: list[Component] = Field(default_factory=list)
    itinerary_summary: Optional[str] = None
    extraction_notes: Optional[str] = None

    @field_validator("price_currency")
    @classmethod
    def _cur(cls, v: str) -> str:
        v = (v or "USD").strip().upper()
        return v if len(v) == 3 else "USD"

    def search_label(self) -> str:
        bits = [self.destination or self.title]
        if self.nights:
            bits.append(f"{self.nights}N")
        if self.days:
            bits.append(f"{self.days}D")
        return " ".join(bits)


class Offer(BaseModel):
    seller: str = ""
    seller_type: SellerType = "Other"
    listed_on: str = ""
    package_name: str = ""
    hotel: Optional[str] = None
    hotel_star: Optional[int] = None
    nights: Optional[int] = None
    days: Optional[int] = None
    destination: Optional[str] = None
    price_from: Optional[float] = None
    price_to: Optional[float] = None
    currency: str = "USD"
    price_basis: Optional[str] = None
    price_pax: Optional[int] = None
    price_usd_from: Optional[float] = None
    price_usd_to: Optional[float] = None
    flights_included: Optional[bool] = None
    transfers: Optional[str] = None
    visa_included: Optional[bool] = None
    offer_kind: str = "multi_day_package"  # multi_day_package | single_activity | activity_combo | hotel_only | flight_only | other
    price_scope: str = "entire_package"  # entire_package | single_item | unclear
    component_matches: dict[str, ComponentStatus] = Field(default_factory=dict)
    evidence: dict[str, str] = Field(default_factory=dict)
    missing: list[str] = Field(default_factory=list)  # your components this offer does not prove
    extras: list[str] = Field(default_factory=list)  # things THEY include that your package does not
    extra_evidence: dict[str, str] = Field(default_factory=dict)
    value_verdict: str = ""  # cheaper_more | cheaper_less | similar | pricier_more | pricier_less
    value_note: str = ""
    reject_reason: str = ""
    match_score: float = 0.0
    match_level: MatchLevel = "rejected"
    components_matched: int = 0
    components_total: int = 0
    in_stats: bool = False  # used in the main price comparison (Same + Near)
    in_wide: bool = False  # any whole package with a trustworthy price (incl. partial matches)
    price_verified: bool = False
    verification: Literal["page", "snippet"] = "page"
    flags: list[str] = Field(default_factory=list)
    notes: str = ""
    url: str = ""
    availability_dates: Optional[str] = None

    @field_validator("currency")
    @classmethod
    def _cur(cls, v: str) -> str:
        v = (v or "USD").strip().upper()
        return v if len(v) == 3 else "USD"


class AnalysisOptions(BaseModel):
    review_before_search: bool = True
    depth: Literal["fast", "standard", "deep"] = "standard"
    strictness: Literal["strict", "standard", "relaxed"] = "standard"
    expand_links: bool = True
    max_candidates: Optional[int] = Field(default=None, ge=1, le=200)
    max_queries: Optional[int] = Field(default=None, ge=1, le=40)
    include_flight_packages_in_stats: bool = False
    extra_queries: list[str] = Field(default_factory=list)


class AnalysisSummary(BaseModel):
    id: str
    created_at: str
    updated_at: str
    status: str
    stage: str = ""
    progress: int = 0
    message: str = ""
    error: Optional[str] = None
    image_name: str = ""
    title: str = ""
    offers_count: int = 0


class StartRequest(BaseModel):
    reference: Optional[ReferencePackage] = None
    options: Optional[AnalysisOptions] = None
