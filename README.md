# 纸样厚度核对与卷序厚度偏差监测 API

## 一、纸样厚度计数核对

登记一批纸样的总片数，分次提交互可重叠的厚度区间计数（整数微米，区间
`[lo, hi)`，含起点不含终点）。给定合格区间，返回**同时满足全部已接收计数**
时合格片数可达到的最小值和最大值。计算按端点做整数规划，不逐片展开。

补报与已有计数不相容时**整份拒收**（HTTP 409），已有结果保持不变、仍可查询。

## 二、卷序厚度偏差监测

一条监测序列在建立时固定目标厚度 `target_thickness`、容差 `tolerance` 和
报警线 `alarm_threshold`（均为整数微米）。每卷按**生产卷序**（从 1 起）录入
一个实测厚度，并关联已有的质检登记号（`inspections` 的 `id`）。

按卷序逐卷处理：

```
amount  = measured - target - tolerance
current = max(0, current + amount)
if current >= threshold:
    记该卷为报警卷，current = 0
```

* 只有超出“目标 + 容差”的部分才计入累计；累计值不会低于零；
* 累计值达到或超过报警线时，**该卷**被记为报警卷，累计值清零后继续；
* 测厚值、关联登记号、处理后的累计值和报警标记在同一事务内提交，读数与
  报警一起生效。

### 乱序补录与增量回算

`PUT /roll-series/{id}/readings/{roll}` 可任意顺序调用，同一卷重复提交即更正。
计算**不从头扫描**：

1. 起点累计值按键查“该卷之前最近一卷”，单行查询；
2. 从被改动卷起按卷序回放；处理完某个**原有卷**后的累计值若与改动前相同，
   即说明其后各卷的输入与起点都与改动前一致，立即停止回放，沿用其后的既有
   报警——余下各卷的读数不再读取，状态也不再查询。

因此乱序补录或更正后的报警卷号与从头计算完全一致，代价只与被影响的后缀
长度有关（常见情况下改一卷只重算该卷）。

## 快速开始

```bash
docker compose up --build
```

打开 <http://localhost:8000/docs> 录入与查询。SQLite 数据保存在命名卷
`inspection-data`（容器内 `/data/inspection.db`）中，重启容器不丢失。

本地不用 Docker 时：

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

## 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/inspections` | 登记总片数（`total_pieces`，可选 `note`） |
| `GET` | `/inspections` | 列出全部登记 |
| `GET` | `/inspections/{id}` | 查看登记及已接收的分次补报 |
| `PATCH` | `/inspections/{id}` | 更正总片数/备注；新总片数与已有计数冲突则拒收 |
| `POST` | `/inspections/{id}/reports` | 提交一批区间计数 `{"segments":[{"lo","hi","count"}]}` |
| `GET` | `/inspections/{id}/reports` | 查看补报历史 |
| `GET` | `/inspections/{id}/bounds?qualified_lo=&qualified_hi=` | 查询合格片数上下界 |
| `POST` | `/roll-series` | 建立监测序列：`target_thickness`、`tolerance`、`alarm_threshold` |
| `GET` | `/roll-series` | 列出全部监测序列 |
| `GET` | `/roll-series/{id}` | 查看逐卷读数与报警卷 |
| `PUT` | `/roll-series/{id}/readings/{roll}` | 录入/更正某卷：`{"measured","inspection_id"}`（可乱序） |
| `PATCH` | `/roll-series/{id}/readings/{roll}` | 只改读数或关联登记号 |
| `DELETE` | `/roll-series/{id}/readings/{roll}` | 删除某卷读数并回算后续 |
| `GET` | `/roll-series/{id}/readings` | 查看逐卷读数、累计值、报警标记 |
| `GET` | `/roll-series/{id}/alarms` | 查看报警卷号及对应质检登记号 |
| `GET` | `/health` | 健康检查 |

### 纸样计数示例

```bash
curl -X POST localhost:8000/inspections \
  -H 'content-type: application/json' -d '{"total_pieces":100}'

curl -X POST localhost:8000/inspections/1/reports \
  -H 'content-type: application/json' -d '{"segments":[{"lo":40,"hi":60,"count":70}]}'

curl -X POST localhost:8000/inspections/1/reports \
  -H 'content-type: application/json' -d '{"segments":[{"lo":50,"hi":70,"count":60}]}'

curl 'localhost:8000/inspections/1/bounds?qualified_lo=50&qualified_hi=60'
# {"inspection_id":1,"total_pieces":100,"qualified_lo":50,"qualified_hi":60,
#  "min_count":30,"max_count":60}

# 冲突补报：整份 409 拒收，上面的结果不受影响
curl -X POST localhost:8000/inspections/1/reports \
  -H 'content-type: application/json' -d '{"segments":[{"lo":40,"hi":60,"count":71}]}'
# {"detail":"补报与已有计数（或本报告内部）不相容，整份拒收；已有结果保持不变"}
```

