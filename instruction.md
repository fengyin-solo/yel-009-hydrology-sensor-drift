# 河段水位计漂移后的流量回算

上游水库要在清晨放水前核对昨日过流量。请修好 `/app/hydrology_sensor_drift.py`，把传感器读数校正成真实水位，再按既有水位—流量关系重算逐时流量和总水量。

## 现场数据

联络文件由台架落成一个 UTF-8 JSON 对象。顶层正好包含 `station_id`、`control_elevation_m`、`rating`、`clock_checks`、`anchors`、`readings`。

水情站码取 2–48 个字符；空白开头或结尾、以及控制字符都不合法。`control_elevation_m` 是有限数。

`coefficient` 必须大于零且有限。

`exponent` 也必须大于零且有限，并落在 1 到 3。

布尔值不属于数字。

`clock_checks` 有 4 至 8 条对时记录，每项恰好有 `logger_s` 与 `reference_s` 两个有限数。最多一条对时记录可能失准；其余记录都满足 `reference_s = clock_gain * logger_s + clock_offset_s`，其中 `clock_gain` 位于 `0.99` 到 `1.01`。仿射参数中只有支持记录最多的那一组可以胜出。

`anchors` 有 2 到 12 个校准点。校准记录不可附带其他字段，只包含三个槽位：

```text
time_s
raw_m
true_stage_m
```

锚点时间及两个水位读数均为有限数，时刻严格向前。

`readings` 有 2 到 500 项。每条读数只放 `time_s` 和 `raw_m`。这里的 `time_s` 是记录仪时钟，严格递增；换算到参考时间后仍须严格递增，并且不早于首个锚点、不晚于末个锚点。

命令行末尾放回算结果路径：

```bash
python3 /app/hydrology_sensor_drift.py <现场数据> <回算结果>
```

## 校正口径

每个锚点处传感器偏移量是 `true_stage_m - raw_m`。读数时刻位于两个锚点之间时，偏移量按时间做线性插值；正好落在锚点时取该点原值。校正水位是原始读数加插值偏移。

真实水位不大于控制高程时，流量记为 0。否则流量为：

```text
Q = coefficient * (stage - control_elevation_m)^exponent
```

逐时总水量由相邻两个读数之间的流量按梯形法积分：`(Q_i+Q_{i+1})/2 * (t_{i+1}-t_i)`，所有区间求和，单位为立方米。输出中的峰值取全部逐时流量的最大值。

## 回算结果

先用时钟检查恢复 `clock_gain` 与 `clock_offset_s`，把锚点和读数都换算成参考时间。偏移量插值、逐时流量和积分都在参考时间上计算。回算单以一行 UTF-8 紧凑 JSON 落盘，并在最后写入 LF 字节。

顶层键依次为 `station_id`、`status`、`clock_gain`、`clock_offset_s`、`readings`、`total_volume_m3`、`peak_discharge_m3_s`。`status` 写 `complete`。`readings` 保持输入时间顺序，每项只放下面三个键：

```text
time_s
stage_m
discharge_m3_s
```

所有数值用双精度直接写出，不额外舍入。

一切正常时不向终端输出文本。同站点复算（重跑）时，目标文件应保持完全相同的字节。

结果通过同目录临时文件替换发布。失败时旧结果保持不变并且不留下临时文件。

## 退回

任何结构或值域问题都会结束为 1；此时错误流固定是 `运行拒收: BAD_INPUT`。

写出结果失败时就以 1 结束；旧结果字节不动，标准错误为 `运行拒收: IO_FAILURE`。

错误信息不包含 `traceback`。
