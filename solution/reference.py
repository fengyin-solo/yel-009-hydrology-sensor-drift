#!/usr/bin/env python3
"""Correct drifting stage records and integrate a rating curve."""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
from itertools import combinations, permutations
from pathlib import Path


TOP = {"station_id", "control_elevation_m", "rating", "clock_checks", "anchors", "readings"}
RATING = {"checks", "check_logger_times_s", "exponent"}
RATING_CHECK = {"raw_m", "discharge_m3_s"}
CLOCK_CHECK = {"logger_s", "reference_s"}
ANCHOR = {"time_s", "raw_m", "true_stage_m"}
READING = {"time_s", "raw_m"}
MAX_RATING_ERRORS = 4


class Refusal(Exception):
    def __init__(self, kind):
        self.kind = kind


def fail(kind):
    sys.stderr.write(f"运行拒收: {kind}\n")
    raise SystemExit(1)


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def pairs_hook(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Refusal("BAD_INPUT")
        result[key] = value
    return result


def validate(payload):
    if not isinstance(payload, dict) or set(payload) != TOP:
        raise Refusal("BAD_INPUT")
    station_id = payload["station_id"]
    if (
        not isinstance(station_id, str)
        or not 2 <= len(station_id) <= 48
        or station_id != station_id.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in station_id)
    ):
        raise Refusal("BAD_INPUT")
    control = payload["control_elevation_m"]
    if not finite(control):
        raise Refusal("BAD_INPUT")
    rating = payload["rating"]
    if not isinstance(rating, dict) or set(rating) != RATING:
        raise Refusal("BAD_INPUT")
    rating_checks = rating["checks"]
    check_times = rating["check_logger_times_s"]
    exponent = rating["exponent"]
    if not finite(exponent) or not 1 <= exponent <= 3:
        raise Refusal("BAD_INPUT")
    if not isinstance(rating_checks, list) or len(rating_checks) != 10:
        raise Refusal("BAD_INPUT")
    if (
        not isinstance(check_times, list)
        or len(check_times) != len(rating_checks)
        or any(not finite(value) for value in check_times)
        or len(set(check_times)) != len(check_times)
    ):
        raise Refusal("BAD_INPUT")
    clean_rating_checks = []
    for item in rating_checks:
        if not isinstance(item, dict) or set(item) != RATING_CHECK:
            raise Refusal("BAD_INPUT")
        if any(not finite(item[key]) for key in RATING_CHECK):
            raise Refusal("BAD_INPUT")
        if item["discharge_m3_s"] <= 0:
            raise Refusal("BAD_INPUT")
        clean_rating_checks.append(
            (
                float(item["raw_m"]),
                float(item["discharge_m3_s"]),
            )
        )
    clock_checks = payload["clock_checks"]
    if not isinstance(clock_checks, list) or not 4 <= len(clock_checks) <= 8:
        raise Refusal("BAD_INPUT")
    clean_clock = []
    for item in clock_checks:
        if not isinstance(item, dict) or set(item) != CLOCK_CHECK:
            raise Refusal("BAD_INPUT")
        if any(not finite(item[key]) for key in CLOCK_CHECK):
            raise Refusal("BAD_INPUT")
        clean_clock.append((float(item["logger_s"]), float(item["reference_s"])))
    anchors = payload["anchors"]
    if not isinstance(anchors, list) or not 2 <= len(anchors) <= 12:
        raise Refusal("BAD_INPUT")
    clean_anchors = []
    previous = -math.inf
    for item in anchors:
        if not isinstance(item, dict) or set(item) != ANCHOR:
            raise Refusal("BAD_INPUT")
        if any(not finite(item[key]) for key in ANCHOR):
            raise Refusal("BAD_INPUT")
        if item["time_s"] <= previous:
            raise Refusal("BAD_INPUT")
        previous = item["time_s"]
        clean_anchors.append(
            (float(item["time_s"]), float(item["raw_m"]), float(item["true_stage_m"]))
        )
    readings = payload["readings"]
    if not isinstance(readings, list) or not 2 <= len(readings) <= 500:
        raise Refusal("BAD_INPUT")
    clean_readings = []
    previous = -math.inf
    for item in readings:
        if not isinstance(item, dict) or set(item) != READING:
            raise Refusal("BAD_INPUT")
        if any(not finite(item[key]) for key in READING):
            raise Refusal("BAD_INPUT")
        if item["time_s"] <= previous:
            raise Refusal("BAD_INPUT")
        previous = item["time_s"]
        clean_readings.append((float(item["time_s"]), float(item["raw_m"])))
    return (
        station_id,
        float(control),
        clean_rating_checks,
        tuple(float(value) for value in check_times),
        float(exponent),
        clean_clock,
        clean_anchors,
        clean_readings,
    )


