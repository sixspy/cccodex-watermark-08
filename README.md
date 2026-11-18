# cc-watermark-lab

CC_Codex 二期（26.11）**数据工程**产品线：**血缘水印台**（BlackSheep + Alpine CDN + SQLite）。

## 一句话骨架（自有，非外仓抄题）

**产出表水印模板 → 批次盖印 → 下游追索 → 缺口对账**

与现有七线正交（不是采集目录、ETL 管道、同步源、解析入库、编排流、种子摄取、留存裁撤）。
不碰外仓常见指纹（Feed 归并 / CDC 游标 / 质检签发 / Slicegate 类仓名）。

## 领域

- **所属领域**：数据工程
- **子方向**：数据血缘与水印（产出表盖印 / 追索闭环）

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | Python 3.12、BlackSheep、Uvicorn、SQLite |
| 前端 | 原生 JS SPA（全中文，底栏），雾灰底 + 青柠强调；不依赖外网 CDN |
| 测试 | pytest + httpx（本地秒级闭环，优先于 compose） |

## 基线已通主链

1. 产出表水印模板（表名 / 列名 / 现行版本）
2. 向运行批次盖印水印值
3. 按水印值追索下游批次
4. 缺口对账（已盖印 vs 应盖印）

预留切口（未做，供后续功能题）：模板换版生效窗、盖印作废回灌、跨表依赖冻结、追索深度限制等。

## 跨日与读日（已做）

读日只有一份来源：`meta.current_biz_date`（缺省回退 `max(run_batches.biz_date)`，即
`run_batches.max(biz_date)`），判定口径集中在 `app/domain.py`。

- **判定时机**：`submit` —— 点盖印时在 `BEGIN IMMEDIATE` 事务内读库内业务日再判，
  打开页面时的快照不参与判定。页面打开后若跨了日，提交会按新读日重新拒。
- **四处同源**：跨日列表 `/api/batches/cross-day`、盖印详情 `/api/batches/{id}`、
  批次水线 `/api/waterline`、追索台 `/api/trace` 都返回同一份
  `read_day` / `read_day_source` / `read_day_mode`。
- **当日池**：`/api/batches?scope=stampable` 只含 `stamp_allowed` 的批次，跨日一律排除；
  `scope=cross_day` 单列。改文案不算修好，闸门在 `evaluate_stamp_gate`。
- **留证**：`stamp_audit` 记录每次判定（`accepted` / `rejected` + `reason_code` + 读日）；
  部分唯一索引 `ux_stamp_audit_accepted` 保证每批只有一条接受留证。
- **追索与对账**：只认同日接受留证（`VALID_STAMP_SQL`），跨日新印不进追索结果，
  对账里单独计 `invalid_stamp_count`（伪盖印）与 `cross_day_count`。
- **并发**：写锁 `immediate_tx` + 条件 `UPDATE ... WHERE status='待盖印'`，8 线程抢同一批
  只有一次成功，其余返回「批次已盖印」。

回归：`tests/test_cross_day_local.py`。

## 本地（推荐反馈环）

```powershell
cd claude-workspace\cc-watermark-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

http://127.0.0.1:7000/login · **admin / 123456**

## Docker

```powershell
docker compose up -d --build
```

端口 **7000**。冒烟：`python ..\scripts\_smoke_lab_access.py --dir .`
