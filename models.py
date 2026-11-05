"""Data models for workouts and extensible parser base class."""

# pylint: disable=too-many-instance-attributes,too-few-public-methods

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Type


@dataclass
class WorkoutRecord:
    """Single time-series data point recorded during a workout."""

    timestamp: datetime
    elapsed_seconds: float
    distance_m: Optional[float] = None
    speed_m_s: Optional[float] = None
    heart_rate_bpm: Optional[int] = None
    cadence_rpm: Optional[int] = None
    altitude_m: Optional[float] = None
    power_watts: Optional[int] = None
    temperature_c: Optional[float] = None
    latitude_deg: Optional[float] = None
    longitude_deg: Optional[float] = None

    @property
    def pace_min_per_km(self) -> Optional[float]:
        """Return pace in min/km, or None if speed is too low/missing."""
        if self.speed_m_s is None or self.speed_m_s <= 0.3:
            return None
        return (1000.0 / self.speed_m_s) / 60.0

    @property
    def pace_min_per_mile(self) -> Optional[float]:
        """Return pace in min/mile, or None if speed is too low/missing."""
        if self.speed_m_s is None or self.speed_m_s <= 0.3:
            return None
        return (1609.344 / self.speed_m_s) / 60.0


@dataclass
class WorkoutLap:
    """Summary of a single lap within a workout."""

    lap_number: int
    start_time: Optional[datetime] = None
    total_elapsed_seconds: float = 0.0
    total_timer_seconds: float = 0.0
    total_distance_m: float = 0.0
    avg_speed_m_s: Optional[float] = None
    avg_heart_rate_bpm: Optional[int] = None
    max_heart_rate_bpm: Optional[int] = None
    avg_cadence_rpm: Optional[int] = None
    total_calories: Optional[int] = None
    total_ascent_m: Optional[float] = None


@dataclass
class WorkoutZone:
    """Watch-configured Heart Rate or Power zone range and time in zone."""

    zone_number: int
    label: str
    low_value: int
    high_value: Optional[int]
    time_seconds: float = 0.0


@dataclass
class WorkoutData:
    """Complete parsed representation of a workout session."""

    file_path: Path
    file_format: str
    device_name: str = "Garmin Device"
    sport: str = "Running"
    sub_sport: Optional[str] = None
    start_time: Optional[datetime] = None
    total_elapsed_seconds: float = 0.0
    total_timer_seconds: float = 0.0
    total_distance_m: float = 0.0
    total_calories: Optional[int] = None
    avg_speed_m_s: Optional[float] = None
    max_speed_m_s: Optional[float] = None
    avg_heart_rate_bpm: Optional[int] = None
    max_heart_rate_bpm: Optional[int] = None
    avg_cadence_rpm: Optional[int] = None
    max_cadence_rpm: Optional[int] = None
    avg_power_watts: Optional[int] = None
    max_power_watts: Optional[int] = None
    total_ascent_m: Optional[float] = None
    total_descent_m: Optional[float] = None
    records: List[WorkoutRecord] = field(default_factory=list)
    laps: List[WorkoutLap] = field(default_factory=list)
    hr_zones: List[WorkoutZone] = field(default_factory=list)
    power_zones: List[WorkoutZone] = field(default_factory=list)

    @property
    def active_duration_seconds(self) -> float:
        """Return timer duration if available, otherwise elapsed duration."""
        return (
            self.total_timer_seconds
            if self.total_timer_seconds > 0
            else self.total_elapsed_seconds
        )


class WorkoutParser(ABC):
    """Abstract base class for workout file parsers (.FIT, .TCX, .GPX)."""

    supported_extensions: List[str] = []

    @abstractmethod
    def parse(self, file_path: Path) -> WorkoutData:
        """Parse a workout file and return a unified WorkoutData instance."""
        raise NotImplementedError


class ParserRegistry:
    """Registry mapping file extensions to their WorkoutParser."""

    _parsers: Dict[str, Type[WorkoutParser]] = {}

    @classmethod
    def register(cls, parser_cls: Type[WorkoutParser]) -> Type[WorkoutParser]:
        """Register a WorkoutParser subclass for its supported extensions."""
        for ext in parser_cls.supported_extensions:
            cls._parsers[ext.lower()] = parser_cls
        return parser_cls

    @classmethod
    def parse_file(cls, file_path: str | Path) -> WorkoutData:
        """Dispatch file parsing to the registered parser for its suffix."""
        path = Path(file_path)
        ext = path.suffix.lower()
        if ext not in cls._parsers:
            supported = ", ".join(sorted(cls._parsers.keys()))
            raise ValueError(
                f"Unsupported file extension '{ext}'. "
                f"Supported formats: {supported}"
            )
        parser = cls._parsers[ext]()
        return parser.parse(path)

    @classmethod
    def get_supported_extensions(cls) -> List[str]:
        """Return sorted list of all registered file extensions."""
        return sorted(cls._parsers.keys())