def recover_clock(checks):
    supported = {}
    for left, right in combinations(checks, 2):
        if left[0] == right[0]:
            continue
        gain = (right[1] - left[1]) / (right[0] - left[0])
        offset = left[1] - gain * left[0]
        if not 0.99 <= gain <= 1.01 or not math.isfinite(offset):
            continue
        score = sum(
            abs(gain * logger + offset - reference) <= 1e-9
            for logger, reference in checks
        )
        key = (round(gain, 12), round(offset, 9))
        supported[key] = max(score, supported.get(key, -1))
    if not supported:
        raise Refusal("BAD_INPUT")
    best = max(supported.values())
    winners = [params for params, score in supported.items() if score == best]
    if best < len(checks) - 1 or len(winners) != 1:
        raise Refusal("BAD_INPUT")
    return winners[0]


def correction_at(anchors, when):
    for left, right in zip(anchors, anchors[1:]):
        if left[0] <= when <= right[0]:
            fraction = (when - left[0]) / (right[0] - left[0])
            left_offset = left[2] - left[1]
            right_offset = right[2] - right[1]
            return left_offset + fraction * (right_offset - left_offset)
    if when == anchors[-1][0]:
        return anchors[-1][2] - anchors[-1][1]
    raise Refusal("BAD_INPUT")


def maximum_matching(edges):
    chosen = {}

    def augment(check, seen):
        for time_index in edges[check]:
            if time_index in seen:
                continue
            seen.add(time_index)
            if time_index not in chosen or augment(chosen[time_index], seen):
                chosen[time_index] = check
                return chosen.get(time_index) == check
        return False

    for check in sorted(range(len(edges)), key=lambda index: len(edges[index])):
        augment(check, set())
    return {check: time_index for time_index, check in chosen.items()}


def recover_rating(rating_checks, check_times, exponent, control, anchors, gain, offset):
    reference_times = tuple(gain * value + offset for value in check_times)
    if any(when < anchors[0][0] or when > anchors[-1][0] for when in reference_times):
        raise Refusal("BAD_INPUT")
    corrections = tuple(correction_at(anchors, when) for when in reference_times)
    support_groups = {}
    best_support = -1
    total_checks = len(rating_checks)

    def edges_for(coefficient, curvature, attenuation):
        edges = []
        for raw, observed in rating_checks:
            row = []
            for time_index, correction in enumerate(corrections):
                depth = raw + correction - control
                shape = 1.0 + curvature * depth + attenuation * depth * depth
                if depth > 0 and shape > 0 and math.isclose(
                    coefficient * depth**exponent * shape,
                    observed,
                    rel_tol=1e-9,
                    abs_tol=1e-12,
                ):
                    row.append(time_index)
            edges.append(tuple(row))
        return tuple(edges)

    for check_indices in combinations(range(total_checks), 3):
        (first, second, third) = check_indices
        raw_first, flow_first = rating_checks[first]
        raw_second, flow_second = rating_checks[second]
        raw_third, flow_third = rating_checks[third]
        for first_time, second_time, third_time in permutations(range(len(reference_times)), 3):
            depth_first = raw_first + corrections[first_time] - control
            depth_second = raw_second + corrections[second_time] - control
            depth_third = raw_third + corrections[third_time] - control
            if min(depth_first, depth_second, depth_third) <= 0:
                continue
            ratio_second = flow_second / flow_first
            ratio_third = flow_third / flow_first
            base_second = (depth_second / depth_first) ** exponent
            base_third = (depth_third / depth_first) ** exponent
            matrix = (
                (
                    ratio_second * depth_first - base_second * depth_second,
                    ratio_second * depth_first * depth_first
                    - base_second * depth_second * depth_second,
                    base_second - ratio_second,
                ),
                (
                    ratio_third * depth_first - base_third * depth_third,
                    ratio_third * depth_first * depth_first
                    - base_third * depth_third * depth_third,
                    base_third - ratio_third,
                ),
            )
            determinant = matrix[0][0] * matrix[1][1] - matrix[0][1] * matrix[1][0]
            scale = max(1.0, abs(matrix[0][0] * matrix[1][1]), abs(matrix[0][1] * matrix[1][0]))
            if not math.isfinite(determinant) or abs(determinant) <= 1e-12 * scale:
                continue
            curvature = (
                matrix[0][2] * matrix[1][1] - matrix[1][2] * matrix[0][1]
            ) / determinant
            attenuation = (
                matrix[0][0] * matrix[1][2] - matrix[1][0] * matrix[0][2]
            ) / determinant
            if (
                not finite(curvature)
                or not finite(attenuation)
                or abs(curvature) > 5
                or abs(attenuation) > 5
            ):
                continue
            base_first = 1.0 + curvature * depth_first + attenuation * depth_first * depth_first
            if base_first <= 0:
                continue
            coefficient = flow_first / (depth_first**exponent * base_first)
            if not finite(coefficient) or coefficient <= 0:
                continue
            edges = edges_for(coefficient, curvature, attenuation)
            if sum(len(row) for row in edges) < total_checks - MAX_RATING_ERRORS:
                continue
            matching = maximum_matching(edges)
            support = len(matching)
            if support > best_support:
                best_support = support
                support_groups = {}
            if support == best_support:
                support_groups.setdefault(edges, []).append(
                    (coefficient, curvature, attenuation)
                )

    if best_support < total_checks - MAX_RATING_ERRORS or len(support_groups) != 1:
        raise Refusal("BAD_INPUT")
    candidates = next(iter(support_groups.values()))
    coefficient = math.fsum(item[0] for item in candidates) / len(candidates)
    curvature = math.fsum(item[1] for item in candidates) / len(candidates)
    attenuation = math.fsum(item[2] for item in candidates) / len(candidates)
    edges = edges_for(coefficient, curvature, attenuation)
    matching = maximum_matching(edges)
    if len(matching) < total_checks - MAX_RATING_ERRORS:
        raise Refusal("BAD_INPUT")
    assigned = [None] * total_checks
    for check, time_index in matching.items():
        assigned[check] = float(check_times[time_index])
    return coefficient, curvature, attenuation, assigned


