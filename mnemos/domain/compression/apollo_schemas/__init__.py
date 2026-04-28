"""APOLLO schemas — schema-aware dense encoding for LLM-to-LLM wire use.

Each schema declares a typed shape (portfolio, decision, person, event,
...) and implements detect + encode + narrate. APOLLOEngine composes
these via an ordered registry and runs them in specific-before-general
order.

v3.3 S-IC ships with PortfolioSchema as the first concrete schema,
derived from InvestorClaw's consultative-layer data model (field
shapes only, no code shared). Additional schemas (decision, person,
event) are scheduled for v3.3 S-II.
"""
from .base import DetectionResult, Schema
from .code import CodeSchema
from .commit import CommitSchema
from .decision import DecisionSchema
from .event import EventSchema
from .person import PersonSchema
from .portfolio import PortfolioSchema

__all__ = [
    "DetectionResult",
    "Schema",
    "PortfolioSchema",
    "DecisionSchema",
    "PersonSchema",
    "EventSchema",
    "CodeSchema",
    "CommitSchema",
]
