"""Windows 11 Desktop GUI for Workout Analyzer."""

# pylint: disable=too-many-lines,too-many-ancestors
# pylint: disable=too-many-instance-attributes,too-many-locals
# pylint: disable=too-many-branches,too-many-statements,too-many-arguments
# pylint: disable=too-many-positional-arguments

from __future__ import annotations

import ctypes
import math
import threading
import tkinter as tk
import urllib.request
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Dict, List, Optional, Tuple

import fit_parser
from models import ParserRegistry, WorkoutData, WorkoutRecord

# Theme palette (Garmin Dark Modern UI)
BG_MAIN = "#0f172a"  # Slate 900
BG_SURFACE = "#1e293b"  # Slate 800
BG_CARD = "#1e293b"  # Slate 800
BG_CARD_BORDER = "#334155"  # Slate 700
FG_PRIMARY = "#f8fafc"  # Slate 50
FG_SECONDARY = "#94a3b8"  # Slate 400
ACCENT_BLUE = "#38bdf8"  # Sky 400
ACCENT_BTN = "#0284c7"  # Sky 600
ACCENT_BTN_HOVER = "#0369a1"

SERIES_COLORS = {
    "pace": "#38bdf8",  # Sky Blue
    "heart_rate": "#f43f5e",  # Coral Red
    "elevation": "#10b981",  # Emerald Green
    "cadence": "#a855f7",  # Purple
    "power": "#f59e0b",  # Amber
}

TILE_CACHE_DIR = Path(__file__).parent / ".tile_cache"


