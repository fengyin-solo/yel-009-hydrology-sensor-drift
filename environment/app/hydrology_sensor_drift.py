#!/usr/bin/env python3
"""Legacy uncorrected stage-to-discharge converter."""

import json
import sys
from pathlib import Path


def main(argv):
    if len(argv) != 3:
        return 1
    source, target = Path(argv[1]), Path(argv[2])
    payload = json.loads(source.read_text(encoding="utf-8"))
    rating = payload["rating"]
    readings = []
    for item in payload["readings"]:
        stage = item["raw_m"]
        depth = max(0.0, stage - payload["control_elevation_m"])
        discharge = rating["coefficient"] * depth**rating["exponent"]
        readings.append(
            {"time_s": item["time_s"], "stage_m": stage, "discharge_m3_s": discharge}
        )
    result = {
        "station_id": payload["station_id"],
        "status": "complete",
        "readings": readings,
        "total_volume_m3": sum(item["discharge_m3_s"] for item in readings),
        "peak_discharge_m3_s": max(item["discharge_m3_s"] for item in readings),
    }
    target.write_text(json.dumps(result, separators=(",", ":")) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
