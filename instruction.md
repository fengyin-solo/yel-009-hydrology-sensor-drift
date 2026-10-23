# 河段水位计漂移后的流量回算

上游水库要在清晨放水前核对昨日过流量。请修好 `/app/hydrology_sensor_drift.py`，把传感器读数校正成真实水位，再按既有水位—流量关系重算逐时流量和总水量。

## 现场数据

联络文件由台架落成一个 UTF-8 JSON 对象。顶层正好包含 `station_id`、`control_elevation_m`、`rating`、`clock_checks`、`anchors`、`readings`。

水情站码取 2–48 个字符；空白开头或结尾、以及控制字符都不合法。`control_elevation_m` 是有限数。

布尔值不属于数字。

`rating` 恰好包含 `checks`。
`rating` 还恰好包含 `check_logger_times_s`。
`rating` 还恰好包含 `exponent`。

`checks` 恰好有 10 项。
每项恰好包含 `raw_m`、`discharge_m3_s` 两个有限数，且实测流量大于零。
旧程序把每次检查的记录仪时刻单独拔到
`check_logger_times_s`，却没有保存它们与检查项的对应关系；该数组与 `checks` 等长，
数值互不相同且均为有限数。每项检查恰好对应其中一个时刻，但当前顺序无关。

`exponent` 是有限数并落在 1 到 3。至多四项检查可能失准，其余检查在恢复出正确
时刻对应后都满足下面的幂律。换算到参考时间后，匹配项必须落在首末锚点之间，
且校正水位高于控制高程。匹配曲线还要服从后文给出的全部参数约束。
在所有合法的时刻双射与曲线参数中，只有支持检查最多的那一组可以胜出。

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

先只用授时检查恢复 `clock_gain` 与 `clock_offset_s`。随后用同一时钟换算流量检查的时间，
按锚点校正每条检查的原始水位。记水位高于控制高程的差为 `depth`，流量检查支持：

```text
Q = coefficient * depth^exponent
    * (1 + curvature_per_m*depth + attenuation_per_m2*depth^2)
```

参考解必须联合恢复检查项到记录仪时刻的唯一双射，以及唯一的 `coefficient`、
`curvature_per_m` 和 `attenuation_per_m2`，允许一条检查失准。三个曲线参数都必须有限；
`coefficient` 大于零，`curvature_per_m` 与 `attenuation_per_m2` 的绝对值都不超过 5，
并且对每个匹配检查，括号内因子必须严格大于零。不能把 `check_logger_times_s` 的数组顺序
直接当作 `checks` 顺序，也不能只用两组对应硬拟合三项参数。

真实水位不大于控制高程时，读数流量记为 0。否则用量检查反算出的参数计算流量。

逐时总水量由相邻两个读数之间的流量按梯形法积分：`(Q_i+Q_{i+1})/2 * (t_{i+1}-t_i)`，所有区间求和，单位为立方米。输出中的峰值取全部逐时流量的最大值。

## 回算结果

先用时钟检查恢复 `clock_gain` 与 `clock_offset_s`，把锚点和读数都换算成参考时间。偏移量插值、逐时流量和积分都在参考时间上计算。回算单以一行 UTF-8 紧凑 JSON 落盘，并在最后写入 LF 字节。

顶层键依次为 `station_id`、`status`、`clock_gain`、`clock_offset_s`、
`coefficient`、`exponent`、`curvature_per_m`、`attenuation_per_m2`、`check_times_s`、
`readings`、`total_volume_m3`、`peak_discharge_m3_s`。
`status` 写 `complete`。`readings` 保持输入时间顺序，每项只放下面三个键：

```text
time_s
stage_m
discharge_m3_s
```

`check_times_s` 与输入 `checks` 等长，保持检查项原顺序；匹配到记录仪时刻的位置写该
有限数值，每个失准检查写 JSON `null`。每个非空时刻在全文恰好出现一次。

所有数值用双精度直接写出，不额外舍入。

一切正常时不向终端输出文本。同站点复算（重跑）时，目标文件应保持完全相同的字节。

结果通过同目录临时文件替换发布。失败时旧结果保持不变并且不留下临时文件。

## 调试预算

这是一次有限时修补。写完后最多运行一个成功样例和一个拒收样例确认入口；不要自行生成随机
fuzz、全排列压力测试或长时间优化循环。核心实现可运行后立即提交，把额外验证留给现有验收测试。

## 退回

任何结构或值域问题都会结束为 1；此时错误流固定是 `运行拒收: BAD_INPUT`。

写出结果失败时就以 1 结束；旧结果字节不动，标准错误为 `运行拒收: IO_FAILURE`。

错误信息不包含 `traceback`。
