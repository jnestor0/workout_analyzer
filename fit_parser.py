"""Pure Python Garmin .FIT binary file parser and TCX/GPX parsers."""

# pylint: disable=too-many-lines,too-few-public-methods,too-many-locals
# pylint: disable=too-many-branches,too-many-statements,too-many-arguments
# pylint: disable=too-many-positional-arguments

from __future__ import annotations

import math
import struct
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from models import (
    ParserRegistry,
    WorkoutData,
    WorkoutLap,
    WorkoutParser,
    WorkoutRecord,
    WorkoutZone,
)

# Garmin FIT epoch is 1989-12-31 00:00:00 UTC
FIT_EPOCH = datetime(1989, 12, 31, 0, 0, 0, tzinfo=timezone.utc)

# Semicircles to degrees conversion factor: 180 / 2^31
SEMICIRCLES_TO_DEG = 180.0 / (2**31)

# FIT Sport enum mapping
FIT_SPORT_MAP: Dict[int, str] = {
    0: "Generic",
    1: "Running",
    2: "Cycling",
    3: "Transition",
    4: "Fitness Equipment",
    5: "Swimming",
    6: "Basketball",
    7: "Soccer",
    8: "Tennis",
    9: "American Football",
    10: "Training",
    11: "Walking",
    12: "Cross Country Skiing",
    13: "Alpine Skiing",
    14: "Snowboarding",
    15: "Rowing",
    16: "Mountaineering",
    17: "Hiking",
    18: "Multisport",
    19: "Paddling",
    21: "E-Biking",
    22: "Motorcycling",
    25: "Golf",
    26: "Hang Gliding",
    27: "Horseback Riding",
    28: "Hunting",
    29: "Fishing",
    30: "Inline Skating",
    31: "Rock Climbing",
    32: "Sailing",
    33: "Ice Skating",
    34: "Sky Diving",
    35: "Snowshoeing",
    36: "Snowmobiling",
    37: "Stand Up Paddleboarding",
    38: "Surfing",
    39: "Wakeboarding",
    40: "Water Skiing",
    41: "Kayaking",
    42: "Rafting",
    43: "Windsurfing",
    44: "Kitesurfing",
    45: "Tactical",
    46: "Jumpmaster",
    47: "Boxing",
    48: "Floor Climbing",
    62: "HIIT",
    64: "Racket",
}

FIT_SUB_SPORT_MAP: Dict[int, str] = {
    0: "Generic",
    1: "Treadmill",
    2: "Street",
    3: "Trail",
    4: "Track",
    5: "Spin",
    6: "Indoor Cycling",
    7: "Road",
    8: "Mountain",
    9: "Downhill",
    10: "Recumbent",
    11: "Cyclocross",
    12: "Hand Cycling",
    13: "Track Cycling",
    14: "Indoor Rowing",
    15: "Elliptical",
    16: "Stair Climbing",
    17: "Lap Swimming",
    18: "Open Water",
    19: "Flexibility Training",
    20: "Strength Training",
    21: "Warm Up",
    22: "Match",
    23: "Exercise",
    24: "Challenge",
    25: "Indoor Skiing",
    26: "Cardio Training",
    27: "Indoor Walking",
    45: "Virtual Activity",
}

# FIT Garmin Product IDs (including Forerunner series)
GARMIN_PRODUCTS: Dict[int, str] = {
    3589: "Garmin Forerunner 745",
    3869: "Garmin Forerunner 55",
    3990: "Garmin Forerunner 945 LTE",
    4024: "Garmin Forerunner 255",
    4025: "Garmin Forerunner 255 Music",
    4022: "Garmin Forerunner 955",
    4313: "Garmin Forerunner 265",
    4314: "Garmin Forerunner 265S",
    4315: "Garmin Forerunner 965",
    4565: "Garmin Forerunner 165",
    4670: "Garmin Forerunner 970",
}

# Base types in FIT protocol:
# base_type_num -> (struct_char, byte_size, invalid_value)
FIT_BASE_TYPES: Dict[int, Tuple[str, int, Any]] = {
    0x00: ("B", 1, 0xFF),  # enum
    0x01: ("b", 1, 0x7F),  # sint8
    0x02: ("B", 1, 0xFF),  # uint8
    0x83: ("h", 2, 0x7FFF),  # sint16
    0x84: ("H", 2, 0xFFFF),  # uint16
    0x85: ("i", 4, 0x7FFFFFFF),  # sint32
    0x86: ("I", 4, 0xFFFFFFFF),  # uint32
    0x07: ("s", 1, b"\x00"),  # string
    0x88: ("f", 4, 0xFFFFFFFF),  # float32
    0x89: ("d", 8, 0xFFFFFFFFFFFFFFFF),  # float64
    0x0A: ("B", 1, 0x00),  # uint8z
    0x8B: ("H", 2, 0x0000),  # uint16z
    0x8C: ("I", 4, 0x00000000),  # uint32z
    0x0D: ("B", 1, 0xFF),  # byte
    0x8E: ("q", 8, 0x7FFFFFFFFFFFFFFF),  # sint64
    0x8F: ("Q", 8, 0xFFFFFFFFFFFFFFFF),  # uint64
    0x90: ("Q", 8, 0x0000000000000000),  # uint64z
}


class _FitDefinitionMessage:
    """Internal representation of a FIT definition message."""

    def __init__(
        self,
        endian: str,
        global_mesg_num: int,
        field_defs: List[Tuple[int, int, int]],
        dev_field_defs: List[Tuple[int, int, int]],
    ) -> None:
        self.endian = endian
        self.global_mesg_num = global_mesg_num
        self.field_defs = field_defs  # [(field_def_num, size, base_type)]
        self.dev_field_defs = dev_field_defs
        self.total_byte_size = sum(f[1] for f in field_defs) + sum(
            df[1] for df in dev_field_defs
        )


