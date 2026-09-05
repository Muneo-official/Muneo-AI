from dataclasses import dataclass


@dataclass
class RiskIssue:
    type: str
    process: str
    title: str
    detail: str
    guide: str
