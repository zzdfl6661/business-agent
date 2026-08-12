# 03 · 数据库设计（MySQL）

> 业务库共 4 张核心表：stores / products / orders / campaigns
> ORM：SQLAlchemy 2.x 声明式（`database/models.py`）｜ 建表：`Base.metadata.create_all` 自动执行

## 1. 设计约定

- 所有表含 `id BIGINT AUTO_INCREMENT PRIMARY KEY` 与 `created_at / updated_at DATETIME`
- 金额一律 `DECIMAL`（精确小数，禁止 FLOAT）
- 时间字段：日期用 `DATE`，精确时间用 `DATETIME`
- 软状态用 `status VARCHAR`（预留枚举扩展，避免 ALTER TABLE）
- 字符集：`utf8mb4`（支持中文 + Emoji）

## 2. 表结构

### 2.1 stores — 门店信息

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | BIGINT | PK, AUTO_INCREMENT | |
| store_code | VARCHAR(32) | UNIQUE, NOT NULL | 门店编码，如 ST0001 |
| store_name | VARCHAR(128) | NOT NULL | 美团后台全名（真实主数据） |
| region | VARCHAR(32) | INDEX | 大区（可空） |
| city | VARCHAR(32) | INDEX | 城市（由门店名关键字推断） |
| address | VARCHAR(255) | | 地址 |
| manager_name | VARCHAR(32) | | 店长 |
| open_date | DATE | | 开业日期 |
| search_keyword | VARCHAR(64) | | 美团后台下拉搜索关键字 |
| budget_keyword | VARCHAR(64) | | 定位共享预算行关键字（阶段二 Playwright 自动化用） |
| status | VARCHAR(16) | DEFAULT 'active' | active/closed |
| created_at / updated_at | DATETIME | | 审计字段 |

> **主数据来源**：`stores.json`（美团后台门店列表，字段 name/search_keyword/budget_keyword/enabled）。
> 当前 37 家真实门店（鬼十八 / Xcape异时刻 / bb boom 密室逃脱品牌线），由 seed 脚本导入；
> `BIZ_STORES_JSON` 指向文件路径，项目内保留 `backend/data/stores.json` 快照，可重复灌入。

### 2.2 products — 商品信息

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | BIGINT | PK | |
| store_id | BIGINT | FK→stores.id ON DELETE CASCADE, INDEX | 商品按门店维度 |
| product_code | VARCHAR(32) | UNIQUE | 商品编码 |
| product_name | VARCHAR(128) | NOT NULL | |
| category | VARCHAR(32) | INDEX | 品类：饮料/小吃/主食… |
| price | DECIMAL(10,2) | NOT NULL | 售价 |
| cost | DECIMAL(10,2) | | 成本 |
| status | VARCHAR(16) | DEFAULT 'active' | active/off_shelf |

### 2.3 orders — 订单信息（核心大表）

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | BIGINT | PK | |
| order_no | VARCHAR(64) | UNIQUE | 订单号 |
| store_id | BIGINT | FK, INDEX | 门店 |
| product_id | BIGINT | FK, INDEX | 商品 |
| quantity | INT | NOT NULL | 数量 |
| unit_price | DECIMAL(10,2) | | 成交单价 |
| total_amount | DECIMAL(12,2) | NOT NULL | 订单金额 |
| discount_amount | DECIMAL(12,2) | DEFAULT 0 | 优惠金额 |
| payment_method | VARCHAR(16) | | wechat/alipay/cash/card |
| order_time | DATETIME | INDEX | 下单时间 |
| order_status | VARCHAR(16) | DEFAULT 'completed' | completed/refunded/cancelled |

**复合索引**：`(store_id, order_time)` ← 查询主路径（按门店×时间窗口取数），必须建立。

> 说明：本表为明细粒度（一行一个商品），数据分析时用 SQL 聚合 → DataFrame，兼顾灵活与性能。
> 数据量预估：90 天 × 5 店 × 300 单 ≈ 13.5 万行（种子数据），MySQL 完全无压力。

### 2.4 campaigns — 推广计划

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | BIGINT | PK | |
| store_id | BIGINT | FK, INDEX | 所属门店 |
| campaign_name | VARCHAR(128) | NOT NULL | 计划名称 |
| campaign_type | VARCHAR(32) | | 满减/折扣券/新客立减 |
| budget | DECIMAL(12,2) | NOT NULL | 总预算 |
| spent_amount | DECIMAL(12,2) | DEFAULT 0 | 已消耗 |
| clicks | INT | DEFAULT 0 | 点击量（ROI 计算） |
| conversions | INT | DEFAULT 0 | 转化数（ROI 计算） |
| start_date / end_date | DATE | | 投放周期 |
| status | VARCHAR(16) | INDEX | planned/running/ended/paused |
| channel | VARCHAR(32) | | 门店/小程序/美团/抖音 |

> 推广 ROI 计算口径：`ROI = 转化数 × 门店客单价 / 消耗`（客单价取该店近 30 天订单均值）。

## 3. 种子数据（scripts/seed.py）

- **门店（真实主数据）**：读 `stores.json`（美团后台，当前 37 家密室逃脱门店），映射 `store_name/search_keyword/budget_keyword` 等字段；`BIZ_STORES_JSON` 可指向最新文件，项目内置 `backend/data/stores.json` 快照
- **商品**：每家 40~65 个（单人票/双人票/主题场次/团建套餐/会员储值，密室逃脱业态）
- **订单**：最近 90 天、每店每天 20~120 单（约 23 万行，`bulk_insert_mappings` 分批）
- **推广计划**：每家 3~4 条（美团/点评/抖音/小程序，含消耗/点击/转化）
- **灌入方式**：分批写入（每批 5000 行），避免大事务超时
- **幂等**：执行前清空四表，可重复运行
- **归因埋点（演示可复现的关键）**：
  1. **1 号店最近 7 天**订单量较前 7 天 **下滑约 30%**（客流掉量）
  2. 其中 **「主题场次」品类**掉量贡献最大（模拟"主力产品排期/供给问题"）
  3. 1 号店存在一条 **running 状态、预算已消耗 > 80%** 的 campaign（模拟"预算花超导致投放提前衰竭"）
  - → 提问「分析最近 7 天 1 号门店营业额下降原因并给建议」，可复现出「客流下滑 + 品类掉量 + 预算花超」三类归因，并驱动「调高/调优预算」的自动化建议

## 4. 建表与连接（database/mysql.py）

```python
engine = create_engine(url, pool_size=5, pool_recycle=3600)
SessionLocal = sessionmaker(bind=engine)
# lifespan 中：Base.metadata.create_all(engine) 自动建表
```

- 连接串：`mysql+pymysql://user:pass@host:3306/business_agent?charset=utf8mb4`
- 数据库名默认 `business_agent`（不存在时脚本先 CREATE DATABASE）

## 5. 查询主路径示例（工具层用）

```sql
-- 某门店近 N 天营业额/订单数/客单价
SELECT DATE(order_time) d, COUNT(*) cnt, SUM(total_amount) amt,
       SUM(total_amount)/COUNT(*) avg_order
FROM orders WHERE store_id=:sid AND order_time>=:start AND order_time<:end
GROUP BY DATE(order_time);

-- 品类贡献（归因分析）
SELECT p.category, SUM(o.total_amount) amt
FROM orders o JOIN products p ON o.product_id=p.id
WHERE o.store_id=:sid AND o.order_time>=:start AND o.order_time<:end
GROUP BY p.category;
```