### 卷序监测示例

目标 100 微米、容差 4、报警线 9：每卷超出 `100+4` 的部分才累计。

```bash
# 建立监测序列，返回其 id（下称 1）
curl -X POST localhost:8000/roll-series \
  -H 'content-type: application/json' \
  -d '{"target_thickness":100,"tolerance":4,"alarm_threshold":9,"note":"1 号机"}

# 按生产卷序录入；先录 1、2、4，再补录 3（乱序也可以）
curl -X PUT localhost:8000/roll-series/1/readings/1 \
  -H 'content-type: application/json' -d '{"measured":108,"inspection_id":1}'
curl -X PUT localhost:8000/roll-series/1/readings/2 \
  -H 'content-type: application/json' -d '{"measured":108,"inspection_id":1}'
curl -X PUT localhost:8000/roll-series/1/readings/4 \
  -H 'content-type: application/json' -d '{"measured":108,"inspection_id":1}'
curl -X PUT localhost:8000/roll-series/1/readings/3 \
  -H 'content-type: application/json' -d '{"measured":108,"inspection_id":1}'
# {"series_id":1,
#  "reading":{"roll_index":3,"measured":108,"inspection_id":1,
#             "cumulative_after":0,"alarmed":true},
#  "alarms":[{"roll_index":3,"measured":108,"inspection_id":1}]}

# 查看全部报警卷及对应质检登记号
curl localhost:8000/roll-series/1/alarms
# {"series_id":1,"alarms":[{"roll_index":3,"measured":108,"inspection_id":1}]}

# 更正（或迟到补录）某卷后自动回算，报警卷号与从头计算一致
curl -X PATCH localhost:8000/roll-series/1/readings/3 \
  -H 'content-type: application/json' -d '{"measured":80}'
```

## 计算方法

把所有区间端点（含查询端点与厚度零点）排序，将厚度轴切成原子区间。落在同一
原子区间内的整数厚度在所有计数约束下等价，因此每个原子只需一个非负整数变量
（片数）。设前缀和为 `S_k`：

* 区间 `[lo, hi)` 计数为 `c` ⇔ `S_j - S_i = c`；
* 总片数 `N` ⇔ `S_last - S_0 = N`；
* 原子片数非负 ⇔ `S_k <= S_{k+1}`。

目标“合格区间片数”为 `S_hi - S_lo`。其最大值等于差分约束图上 `lo → hi` 的
最短路，最小值等于 `hi → lo` 最短路取负。约束矩阵为连续一矩阵（全幺模），
最短路给出的整数解即原整数规划的精确最优解。负环 ⇔ 计数互相矛盾。

复杂度只取决于端点数量与图规模，与总片数无关；实现使用 SPFA 求最短路，
负环触发时用 Bellman-Ford 复核，避免误判。

## 测试

```bash
pip install -r requirements-dev.txt
pytest
```

* `tests/test_solver.py`：重叠区间、相邻不重叠区间、零计数、冲突检测、
  大数量不展开，以及 40 组与逐片枚举对拍。
* `tests/test_api.py`：登记、补报、跨批次重叠、跨批次冲突整份拒收、
  单份报告内部冲突拒收、拒收后原结果可查、`PATCH` 冲突拒收、参数校验、`/docs`。
* `tests/test_concurrency.py`：并发冲突补报只有一个成功；并发相容补报全部接收。
* `tests/test_rolls.py`：单卷累计/归零/报警清零、报警线恰好命中、乱序输入
  排序、配置校验；300 组随机更正与 200 组随机乱序补录全部与从头回算对拍；
  增量回放在累计值对齐处提前收敛（惰性输入只消费到收敛卷）。
* `tests/test_rolls_api.py`：建序列与参数校验、读数录入/更正/删除、乱序补录、
  迟到读数使报警前移/后移或消失、报警卷关联质检登记号、失败写入不改变原结果。
* `tests/test_rolls_incremental.py`：用数据库访问计数证明不从头扫描——更正第
  40 卷只按键查 1 次前驱状态；第 3 卷收敛后 4..100 卷的读数一行都不会读；
  重启后累计值与报警仍在。
* `tests/test_rolls_concurrency.py`：并发更正与并发乱序补录后，逐卷累计值和
  报警标记与从头回算一致。