def _decode_fit_field(raw_bytes: bytes, endian: str, base_type: int) -> Any:
    """Decode raw field bytes according to FIT base type and endianness."""
    base_info = FIT_BASE_TYPES.get(base_type)
    if base_info is None:
        # Fallback lookup by masking lower 5 bits
        low_bits = base_type & 0x1F
        for k, v in FIT_BASE_TYPES.items():
            if (k & 0x1F) == low_bits:
                base_info = v
                break
    if base_info is None:
        return None

    fmt_char, type_size, invalid_val = base_info
    size = len(raw_bytes)

    if fmt_char == "s":
        raw_str = raw_bytes.split(b"\x00", 1)[0]
        text = raw_str.decode("utf-8", errors="ignore").strip()
        return text if text else None

    if size < type_size or size % type_size != 0:
        return None

    count = size // type_size
    if fmt_char in ("f", "d"):
        # Check raw integer bits for invalid sentinel before unpacking float
        int_char = "I" if type_size == 4 else "Q"
        int_vals = struct.unpack(f"{endian}{count}{int_char}", raw_bytes)
        vals = struct.unpack(f"{endian}{count}{fmt_char}", raw_bytes)
        valid_vals = [v for v, iv in zip(vals, int_vals) if iv != invalid_val]
    else:
        vals = struct.unpack(f"{endian}{count}{fmt_char}", raw_bytes)
        valid_vals = [v for v in vals if v != invalid_val]

    if not valid_vals:
        return None
    return valid_vals[0] if count == 1 else valid_vals