def format_duration(seconds: float) -> str:
    """Format duration in seconds as H:MM:SS or MM:SS."""
    total_sec = max(0, int(round(seconds)))
    hrs = total_sec // 3600
    mins = (total_sec % 3600) // 60
    secs = total_sec % 60
    if hrs > 0:
        return f"{hrs}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def format_pace(speed_m_s: Optional[float], imperial: bool) -> str:
    """Format speed (m/s) into pace string 'M:SS /mi' or 'M:SS /km'."""
    if speed_m_s is None or speed_m_s <= 0.3:
        return "--:--"
    unit_meters = 1609.344 if imperial else 1000.0
    unit_label = "/mi" if imperial else "/km"
    sec_per_unit = unit_meters / speed_m_s
    mins = int(sec_per_unit // 60)
    secs = int(round(sec_per_unit % 60))
    if secs == 60:
        mins += 1
        secs = 0
    return f"{mins}:{secs:02d} {unit_label}"


def format_pace_short(pace_min: float) -> str:
    """Format fractional minutes into M:SS."""
    total_secs = max(0, int(round(pace_min * 60)))
    m = total_secs // 60
    s = total_secs % 60
    return f"{m}:{s:02d}"


def _latlon_to_global_px(
    lat_deg: float, lon_deg: float, zoom: int
) -> Tuple[float, float]:
    """Convert (lat, lon) to Web Mercator global pixel coordinates."""
    n = 2.0**zoom
    x_px = ((lon_deg + 180.0) / 360.0) * n * 256.0
    lat_rad = math.radians(max(-85.0511, min(85.0511, lat_deg)))
    y_px = (
        (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi)
        / 2.0
        * n
        * 256.0
    )
    return x_px, y_px


class InteractiveChartCanvas(tk.Canvas):
    """Interactive time-series and GPS route chart widget."""

    def __init__(self, parent: tk.Widget, **kwargs) -> None:
        super().__init__(
            parent,
            bg=BG_SURFACE,
            highlightthickness=1,
            highlightbackground=BG_CARD_BORDER,
            **kwargs,
        )
        self.workout: Optional[WorkoutData] = None
        self.imperial: bool = False
        self.chart_mode: str = (
            "pace"  # pace, heart_rate, elevation, cadence, power, dual, route
        )
        self.smooth_window: int = 1
        self.pace_x_axis_var = tk.StringVar(value="Pace Over Time")

        # On-graph selector for Pace X-axis mode (Time vs Distance)
        self.pace_axis_selector = ttk.Combobox(
            self,
            textvariable=self.pace_x_axis_var,
            values=("Pace Over Time", "Pace Over Distance"),
            state="readonly",
            width=19,
            style="Units.TCombobox",
            font=("Segoe UI Semibold", 11),
        )
        self.pace_axis_selector.bind(
            "<<ComboboxSelected>>",
            lambda _e: self.redraw(),
        )

        self._plot_points: List[Tuple[float, float, WorkoutRecord, str]] = []
        self._plot_bbox: Tuple[int, int, int, int] = (70, 35, 700, 350)

        # Cache of loaded PhotoImage objects keyed by (z, x, y)
        self._tile_images: Dict[Tuple[int, int, int], tk.PhotoImage] = {}
        self._pending_tiles: set[Tuple[int, int, int]] = set()

        self.bind("<Configure>", lambda _e: self.redraw())
        self.bind("<Motion>", self._on_mouse_move)
        self.bind("<Leave>", lambda _e: self.delete("hover"))

    @property
    def is_pace_by_distance(self) -> bool:
        """Return True if Pace chart X-axis is set to Distance."""
        return (
            self.chart_mode == "pace"
            and self.pace_x_axis_var.get() == "Pace Over Distance"
        )

    def set_data(
        self,
        workout: Optional[WorkoutData],
        imperial: bool = False,
        chart_mode: str = "pace",
        smooth_window: int = 1,
    ) -> None:
        """Update chart state and trigger a canvas redraw."""
        self.workout = workout
        self.imperial = imperial
        self.chart_mode = chart_mode
        self.smooth_window = smooth_window
        self.redraw()

    def redraw(self) -> None:
        """Clear and redraw the active chart view on the canvas."""
        self.delete("all")
        self._plot_points.clear()

        w = self.winfo_width()
        h = self.winfo_height()

        # Show or hide the Pace X-axis selector on the graph
        if self.chart_mode == "pace" and w >= 100 and h >= 100:
            self.pace_axis_selector.place(x=w - 34, y=10, anchor="ne")
        else:
            self.pace_axis_selector.place_forget()

        if w < 100 or h < 100:
            return

        if not self.workout or not self.workout.records:
            self.create_text(
                w // 2,
                h // 2,
                text=(
                    "Load a .FIT, .TCX, or .GPX workout file "
                    "to view interactive graphs"
                ),
                fill=FG_SECONDARY,
                font=("Segoe UI", 14),
            )
            return

        if self.chart_mode == "route":
            self._draw_gps_route(w, h)
        elif self.chart_mode == "dual":
            self._draw_dual_chart(w, h)
        elif self.chart_mode == "zones":
            self._draw_zones_chart(w, h)
        else:
            self._draw_single_series(w, h, self.chart_mode)

    def _moving_average(self, values: List[float], window: int) -> List[float]:
        if window <= 1 or len(values) <= 2:
            return values
        half = window // 2
        smoothed: List[float] = []
        for i in range(len(values)):
            start = max(0, i - half)
            end = min(len(values), i + half + 1)
            segment = values[start:end]
            smoothed.append(sum(segment) / len(segment))
        return smoothed

    def _extract_series(
        self,
        mode: str,
        use_distance_x: bool = False,
    ) -> Tuple[
        List[float],
        List[float],
        List[WorkoutRecord],
        str,
        str,
        bool,
        Optional[float],
    ]:
        """Return (x_vals, y_vals, records, title, unit, invert_y, avg)."""
        assert self.workout is not None
        xs: List[float] = []
        ys: List[float] = []
        recs: List[WorkoutRecord] = []

        invert_y = False
        avg_val: Optional[float] = None
        dist_divisor = 1609.344 if self.imperial else 1000.0

        if mode == "pace":
            unit = "min/mi" if self.imperial else "min/km"
            axis_label = "Distance" if use_distance_x else "Time"
            title = f"Pace Over {axis_label} ({unit})"
            invert_y = True  # Faster pace (lower min/distance) at the top
            for r in self.workout.records:
                p = r.pace_min_per_mile if self.imperial else r.pace_min_per_km
                if p is not None and 2.5 <= p <= 25.0:
                    if use_distance_x:
                        if r.distance_m is None:
                            continue
                        xs.append(r.distance_m / dist_divisor)
                    else:
                        xs.append(r.elapsed_seconds)
                    ys.append(p)
                    recs.append(r)
            if self.workout.avg_speed_m_s and self.workout.avg_speed_m_s > 0.3:
                avg_val = (dist_divisor / self.workout.avg_speed_m_s) / 60.0
            elif ys:
                avg_val = sum(ys) / len(ys)

        elif mode == "heart_rate":
            unit = "bpm"
            title = "Heart Rate Over Time (bpm)"
            for r in self.workout.records:
                if r.heart_rate_bpm is not None and r.heart_rate_bpm > 30:
                    xs.append(r.elapsed_seconds)
                    ys.append(float(r.heart_rate_bpm))
                    recs.append(r)
            if self.workout.avg_heart_rate_bpm is not None:
                avg_val = float(self.workout.avg_heart_rate_bpm)
            elif ys:
                avg_val = sum(ys) / len(ys)

        elif mode == "elevation":
            unit = "ft" if self.imperial else "m"
            title = f"Elevation Profile ({unit})"
            factor = 3.28084 if self.imperial else 1.0
            for r in self.workout.records:
                if r.altitude_m is not None:
                    xs.append(r.elapsed_seconds)
                    ys.append(r.altitude_m * factor)
                    recs.append(r)
            if ys:
                avg_val = sum(ys) / len(ys)

        elif mode == "cadence":
            unit = "spm"
            title = "Cadence Over Time (spm)"
            for r in self.workout.records:
                if r.cadence_rpm is not None and r.cadence_rpm > 0:
                    xs.append(r.elapsed_seconds)
                    ys.append(float(r.cadence_rpm))
                    recs.append(r)
            if self.workout.avg_cadence_rpm is not None:
                avg_val = float(self.workout.avg_cadence_rpm)
            elif ys:
                avg_val = sum(ys) / len(ys)

        elif mode == "power":
            unit = "W"
            title = "Running Power Over Time (Watts)"
            for r in self.workout.records:
                if r.power_watts is not None and r.power_watts > 0:
                    xs.append(r.elapsed_seconds)
                    ys.append(float(r.power_watts))
                    recs.append(r)
            if self.workout.avg_power_watts is not None:
                avg_val = float(self.workout.avg_power_watts)
            elif ys:
                avg_val = sum(ys) / len(ys)
        else:
            unit = ""
            title = ""

        ys = self._moving_average(ys, self.smooth_window)
        return xs, ys, recs, title, unit, invert_y, avg_val

    def _draw_single_series(self, w: int, h: int, mode: str) -> None:
        use_dist_x = self.is_pace_by_distance
        xs, ys, recs, title, unit, invert_y, avg_val = self._extract_series(
            mode, use_distance_x=use_dist_x
        )
        if len(xs) < 2:
            mode_title = mode.replace("_", " ").title()
            self.create_text(
                w // 2,
                h // 2,
                text=f"No {mode_title} data recorded in this file.",
                fill=FG_SECONDARY,
                font=("Segoe UI", 14),
            )
            return

        left, top, right, bottom = 78, 46, w - 34, h - 48
        self._plot_bbox = (left, top, right, bottom)
        plot_w = max(1, right - left)
        plot_h = max(1, bottom - top)

        # Title
        self.create_text(
            left,
            18,
            text=title,
            anchor="w",
            fill=FG_PRIMARY,
            font=("Segoe UI Semibold", 14),
        )

        min_x, max_x = xs[0], max(xs[-1], xs[0] + 1e-3)
        min_y, max_y = min(ys), max(ys)
        if avg_val is not None:
            min_y = min(min_y, avg_val)
            max_y = max(max_y, avg_val)
        y_span = max_y - min_y
        if y_span < 1e-3:
            min_y -= 1.0
            max_y += 1.0
        else:
            pad = y_span * 0.08
            min_y -= pad
            max_y += pad

        # Horizontal grid lines & Y axis labels
        num_y_ticks = 5
        for i in range(num_y_ticks + 1):
            frac = i / num_y_ticks
            y_px = top + frac * plot_h
            val = (
                (min_y + frac * (max_y - min_y))
                if invert_y
                else (max_y - frac * (max_y - min_y))
            )
            self.create_line(
                left,
                y_px,
                right,
                y_px,
                fill="#293548",
                dash=(3, 3),
            )
            lbl = format_pace_short(val) if mode == "pace" else f"{val:.0f}"
            self.create_text(
                left - 10,
                y_px,
                text=lbl,
                anchor="e",
                fill=FG_SECONDARY,
                font=("Segoe UI", 11),
            )

        # Vertical grid lines & X axis labels (Time or Distance)
        dist_unit = "mi" if self.imperial else "km"
        num_x_ticks = min(8, max(3, plot_w // 110))
        for i in range(num_x_ticks + 1):
            frac = i / num_x_ticks
            x_px = left + frac * plot_w
            x_val = min_x + frac * (max_x - min_x)
            self.create_line(
                x_px,
                top,
                x_px,
                bottom,
                fill="#293548",
                dash=(2, 4),
            )
            if use_dist_x:
                x_lbl = f"{x_val:.2f} {dist_unit}"
            else:
                x_lbl = format_duration(x_val)
            self.create_text(
                x_px,
                bottom + 18,
                text=x_lbl,
                anchor="n",
                fill=FG_SECONDARY,
                font=("Segoe UI", 11),
            )

        # Axes border
        self.create_line(left, top, left, bottom, fill=BG_CARD_BORDER, width=1)
        self.create_line(
            left,
            bottom,
            right,
            bottom,
            fill=BG_CARD_BORDER,
            width=1,
        )

        # Map data points to canvas coordinates
        color = SERIES_COLORS.get(mode, ACCENT_BLUE)
        fill_stipple = "gray25"
        coords: List[float] = []

        for x_val, y_val, rec in zip(xs, ys, recs):
            x_px = left + ((x_val - min_x) / (max_x - min_x)) * plot_w
            if invert_y:
                y_px = top + ((y_val - min_y) / (max_y - min_y)) * plot_h
            else:
                y_px = bottom - ((y_val - min_y) / (max_y - min_y)) * plot_h
            coords.extend([x_px, y_px])

            val_str = (
                f"{format_pace_short(y_val)} {unit}"
                if mode == "pace"
                else f"{y_val:.0f} {unit}"
            )
            self._plot_points.append((x_px, y_px, rec, val_str))

        # Subtle area polygon under curve
        if len(coords) >= 4:
            poly_coords = [coords[0], bottom] + coords + [coords[-2], bottom]
            self.create_polygon(
                poly_coords,
                fill=color,
                stipple=fill_stipple,
                outline="",
            )
            self.create_line(
                coords,
                fill=color,
                width=2.2,
                smooth=True,
            )

        # Average Line for Pace, Heart Rate, Elevation, Cadence, and Power
        if (
            mode in ("pace", "heart_rate", "elevation", "cadence", "power")
            and avg_val is not None
        ):
            avg_frac = (avg_val - min_y) / (max_y - min_y)
            if invert_y:
                avg_y_px = top + avg_frac * plot_h
            else:
                avg_y_px = bottom - avg_frac * plot_h

            self.create_line(
                left,
                avg_y_px,
                right,
                avg_y_px,
                fill="#f8fafc",
                width=1.8,
                dash=(6, 4),
            )
            if mode == "pace":
                avg_text = f"Avg: {format_pace_short(avg_val)} {unit}"
            else:
                avg_text = f"Avg: {avg_val:.0f} {unit}"
            # Badge background for readability
            badge_w = len(avg_text) * 7 + 18
            badge_x = right - badge_w - 8
            badge_y = max(top + 4, min(avg_y_px - 12, bottom - 26))
            self.create_rectangle(
                badge_x,
                badge_y,
                badge_x + badge_w,
                badge_y + 24,
                fill="#0f172a",
                outline=color,
                width=1,
            )
            self.create_text(
                badge_x + badge_w / 2,
                badge_y + 12,
                text=avg_text,
                fill=FG_PRIMARY,
                font=("Segoe UI Semibold", 10),
            )

    def _draw_dual_chart(self, w: int, h: int) -> None:
        """Draw Pace + Heart Rate overlay chart with average lines for both."""
        xs_p, ys_p, recs_p, _, unit_p, _, avg_p = self._extract_series("pace")
        xs_h, ys_h, _recs_h, _, unit_h, _, avg_h = self._extract_series(
            "heart_rate",
        )

        if len(xs_p) < 2 and len(xs_h) < 2:
            self.create_text(
                w // 2,
                h // 2,
                text="Insufficient Pace or Heart Rate data for overlay.",
                fill=FG_SECONDARY,
                font=("Segoe UI", 14),
            )
            return

        left, top, right, bottom = 78, 46, w - 78, h - 48
        self._plot_bbox = (left, top, right, bottom)
        plot_w = max(1, right - left)
        plot_h = max(1, bottom - top)

        self.create_text(
            left,
            18,
            text=f"Pace ({unit_p}) & Heart Rate ({unit_h}) Overlay",
            anchor="w",
            fill=FG_PRIMARY,
            font=("Segoe UI Semibold", 14),
        )

        # Legend
        self.create_rectangle(
            right - 250,
            12,
            right - 236,
            26,
            fill=SERIES_COLORS["pace"],
            outline="",
        )
        self.create_text(
            right - 230,
            19,
            text="Pace",
            anchor="w",
            fill=FG_PRIMARY,
            font=("Segoe UI", 11),
        )
        self.create_rectangle(
            right - 145,
            12,
            right - 131,
            26,
            fill=SERIES_COLORS["heart_rate"],
            outline="",
        )
        self.create_text(
            right - 125,
            19,
            text="Heart Rate",
            anchor="w",
            fill=FG_PRIMARY,
            font=("Segoe UI", 11),
        )

        all_x = xs_p if xs_p else xs_h
        min_x, max_x = all_x[0], max(all_x[-1], all_x[0] + 1.0)

        # Grid & X ticks
        num_x_ticks = min(8, max(3, plot_w // 110))
        for i in range(num_x_ticks + 1):
            frac = i / num_x_ticks
            x_px = left + frac * plot_w
            t_val = min_x + frac * (max_x - min_x)
            self.create_line(
                x_px,
                top,
                x_px,
                bottom,
                fill="#293548",
                dash=(2, 4),
            )
            self.create_text(
                x_px,
                bottom + 18,
                text=format_duration(t_val),
                anchor="n",
                fill=FG_SECONDARY,
                font=("Segoe UI", 11),
            )

        # Pace series (Left Y axis, inverted)
        if len(xs_p) >= 2:
            min_p, max_p = min(ys_p) * 0.95, max(ys_p) * 1.05
            for i in range(6):
                frac = i / 5
                y_px = top + frac * plot_h
                val = min_p + frac * (max_p - min_p)
                self.create_line(
                    left,
                    y_px,
                    right,
                    y_px,
                    fill="#293548",
                    dash=(3, 3),
                )
                self.create_text(
                    left - 10,
                    y_px,
                    text=format_pace_short(val),
                    anchor="e",
                    fill=SERIES_COLORS["pace"],
                    font=("Segoe UI", 11),
                )

            coords_p: List[float] = []
            for x_v, y_v, rec in zip(xs_p, ys_p, recs_p):
                x_px = left + ((x_v - min_x) / (max_x - min_x)) * plot_w
                y_px = top + ((y_v - min_p) / (max_p - min_p)) * plot_h
                coords_p.extend([x_px, y_px])
                hr_v = rec.heart_rate_bpm
                hr_str = f" | {hr_v} bpm" if hr_v else ""
                self._plot_points.append(
                    (
                        x_px,
                        y_px,
                        rec,
                        f"{format_pace_short(y_v)} {unit_p}{hr_str}",
                    )
                )
            self.create_line(
                coords_p, fill=SERIES_COLORS["pace"], width=2.0, smooth=True
            )

            # Average Pace line
            if avg_p is not None and min_p <= avg_p <= max_p:
                avg_p_y = top + ((avg_p - min_p) / (max_p - min_p)) * plot_h
                self.create_line(
                    left,
                    avg_p_y,
                    right,
                    avg_p_y,
                    fill=SERIES_COLORS["pace"],
                    width=1.6,
                    dash=(6, 4),
                )
                p_lbl = f"Avg Pace: {format_pace_short(avg_p)} {unit_p}"
                bw = len(p_lbl) * 7 + 18
                bx = left + 10
                by = max(top + 4, min(avg_p_y - 12, bottom - 26))
                self.create_rectangle(
                    bx,
                    by,
                    bx + bw,
                    by + 23,
                    fill="#0f172a",
                    outline=SERIES_COLORS["pace"],
                )
                self.create_text(
                    bx + bw / 2,
                    by + 11,
                    text=p_lbl,
                    fill=SERIES_COLORS["pace"],
                    font=("Segoe UI Semibold", 10),
                )

        # Heart rate series (Right Y axis)
        if len(xs_h) >= 2:
            min_h, max_h = min(ys_h) - 5, max(ys_h) + 5
            for i in range(6):
                frac = i / 5
                y_px = top + frac * plot_h
                val = max_h - frac * (max_h - min_h)
                self.create_text(
                    right + 10,
                    y_px,
                    text=f"{val:.0f}",
                    anchor="w",
                    fill=SERIES_COLORS["heart_rate"],
                    font=("Segoe UI", 11),
                )

            coords_h: List[float] = []
            for x_v, y_v in zip(xs_h, ys_h):
                x_px = left + ((x_v - min_x) / (max_x - min_x)) * plot_w
                y_px = bottom - ((y_v - min_h) / (max_h - min_h)) * plot_h
                coords_h.extend([x_px, y_px])
            self.create_line(
                coords_h,
                fill=SERIES_COLORS["heart_rate"],
                width=2.0,
                smooth=True,
            )

            # Average Heart Rate line
            if avg_h is not None and min_h <= avg_h <= max_h:
                avg_h_y = bottom - ((avg_h - min_h) / (max_h - min_h)) * plot_h
                self.create_line(
                    left,
                    avg_h_y,
                    right,
                    avg_h_y,
                    fill=SERIES_COLORS["heart_rate"],
                    width=1.6,
                    dash=(6, 4),
                )
                h_lbl = f"Avg HR: {avg_h:.0f} bpm"
                bw = len(h_lbl) * 7 + 18
                bx = right - bw - 10
                by = max(top + 4, min(avg_h_y - 12, bottom - 26))
                self.create_rectangle(
                    bx,
                    by,
                    bx + bw,
                    by + 23,
                    fill="#0f172a",
                    outline=SERIES_COLORS["heart_rate"],
                )
                self.create_text(
                    bx + bw / 2,
                    by + 11,
                    text=h_lbl,
                    fill=SERIES_COLORS["heart_rate"],
                    font=("Segoe UI Semibold", 10),
                )

        self.create_line(left, top, left, bottom, fill=BG_CARD_BORDER)
        self.create_line(right, top, right, bottom, fill=BG_CARD_BORDER)
        self.create_line(left, bottom, right, bottom, fill=BG_CARD_BORDER)

    def _draw_zones_chart(self, w: int, h: int) -> None:
        """Draw horizontal bar graphs for HR Zones and Power Zones."""
        assert self.workout is not None
        recs = self.workout.records
        zone_colors = ["#f43f5e", "#f97316", "#10b981", "#38bdf8", "#94a3b8"]

        # Compute sample intervals (s) for fallback if no FIT zone messages
        dts = fit_parser.compute_sample_intervals(recs)

        # 1. Heart Rate Zones: prefer watch-configured zones from FIT file
        # (ordered Zone 5 down to Zone 1)
        if len(self.workout.hr_zones) >= 5:
            ordered_hr = sorted(
                self.workout.hr_zones[:5],
                key=lambda z: z.zone_number,
                reverse=True,
            )
            hr_zone_specs = []
            for idx, z in enumerate(ordered_hr):
                if z.high_value is not None and z.zone_number < 5:
                    r_str = f"{z.low_value}–{z.high_value} bpm"
                elif z.high_value is not None:
                    r_str = f"{z.low_value}–{z.high_value} bpm"
                else:
                    r_str = f">{z.low_value - 1} bpm"
                hr_zone_specs.append(
                    (
                        f"Zone {z.zone_number}",
                        z.label,
                        r_str,
                        z.time_seconds,
                        zone_colors[idx],
                    )
                )
        else:
            max_hr = max(185, self.workout.max_heart_rate_bpm or 190)
            z1_hr = int(round(max_hr * 0.50))
            z2_hr = int(round(max_hr * 0.60))
            z3_hr = int(round(max_hr * 0.70))
            z4_hr = int(round(max_hr * 0.80))
            z5_hr = int(round(max_hr * 0.90))

            hr_times = [0.0] * 5
            for rec, dt in zip(recs, dts):
                hr = rec.heart_rate_bpm
                if hr is None or hr <= 30:
                    continue
                if hr >= z5_hr:
                    hr_times[0] += dt
                elif hr >= z4_hr:
                    hr_times[1] += dt
                elif hr >= z3_hr:
                    hr_times[2] += dt
                elif hr >= z2_hr:
                    hr_times[3] += dt
                else:
                    hr_times[4] += dt

            hr_zone_specs = [
                (
                    "Zone 5",
                    "Maximum",
                    f"{z5_hr}–{max_hr} bpm",
                    hr_times[0],
                    "#f43f5e",
                ),
                (
                    "Zone 4",
                    "Threshold",
                    f"{z4_hr}–{z5_hr - 1} bpm",
                    hr_times[1],
                    "#f97316",
                ),
                (
                    "Zone 3",
                    "Aerobic",
                    f"{z3_hr}–{z4_hr - 1} bpm",
                    hr_times[2],
                    "#10b981",
                ),
                (
                    "Zone 2",
                    "Easy",
                    f"{z2_hr}–{z3_hr - 1} bpm",
                    hr_times[3],
                    "#38bdf8",
                ),
                (
                    "Zone 1",
                    "Warm Up",
                    f"{z1_hr}–{z2_hr - 1} bpm",
                    hr_times[4],
                    "#94a3b8",
                ),
            ]

        # 2. Power Zones: prefer watch-configured zones from FIT file
        # (ordered Zone 5 down to Zone 1)
        if len(self.workout.power_zones) >= 5:
            ordered_pwr = sorted(
                self.workout.power_zones[:5],
                key=lambda z: z.zone_number,
                reverse=True,
            )
            pwr_zone_specs = []
            for idx, z in enumerate(ordered_pwr):
                if z.high_value is not None:
                    r_str = f"{z.low_value}–{z.high_value} W"
                else:
                    r_str = f">{z.low_value - 1} W"
                pwr_zone_specs.append(
                    (
                        f"Zone {z.zone_number}",
                        z.label,
                        r_str,
                        z.time_seconds,
                        zone_colors[idx],
                    )
                )
        else:
            tp = int(round((self.workout.avg_power_watts or 265) * 1.04))
            z1_p = int(round(tp * 0.55))
            z2_p = int(round(tp * 0.75))
            z3_p = int(round(tp * 0.90))
            z4_p = int(round(tp * 1.00))
            z5_p = int(round(tp * 1.15))

            pwr_times = [0.0] * 5
            for rec, dt in zip(recs, dts):
                pwr = rec.power_watts
                if pwr is None or pwr <= 0:
                    continue
                if pwr >= z5_p:
                    pwr_times[0] += dt
                elif pwr >= z4_p:
                    pwr_times[1] += dt
                elif pwr >= z3_p:
                    pwr_times[2] += dt
                elif pwr >= z2_p:
                    pwr_times[3] += dt
                else:
                    pwr_times[4] += dt

            pwr_zone_specs = [
                (
                    "Zone 5",
                    "Maximum",
                    f">{z5_p - 1} W",
                    pwr_times[0],
                    "#f43f5e",
                ),
                (
                    "Zone 4",
                    "Threshold",
                    f"{z4_p}–{z5_p - 1} W",
                    pwr_times[1],
                    "#f97316",
                ),
                (
                    "Zone 3",
                    "Tempo",
                    f"{z3_p}–{z4_p - 1} W",
                    pwr_times[2],
                    "#10b981",
                ),
                (
                    "Zone 2",
                    "Endurance",
                    f"{z2_p}–{z3_p - 1} W",
                    pwr_times[3],
                    "#38bdf8",
                ),
                (
                    "Zone 1",
                    "Recovery",
                    f"{z1_p}–{z2_p - 1} W",
                    pwr_times[4],
                    "#94a3b8",
                ),
            ]

        mid_y = h // 2
        self._draw_zone_bar_section(
            title="Time in Heart Rate Zones",
            top_y=16,
            bottom_y=mid_y - 10,
            width=w,
            zone_specs=hr_zone_specs,
            empty_msg="No Heart Rate data recorded in this workout.",
        )

        # Divider line between HR Zones and Power Zones
        self.create_line(
            36,
            mid_y,
            w - 36,
            mid_y,
            fill=BG_CARD_BORDER,
            width=1,
        )

        self._draw_zone_bar_section(
            title="Time in Power Zones",
            top_y=mid_y + 14,
            bottom_y=h - 16,
            width=w,
            zone_specs=pwr_zone_specs,
            empty_msg="No Power data recorded in this workout.",
        )

    def _draw_zone_bar_section(
        self,
        title: str,
        top_y: int,
        bottom_y: int,
        width: int,
        zone_specs: List[Tuple[str, str, str, float, str]],
        empty_msg: str,
    ) -> None:
        """Render a single 5-zone horizontal bar chart section."""
        left_pad = 36
        right_pad = 36

        self.create_text(
            left_pad,
            top_y + 10,
            text=title,
            anchor="w",
            fill=FG_PRIMARY,
            font=("Segoe UI Semibold", 14),
        )

        total_time = sum(t for _, _, _, t, _ in zone_specs)
        if total_time <= 0:
            self.create_text(
                width // 2,
                (top_y + bottom_y) // 2 + 10,
                text=empty_msg,
                fill=FG_SECONDARY,
                font=("Segoe UI", 12),
            )
            return

        bars_top = top_y + 36
        avail_h = max(60, bottom_y - bars_top)
        row_h = avail_h / len(zone_specs)
        bar_h = min(26.0, max(12.0, row_h * 0.58))

        # Fixed columns for Zone Name, Label, Range, Bar Track, Duration/Pct
        bar_left = left_pad + 290
        bar_right = max(bar_left + 120, width - right_pad - 145)
        bar_max_w = bar_right - bar_left

        for idx, (z_num, z_label, z_range, z_sec, z_color) in enumerate(
            zone_specs,
        ):
            cy = bars_top + (idx + 0.5) * row_h
            pct = (z_sec / total_time) * 100.0 if total_time > 0 else 0.0

            # Zone number & label (e.g., "Zone 5  •  Maximum")
            self.create_text(
                left_pad,
                cy,
                text=f"{z_num}  •  {z_label}",
                anchor="w",
                fill=FG_PRIMARY,
                font=("Segoe UI Semibold", 11),
            )

            # Zone range (e.g., "152–170 bpm" or "266–296 W")
            self.create_text(
                bar_left - 14,
                cy,
                text=z_range,
                anchor="e",
                fill=FG_SECONDARY,
                font=("Segoe UI", 11),
            )

            # Full-width background track
            y0 = cy - bar_h / 2.0
            y1 = cy + bar_h / 2.0
            self.create_rectangle(
                bar_left,
                y0,
                bar_right,
                y1,
                fill="#0f172a",
                outline=BG_CARD_BORDER,
                width=1,
            )

            # Filled horizontal bar proportional to percentage of time in zone
            if pct > 0:
                fill_w = max(4.0, (pct / 100.0) * bar_max_w)
                self.create_rectangle(
                    bar_left,
                    y0,
                    min(bar_right, bar_left + fill_w),
                    y1,
                    fill=z_color,
                    outline="",
                )

            # Duration & Percentage label on the right of the bar
            time_str = format_duration(z_sec)
            self.create_text(
                bar_right + 14,
                cy,
                text=f"{time_str}  ({pct:.1f}%)",
                anchor="w",
                fill=FG_PRIMARY,
                font=("Segoe UI Semibold", 11),
            )

    def _choose_zoom_level(
        self,
        min_lat: float,
        max_lat: float,
        min_lon: float,
        max_lon: float,
        plot_w: int,
        plot_h: int,
    ) -> int:
        """Select Web Mercator zoom level so route fits plot_w x plot_h."""
        for z in range(17, 1, -1):
            x1, y1 = _latlon_to_global_px(max_lat, min_lon, z)
            x2, y2 = _latlon_to_global_px(min_lat, max_lon, z)
            span_x = abs(x2 - x1)
            span_y = abs(y2 - y1)
            if span_x <= plot_w * 0.78 and span_y <= plot_h * 0.78:
                return z
        return 12

    def _request_osm_tile(
        self,
        z: int,
        x: int,
        y: int,
    ) -> Optional[tk.PhotoImage]:
        """Return cached PhotoImage for tile (z, x, y), or fetch async."""
        key = (z, x, y)
        if key in self._tile_images:
            return self._tile_images[key]

        tile_file = TILE_CACHE_DIR / f"{z}_{x}_{y}.png"
        if tile_file.exists() and tile_file.stat().st_size > 100:
            try:
                img = tk.PhotoImage(file=str(tile_file))
                self._tile_images[key] = img
                return img
            except tk.TclError:
                pass

        if key not in self._pending_tiles:
            self._pending_tiles.add(key)
            threading.Thread(
                target=self._download_tile_worker,
                args=(z, x, y, tile_file),
                daemon=True,
            ).start()
        return None

    def _download_tile_worker(
        self,
        z: int,
        x: int,
        y: int,
        dest: Path,
    ) -> None:
        """Background thread to download a 256x256 OpenStreetMap tile."""
        key = (z, x, y)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            url = f"https://tile.openstreetmap.org/{z}/{x}/{y}.png"
            ua = "GarminWorkoutAnalyzer/1.0 (Windows 11 Desktop)"
            req = urllib.request.Request(url, headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                data = resp.read()
            if data.startswith(b"\x89PNG"):
                dest.write_bytes(data)
                if self.chart_mode == "route":
                    self.after(20, self.redraw)
        except (OSError, ValueError, tk.TclError):
            pass
        finally:
            self._pending_tiles.discard(key)

    def _draw_procedural_basemap(
        self,
        left: int,
        top: int,
        right: int,
        bottom: int,
        mid_lat: float,
        mid_lon: float,
        center_gx: float,
        center_gy: float,
        zoom: int,
    ) -> None:
        """Draw vector street/terrain basemap when tiles are offline."""
        cx = (left + right) / 2.0
        cy = (top + bottom) / 2.0

        # Base land color
        self.create_rectangle(
            left,
            top,
            right,
            bottom,
            fill="#162231",
            outline="",
        )

        # Subtle park / green zone polygons derived from tile grid
        base_tx = int(center_gx // 256)
        base_ty = int(center_gy // 256)
        for dx in range(-2, 3):
            for dy in range(-2, 3):
                tx = base_tx + dx
                ty = base_ty + dy
                px0 = cx + (tx * 256 - center_gx)
                py0 = cy + (ty * 256 - center_gy)

                # Deterministic pseudo-random park & water blocks per tile
                seed = (tx * 73856093) ^ (ty * 19349663)
                if seed % 3 == 0:
                    self.create_rectangle(
                        px0 + 24,
                        py0 + 24,
                        px0 + 135,
                        py0 + 120,
                        fill="#132d27",
                        outline="#1b3d35",
                    )
                if seed % 5 == 0:
                    # Creek / waterway curve
                    creek_pts = [
                        px0,
                        py0 + 190,
                        px0 + 90,
                        py0 + 145,
                        px0 + 180,
                        py0 + 170,
                        px0 + 256,
                        py0 + 115,
                    ]
                    self.create_line(
                        creek_pts,
                        fill="#13364e",
                        width=6,
                        smooth=True,
                    )

                # Secondary city street grid inside tile
                for sub in (64, 128, 192):
                    self.create_line(
                        px0 + sub,
                        py0,
                        px0 + sub,
                        py0 + 256,
                        fill="#1f2e42",
                        width=1,
                    )
                    self.create_line(
                        px0,
                        py0 + sub,
                        px0 + 256,
                        py0 + sub,
                        fill="#1f2e42",
                        width=1,
                    )

                # Major arterial roads along tile boundaries
                self.create_line(
                    px0,
                    py0,
                    px0 + 256,
                    py0,
                    fill="#2b3f59",
                    width=2,
                )
                self.create_line(
                    px0,
                    py0,
                    px0,
                    py0 + 256,
                    fill="#2b3f59",
                    width=2,
                )

        # Coordinate graticule lines with lat/lon degree labels
        deg_step = 0.005 if zoom >= 14 else (0.01 if zoom >= 12 else 0.05)
        start_lat = math.floor((mid_lat - 0.05) / deg_step) * deg_step
        for i in range(25):
            lat_v = start_lat + i * deg_step
            _, gy = _latlon_to_global_px(lat_v, mid_lon, zoom)
            py = cy + (gy - center_gy)
            if top <= py <= bottom:
                self.create_line(
                    left,
                    py,
                    right,
                    py,
                    fill="#293b52",
                    dash=(2, 4),
                )
                self.create_text(
                    left + 6,
                    py - 7,
                    text=f"{lat_v:.3f}°N",
                    anchor="w",
                    fill="#64748b",
                    font=("Segoe UI", 8),
                )

        start_lon = math.floor((mid_lon - 0.05) / deg_step) * deg_step
        for i in range(25):
            lon_v = start_lon + i * deg_step
            gx, _ = _latlon_to_global_px(mid_lat, lon_v, zoom)
            px = cx + (gx - center_gx)
            if left <= px <= right:
                self.create_line(
                    px,
                    top,
                    px,
                    bottom,
                    fill="#293b52",
                    dash=(2, 4),
                )
                self.create_text(
                    px + 4,
                    bottom - 10,
                    text=f"{lon_v:.3f}°",
                    anchor="w",
                    fill="#64748b",
                    font=("Segoe UI", 8),
                )

    def _draw_gps_route(self, w: int, h: int) -> None:
        """Render the GPS workout route overlaid on top of a map."""
        assert self.workout is not None
        gps_recs = [
            r
            for r in self.workout.records
            if r.latitude_deg is not None and r.longitude_deg is not None
        ]
        if len(gps_recs) < 2:
            self.create_text(
                w // 2,
                h // 2,
                text="No GPS track coordinates found in this workout.",
                fill=FG_SECONDARY,
                font=("Segoe UI", 14),
            )
            return

        left, top, right, bottom = 36, 46, w - 36, h - 36
        self._plot_bbox = (left, top, right, bottom)
        plot_w = max(1, right - left)
        plot_h = max(1, bottom - top)

        lats: List[float] = []
        lons: List[float] = []
        for r in gps_recs:
            if r.latitude_deg is not None and r.longitude_deg is not None:
                lats.append(r.latitude_deg)
                lons.append(r.longitude_deg)
        min_lat, max_lat = min(lats), max(lats)
        min_lon, max_lon = min(lons), max(lons)

        mid_lat = (min_lat + max_lat) / 2.0
        mid_lon = (min_lon + max_lon) / 2.0

        zoom = self._choose_zoom_level(
            min_lat, max_lat, min_lon, max_lon, plot_w, plot_h
        )
        center_gx, center_gy = _latlon_to_global_px(mid_lat, mid_lon, zoom)
        cx = (left + right) / 2.0
        cy = (top + bottom) / 2.0

        # 1. Draw procedural street/topographic basemap first
        self._draw_procedural_basemap(
            left,
            top,
            right,
            bottom,
            mid_lat,
            mid_lon,
            center_gx,
            center_gy,
            zoom,
        )

        # 2. Overlay any cached/downloaded OpenStreetMap 256x256 PNG tiles
        min_tx = int((center_gx - plot_w / 2.0) // 256) - 1
        max_tx = int((center_gx + plot_w / 2.0) // 256) + 1
        min_ty = int((center_gy - plot_h / 2.0) // 256) - 1
        max_ty = int((center_gy + plot_h / 2.0) // 256) + 1
        max_tile_idx = (2**zoom) - 1

        tiles_drawn = 0
        for tx in range(min_tx, max_tx + 1):
            for ty in range(min_ty, max_ty + 1):
                if 0 <= tx <= max_tile_idx and 0 <= ty <= max_tile_idx:
                    img = self._request_osm_tile(zoom, tx, ty)
                    if img is not None:
                        tile_x = cx + (tx * 256 - center_gx)
                        tile_y = cy + (ty * 256 - center_gy)
                        self.create_image(
                            tile_x,
                            tile_y,
                            image=img,
                            anchor="nw",
                        )
                        tiles_drawn += 1

        # 3. Mask outside map frame so overflowing 256x256 tiles are clipped
        self.create_rectangle(0, 0, w, top, fill=BG_SURFACE, outline="")
        self.create_rectangle(0, bottom, w, h, fill=BG_SURFACE, outline="")
        self.create_rectangle(
            0,
            top,
            left,
            bottom,
            fill=BG_SURFACE,
            outline="",
        )
        self.create_rectangle(
            right,
            top,
            w,
            bottom,
            fill=BG_SURFACE,
            outline="",
        )
        self.create_rectangle(
            left, top, right, bottom, outline=BG_CARD_BORDER, width=1.5
        )

        # Title above clipped map
        self.create_text(
            left,
            18,
            text="GPS Route Map (Start = Green, Finish = Red)",
            anchor="w",
            fill=FG_PRIMARY,
            font=("Segoe UI Semibold", 14),
        )

        # 4. Draw GPS route casing + main line on top of the map
        coords: List[float] = []
        for r in gps_recs:
            assert r.longitude_deg is not None and r.latitude_deg is not None
            gx, gy = _latlon_to_global_px(
                r.latitude_deg,
                r.longitude_deg,
                zoom,
            )
            x_px = cx + (gx - center_gx)
            y_px = cy + (gy - center_gy)
            coords.extend([x_px, y_px])
            pace_str = format_pace(r.speed_m_s, self.imperial)
            self._plot_points.append((x_px, y_px, r, f"Pace: {pace_str}"))

        if len(coords) >= 4:
            # Dark outer casing so the route stands out on light/dark map tiles
            self.create_line(
                coords,
                fill="#0f172a",
                width=6.0,
                capstyle="round",
                joinstyle="round",
                smooth=True,
            )
            self.create_line(
                coords,
                fill="#0284c7" if tiles_drawn > 0 else ACCENT_BLUE,
                width=3.5,
                capstyle="round",
                joinstyle="round",
                smooth=True,
            )

            # Kilometer / Mile split markers along the route
            unit_m = 1609.344 if self.imperial else 1000.0
            next_split = unit_m
            split_num = 1
            for x_px, y_px, rec, _ in self._plot_points:
                if rec.distance_m is not None and rec.distance_m >= next_split:
                    self.create_oval(
                        x_px - 8,
                        y_px - 8,
                        x_px + 8,
                        y_px + 8,
                        fill="#0f172a",
                        outline=ACCENT_BLUE,
                        width=1.5,
                    )
                    self.create_text(
                        x_px,
                        y_px,
                        text=str(split_num),
                        fill="#ffffff",
                        font=("Segoe UI Bold", 8),
                    )
                    split_num += 1
                    next_split += unit_m

            # Start marker (Green)
            sx, sy = coords[0], coords[1]
            self.create_oval(
                sx - 7,
                sy - 7,
                sx + 7,
                sy + 7,
                fill="#10b981",
                outline="#ffffff",
                width=2,
            )
            # Finish marker (Red)
            fx, fy = coords[-2], coords[-1]
            self.create_oval(
                fx - 7,
                fy - 7,
                fx + 7,
                fy + 7,
                fill="#f43f5e",
                outline="#ffffff",
                width=2,
            )

        # 5. Compass rose & map scale bar overlay
        self._draw_map_hud(
            left,
            top,
            right,
            bottom,
            mid_lat,
            zoom,
            tiles_drawn > 0,
        )

    def _draw_map_hud(
        self,
        left: int,
        top: int,
        right: int,
        bottom: int,
        mid_lat: float,
        zoom: int,
        has_osm: bool,
    ) -> None:
        """Draw compass rose and distance scale bar on the map corner."""
        # Compass in top-right corner
        cx, cy = right - 32, top + 32
        self.create_oval(
            cx - 18,
            cy - 18,
            cx + 18,
            cy + 18,
            fill="#0f172a",
            outline=BG_CARD_BORDER,
        )
        self.create_polygon(
            cx,
            cy - 12,
            cx - 5,
            cy + 4,
            cx,
            cy,
            cx + 5,
            cy + 4,
            fill="#f43f5e",
            outline="",
        )
        self.create_text(
            cx,
            cy - 11,
            text="N",
            fill="#ffffff",
            font=("Segoe UI Bold", 8),
            anchor="n",
        )

        # Scale bar in bottom-right corner
        cos_lat = math.cos(math.radians(mid_lat))
        meters_per_px = (156543.03392 * cos_lat) / (2**zoom)
        target_px = 90.0
        target_m = meters_per_px * target_px
        if self.imperial:
            feet = target_m * 3.28084
            if feet >= 2640:
                mi = round(feet / 5280.0, 1) or 0.5
                bar_m = mi * 1609.344
                label = f"{mi:g} mi"
            else:
                ft = max(100, int(round(feet / 100.0) * 100))
                bar_m = ft / 3.28084
                label = f"{ft} ft"
        else:
            if target_m >= 1000:
                km = round(target_m / 1000.0, 1) or 0.5
                bar_m = km * 1000.0
                label = f"{km:g} km"
            else:
                m = max(50, int(round(target_m / 50.0) * 50))
                bar_m = float(m)
                label = f"{m} m"

        bar_px = max(30.0, min(160.0, bar_m / meters_per_px))
        bx2 = right - 14
        bx1 = bx2 - bar_px
        by = bottom - 14

        self.create_rectangle(
            bx1 - 8,
            by - 18,
            bx2 + 8,
            by + 6,
            fill="#0f172a",
            outline=BG_CARD_BORDER,
        )
        self.create_line(bx1, by, bx2, by, fill=FG_PRIMARY, width=2)
        self.create_line(bx1, by - 4, bx1, by + 2, fill=FG_PRIMARY, width=2)
        self.create_line(bx2, by - 4, bx2, by + 2, fill=FG_PRIMARY, width=2)
        self.create_text(
            (bx1 + bx2) / 2,
            by - 10,
            text=label,
            fill=FG_PRIMARY,
            font=("Segoe UI", 10),
        )

        if has_osm:
            self.create_text(
                left + 8,
                bottom - 8,
                text="© OpenStreetMap contributors",
                anchor="sw",
                fill="#0f172a",
                font=("Segoe UI", 8),
            )

    def _on_mouse_move(self, event: tk.Event) -> None:
        self.delete("hover")
        if not self._plot_points:
            return

        left, top, right, bottom = self._plot_bbox
        if not (left <= event.x <= right and top <= event.y <= bottom):
            return

        # Find nearest point (by X for series, or Euclidean dist for route map)
        if self.chart_mode == "route":
            nearest = min(
                self._plot_points,
                key=lambda pt: (pt[0] - event.x) ** 2 + (pt[1] - event.y) ** 2,
            )
        else:
            nearest = min(
                self._plot_points,
                key=lambda pt: abs(pt[0] - event.x),
            )

        px, py, rec, label_str = nearest

        if self.chart_mode != "route":
            self.create_line(
                px,
                top,
                px,
                bottom,
                fill="#64748b",
                dash=(4, 2),
                tags="hover",
            )

        self.create_oval(
            px - 5,
            py - 5,
            px + 5,
            py + 5,
            fill=FG_PRIMARY,
            outline=ACCENT_BLUE,
            width=2,
            tags="hover",
        )

        dist_divisor = 1609.344 if self.imperial else 1000.0
        rec_dist = rec.distance_m
        dist_val = rec_dist / dist_divisor if rec_dist is not None else 0.0
        dist_unit = "mi" if self.imperial else "km"
        hr_v = rec.heart_rate_bpm
        hr_part = f"  •  HR: {hr_v} bpm" if hr_v else ""
        tooltip_text = (
            f"Time: {format_duration(rec.elapsed_seconds)}  •  "
            f"Dist: {dist_val:.2f} {dist_unit}\n"
            f"{label_str}{hr_part}"
        )

        box_w = 285
        box_h = 52
        tx = px + 14 if px + 14 + box_w < right else px - box_w - 14
        ty = max(top + 6, min(py - box_h // 2, bottom - box_h - 6))

        self.create_rectangle(
            tx,
            ty,
            tx + box_w,
            ty + box_h,
            fill="#0f172a",
            outline=ACCENT_BLUE,
            width=1,
            tags="hover",
        )
        self.create_text(
            tx + 10,
            ty + box_h // 2,
            text=tooltip_text,
            anchor="w",
            fill=FG_PRIMARY,
            font=("Segoe UI", 11),
            tags="hover",
        )


class MetricCard(tk.Frame):
    """Summary card displaying a single workout metric."""

    def __init__(
        self,
        parent: tk.Widget,
        title: str,
        value: str = "--",
        subtext: str = "",
    ) -> None:
        super().__init__(
            parent,
            bg=BG_CARD,
            highlightthickness=1,
            highlightbackground=BG_CARD_BORDER,
            padx=14,
            pady=10,
        )
        self.title_lbl = tk.Label(
            self,
            text=title.upper(),
            bg=BG_CARD,
            fg=FG_SECONDARY,
            font=("Segoe UI Semibold", 10),
            anchor="w",
        )
        self.title_lbl.pack(fill="x")

        self.val_lbl = tk.Label(
            self,
            text=value,
            bg=BG_CARD,
            fg=FG_PRIMARY,
            font=("Segoe UI Bold", 19),
            anchor="w",
        )
        self.val_lbl.pack(fill="x", pady=(2, 0))

        self.sub_lbl = tk.Label(
            self,
            text=subtext,
            bg=BG_CARD,
            fg=ACCENT_BLUE,
            font=("Segoe UI", 10),
            anchor="w",
        )
        self.sub_lbl.pack(fill="x")

    def update_metric(self, value: str, subtext: str = "") -> None:
        """Update the displayed primary value and secondary subtext."""
        self.val_lbl.config(text=value)
        self.sub_lbl.config(text=subtext)


class WorkoutAnalyzerApp(tk.Tk):
    """Main Windows 11 application window for Workout Analyzer."""

    def __init__(self) -> None:
        super().__init__()
        self._enable_high_dpi()
        self.title("Workout Analyzer")
        self.geometry("1840x980")
        self.minsize(1680, 760)
        self.configure(bg=BG_MAIN)

        self.current_workout: Optional[WorkoutData] = None
        self.units_var = tk.StringVar(value="Metric")
        self.selected_chart = tk.StringVar(value="pace")
        self.smoothing_enabled_var = tk.BooleanVar(value=False)
        self.smoothing_var = tk.IntVar(value=5)

        self._configure_ttk_styles()
        self._build_layout()

    @property
    def is_imperial(self) -> bool:
        """Return True when Imperial units are selected."""
        return self.units_var.get() == "Imperial"

    def _enable_high_dpi(self) -> None:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass

    def _configure_ttk_styles(self) -> None:
        self.option_add("*TCombobox*Listbox.font", ("Segoe UI", 11))
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(
            "Treeview",
            background=BG_SURFACE,
            foreground=FG_PRIMARY,
            fieldbackground=BG_SURFACE,
            rowheight=34,
            borderwidth=0,
            font=("Segoe UI", 11),
        )
        style.configure(
            "Treeview.Heading",
            background="#334155",
            foreground=FG_PRIMARY,
            relief="flat",
            font=("Segoe UI Semibold", 11),
        )
        style.map("Treeview", background=[("selected", ACCENT_BTN)])

        style.configure(
            "Units.TCombobox",
            fieldbackground=BG_MAIN,
            background="#334155",
            foreground=FG_PRIMARY,
            arrowcolor=FG_PRIMARY,
            bordercolor=BG_CARD_BORDER,
            lightcolor=BG_CARD_BORDER,
            darkcolor=BG_CARD_BORDER,
            padding=4,
        )
        style.map(
            "Units.TCombobox",
            fieldbackground=[("readonly", BG_MAIN)],
            selectbackground=[("readonly", BG_MAIN)],
            selectforeground=[("readonly", FG_PRIMARY)],
            foreground=[("readonly", FG_PRIMARY)],
            background=[("readonly", "#334155")],
        )

    def _build_layout(self) -> None:
        # 1. Top Header Bar
        header = tk.Frame(self, bg=BG_SURFACE, padx=20, pady=12)
        header.pack(fill="x", side="top")

        title_box = tk.Frame(header, bg=BG_SURFACE)
        title_box.pack(side="left")

        tk.Label(
            title_box,
            text="WORKOUT ANALYZER",
            bg=BG_SURFACE,
            fg=ACCENT_BLUE,
            font=("Segoe UI Bold", 17),
        ).pack(anchor="w")

        self.file_info_lbl = tk.Label(
            title_box,
            text="No workout file loaded",
            bg=BG_SURFACE,
            fg=FG_SECONDARY,
            font=("Segoe UI", 11),
        )
        self.file_info_lbl.pack(anchor="w")

        # Header Right Controls
        controls = tk.Frame(header, bg=BG_SURFACE)
        controls.pack(side="right")

        tk.Label(
            controls,
            text="Units:",
            bg=BG_SURFACE,
            fg=FG_SECONDARY,
            font=("Segoe UI Semibold", 11),
        ).pack(side="left", padx=(0, 6))

        self.unit_selector = ttk.Combobox(
            controls,
            textvariable=self.units_var,
            values=("Metric", "Imperial"),
            state="readonly",
            width=10,
            style="Units.TCombobox",
            font=("Segoe UI Semibold", 11),
        )
        self.unit_selector.pack(side="left", padx=(0, 14))
        self.unit_selector.bind(
            "<<ComboboxSelected>>", lambda _e: self._on_settings_changed()
        )

        open_btn = tk.Button(
            controls,
            text="Open Workout File (.FIT / .TCX / .GPX)",
            command=self.browse_and_load_file,
            bg=ACCENT_BTN,
            fg="#ffffff",
            activebackground=ACCENT_BTN_HOVER,
            activeforeground="#ffffff",
            relief="flat",
            padx=16,
            pady=6,
            font=("Segoe UI Semibold", 11),
            cursor="hand2",
        )
        open_btn.pack(side="left")

        # 2. Summary Metric Cards Grid (8 cards across)
        cards_frame = tk.Frame(self, bg=BG_MAIN, padx=20, pady=12)
        cards_frame.pack(fill="x")

        self.cards = {
            "activity": MetricCard(cards_frame, "Activity Type"),
            "duration": MetricCard(cards_frame, "Duration"),
            "distance": MetricCard(cards_frame, "Total Distance"),
            "pace": MetricCard(cards_frame, "Average Pace"),
            "hr": MetricCard(cards_frame, "Heart Rate"),
            "calories": MetricCard(cards_frame, "Calories"),
            "cadence": MetricCard(cards_frame, "Cadence"),
            "elevation": MetricCard(cards_frame, "Elevation / Power"),
        }

        for idx, card in enumerate(self.cards.values()):
            cards_frame.columnconfigure(idx, weight=1, uniform="metric")
            card.grid(row=0, column=idx, sticky="nsew", padx=4)

        # 3. Main Content Split: Left = Interactive Charts, Right = Laps Table
        content = tk.Frame(self, bg=BG_MAIN, padx=20, pady=4)
        content.pack(fill="both", expand=True)
        content.columnconfigure(0, weight=7)
        content.columnconfigure(1, weight=3)
        content.rowconfigure(1, weight=1)

        # Chart selector toolbar
        chart_bar = tk.Frame(content, bg=BG_MAIN)
        chart_bar.grid(row=0, column=0, sticky="ew", pady=(0, 8), padx=(0, 10))

        tk.Label(
            chart_bar,
            text="Graph View:",
            bg=BG_MAIN,
            fg=FG_SECONDARY,
            font=("Segoe UI Semibold", 11),
        ).pack(side="left", padx=(0, 8))

        chart_options = [
            ("Pace", "pace"),
            ("Heart Rate", "heart_rate"),
            ("Pace + HR Overlay", "dual"),
            ("Elevation", "elevation"),
            ("Cadence", "cadence"),
            ("Power", "power"),
            ("Time in Zones", "zones"),
            ("GPS Route Map", "route"),
        ]
        self._chart_buttons: dict[str, tk.Button] = {}
        for label, mode in chart_options:
            btn = tk.Button(
                chart_bar,
                text=label,
                command=self._make_chart_mode_cmd(mode),
                relief="flat",
                padx=10,
                pady=4,
                font=("Segoe UI", 11),
                cursor="hand2",
            )
            btn.pack(side="left", padx=2)
            self._chart_buttons[mode] = btn

        # Smoothing control
        smooth_frame = tk.Frame(chart_bar, bg=BG_MAIN)
        smooth_frame.pack(side="right")
        self.smooth_check = tk.Checkbutton(
            smooth_frame,
            text="Smoothing:",
            variable=self.smoothing_enabled_var,
            command=self._on_smoothing_toggled,
            bg=BG_MAIN,
            fg=FG_SECONDARY,
            selectcolor=BG_SURFACE,
            activebackground=BG_MAIN,
            activeforeground=FG_PRIMARY,
            font=("Segoe UI", 11),
        )
        self.smooth_check.pack(side="left", padx=(0, 4))
        self.smooth_spin = ttk.Spinbox(
            smooth_frame,
            from_=2,
            to=25,
            width=4,
            textvariable=self.smoothing_var,
            command=self._on_settings_changed,
            state="disabled",
            font=("Segoe UI", 11),
        )
        self.smooth_spin.pack(side="left")
        self.smooth_spin.bind(
            "<Return>",
            lambda _e: self._on_settings_changed(),
        )
        self.smooth_spin.bind(
            "<FocusOut>",
            lambda _e: self._on_settings_changed(),
        )

        # Interactive Graph Canvas
        self.chart_canvas = InteractiveChartCanvas(content)
        self.chart_canvas.grid(
            row=1, column=0, sticky="nsew", padx=(0, 10), pady=(0, 12)
        )

        # Laps Table Header + Treeview
        laps_header = tk.Frame(content, bg=BG_MAIN)
        laps_header.grid(row=0, column=1, sticky="ew", pady=(0, 8))
        tk.Label(
            laps_header,
            text="Workout Laps & Splits",
            bg=BG_MAIN,
            fg=FG_PRIMARY,
            font=("Segoe UI Semibold", 12),
        ).pack(side="left")

        laps_container = tk.Frame(
            content,
            bg=BG_SURFACE,
            highlightthickness=1,
            highlightbackground=BG_CARD_BORDER,
        )
        laps_container.grid(row=1, column=1, sticky="nsew", pady=(0, 12))

        cols = ("lap", "time", "dist", "pace", "hr")
        self.laps_tree = ttk.Treeview(
            laps_container,
            columns=cols,
            show="headings",
            selectmode="browse",
        )
        self.laps_tree.heading("lap", text="#")
        self.laps_tree.heading("time", text="Time")
        self.laps_tree.heading("dist", text="Distance")
        self.laps_tree.heading("pace", text="Avg Pace")
        self.laps_tree.heading("hr", text="Avg / Max HR")

        self.laps_tree.column("lap", width=42, anchor="center")
        self.laps_tree.column("time", width=80, anchor="center")
        self.laps_tree.column("dist", width=90, anchor="center")
        self.laps_tree.column("pace", width=105, anchor="center")
        self.laps_tree.column("hr", width=112, anchor="center")

        scrollbar = ttk.Scrollbar(
            laps_container, orient="vertical", command=self.laps_tree.yview
        )
        self.laps_tree.configure(yscrollcommand=scrollbar.set)
        self.laps_tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self._update_chart_button_styles()

    def _make_chart_mode_cmd(self, mode: str) -> Callable[[], None]:
        return lambda: self._select_chart_mode(mode)

    def _select_chart_mode(self, mode: str) -> None:
        self.selected_chart.set(mode)
        self._update_chart_button_styles()
        self._refresh_chart()

    def _update_chart_button_styles(self) -> None:
        active = self.selected_chart.get()
        for mode, btn in self._chart_buttons.items():
            if mode == active:
                btn.config(bg=ACCENT_BTN, fg="#ffffff")
            else:
                btn.config(bg=BG_SURFACE, fg=FG_SECONDARY)

    def browse_and_load_file(self) -> None:
        """Open a file dialog to select and load a workout file."""
        filetypes = [
            (
                "Workout Files (*.fit;*.tcx;*.gpx)",
                "*.fit *.FIT *.tcx *.TCX *.gpx *.GPX",
            ),
            ("FIT Files (*.fit)", "*.fit *.FIT"),
            ("TCX Files (*.tcx)", "*.tcx *.TCX"),
            ("GPX Files (*.gpx)", "*.gpx *.GPX"),
            ("All Files (*.*)", "*.*"),
        ]
        chosen = filedialog.askopenfilename(
            title="Select Workout File",
            filetypes=filetypes,
        )
        if chosen:
            self.load_workout_file(Path(chosen))

    def load_workout_file(self, file_path: Path) -> None:
        """Parse the given workout file and refresh the application UI."""
        try:
            workout = ParserRegistry.parse_file(file_path)
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            messagebox.showerror(
                "Error Loading Workout File",
                f"Could not parse '{file_path.name}':\n\n{exc}",
            )
            return

        self.current_workout = workout
        self._refresh_ui()

    def _on_smoothing_toggled(self) -> None:
        if self.smoothing_enabled_var.get():
            self.smooth_spin.configure(state="normal")
        else:
            self.smooth_spin.configure(state="disabled")
        self._refresh_chart()

    def _on_settings_changed(self) -> None:
        self._refresh_ui()

    def _refresh_ui(self) -> None:
        w = self.current_workout
        if w is None:
            self._refresh_chart()
            return

        imp = self.is_imperial
        if w.start_time:
            date_str = w.start_time.strftime("%Y-%m-%d %H:%M")
        else:
            date_str = "Unknown Date"
        self.file_info_lbl.config(
            text=(
                f"{w.file_path.name}  •  {w.device_name}  •  "
                f"{date_str}  •  {len(w.records):,} data points"
            )
        )

        # 1. Activity card
        sub = w.sub_sport if w.sub_sport else w.file_format
        self.cards["activity"].update_metric(
            w.sport,
            f"{sub} ({w.device_name})",
        )

        # 2. Duration card
        self.cards["duration"].update_metric(
            format_duration(w.active_duration_seconds),
            f"Elapsed: {format_duration(w.total_elapsed_seconds)}",
        )

        # 3. Distance card
        if imp:
            dist_val = w.total_distance_m / 1609.344
            dist_str = f"{dist_val:.2f} mi"
            km_val = w.total_distance_m / 1000.0
            sub_dist = f"{w.total_distance_m:,.0f} m ({km_val:.2f} km)"
        else:
            dist_val = w.total_distance_m / 1000.0
            dist_str = f"{dist_val:.2f} km"
            sub_dist = f"{w.total_distance_m / 1609.344:.2f} mi"
        self.cards["distance"].update_metric(dist_str, sub_dist)

        # 4. Pace card
        avg_pace_str = format_pace(w.avg_speed_m_s, imp)
        max_pace_str = format_pace(w.max_speed_m_s, imp)
        self.cards["pace"].update_metric(avg_pace_str, f"Best: {max_pace_str}")

        # 5. Heart Rate card
        avg_hr_v = w.avg_heart_rate_bpm
        max_hr_v = w.max_heart_rate_bpm
        avg_hr = f"{avg_hr_v} bpm" if avg_hr_v else "--"
        max_hr = f"Max: {max_hr_v} bpm" if max_hr_v else ""
        self.cards["hr"].update_metric(avg_hr, max_hr)

        # 6. Calories card
        cal_str = f"{w.total_calories:,} kcal" if w.total_calories else "--"
        if w.total_calories and w.active_duration_seconds > 60:
            cals_per_hr = int(
                round(w.total_calories / (w.active_duration_seconds / 3600.0))
            )
            hr_est = f"{cals_per_hr} kcal/hr"
        else:
            hr_est = ""
        self.cards["calories"].update_metric(cal_str, hr_est)

        # 7. Cadence card
        cad_str = f"{w.avg_cadence_rpm} spm" if w.avg_cadence_rpm else "--"
        max_cad_v = w.max_cadence_rpm
        max_cad_str = f"Max: {max_cad_v} spm" if max_cad_v else ""
        self.cards["cadence"].update_metric(cad_str, max_cad_str)

        # 8. Elevation / Power card
        if w.total_ascent_m is not None:
            if imp:
                asc_str = f"+{w.total_ascent_m * 3.28084:.0f} ft"
            else:
                asc_str = f"+{w.total_ascent_m:.0f} m"
        else:
            asc_str = "--"
        avg_pwr_v = w.avg_power_watts
        pwr_sub = f"Avg Power: {avg_pwr_v} W" if avg_pwr_v else ""
        self.cards["elevation"].update_metric(asc_str, pwr_sub)

        # Populate Laps table
        for item in self.laps_tree.get_children():
            self.laps_tree.delete(item)

        for lap in w.laps:
            l_dist = (
                f"{lap.total_distance_m / 1609.344:.2f} mi"
                if imp
                else f"{lap.total_distance_m / 1000.0:.2f} km"
            )
            l_pace = format_pace(lap.avg_speed_m_s, imp)
            l_hr = (
                f"{lap.avg_heart_rate_bpm} / {lap.max_heart_rate_bpm or '--'}"
                if lap.avg_heart_rate_bpm
                else "--"
            )
            self.laps_tree.insert(
                "",
                "end",
                values=(
                    lap.lap_number,
                    format_duration(lap.total_timer_seconds),
                    l_dist,
                    l_pace,
                    l_hr,
                ),
            )

        self._refresh_chart()

    def _refresh_chart(self) -> None:
        if self.smoothing_enabled_var.get():
            try:
                smooth = max(2, int(self.smoothing_var.get()))
            except (ValueError, tk.TclError):
                smooth = 5
        else:
            smooth = 1
        self.chart_canvas.set_data(
            workout=self.current_workout,
            imperial=self.is_imperial,
            chart_mode=self.selected_chart.get(),
            smooth_window=smooth,
        )


def main() -> None:
    """Launch the Workout Analyzer desktop application."""
    app = WorkoutAnalyzerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
