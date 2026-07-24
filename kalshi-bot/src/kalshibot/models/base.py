"""Prediction model plugin interface.

A model receives a Market plus a MarketContext (which wraps an AsOfView -- so it
structurally cannot see the future) and returns a Prediction: a probability, an
uncertainty measure, and the structured inputs that produced it. The inputs are
persisted with every decision, which is what makes later calibration possible.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

from ..storage.dao import AsOfView


@dataclass(frozen=True)
class Market:
    ticker: str
    series: str
    city: Optional[str]
    # Parsed settlement threshold for temperature markets, if known.
    # kind: "above"/"below"/"between"; bounds in deg F.
    threshold_kind: Optional[str] = None
    lower_f: Optional[float] = None
    upper_f: Optional[float] = None
    target_date: Optional[str] = None
    nws_station: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None


@dataclass(frozen=True)
class MarketContext:
    """Everything a model is allowed to see, all of it as-of the decision time."""
    view: AsOfView
    decision_ts: int


@dataclass(frozen=True)
class Prediction:
    probability: float                       # P(market resolves YES), in [0, 1]
    uncertainty: Optional[float]             # e.g. std-dev of the driver, or None
    inputs: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 0.0 <= self.probability <= 1.0:
            raise ValueError(f"probability must be in [0,1], got {self.probability}")


class PredictionModel(ABC):
    name: str = "base"

    @abstractmethod
    def predict(self, market: Market, context: MarketContext) -> Optional[Prediction]:
        """Return a calibrated probability plus its inputs, or None if the model
        lacks the data to make a call (e.g. no forecast known yet as-of now)."""
        raise NotImplementedError