def reconstruct(
    station_id,
    control,
    rating_checks,
    check_times,
    exponent,
    clock_checks,
    anchors,
    readings,
):
    gain, offset = recover_clock(clock_checks)
    anchors = [
        (gain * when + offset, raw, stage)
        for when, raw, stage in anchors
    ]
    coefficient, curvature, attenuation, assigned_times = recover_rating(
        rating_checks,
        check_times,
        exponent,
        control,
        anchors,
        gain,
        offset,
    )
    readings = [
        (gain * when + offset, raw)
        for when, raw in readings
    ]
    if readings[0][0] < anchors[0][0] or readings[-1][0] > anchors[-1][0]:
        raise Refusal("BAD_INPUT")
    rows = []
    for when, raw in readings:
        stage = raw + correction_at(anchors, when)
        depth = max(0.0, stage - control)
        shape = 1.0 + curvature * depth + attenuation * depth * depth
        discharge = coefficient * depth**exponent * max(0.0, shape)
        rows.append({"time_s": when, "stage_m": stage, "discharge_m3_s": discharge})
    volume = sum(
        (left["discharge_m3_s"] + right["discharge_m3_s"])
        / 2.0
        * (right["time_s"] - left["time_s"])
        for left, right in zip(rows, rows[1:])
    )
    return {
        "station_id": station_id,
        "status": "complete",
        "clock_gain": gain,
        "clock_offset_s": offset,
        "coefficient": coefficient,
        "exponent": exponent,
        "curvature_per_m": curvature,
        "attenuation_per_m2": attenuation,
        "check_times_s": assigned_times,
        "readings": rows,
        "total_volume_m3": volume,
        "peak_discharge_m3_s": max(item["discharge_m3_s"] for item in rows),
    }


def publish(target, payload):
    if not target.parent.is_dir() or target.is_dir() or not os.access(target.parent, os.W_OK):
        raise Refusal("IO_FAILURE")
    old = target.read_bytes() if target.exists() else None
    stage = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=".hydro-", dir=target.parent)
        stage = Path(name)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(stage, target)
        stage = None
    except OSError:
        if old is None:
            target.unlink(missing_ok=True)
        else:
            target.write_bytes(old)
        raise Refusal("IO_FAILURE")
    finally:
        if stage is not None:
            stage.unlink(missing_ok=True)


def main(argv):
    if len(argv) != 3:
        fail("BAD_INPUT")
    source, target = Path(argv[1]), Path(argv[2])
    try:
        payload = json.loads(source.read_text(encoding="utf-8"), object_pairs_hook=pairs_hook)
        result = reconstruct(*validate(payload))
        encoded = (json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        publish(target, encoded)
        return 0
    except Refusal as exc:
        fail(exc.kind)
    except (OSError, UnicodeError, ValueError):
        fail("BAD_INPUT")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
