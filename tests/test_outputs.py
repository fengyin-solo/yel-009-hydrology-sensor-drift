"""Acceptance tests for drifting stage reconstruction."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ENTRY = os.environ.get("HYDROLOGY_ENTRY", "/app/hydrology_sensor_drift.py")
TRUSTED_PYTHON = Path("/usr/local/bin/python3")
SETPRIV = Path("/usr/bin/setpriv")


def candidate_argv(*arguments):
    values = [str(value) for value in arguments]
    if SETPRIV.is_file() and TRUSTED_PYTHON.is_file():
        return [
            str(SETPRIV),
            "--reuid=65534",
            "--regid=65534",
            "--clear-groups",
            str(TRUSTED_PYTHON),
            "-I",
            "-B",
            *values,
        ]
    return [sys.executable, "-I", "-B", *values]


def expose_to_candidate(path):
    if not SETPRIV.is_file():
        return
    path.chmod(0o777)
    for parent in path.parents:
        if parent == Path("/tmp"):
            break
        parent.chmod(parent.stat().st_mode | 0o001)


def invoke_candidate(*arguments, timeout=30):
    return subprocess.run(
        candidate_argv(*arguments),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def rating_checks(coefficient=2.0, exponent=1.5):
    gain, offset = 1.01, -5.0
    anchors = ((-5.0, 1.0, 1.1), (96.0, 1.5, 1.7))
    points = ((10.0, 1.05), (30.0, 1.18), (55.0, 1.30), (80.0, 1.42), (95.0, 1.54))
    checks = []
    for logger, raw in points:
        when = gain * logger + offset
        fraction = (when - anchors[0][0]) / (anchors[1][0] - anchors[0][0])
        left_offset = anchors[0][2] - anchors[0][1]
        right_offset = anchors[1][2] - anchors[1][1]
        stage = raw + left_offset + fraction * (right_offset - left_offset)
        checks.append(
            {
                "logger_s": logger,
                "raw_m": raw,
                "discharge_m3_s": coefficient * (stage - 1.0) ** exponent,
            }
        )
    checks[-1]["discharge_m3_s"] = 999.0
    return checks


def remap_clock(payload, gain, offset):
    old_gain, old_offset = 1.01, -5.0
    for field in ("anchors", "readings"):
        for item in payload[field]:
            reference = old_gain * item["time_s"] + old_offset
            item["time_s"] = (reference - offset) / gain
    for item in payload["rating"]["checks"]:
        reference = old_gain * item["logger_s"] + old_offset
        item["logger_s"] = (reference - offset) / gain
    payload["clock_checks"] = [
        {
            "logger_s": logger,
            "reference_s": gain * logger + offset,
        }
        for logger in (0.0, 20.0, 40.0, 60.0)
    ]
    payload["clock_checks"][-1]["reference_s"] += 1.0
    return payload


def sample():
    return {
        "station_id": "RIVER-N17",
        "control_elevation_m": 1.0,
        "rating": {"checks": rating_checks()},
        "clock_checks": [
            {"logger_s": 0.0, "reference_s": -5.0},
            {"logger_s": 20.0, "reference_s": 15.2},
            {"logger_s": 40.0, "reference_s": 35.4},
            {"logger_s": 60.0, "reference_s": 66.0},
        ],
        "anchors": [
            {"time_s": 0.0, "raw_m": 1.0, "true_stage_m": 1.1},
            {"time_s": 100.0, "raw_m": 1.5, "true_stage_m": 1.7},
        ],
        "readings": [
            {"time_s": 0.0, "raw_m": 1.0},
            {"time_s": 50.0, "raw_m": 1.2},
            {"time_s": 100.0, "raw_m": 1.4},
        ],
    }


def write(path, document, raw=None):
    if raw is None:
        raw = json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n"
    path.write_bytes(raw.encode("utf-8"))
    return path


def invoke(tmp_path, document, raw=None, name="result"):
    source = write(tmp_path / "station.json", document, raw=raw)
    output = tmp_path / f"{name}.json"
    expose_to_candidate(tmp_path)
    result = invoke_candidate(ENTRY, source, output)
    return result, source, output


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_success_is_quiet(tmp_path):
    """题面：「一切正常时不向终端输出文本。」"""
    result, _, output = invoke(tmp_path, sample())
    assert result.returncode == 0 and result.stdout == "" and result.stderr == "" and output.is_file()


def test_stage_offset_is_linearly_interpolated(tmp_path):
    """题面：「读数时刻位于两个锚点之间时，偏移量按时间做线性插值」。"""
    result, _, output = invoke(tmp_path, sample())
    stages = [item["stage_m"] for item in read(output)["readings"]]
    assert result.returncode == 0 and stages == pytest.approx([1.1, 1.35, 1.6])


def test_rating_parameters_are_recovered_from_discharge_checks(tmp_path):
    # CASE HYDRO-03-RATING HYDRO-31-RATING
    """题面：「参考解必须从这些检查中反算唯一的 `coefficient` 与 `exponent`，允许一条失准记录；」"""
    result, _, output = invoke(tmp_path, sample())
    document = read(output)
    assert result.returncode == 0
    assert document["coefficient"] == pytest.approx(2.0, rel=1e-10)
    assert document["exponent"] == pytest.approx(1.5, rel=1e-10)
    flows = [item["discharge_m3_s"] for item in document["readings"]]
    expected = [2.0 * depth**1.5 for depth in [0.1, 0.35, 0.6]]
    assert flows == pytest.approx(expected)


@pytest.mark.parametrize("exponent", [1.0, 3.0])
def test_rating_exponent_endpoints_are_accepted(tmp_path, exponent):
    """题面：「反算出的 `coefficient` 必须大于零且有限，`exponent` 必须有限并落在 1 到 3；」"""
    payload = sample()
    payload["rating"]["checks"] = rating_checks(exponent=exponent)
    result, _, output = invoke(tmp_path, payload)
    assert result.returncode == 0
    assert read(output)["exponent"] == pytest.approx(exponent, rel=1e-10)


def test_rating_check_shape_is_strict(tmp_path):
    """题面：「流量检查有 4 到 10 项」。"""
    payload = sample()
    payload["rating"]["checks"] = payload["rating"]["checks"][:3]
    result, _, output = invoke(tmp_path, payload)
    assert result.returncode == 1 and result.stderr == "运行拒收: BAD_INPUT\n" and not output.exists()
    payload = sample()
    payload["rating"]["checks"][0]["quality"] = "estimated"
    result, _, output = invoke(tmp_path, payload)
    assert result.returncode == 1 and result.stderr == "运行拒收: BAD_INPUT\n" and not output.exists()


def test_rating_checks_must_stay_inside_the_anchor_window(tmp_path):
    """题面：「换算到参考时间后，每项检查必须落在首末锚点之间」。"""
    payload = sample()
    payload["rating"]["checks"][0]["logger_s"] = 1000.0
    result, _, output = invoke(tmp_path, payload)
    assert result.returncode == 1 and result.stderr == "运行拒收: BAD_INPUT\n" and not output.exists()


def test_rating_checks_require_positive_corrected_depth(tmp_path):
    """题面：「校正水位高于控制高程」。"""
    payload = sample()
    payload["rating"]["checks"][0]["raw_m"] = 0.8
    result, _, output = invoke(tmp_path, payload)
    assert result.returncode == 1 and result.stderr == "运行拒收: BAD_INPUT\n" and not output.exists()


def test_rating_checks_need_a_unique_consensus_curve(tmp_path):
    """题面：「只有支持记录最多的那一组可以胜出」。"""
    payload = sample()
    for item in payload["rating"]["checks"]:
        item["discharge_m3_s"] = 1.0
    result, _, output = invoke(tmp_path, payload)
    assert result.returncode == 1 and result.stderr == "运行拒收: BAD_INPUT\n" and not output.exists()


def test_volume_uses_trapezoid_integration(tmp_path):
    """题面：「逐时总水量由相邻两个读数之间的流量按梯形法积分」。"""
    result, _, output = invoke(tmp_path, sample())
    document = read(output)
    flows = [item["discharge_m3_s"] for item in document["readings"]]
    expected = (flows[0] + flows[1]) / 2 * 50.5 + (flows[1] + flows[2]) / 2 * 50.5
    assert result.returncode == 0 and document["total_volume_m3"] == pytest.approx(expected)


def test_peak_is_maximum_corrected_discharge(tmp_path):
    """题面：「输出中的峰值取全部逐时流量的最大值。」"""
    result, _, output = invoke(tmp_path, sample())
    document = read(output)
    assert result.returncode == 0 and document["peak_discharge_m3_s"] == max(
        item["discharge_m3_s"] for item in document["readings"]
    )


def test_top_level_keys_are_exact(tmp_path):
    """题面：「顶层正好包含 `station_id`、`control_elevation_m`、`rating`、`clock_checks`、`anchors`、`readings`。」"""
    payload = sample()
    payload["operator"] = "night"
    result, _, output = invoke(tmp_path, payload)
    assert result.returncode == 1 and result.stderr == "运行拒收: BAD_INPUT\n" and not output.exists()


def test_rating_keys_are_exact(tmp_path):
    """题面：「`rating` 恰好包含 `checks`。」"""
    payload = sample()
    payload["rating"]["offset"] = 0.0
    result, _, output = invoke(tmp_path, payload)
    assert result.returncode == 1 and result.stderr == "运行拒收: BAD_INPUT\n" and not output.exists()


def test_duplicate_json_key_is_refused(tmp_path):
    """题面：「任何结构或值域问题都会结束为 1；此时错误流固定是 `运行拒收: BAD_INPUT`。」"""
    raw = json.dumps(sample(), separators=(",", ":"))
    raw = raw[:-1] + ',"station_id":"RIVER-N18"}'
    result, _, output = invoke(tmp_path, None, raw=raw)
    assert result.returncode == 1 and result.stderr == "运行拒收: BAD_INPUT\n" and not output.exists()


@pytest.mark.parametrize("station_id", ["A", " leading", "trailing ", "A" * 49, "A\nB"])
def test_station_identifier_is_strict(tmp_path, station_id):
    """题面：「水情站码取 2–48 个字符；空白开头或结尾、以及控制字符都不合法。」"""
    payload = sample()
    payload["station_id"] = station_id
    result, _, output = invoke(tmp_path, payload)
    assert result.returncode == 1 and result.stderr == "运行拒收: BAD_INPUT\n" and not output.exists()


@pytest.mark.parametrize("station_id", ["A1", "A" * 48])
def test_station_identifier_length_endpoints_are_accepted(tmp_path, station_id):
    """题面：「水情站码取 2–48 个字符」。"""
    payload = sample()
    payload["station_id"] = station_id
    result, _, output = invoke(tmp_path, payload)
    assert result.returncode == 0 and read(output)["station_id"] == station_id


def test_boolean_reading_is_refused(tmp_path):
    """题面：「布尔值不属于数字。」"""
    payload = sample()
    payload["readings"][0]["raw_m"] = False
    result, _, output = invoke(tmp_path, payload)
    assert result.returncode == 1 and result.stderr == "运行拒收: BAD_INPUT\n" and not output.exists()


def test_anchor_times_must_be_strictly_increasing(tmp_path):
    """题面：「锚点时间及两个水位读数均为有限数，时刻严格向前。」"""
    payload = sample()
    payload["anchors"][1]["time_s"] = 0.0
    result, _, output = invoke(tmp_path, payload)
    assert result.returncode == 1 and result.stderr == "运行拒收: BAD_INPUT\n" and not output.exists()


def test_readings_outside_anchor_span_are_refused(tmp_path):
    """题面：「这里的 `time_s` 是记录仪时钟，严格递增；换算到参考时间后仍须严格递增，并且不早于首个锚点、不晚于末个锚点。」"""
    payload = sample()
    payload["readings"][0]["time_s"] = -1.0
    result, _, output = invoke(tmp_path, payload)
    assert result.returncode == 1 and result.stderr == "运行拒收: BAD_INPUT\n" and not output.exists()


def test_exponent_range_is_enforced(tmp_path):
    """题面：「反算出的 `coefficient` 必须大于零且有限，`exponent` 必须有限并落在 1 到 3；」"""
    for exponent in (0.5, 3.5):
        payload = sample()
        payload["rating"]["checks"] = rating_checks(exponent=exponent)
        result, _, output = invoke(tmp_path, payload)
        assert result.returncode == 1 and result.stderr == "运行拒收: BAD_INPUT\n" and not output.exists()


def test_nonpositive_coefficient_is_refused(tmp_path):
    """题面：「反算出的 `coefficient` 必须大于零且有限，`exponent` 必须有限并落在 1 到 3；」"""
    payload = sample()
    payload["rating"]["checks"] = rating_checks(coefficient=-2.0)
    result, _, output = invoke(tmp_path, payload)
    assert result.returncode == 1 and result.stderr == "运行拒收: BAD_INPUT\n" and not output.exists()


def test_result_has_fixed_compact_shape(tmp_path):
    """题面：「回算单以一行 UTF-8 紧凑 JSON 落盘，并在最后写入 LF 字节。」"""
    result, _, output = invoke(tmp_path, sample())
    raw = output.read_bytes()
    document = json.loads(raw)
    assert result.returncode == 0
    assert list(document) == [
        "station_id",
        "status",
        "clock_gain",
        "clock_offset_s",
        "coefficient",
        "exponent",
        "readings",
        "total_volume_m3",
        "peak_discharge_m3_s",
    ]
    assert b": " not in raw and raw.endswith(b"\n") and raw.count(b"\n") == 1


def test_anchor_has_exact_keys(tmp_path):
    """题面：「校准记录不可附带其他字段，只包含三个槽位：」。"""
    payload = sample()
    payload["anchors"][0]["offset_m"] = 0.0
    result, _, output = invoke(tmp_path, payload)
    assert result.returncode == 1 and result.stderr == "运行拒收: BAD_INPUT\n" and not output.exists()


def test_clock_mapping_is_recovered_and_one_bad_check_ignored(tmp_path):
    # CASE HYDRO-23-CLOCK HYDRO-30-CLOCK
    """题面：「`clock_checks` 有 4 至 8 条对时记录，每项恰好有 `logger_s` 与 `reference_s` 两个有限数。最多一条对时记录可能失准；其余记录都满足 `reference_s = clock_gain * logger_s + clock_offset_s`，其中 `clock_gain` 位于 `0.99` 到 `1.01`。仿射参数中只有支持记录最多的那一组可以胜出。」"""
    result, _, output = invoke(tmp_path, sample())
    document = read(output)
    assert result.returncode == 0
    assert document["clock_gain"] == pytest.approx(1.01, abs=1e-12)
    assert document["clock_offset_s"] == pytest.approx(-5.0, abs=1e-9)
    assert [item["time_s"] for item in document["readings"]] == pytest.approx([-5.0, 45.5, 96.0])


def test_clock_gain_lower_endpoint_is_accepted(tmp_path):
    """题面：「其中 `clock_gain` 位于 `0.99` 到 `1.01`」。"""
    payload = remap_clock(sample(), gain=0.99, offset=-3.0)
    result, _, output = invoke(tmp_path, payload)
    assert result.returncode == 0
    assert read(output)["clock_gain"] == pytest.approx(0.99, abs=1e-12)


def test_clock_check_shape_is_strict(tmp_path):
    """题面：「`clock_checks` 有 4 至 8 条对时记录，每项恰好有 `logger_s` 与 `reference_s` 两个有限数。」"""
    payload = sample()
    del payload["clock_checks"][0]["reference_s"]
    result, _, output = invoke(tmp_path, payload)
    assert result.returncode == 1 and result.stderr == "运行拒收: BAD_INPUT\n" and not output.exists()


def test_clock_checks_must_determine_one_affine_map(tmp_path):
    """题面：「`clock_checks` 有 4 至 8 条对时记录，每项恰好有 `logger_s` 与 `reference_s` 两个有限数。最多一条对时记录可能失准；其余记录都满足 `reference_s = clock_gain * logger_s + clock_offset_s`，其中 `clock_gain` 位于 `0.99` 到 `1.01`。仿射参数中只有支持记录最多的那一组可以胜出。」"""
    payload = sample()
    payload["clock_checks"] = [
        {"logger_s": 10.0, "reference_s": 10.0}
        for _ in range(4)
    ]
    result, _, output = invoke(tmp_path, payload)
    assert result.returncode == 1 and result.stderr == "运行拒收: BAD_INPUT\n" and not output.exists()


def test_reading_has_exact_keys(tmp_path):
    """题面：「每条读数只放 `time_s` 和 `raw_m`。」"""
    payload = sample()
    payload["readings"][0]["quality"] = "good"
    result, _, output = invoke(tmp_path, payload)
    assert result.returncode == 1 and result.stderr == "运行拒收: BAD_INPUT\n" and not output.exists()


def test_output_reading_has_exact_keys(tmp_path):
    """题面：「`readings` 保持输入时间顺序，每项只放下面三个键：」。"""
    result, _, output = invoke(tmp_path, sample())
    assert result.returncode == 0
    assert all(set(item) == {"time_s", "stage_m", "discharge_m3_s"} for item in read(output)["readings"])


def test_success_leaves_no_temporary_file(tmp_path):
    """题面：「失败时旧结果保持不变并且不留下临时文件。」"""
    result, _, output = invoke(tmp_path, sample())
    leftovers = [item.name for item in tmp_path.iterdir() if item.name.startswith(".hydro-")]
    assert result.returncode == 0 and output.exists() and leftovers == []


def test_absent_parent_is_io_failure(tmp_path):
    """题面：「写出结果失败时就以 1 结束；旧结果字节不动，标准错误为 `运行拒收: IO_FAILURE`。」"""
    source = write(tmp_path / "station.json", sample())
    output = tmp_path / "absent-parent" / "result.json"
    expose_to_candidate(tmp_path)
    result = invoke_candidate(ENTRY, source, output)
    assert result.returncode == 1 and result.stderr == "运行拒收: IO_FAILURE\n" and not output.exists()


def test_bad_input_preserves_existing_result(tmp_path):
    """题面：「错误信息不包含 `traceback`。」"""
    result, source, output = invoke(tmp_path, sample())
    before = output.read_bytes()
    payload = sample()
    payload["rating"]["checks"][0]["discharge_m3_s"] = True
    write(source, payload)
    rejected = invoke_candidate(ENTRY, source, output)
    assert result.returncode == 0 and rejected.returncode == 1
    assert output.read_bytes() == before and "traceback" not in rejected.stderr.lower()


def test_repeat_run_is_byte_identical(tmp_path):
    """题面：「同站点复算（重跑）时，目标文件应保持完全相同的字节。」"""
    result, source, output = invoke(tmp_path, sample())
    first = output.read_bytes()
    second = invoke_candidate(ENTRY, source, output)
    assert result.returncode == 0 and second.returncode == 0 and output.read_bytes() == first