@ParserRegistry.register
class FitWorkoutParser(WorkoutParser):
    """Garmin .FIT binary file parser supporting all header formats."""

    supported_extensions = [".fit"]

    def parse(self, file_path: Path) -> WorkoutData:
        data = file_path.read_bytes()
        if len(data) < 12:
            raise ValueError(
                "Invalid FIT file: file is smaller than 12-byte header.",
            )

        header_size = data[0]
        if data[8:12] != b".FIT":
            raise ValueError(
                "Invalid FIT file: missing '.FIT' signature in header.",
            )

        data_size = struct.unpack("<I", data[4:8])[0]
        pos = header_size
        end_pos = min(header_size + data_size, len(data))

        local_defs: Dict[int, _FitDefinitionMessage] = {}
        last_timestamp: Optional[int] = None

        workout = WorkoutData(
            file_path=file_path,
            file_format="FIT",
            device_name="Garmin Watch",
            sport="Running",
        )

        records_raw: List[Dict[int, Any]] = []
        laps_raw: List[Dict[int, Any]] = []
        sessions_raw: List[Dict[int, Any]] = []
        file_ids_raw: List[Dict[int, Any]] = []
        device_infos_raw: List[Dict[int, Any]] = []
        time_in_zones_raw: List[Dict[int, Any]] = []
        zones_target_raw: List[Dict[int, Any]] = []
        hr_zones_raw: List[Dict[int, Any]] = []
        power_zones_raw: List[Dict[int, Any]] = []

        while pos < end_pos:
            record_header = data[pos]
            pos += 1

            # Check if compressed timestamp header (bit 7 == 1)
            if (record_header & 0x80) != 0:
                local_mesg_num = (record_header >> 5) & 0x03
                time_offset = record_header & 0x1F
                if last_timestamp is not None:
                    new_ts = (last_timestamp & ~0x1F) + time_offset
                    if new_ts < last_timestamp:
                        new_ts += 0x20
                    last_timestamp = new_ts

                defn = local_defs.get(local_mesg_num)
                if defn is None or pos + defn.total_byte_size > end_pos:
                    break
                fields, pos = self._read_data_message(data, pos, defn)
                if last_timestamp is not None and 253 not in fields:
                    fields[253] = last_timestamp
                self._collect_message(
                    defn.global_mesg_num,
                    fields,
                    records_raw,
                    laps_raw,
                    sessions_raw,
                    file_ids_raw,
                    device_infos_raw,
                    time_in_zones_raw,
                    zones_target_raw,
                    hr_zones_raw,
                    power_zones_raw,
                )
                continue

            # Normal header (bit 7 == 0)
            is_definition = (record_header & 0x40) != 0
            has_dev_data = (record_header & 0x20) != 0
            local_mesg_num = record_header & 0x0F

            if is_definition:
                if pos + 5 > end_pos:
                    break
                architecture = data[pos + 1]
                endian = ">" if architecture == 1 else "<"
                global_mesg_num = struct.unpack_from(
                    f"{endian}H",
                    data,
                    pos + 2,
                )[0]
                num_fields = data[pos + 4]
                pos += 5

                field_defs: List[Tuple[int, int, int]] = []
                for _ in range(num_fields):
                    if pos + 3 > end_pos:
                        break
                    f_num, f_size, f_type = (
                        data[pos],
                        data[pos + 1],
                        data[pos + 2],
                    )
                    field_defs.append((f_num, f_size, f_type))
                    pos += 3

                dev_field_defs: List[Tuple[int, int, int]] = []
                if has_dev_data and pos < end_pos:
                    num_dev_fields = data[pos]
                    pos += 1
                    for _ in range(num_dev_fields):
                        if pos + 3 > end_pos:
                            break
                        df_num, df_size, df_idx = (
                            data[pos],
                            data[pos + 1],
                            data[pos + 2],
                        )
                        dev_field_defs.append((df_num, df_size, df_idx))
                        pos += 3

                local_defs[local_mesg_num] = _FitDefinitionMessage(
                    endian=endian,
                    global_mesg_num=global_mesg_num,
                    field_defs=field_defs,
                    dev_field_defs=dev_field_defs,
                )
            else:
                defn = local_defs.get(local_mesg_num)
                if defn is None or pos + defn.total_byte_size > end_pos:
                    break
                fields, pos = self._read_data_message(data, pos, defn)
                if 253 in fields and isinstance(fields[253], int):
                    last_timestamp = fields[253]
                self._collect_message(
                    defn.global_mesg_num,
                    fields,
                    records_raw,
                    laps_raw,
                    sessions_raw,
                    file_ids_raw,
                    device_infos_raw,
                    time_in_zones_raw,
                    zones_target_raw,
                    hr_zones_raw,
                    power_zones_raw,
                )

        self._populate_workout(
            workout,
            records_raw,
            laps_raw,
            sessions_raw,
            file_ids_raw,
            device_infos_raw,
            time_in_zones_raw,
            zones_target_raw,
            hr_zones_raw,
            power_zones_raw,
        )
        return workout

    def _read_data_message(
        self, data: bytes, pos: int, defn: _FitDefinitionMessage
    ) -> Tuple[Dict[int, Any], int]:
        fields: Dict[int, Any] = {}
        for f_num, f_size, f_type in defn.field_defs:
            next_pos = pos + f_size
            raw = data[pos:next_pos]
            pos = next_pos
            val = _decode_fit_field(raw, defn.endian, f_type)
            if val is not None:
                fields[f_num] = val
        # Skip developer fields
        for _df_num, df_size, _df_idx in defn.dev_field_defs:
            pos += df_size
        return fields, pos

    def _collect_message(
        self,
        global_mesg_num: int,
        fields: Dict[int, Any],
        records_raw: List[Dict[int, Any]],
        laps_raw: List[Dict[int, Any]],
        sessions_raw: List[Dict[int, Any]],
        file_ids_raw: List[Dict[int, Any]],
        device_infos_raw: List[Dict[int, Any]],
        time_in_zones_raw: List[Dict[int, Any]],
        zones_target_raw: List[Dict[int, Any]],
        hr_zones_raw: List[Dict[int, Any]],
        power_zones_raw: List[Dict[int, Any]],
    ) -> None:
        if global_mesg_num == 0:  # file_id
            file_ids_raw.append(fields)
        elif global_mesg_num == 7:  # zones_target
            zones_target_raw.append(fields)
        elif global_mesg_num == 18:  # session
            sessions_raw.append(fields)
        elif global_mesg_num == 19:  # lap
            laps_raw.append(fields)
        elif global_mesg_num == 20:  # record
            records_raw.append(fields)
        elif global_mesg_num == 23:  # device_info
            device_infos_raw.append(fields)
        elif global_mesg_num == 53:  # hr_zone
            hr_zones_raw.append(fields)
        elif global_mesg_num == 65:  # power_zone
            power_zones_raw.append(fields)
        elif global_mesg_num == 216:  # time_in_zone
            time_in_zones_raw.append(fields)

    def _populate_workout(
        self,
        workout: WorkoutData,
        records_raw: List[Dict[int, Any]],
        laps_raw: List[Dict[int, Any]],
        sessions_raw: List[Dict[int, Any]],
        file_ids_raw: List[Dict[int, Any]],
        device_infos_raw: List[Dict[int, Any]],
        time_in_zones_raw: List[Dict[int, Any]],
        zones_target_raw: List[Dict[int, Any]],
        hr_zones_raw: List[Dict[int, Any]],
        power_zones_raw: List[Dict[int, Any]],
    ) -> None:
        # 1. Device identification
        for fid in file_ids_raw:
            prod_name = fid.get(8)  # product_name string
            garmin_prod = fid.get(2)  # product uint16
            if isinstance(prod_name, str) and prod_name:
                workout.device_name = prod_name
                break
            if isinstance(garmin_prod, int) and garmin_prod in GARMIN_PRODUCTS:
                workout.device_name = GARMIN_PRODUCTS[garmin_prod]
                break

        for dinfo in device_infos_raw:
            prod_name = dinfo.get(27)
            if isinstance(prod_name, str) and prod_name:
                workout.device_name = prod_name
                break

        # 2. Records (time-series)
        first_ts: Optional[datetime] = None
        sport_is_running_or_walking = True

        # Check sport early if session exists
        if sessions_raw:
            s_sport = sessions_raw[0].get(5)
            if isinstance(s_sport, int):
                workout.sport = FIT_SPORT_MAP.get(
                    s_sport,
                    f"Sport ({s_sport})",
                )
                sport_is_running_or_walking = s_sport in (1, 11, 17)

        for r in records_raw:
            ts_val = r.get(253)
            if not isinstance(ts_val, int):
                continue
            dt = FIT_EPOCH + timedelta(seconds=ts_val)
            if first_ts is None:
                first_ts = dt
            elapsed = (dt - first_ts).total_seconds()

            # Distance (field 5, scale 100 -> m)
            dist_m = _as_float(r.get(5), 100.0)

            # Speed: prefer enhanced_speed (field 73, scale 1000 -> m/s),
            # fallback speed (field 6, scale 1000 -> m/s)
            speed_m_s = _as_float(r.get(73, r.get(6)), 1000.0)

            # Altitude: prefer enhanced_altitude (field 78, scale 5,
            # offset 500), fallback altitude (field 2, scale 5, offset 500)
            alt_raw = r.get(78, r.get(2))
            altitude_m = (
                (float(alt_raw) / 5.0) - 500.0
                if isinstance(alt_raw, (int, float))
                else None
            )

            # Heart rate (field 3, bpm)
            hr_raw = r.get(3)
            hr_bpm = int(hr_raw) if isinstance(hr_raw, int) else None

            # Cadence (field 4, strides/cycles per min; for running, Garmin
            # records single-leg cadence, multiplied by 2 for SPM)
            cad_raw = r.get(4)
            if isinstance(cad_raw, int):
                cad_rpm = (
                    cad_raw * 2
                    if (sport_is_running_or_walking and cad_raw < 125)
                    else cad_raw
                )
            else:
                cad_rpm = None

            # Power (field 7, watts)
            pwr_raw = r.get(7)
            power_w = int(pwr_raw) if isinstance(pwr_raw, int) else None

            # Temperature (field 13, deg C)
            temp_c = _as_float(r.get(13))

            # GPS coordinates (field 0 = lat, field 1 = lon in semicircles)
            lat_raw = r.get(0)
            lon_raw = r.get(1)
            lat_deg = (
                float(lat_raw) * SEMICIRCLES_TO_DEG
                if isinstance(lat_raw, int)
                else None
            )
            lon_deg = (
                float(lon_raw) * SEMICIRCLES_TO_DEG
                if isinstance(lon_raw, int)
                else None
            )

            workout.records.append(
                WorkoutRecord(
                    timestamp=dt,
                    elapsed_seconds=elapsed,
                    distance_m=dist_m,
                    speed_m_s=speed_m_s,
                    heart_rate_bpm=hr_bpm,
                    cadence_rpm=cad_rpm,
                    altitude_m=altitude_m,
                    power_watts=power_w,
                    temperature_c=temp_c,
                    latitude_deg=lat_deg,
                    longitude_deg=lon_deg,
                )
            )

        # 3. Laps (message 19)
        for idx, lap in enumerate(laps_raw, start=1):
            start_raw = lap.get(2)
            start_dt = (
                FIT_EPOCH + timedelta(seconds=start_raw)
                if isinstance(start_raw, int)
                else None
            )
            el_raw = lap.get(7, 0)
            tm_raw = lap.get(8, el_raw)
            dist_raw = lap.get(9, 0)
            avg_spd_raw = lap.get(110, lap.get(13))
            avg_hr = lap.get(15)
            max_hr = lap.get(16)
            avg_cad = lap.get(17)
            if (
                isinstance(avg_cad, int)
                and sport_is_running_or_walking
                and avg_cad < 125
            ):
                avg_cad *= 2
            cals = lap.get(11)
            asc = lap.get(21)
            lap_cals = int(cals) if isinstance(cals, int) else None

            workout.laps.append(
                WorkoutLap(
                    lap_number=idx,
                    start_time=start_dt,
                    total_elapsed_seconds=(
                        float(el_raw) / 1000.0
                        if isinstance(el_raw, (int, float))
                        else 0.0
                    ),
                    total_timer_seconds=(
                        float(tm_raw) / 1000.0
                        if isinstance(tm_raw, (int, float))
                        else 0.0
                    ),
                    total_distance_m=(
                        float(dist_raw) / 100.0
                        if isinstance(dist_raw, (int, float))
                        else 0.0
                    ),
                    avg_speed_m_s=(
                        float(avg_spd_raw) / 1000.0
                        if isinstance(avg_spd_raw, (int, float))
                        else None
                    ),
                    avg_heart_rate_bpm=(
                        int(avg_hr) if isinstance(avg_hr, int) else None
                    ),
                    max_heart_rate_bpm=(
                        int(max_hr) if isinstance(max_hr, int) else None
                    ),
                    avg_cadence_rpm=(
                        int(avg_cad) if isinstance(avg_cad, int) else None
                    ),
                    total_calories=lap_cals,
                    total_ascent_m=(
                        float(asc) if isinstance(asc, (int, float)) else None
                    ),
                )
            )

        # 4. Session summary (message 18), with fallback from records
        if sessions_raw:
            sess = sessions_raw[0]
            s_sport = sess.get(5)
            if isinstance(s_sport, int):
                workout.sport = FIT_SPORT_MAP.get(
                    s_sport,
                    f"Sport ({s_sport})",
                )
            s_sub = sess.get(6)
            if isinstance(s_sub, int) and s_sub != 0:
                workout.sub_sport = FIT_SUB_SPORT_MAP.get(
                    s_sub,
                    f"SubSport ({s_sub})",
                )

            st_raw = sess.get(2)
            if isinstance(st_raw, int):
                workout.start_time = FIT_EPOCH + timedelta(seconds=st_raw)

            el_raw = sess.get(7)
            if isinstance(el_raw, (int, float)):
                workout.total_elapsed_seconds = float(el_raw) / 1000.0

            tm_raw = sess.get(8)
            if isinstance(tm_raw, (int, float)):
                workout.total_timer_seconds = float(tm_raw) / 1000.0

            dist_raw = sess.get(9)
            if isinstance(dist_raw, (int, float)):
                workout.total_distance_m = float(dist_raw) / 100.0

            cal_raw = sess.get(11)
            if isinstance(cal_raw, int):
                workout.total_calories = cal_raw

            avg_spd = sess.get(124, sess.get(14))
            if isinstance(avg_spd, (int, float)):
                workout.avg_speed_m_s = float(avg_spd) / 1000.0

            max_spd = sess.get(125, sess.get(15))
            if isinstance(max_spd, (int, float)):
                workout.max_speed_m_s = float(max_spd) / 1000.0

            avg_hr = sess.get(16)
            if isinstance(avg_hr, int):
                workout.avg_heart_rate_bpm = avg_hr

            max_hr = sess.get(17)
            if isinstance(max_hr, int):
                workout.max_heart_rate_bpm = max_hr

            avg_cad = sess.get(18)
            if isinstance(avg_cad, int):
                workout.avg_cadence_rpm = (
                    avg_cad * 2
                    if (sport_is_running_or_walking and avg_cad < 125)
                    else avg_cad
                )

            max_cad = sess.get(19)
            if isinstance(max_cad, int):
                workout.max_cadence_rpm = (
                    max_cad * 2
                    if (sport_is_running_or_walking and max_cad < 125)
                    else max_cad
                )

            avg_pwr = sess.get(20)
            if isinstance(avg_pwr, int):
                workout.avg_power_watts = avg_pwr

            max_pwr = sess.get(21)
            if isinstance(max_pwr, int):
                workout.max_power_watts = max_pwr

            asc = sess.get(22)
            if isinstance(asc, (int, float)):
                workout.total_ascent_m = float(asc)

            desc = sess.get(23)
            if isinstance(desc, (int, float)):
                workout.total_descent_m = float(desc)

        # Fill in any missing summary fields from time-series records
        _backfill_summary_from_records(workout)

        # 5. Populate watch-configured HR and Power zones from FIT messages
        _populate_zones_from_fit(
            workout,
            time_in_zones_raw,
            zones_target_raw,
            hr_zones_raw,
            power_zones_raw,
            sessions_raw,
        )


