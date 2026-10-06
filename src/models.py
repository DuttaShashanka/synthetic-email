from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class ParsedEmail:
    sender: str = ""
    recipients: List[str] = field(default_factory=list)
    date: str = ""
    subject: str = ""
    body: str = ""


@dataclass
class TransformContext:
    person_map: Dict[str, str] = field(default_factory=dict)
    org_map: Dict[str, str] = field(default_factory=dict)
    email_map: Dict[str, str] = field(default_factory=dict)
    phone_map: Dict[str, str] = field(default_factory=dict)
    date_map: Dict[str, str] = field(default_factory=dict)
    money_map: Dict[str, str] = field(default_factory=dict)
    location_map: Dict[str, str] = field(default_factory=dict)
    project_map: Dict[str, str] = field(default_factory=dict)
    replacements: List[Dict[str, str]] = field(default_factory=list)


@dataclass
class ValidationReport:
    valid: bool
    risk_score: float
    errors: List[str]
    warnings: List[str]
    metrics: Dict[str, float]
