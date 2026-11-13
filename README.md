# 03-syncledger（双核对账）

Syncledger — 跨库/跨系统对齐对账：双端抽取 → 对齐键对账 → 差异处置 → 批次互证。

与 Feedstitch（多源 Feed 归并）、Batchgate（作业依赖编排）主链路不同。夹具全本地，不连活库。

## 启动

```bash
docker compose up --build
```

| 入口 | 地址 |
| --- | --- |
| 前端 | http://localhost:5300 |
| API | http://localhost:10300 |

## 主链

配置对账对与对齐键 → 触发双端抽取并对齐 → 落差异行与批次计数 → 页面互证

## 种子对账对

- **inventory_hq_vs_store**：总部库存 ↔ 门店库存（`sku_id`+`store_id`，比对 `qty`）
- **payment_vs_ledger**：支付网关 ↔ 账务流水（`txn_id`，比对 `amount`/`currency`）
- **sku_catalog_a_vs_b**：目录 A ↔ 目录 B（`sku_code`，比对 `name`/`category`）

底座默认只**记差异、不自动盖写**；差异处置走独立闭环（见下）。侧栏不叫「工单」，避免撞禁名单「工单客服」。

## 差异处置闭环（diff_disposition_flow）

从差异行开处置单，状态机四态，非法跳转服务端拒绝：

```
待认领 pending_claim --认领--> 处理中 in_progress --提交复核--> 待复核 pending_review
                                   ↑                              │
                                   └──────── 复核驳回(必填原因) ───┘
待复核 --复核通过--> 已关闭 closed（关闭是复核通过的唯一出口）
```

规则与互证：

- **认领后才能回灌**：未认领（待认领/待复核/已关闭）发起回灌一律 400。
- **回灌真写侧**：约定字段（对账对的 `compare_fields`）写入指定侧的**侧快照**（`side_snapshots` 表），夹具 JSON 原件不改写；下一趟对账把快照叠加在夹具基线上，账才对得上。每次回灌留旧值/新值/结果/原因（`reinject_records`）。
- **失败不晋级**：回灌写入失败（可用 `simulate_failure` 故障注入）时处置单留在 `处理中`，失败记录照样留痕，且禁止进入待复核；侧数据不会被失败写入污染。
- **复核对侧值**：复核通过前服务端重读侧快照，与每条成功回灌记录逐条比对；侧值不符（含事后漂移）拒绝通过。驳回必填原因并回到处理中。
- **禁止未复核关单**：没有独立关单接口，`closed` 只能由待复核上的复核通过到达；无成功回灌记录不得提交复核。
- **历史保留与列表区分**：关单不删差异，差异列表以 `未处置 / 处置中 / 已关闭` 标记区分，并链到处置单。
- **互证**：`GET /api/dispositions/evidence` 汇总各状态数量、回灌台账、侧快照与差异列表的勾稽检查；总览页分未关/已关闭计数。

主要接口：`POST /api/dispositions`、`/claim`、`/reinject`、`/submit-review`、`/review`，`GET /api/dispositions/{id}`、`/api/dispositions/evidence`、`/api/side-snapshots`。

测试：`cd backend && pytest`（每用例独立临时库，覆盖通关、回灌失败、复核驳回、侧值漂移拦截、非法跳转、互证一致）。

## 技术栈

Python 3.12 + FastAPI + SQLite；Vue 3 + Vite + Nginx。

## 0-1 切口（其余未实现，见出题规划）

`master_slave_pick` / `side_only_policy` / `window_buffer` / `batch_idempotency` / …