def _as_float(val: Any, scale: float = 1.0) -> Optional[float]:
    if isinstance(val, (int, float)):
        return float(val) / scale
    return None


def _as_int_list(val: Any) -> List[int]:
    if isinstance(val, list):
        return [int(x) for x in val if isinstance(x, (int, float))]
    if isinstance(val, (int, float)):
        return [int(val)]
    return []


def compute_sample_intervals(recs: List[WorkoutRecord]) -> List[float]:
    """Compute sample time intervals (seconds) for each workout record."""
    dts: List[float] = []
    for i, rec in enumerate(recs):
        if i > 0:
            raw_dt = rec.elapsed_seconds - recs[i - 1].elapsed_seconds
            dt = max(0.0, min(30.0, raw_dt))
        elif len(recs) > 1:
            raw_dt = recs[1].elapsed_seconds - rec.elapsed_seconds
            dt = max(0.0, min(30.0, raw_dt))
        else:
            dt = 1.0
        dts.append(dt)
    return dts


def _populate_zones_from_fit(
    workout: WorkoutData,
    time_in_zones_raw: List[Dict[int, Any]],
    zones_target_raw: List[Dict[int, Any]],
    hr_zones_raw: List[Dict[int, Any]],
    power_zones_raw: List[Dict[int, Any]],
    sessions_raw: List[Dict[int, Any]],
) -> None:
    """Extract watch-configured HR and Power zone boundaries and times."""
    hr_labels = ["Warm Up", "Easy", "Aerobic", "Threshold", "Maximum"]
    pwr_labels = ["Recovery", "Endurance", "Tempo", "Threshold", "Maximum"]

    # Prefer time_in_zone (mesg 216) referencing session (reference_mesg == 18)
    session_tiz: Optional[Dict[int, Any]] = None
    for tiz in time_in_zones_raw:
        if tiz.get(0) == 18:
            session_tiz = tiz
            break
    if session_tiz is None and time_in_zones_raw:
        session_tiz = time_in_zones_raw[-1]

    # Sample intervals (seconds) for fallback record-based zone calculation
    recs = workout.records
    dts = compute_sample_intervals(recs)

    # --- 1. Heart Rate Zones ---
    hr_bounds: List[int] = []
    raw_hr_times: List[int] = []

    if session_tiz is not None:
        for b in _as_int_list(session_tiz.get(6)):
            if 30 <= b < 255 and (not hr_bounds or b > hr_bounds[-1]):
                hr_bounds.append(b)
        raw_hr_times = _as_int_list(session_tiz.get(2))

    if not hr_bounds:
        for tiz in time_in_zones_raw:
            for b in _as_int_list(tiz.get(6)):
                if 30 <= b < 255 and (not hr_bounds or b > hr_bounds[-1]):
                    hr_bounds.append(b)
            if hr_bounds:
                break

    if not hr_bounds and hr_zones_raw:
        for hz in hr_zones_raw:
            raw_hb = hz.get(1)
            if isinstance(raw_hb, (int, float)) and 30 <= int(raw_hb) < 255:
                if not hr_bounds or int(raw_hb) > hr_bounds[-1]:
                    hr_bounds.append(int(raw_hb))

    if not raw_hr_times and sessions_raw:
        raw_hr_times = _as_int_list(sessions_raw[0].get(57))

    # Build 5 HR zone ranges [(low, high), ...] for Zones 1..5
    hr_ranges: List[Tuple[int, Optional[int]]] = []
    has_z0_hr_bound = False
    if len(hr_bounds) >= 6:
        has_z0_hr_bound = True
        for i in range(5):
            low_v = hr_bounds[i] if i == 0 else hr_bounds[i] + 1
            high_v = hr_bounds[i + 1]
            hr_ranges.append((low_v, high_v))
    elif len(hr_bounds) == 5:
        z0_low = int(round(hr_bounds[4] * 0.50))
        hr_ranges.append((z0_low, hr_bounds[0]))
        for i in range(1, 5):
            hr_ranges.append((hr_bounds[i - 1] + 1, hr_bounds[i]))
    else:
        # Check watch-configured max_heart_rate in time_in_zone (field 11)
        # or zones_target (field 1)
        cfg_max_hr: Optional[int] = None
        if session_tiz is not None and isinstance(session_tiz.get(11), int):
            cfg_max_hr = int(session_tiz[11])
        if cfg_max_hr is None and zones_target_raw:
            mhr = zones_target_raw[0].get(1)
            if isinstance(mhr, int) and 100 <= mhr < 255:
                cfg_max_hr = mhr
        if cfg_max_hr is not None:
            hr_b = [
                int(round(cfg_max_hr * pct))
                for pct in (0.50, 0.60, 0.70, 0.80, 0.90, 1.00)
            ]
            for i in range(5):
                low_v = hr_b[i] if i == 0 else hr_b[i] + 1
                hr_ranges.append((low_v, hr_b[i + 1]))

    if hr_ranges:
        hr_zone_secs = [0.0] * 5
        has_z0_times = len(raw_hr_times) >= 6 and sum(raw_hr_times[1:6]) > 0
        has_5_times = len(raw_hr_times) >= 5 and sum(raw_hr_times[:5]) > 0
        if has_z0_hr_bound and has_z0_times:
            for i in range(5):
                hr_zone_secs[i] = float(raw_hr_times[i + 1]) / 1000.0
        elif not has_z0_hr_bound and has_5_times:
            for i in range(5):
                hr_zone_secs[i] = float(raw_hr_times[i]) / 1000.0
        else:
            for rec, dt in zip(recs, dts):
                hr = rec.heart_rate_bpm
                if hr is None or hr < hr_ranges[0][0]:
                    continue
                for z_idx in range(4, -1, -1):
                    if hr >= hr_ranges[z_idx][0]:
                        hr_zone_secs[z_idx] += dt
                        break

        for i in range(5):
            workout.hr_zones.append(
                WorkoutZone(
                    zone_number=i + 1,
                    label=hr_labels[i],
                    low_value=hr_ranges[i][0],
                    high_value=hr_ranges[i][1],
                    time_seconds=hr_zone_secs[i],
                )
            )

    # --- 2. Power Zones ---
    pwr_bounds: List[int] = []
    raw_pwr_times: List[int] = []

    if session_tiz is not None:
        for b in _as_int_list(session_tiz.get(9)):
            if 10 <= b < 65534 and (not pwr_bounds or b > pwr_bounds[-1]):
                pwr_bounds.append(b)
        raw_pwr_times = _as_int_list(session_tiz.get(5))

    if not pwr_bounds:
        for tiz in time_in_zones_raw:
            for b in _as_int_list(tiz.get(9)):
                if 10 <= b < 65534 and (not pwr_bounds or b > pwr_bounds[-1]):
                    pwr_bounds.append(b)
            if pwr_bounds:
                break

    if not pwr_bounds and power_zones_raw:
        for pz in power_zones_raw:
            raw_pb = pz.get(1)
            if isinstance(raw_pb, (int, float)) and 10 <= int(raw_pb) < 65534:
                if not pwr_bounds or int(raw_pb) > pwr_bounds[-1]:
                    pwr_bounds.append(int(raw_pb))

    if not raw_pwr_times and sessions_raw:
        raw_pwr_times = _as_int_list(sessions_raw[0].get(60))

    pwr_ranges: List[Tuple[int, Optional[int]]] = []
    has_z0_pwr_bound = False
    if len(pwr_bounds) >= 6:
        has_z0_pwr_bound = True
        for i in range(5):
            low_v = pwr_bounds[i] if i == 0 else pwr_bounds[i] + 1
            pwr_high: Optional[int] = pwr_bounds[i + 1]
            if i == 4 and pwr_high is not None and pwr_high >= 1000:
                pwr_high = None
            pwr_ranges.append((low_v, pwr_high))
    elif len(pwr_bounds) == 5:
        z0_low = int(round(pwr_bounds[0] * 0.75))
        pwr_ranges.append((z0_low, pwr_bounds[0]))
        for i in range(1, 5):
            pwr_high = pwr_bounds[i]
            if i == 4 and pwr_high is not None and pwr_high >= 1000:
                pwr_high = None
            pwr_ranges.append((pwr_bounds[i - 1] + 1, pwr_high))
    else:
        cfg_ftp: Optional[int] = None
        if session_tiz is not None and isinstance(session_tiz.get(15), int):
            cfg_ftp = int(session_tiz[15])
        if cfg_ftp is None and zones_target_raw:
            ftp_val = zones_target_raw[0].get(3)
            if isinstance(ftp_val, int) and 50 <= ftp_val < 2000:
                cfg_ftp = ftp_val
        if cfg_ftp is not None:
            pcts = (0.65, 0.80, 0.90, 1.00, 1.15)
            pwr_b = [int(round(cfg_ftp * pct)) for pct in pcts]
            for i in range(4):
                low_v = pwr_b[i] if i == 0 else pwr_b[i] + 1
                pwr_ranges.append((low_v, pwr_b[i + 1]))
            pwr_ranges.append((pwr_b[4] + 1, None))

    if pwr_ranges:
        pwr_zone_secs = [0.0] * 5
        has_z0_ptimes = len(raw_pwr_times) >= 6 and sum(raw_pwr_times[1:6]) > 0
        has_5_ptimes = len(raw_pwr_times) >= 5 and sum(raw_pwr_times[:5]) > 0
        if has_z0_pwr_bound and has_z0_ptimes:
            for i in range(5):
                pwr_zone_secs[i] = float(raw_pwr_times[i + 1]) / 1000.0
        elif not has_z0_pwr_bound and has_5_ptimes:
            for i in range(5):
                pwr_zone_secs[i] = float(raw_pwr_times[i]) / 1000.0
        else:
            for rec, dt in zip(recs, dts):
                pwr = rec.power_watts
                if pwr is None or pwr < pwr_ranges[0][0]:
                    continue
                for z_idx in range(4, -1, -1):
                    if pwr >= pwr_ranges[z_idx][0]:
                        pwr_zone_secs[z_idx] += dt
                        break

        for i in range(5):
            workout.power_zones.append(
                WorkoutZone(
                    zone_number=i + 1,
                    label=pwr_labels[i],
                    low_value=pwr_ranges[i][0],
                    high_value=pwr_ranges[i][1],
                    time_seconds=pwr_zone_secs[i],
                )
            )


