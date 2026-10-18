"""Result records produced by the engine. Plain dataclasses so they pickle across processes."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

import pandas as pd


class Status:
    OK = "ok"
    MISSING_SIGNAL = "missing_signal"
    MOMENT_NOT_FOUND = "moment_not_found"
    NO_SAMPLES = "no_samples"
    ERROR = "error"


@dataclass
class Occurrence:
    file: str
    event: str
    index: int  # 1-based within file+event
    t_start: float
    t_end: float
    t_window_start: float
    t_window_end: float


@dataclass
class MomentRecord:
    file: str
    event: str
    occurrence: int
    moment: str
    time: Optional[float]
    status: str
    message: str = ""


@dataclass
class KpiResult:
    file: str
    event: str
    occurrence: int
    kpi: str
    value: Optional[float]
    unit: str
    moment_time: Optional[float]
    status: str
    message: str = ""
    text: str = ""   # a text result (KPIs of the 'which' form), e.g. "PB + FB"; value then holds its code


@dataclass
class ChannelUse:
    signal: str
    channel: str
    group: Optional[int]
    file_unit: str
    converted_to: str = ""


@dataclass
class FileIssue:
    severity: str  # error | warning | info
    message: str


@dataclass
class Trace:
    """Signals captured around one occurrence for verification plots."""

    file: str
    event: str
    occurrence: int
    time: list
    signals: dict  # name -> list[float]


@dataclass
class FileOutcome:
    file: str
    status: str = Status.OK  # ok | error
    duration_s: Optional[float] = None
    occurrences: list = field(default_factory=list)
    moments: list = field(default_factory=list)
    results: list = field(default_factory=list)
    channels: list = field(default_factory=list)
    issues: list = field(default_factory=list)
    traces: list = field(default_factory=list)

    def error(self, message: str) -> "FileOutcome":
        self.status = Status.ERROR
        self.issues.append(FileIssue("error", message))
        return self


def to_frame(records: list) -> pd.DataFrame:
    return pd.DataFrame([asdict(r) for r in records])
