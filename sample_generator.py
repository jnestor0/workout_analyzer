"""Generate a realistic Garmin Forerunner 970 .FIT workout file for testing."""

# pylint: disable=too-many-locals,too-many-statements

from __future__ import annotations

import math
import struct
from datetime import datetime, timezone
from pathlib import Path

from fit_parser import FIT_EPOCH, SEMICIRCLES_TO_DEG


def _fit_crc16(data: bytes) -> int:
    """Compute standard Garmin FIT 16-bit CRC."""
    crc_table = (
        0x0000,
        0xCC01,
        0xD801,
        0x1400,
        0xF001,
        0x3C00,
        0x2800,
        0xE401,
        0xA001,
        0x6C00,
        0x7800,
        0xB401,
        0x5000,
        0x9C01,
        0x8801,
        0x4400,
    )
    crc = 0
    for byte in data:
        tmp = crc_table[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ crc_table[byte & 0xF]
        tmp = crc_table[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ crc_table[(byte >> 4) & 0xF]
    return crc


def generate_sample_forerunner970_fit(output_path: str | Path) -> Path:
    """Create a binary .FIT file simulating a 5K run on a Forerunner 970."""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    payload = bytearray()

    start_dt = datetime(2026, 11, 4, 12, 30, 0, tzinfo=timezone.utc)
    start_ts = int((start_dt - FIT_EPOCH).total_seconds())

    # 1. Definition Message 0: file_id (global mesg 0)
    # Fields: type (0, enum, 1B), manufacturer (1, uint16, 2B),
    # product (2, uint16, 2B),
    # time_created (4, uint32, 4B), product_name (8, string, 24B)
    payload += struct.pack(
        "<BBBHB",
        0x40 | 0,  # Definition header, local mesg 0
        0,  # Reserved
        0,  # Little endian
        0,  # Global mesg 0 (file_id)
        5,  # 5 fields
    )
    payload += struct.pack(
        "<BBBBBBBBBBBBBBB",
        0,
        1,
        0x00,  # field 0: type (enum)
        1,
        2,
        0x84,  # field 1: manufacturer (uint16)
        2,
        2,
        0x84,  # field 2: garmin_product (uint16)
        4,
        4,
        0x86,  # field 4: time_created (uint32)
        8,
        24,
        0x07,  # field 8: product_name (string, 24 bytes)
    )

    # Data Message 0: file_id
    prod_name_bytes = b"Garmin Forerunner 970\x00".ljust(24, b"\x00")
    payload += struct.pack(
        "<B B H H I 24s",
        0x00 | 0,  # Data header, local mesg 0
        4,  # Activity file type = 4
        1,  # Manufacturer = Garmin (1)
        4670,  # Product ID = Forerunner 970 (4670)
        start_ts,
        prod_name_bytes,
    )

    # 2. Definition Message 1: record (global mesg 20)
    # Fields:
    # 253: timestamp (uint32, 4B)
    # 0: position_lat (sint32, 4B)
    # 1: position_long (sint32, 4B)
    # 5: distance (uint32, 4B, scale 100)
    # 73: enhanced_speed (uint32, 4B, scale 1000)
    # 78: enhanced_altitude (uint32, 4B, scale 5, offset 500)
    # 3: heart_rate (uint8, 1B)
    # 4: cadence (uint8, 1B)
    # 7: power (uint16, 2B)
    # 13: temperature (sint8, 1B)
    payload += struct.pack(
        "<BBBHB",
        0x40 | 1,  # Definition header, local mesg 1
        0,
        0,  # Little endian
        20,  # Global mesg 20 (record)
        10,  # 10 fields
    )
    record_fields = [
        (253, 4, 0x86),
        (0, 4, 0x85),
        (1, 4, 0x85),
        (5, 4, 0x86),
        (73, 4, 0x86),
        (78, 4, 0x86),
        (3, 1, 0x02),
        (4, 1, 0x02),
        (7, 2, 0x84),
        (13, 1, 0x01),
    ]
    for f_num, f_size, f_type in record_fields:
        payload += struct.pack("<BBB", f_num, f_size, f_type)

    # Simulate a 26-minute (~1560s) 5.2 km run sampled every 2s (780 points)
    num_samples = 780
    dt_step = 2
    total_dist_m = 0.0
    base_lat = 40.0150  # Boulder, CO
    base_lon = -105.2705
    loop_radius_deg = 0.0072

    sum_hr = 0
    max_hr = 0
    sum_cad = 0
    max_cad = 0
    sum_pwr = 0
    max_pwr = 0
    max_spd_m_s = 0.0
    total_ascent = 0.0
    total_descent = 0.0
    prev_alt: float | None = None

    lap_records = []
    current_lap_start_ts = start_ts
    current_lap_dist = 0.0
    current_lap_time = 0
    current_lap_hrs = []
    current_lap_cads = []
    lap_threshold_m = 1000.0  # 1 km auto-laps

    for i in range(num_samples):
        elapsed = i * dt_step
        ts = start_ts + elapsed
        progress = i / max(1, num_samples - 1)

        # Realistic running speed profile: warm up, cruise with slight hills,
        # fast finish kick
        hill_wave = math.sin(progress * 4 * math.pi)
        speed_m_s = (
            3.35
            + 0.35 * math.sin(progress * math.pi)
            - 0.18 * hill_wave
            + (0.45 * ((progress - 0.85) / 0.15) if progress > 0.85 else 0.0)
        )
        step_dist = speed_m_s * dt_step if i > 0 else 0.0
        total_dist_m += step_dist
        current_lap_dist += step_dist
        current_lap_time += dt_step if i > 0 else 0

        max_spd_m_s = max(max_spd_m_s, speed_m_s)

        # Altitude profile around 1620m with rolling hills
        base_wave = math.sin(progress * 2 * math.pi - 0.4)
        alt_m = 1620.0 + 18.0 * base_wave + 6.0 * hill_wave
        if prev_alt is not None:
            diff = alt_m - prev_alt
            if diff > 0:
                total_ascent += diff
            else:
                total_descent += abs(diff)
        prev_alt = alt_m

        # Heart rate profile: ramps up from 122 bpm to ~174 bpm,
        # modulated by hills and final kick
        warmup_factor = 1.0 - math.exp(-elapsed / 140.0)
        hr_bpm = int(
            round(
                118
                + 44 * warmup_factor
                + 6 * hill_wave
                + (10 * ((progress - 0.82) / 0.18) if progress > 0.82 else 0.0)
            )
        )
        hr_bpm = max(95, min(195, hr_bpm))
        sum_hr += hr_bpm
        max_hr = max(max_hr, hr_bpm)
        current_lap_hrs.append(hr_bpm)

        # Running cadence (stored as single-foot cycles/min in FIT,
        # e.g. 88 -> 176 spm)
        spm = int(round(170 + 8 * (speed_m_s - 3.2) + 2 * math.sin(i * 0.1)))
        half_cad = max(75, min(105, spm // 2))
        sum_cad += half_cad
        max_cad = max(max_cad, half_cad)
        current_lap_cads.append(half_cad)

        # Running power (Garmin native wrist power in watts)
        pwr_w = int(round(265 + 45 * (speed_m_s - 3.3) + 18 * hill_wave))
        sum_pwr += pwr_w
        max_pwr = max(max_pwr, pwr_w)

        # GPS coordinates around a scenic loop
        angle = progress * 2 * math.pi
        lat_deg = base_lat + loop_radius_deg * math.sin(angle)
        lon_deg = base_lon + loop_radius_deg * 1.25 * math.cos(angle)
        lat_sc = int(round(lat_deg / SEMICIRCLES_TO_DEG))
        lon_sc = int(round(lon_deg / SEMICIRCLES_TO_DEG))

        temp_c = int(round(18 + 3 * progress))

        dist_scaled = int(round(total_dist_m * 100.0))
        spd_scaled = int(round(speed_m_s * 1000.0))
        alt_scaled = int(round((alt_m + 500.0) * 5.0))

        payload += struct.pack(
            "<B I ii I I I B B H b",
            0x00 | 1,  # Data header, local mesg 1
            ts,
            lat_sc,
            lon_sc,
            dist_scaled,
            spd_scaled,
            alt_scaled,
            hr_bpm,
            half_cad,
            pwr_w,
            temp_c,
        )

        # Check if 1km lap completed
        if current_lap_dist >= lap_threshold_m or i == num_samples - 1:
            if current_lap_time > 0:
                lap_spd = (current_lap_dist / current_lap_time) * 1000.0
                lap_cad = sum(current_lap_cads) / len(current_lap_cads)
                lap_records.append(
                    {
                        "start_ts": current_lap_start_ts,
                        "end_ts": ts,
                        "elapsed_ms": current_lap_time * 1000,
                        "dist_cm": int(round(current_lap_dist * 100.0)),
                        "avg_spd_mms": int(round(lap_spd)),
                        "avg_hr": int(
                            round(sum(current_lap_hrs) / len(current_lap_hrs))
                        ),
                        "max_hr": max(current_lap_hrs),
                        "avg_cad": int(round(lap_cad)),
                        "calories": int(round(current_lap_dist * 0.068)),
                    }
                )
                current_lap_start_ts = ts
                current_lap_dist = 0.0
                current_lap_time = 0
                current_lap_hrs = []
                current_lap_cads = []

    # 3. Definition Message 2: lap (global mesg 19)
    payload += struct.pack(
        "<BBBHB",
        0x40 | 2,
        0,
        0,
        19,  # Global mesg 19 (lap)
        9,
    )
    lap_fields = [
        (253, 4, 0x86),  # timestamp
        (2, 4, 0x86),  # start_time
        (7, 4, 0x86),  # total_elapsed_time (ms)
        (8, 4, 0x86),  # total_timer_time (ms)
        (9, 4, 0x86),  # total_distance (cm)
        (13, 2, 0x84),  # avg_speed (mm/s)
        (15, 1, 0x02),  # avg_heart_rate
        (16, 1, 0x02),  # max_heart_rate
        (11, 2, 0x84),  # total_calories
    ]
    for f_num, f_size, f_type in lap_fields:
        payload += struct.pack("<BBB", f_num, f_size, f_type)

    for lap in lap_records:
        payload += struct.pack(
            "<B I I I I I H B B H",
            0x00 | 2,
            lap["end_ts"],
            lap["start_ts"],
            lap["elapsed_ms"],
            lap["elapsed_ms"],
            lap["dist_cm"],
            lap["avg_spd_mms"],
            lap["avg_hr"],
            lap["max_hr"],
            lap["calories"],
        )

    # 4. Definition Message 3: session (global mesg 18)
    payload += struct.pack(
        "<BBBHB",
        0x40 | 3,
        0,
        0,
        18,  # Global mesg 18 (session)
        16,
    )
    session_fields = [
        (253, 4, 0x86),  # timestamp
        (2, 4, 0x86),  # start_time
        (5, 1, 0x00),  # sport (1 = running)
        (6, 1, 0x00),  # sub_sport (2 = street)
        (7, 4, 0x86),  # total_elapsed_time (ms)
        (8, 4, 0x86),  # total_timer_time (ms)
        (9, 4, 0x86),  # total_distance (cm)
        (11, 2, 0x84),  # total_calories (kcal)
        (14, 2, 0x84),  # avg_speed (mm/s)
        (15, 2, 0x84),  # max_speed (mm/s)
        (16, 1, 0x02),  # avg_heart_rate (bpm)
        (17, 1, 0x02),  # max_heart_rate (bpm)
        (18, 1, 0x02),  # avg_cadence (strides/min)
        (19, 1, 0x02),  # max_cadence (strides/min)
        (20, 2, 0x84),  # avg_power (watts)
        (22, 2, 0x84),  # total_ascent (m)
    ]
    for f_num, f_size, f_type in session_fields:
        payload += struct.pack("<BBB", f_num, f_size, f_type)

    total_time_s = (num_samples - 1) * dt_step
    avg_spd_mms = int(round((total_dist_m / total_time_s) * 1000.0))
    max_spd_mms = int(round(max_spd_m_s * 1000.0))

    payload += struct.pack(
        "<B I I B B I I I H H H B B B B H H",
        0x00 | 3,
        start_ts + total_time_s,
        start_ts,
        1,  # Running
        2,  # Street
        total_time_s * 1000,
        total_time_s * 1000,
        int(round(total_dist_m * 100.0)),
        int(round(total_dist_m * 0.068)),  # ~365 kcal
        avg_spd_mms,
        max_spd_mms,
        int(round(sum_hr / num_samples)),
        max_hr,
        int(round(sum_cad / num_samples)),
        max_cad,
        int(round(sum_pwr / num_samples)),
        int(round(total_ascent)),
    )

    # 5. Definition Message 4: time_in_zone (global mesg 216)
    payload += struct.pack(
        "<BBBHB",
        0x40 | 4,
        0,
        0,
        216,  # Global mesg 216 (time_in_zone)
        3,
    )
    tiz_fields = [
        (0, 2, 0x84),  # reference_mesg (18 = session)
        (6, 6, 0x02),  # hr_zone_high_boundary (6 x uint8: z0..z5 high bpm)
        (9, 12, 0x84),  # power_zone_high_boundary (6 x uint16: z0..z5 high W)
    ]
    for f_num, f_size, f_type in tiz_fields:
        payload += struct.pack("<BBB", f_num, f_size, f_type)

    payload += struct.pack(
        "<B H 6B 6H",
        0x00 | 4,
        18,  # session reference
        96,
        115,
        134,
        153,
        172,
        192,  # watch-configured HR zones
        175,
        225,
        265,
        300,
        345,
        1500,  # watch-configured Power zones
    )

    # Assemble 14-byte FIT header + payload + 2-byte CRC
    data_size = len(payload)
    header_without_crc = struct.pack(
        "<BBHI4s",
        14,  # header size
        0x20,  # protocol version 2.0
        2140,  # profile version 21.40
        data_size,
        b".FIT",
    )
    header_crc = _fit_crc16(header_without_crc)
    header = header_without_crc + struct.pack("<H", header_crc)

    full_body = header + bytes(payload)
    file_crc = _fit_crc16(full_body)
    out.write_bytes(full_body + struct.pack("<H", file_crc))
    return out