def _backfill_summary_from_records(workout: WorkoutData) -> None:
    """Populate any missing summary metrics from the workout's records."""
    recs = workout.records
    if not recs:
        return

    if workout.start_time is None:
        workout.start_time = recs[0].timestamp

    if workout.total_elapsed_seconds <= 0:
        workout.total_elapsed_seconds = recs[-1].elapsed_seconds
    if workout.total_timer_seconds <= 0:
        workout.total_timer_seconds = workout.total_elapsed_seconds

    distances = [r.distance_m for r in recs if r.distance_m is not None]
    if workout.total_distance_m <= 0 and distances:
        workout.total_distance_m = max(distances)

    speeds = [r.speed_m_s for r in recs if r.speed_m_s and r.speed_m_s > 0]
    if (
        workout.avg_speed_m_s is None
        and workout.active_duration_seconds > 0
        and workout.total_distance_m > 0
    ):
        workout.avg_speed_m_s = (
            workout.total_distance_m / workout.active_duration_seconds
        )
    elif workout.avg_speed_m_s is None and speeds:
        workout.avg_speed_m_s = sum(speeds) / len(speeds)

    if workout.max_speed_m_s is None and speeds:
        workout.max_speed_m_s = max(speeds)

    hrs = [r.heart_rate_bpm for r in recs if r.heart_rate_bpm is not None]
    if workout.avg_heart_rate_bpm is None and hrs:
        workout.avg_heart_rate_bpm = round(sum(hrs) / len(hrs))
    if workout.max_heart_rate_bpm is None and hrs:
        workout.max_heart_rate_bpm = max(hrs)

    cads = [r.cadence_rpm for r in recs if r.cadence_rpm and r.cadence_rpm > 0]
    if workout.avg_cadence_rpm is None and cads:
        workout.avg_cadence_rpm = round(sum(cads) / len(cads))
    if workout.max_cadence_rpm is None and cads:
        workout.max_cadence_rpm = max(cads)

    pwrs = [r.power_watts for r in recs if r.power_watts and r.power_watts > 0]
    if workout.avg_power_watts is None and pwrs:
        workout.avg_power_watts = round(sum(pwrs) / len(pwrs))
    if workout.max_power_watts is None and pwrs:
        workout.max_power_watts = max(pwrs)

    alts = [r.altitude_m for r in recs if r.altitude_m is not None]
    if workout.total_ascent_m is None and len(alts) > 1:
        ascent = 0.0
        descent = 0.0
        for prev_a, curr_a in zip(alts, alts[1:]):
            diff = curr_a - prev_a
            if diff > 0:
                ascent += diff
            elif diff < 0:
                descent += abs(diff)
        workout.total_ascent_m = round(ascent, 1)
        workout.total_descent_m = round(descent, 1)


@ParserRegistry.register
class TcxWorkoutParser(WorkoutParser):
    """Parser for Garmin Training Center XML (.tcx) workout files."""

    supported_extensions = [".tcx"]

    def parse(self, file_path: Path) -> WorkoutData:
        tree = ET.parse(file_path)
        root = tree.getroot()

        workout = WorkoutData(
            file_path=file_path,
            file_format="TCX",
            device_name="Garmin Device",
        )

        # Strip XML namespaces for straightforward tag lookup
        for elem in root.iter():
            if "}" in elem.tag:
                elem.tag = elem.tag.split("}", 1)[1]

        activity = root.find(".//Activity")
        if activity is not None and "Sport" in activity.attrib:
            workout.sport = activity.attrib["Sport"]

        creator_name = root.findtext(".//Creator/Name")
        if creator_name:
            workout.device_name = creator_name.strip()

        first_ts: Optional[datetime] = None
        total_calories = 0

        for idx, lap_elem in enumerate(root.findall(".//Lap"), start=1):
            st_str = lap_elem.attrib.get("StartTime")
            lap_start = _parse_iso_datetime(st_str) if st_str else None
            lap_time = float(lap_elem.findtext("TotalTimeSeconds") or 0.0)
            lap_dist = float(lap_elem.findtext("DistanceMeters") or 0.0)
            lap_cals = int(float(lap_elem.findtext("Calories") or 0))
            total_calories += lap_cals

            avg_hr_str = lap_elem.findtext("AverageHeartRateBpm/Value")
            max_hr_str = lap_elem.findtext("MaximumHeartRateBpm/Value")
            lap_spd = (lap_dist / lap_time) if lap_time > 0 else None
            lap_avg_hr = int(float(avg_hr_str)) if avg_hr_str else None
            lap_max_hr = int(float(max_hr_str)) if max_hr_str else None

            workout.laps.append(
                WorkoutLap(
                    lap_number=idx,
                    start_time=lap_start,
                    total_elapsed_seconds=lap_time,
                    total_timer_seconds=lap_time,
                    total_distance_m=lap_dist,
                    avg_speed_m_s=lap_spd,
                    avg_heart_rate_bpm=lap_avg_hr,
                    max_heart_rate_bpm=lap_max_hr,
                    total_calories=lap_cals if lap_cals > 0 else None,
                )
            )

            for tp in lap_elem.findall(".//Trackpoint"):
                t_str = tp.findtext("Time")
                if not t_str:
                    continue
                dt = _parse_iso_datetime(t_str)
                if dt is None:
                    continue
                if first_ts is None:
                    first_ts = dt
                elapsed = (dt - first_ts).total_seconds()

                dist_str = tp.findtext("DistanceMeters")
                alt_str = tp.findtext("AltitudeMeters")
                hr_str = tp.findtext("HeartRateBpm/Value")
                cad_str = tp.findtext("Cadence")
                if not cad_str:
                    cad_str = tp.findtext(".//RunCadence")
                spd_str = tp.findtext(".//Speed")
                pwr_str = tp.findtext(".//Watts")
                lat_str = tp.findtext("Position/LatitudeDegrees")
                lon_str = tp.findtext("Position/LongitudeDegrees")

                dist_m = float(dist_str) if dist_str else None
                spd_m_s = float(spd_str) if spd_str else None
                if spd_m_s is None and dist_m is not None and workout.records:
                    prev = workout.records[-1]
                    dt_s = elapsed - prev.elapsed_seconds
                    if dt_s > 0 and prev.distance_m is not None:
                        spd_m_s = max(0.0, (dist_m - prev.distance_m) / dt_s)

                cad_val = int(float(cad_str)) if cad_str else None
                if (
                    cad_val is not None
                    and cad_val < 125
                    and workout.sport.lower() == "running"
                ):
                    cad_val *= 2

                workout.records.append(
                    WorkoutRecord(
                        timestamp=dt,
                        elapsed_seconds=elapsed,
                        distance_m=dist_m,
                        speed_m_s=spd_m_s,
                        heart_rate_bpm=int(float(hr_str)) if hr_str else None,
                        cadence_rpm=cad_val,
                        altitude_m=float(alt_str) if alt_str else None,
                        power_watts=int(float(pwr_str)) if pwr_str else None,
                        latitude_deg=float(lat_str) if lat_str else None,
                        longitude_deg=float(lon_str) if lon_str else None,
                    )
                )

        if total_calories > 0:
            workout.total_calories = total_calories
        if workout.laps:
            l_times = [lap.total_elapsed_seconds for lap in workout.laps]
            l_dists = [lap.total_distance_m for lap in workout.laps]
            workout.total_elapsed_seconds = sum(l_times)
            workout.total_timer_seconds = workout.total_elapsed_seconds
            workout.total_distance_m = sum(l_dists)

        _backfill_summary_from_records(workout)
        return workout


@ParserRegistry.register
class GpxWorkoutParser(WorkoutParser):
    """Parser for GPS Exchange Format (.gpx) workout files."""

    supported_extensions = [".gpx"]

    def parse(self, file_path: Path) -> WorkoutData:
        tree = ET.parse(file_path)
        root = tree.getroot()

        for elem in root.iter():
            if "}" in elem.tag:
                elem.tag = elem.tag.split("}", 1)[1]

        creator = root.attrib.get("creator", "GPS Device")
        sport_text = root.findtext(".//trk/type") or "Running"

        workout = WorkoutData(
            file_path=file_path,
            file_format="GPX",
            device_name=creator,
            sport=sport_text.capitalize(),
        )

        first_ts: Optional[datetime] = None
        cumulative_dist = 0.0

        for trkpt in root.findall(".//trkpt"):
            t_str = trkpt.findtext("time")
            if not t_str:
                continue
            dt = _parse_iso_datetime(t_str)
            if dt is None:
                continue
            if first_ts is None:
                first_ts = dt
            elapsed = (dt - first_ts).total_seconds()

            lat = float(trkpt.attrib["lat"]) if "lat" in trkpt.attrib else None
            lon = float(trkpt.attrib["lon"]) if "lon" in trkpt.attrib else None
            ele_str = trkpt.findtext("ele")
            hr_str = trkpt.findtext(".//hr")
            cad_str = trkpt.findtext(".//cad")
            pwr_str = trkpt.findtext(".//power")
            temp_str = trkpt.findtext(".//atemp")

            speed_m_s: Optional[float] = None
            if workout.records and lat is not None and lon is not None:
                prev = workout.records[-1]
                prev_lat = prev.latitude_deg
                prev_lon = prev.longitude_deg
                if prev_lat is not None and prev_lon is not None:
                    step_dist = _haversine_m(prev_lat, prev_lon, lat, lon)
                    cumulative_dist += step_dist
                    dt_s = elapsed - prev.elapsed_seconds
                    if dt_s > 0:
                        speed_m_s = step_dist / dt_s

            cad_val = int(float(cad_str)) if cad_str else None
            if cad_val is not None and cad_val < 125:
                cad_val *= 2

            workout.records.append(
                WorkoutRecord(
                    timestamp=dt,
                    elapsed_seconds=elapsed,
                    distance_m=cumulative_dist,
                    speed_m_s=speed_m_s,
                    heart_rate_bpm=int(float(hr_str)) if hr_str else None,
                    cadence_rpm=cad_val,
                    altitude_m=float(ele_str) if ele_str else None,
                    power_watts=int(float(pwr_str)) if pwr_str else None,
                    temperature_c=float(temp_str) if temp_str else None,
                    latitude_deg=lat,
                    longitude_deg=lon,
                )
            )

        _backfill_summary_from_records(workout)
        return workout


def _parse_iso_datetime(value: str) -> Optional[datetime]:
    try:
        cleaned = value.strip()
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    sin_dp = math.sin(dp / 2) ** 2
    sin_dl = math.sin(dl / 2) ** 2
    a = sin_dp + math.cos(p1) * math.cos(p2) * sin_dl
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))
