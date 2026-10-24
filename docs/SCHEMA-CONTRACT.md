# Schema 契约（自动生成）

> 由 `python -m app.schema_contract --write-doc` 生成，请勿手改。
> 语义事实源：`contracts/schema.contract.yaml`；
> 物理结构与数据：`fixtures/dw.sql`（内存 SQLite 加载，不需要 MySQL）。

## 数据集

| 项 | 值 |
|---|---|
| 表数 | 5 |
| 列数 | 24 |
| 事实表行数 | 115 |
| 日期范围 | 20250101 – 20250331 |
| GMV | 279,159.5 |
| 销量 | 622 |
| AOV | 2,427.47 |

> 数据只覆盖 2025 Q1（90 天）；越界查询必须显式提示，不能静默返回空。

## 表契约

### `dim_region`（dim）

- **粒度**：一行一个 region_id（省级行政区）；region_name 是省的分组，不是唯一键；本表无 city 列。
- **主键**：`region_id`
- **说明**：地区维度表。粒度只到省/大区，没有城市粒度。

| 列 | 类型 | 可空 | 角色 | 值域 | 说明 | 别名 |
|---|---|---|---|---|---|---|
| `region_id` | `VARCHAR(20)` | 否 | primary_key | 枚举：R001 / R002 / R003 / R004 / R005 / R006 | 地区唯一标识。 | 地区ID / 区域ID |
| `province` | `VARCHAR(50)` | 是 | dimension | 枚举：广东省 / 浙江省 / 四川省 / 北京市 / 上海市 / 湖北省 | 订单所属的省级行政区；只到省，无城市粒度。用户问『城市』时按省回答，别名必须体现真实粒度。 | 省份 / 省 / 所在省份 |
| `region_name` | `VARCHAR(50)` | 是 | dimension | 枚举：华南 / 华东 / 西南 / 华北 / 华中 | 大区名称（省的上级分组，不是唯一键）。 | 地区 / 区域 / 大区 |
| `country` | `VARCHAR(50)` | 是 | dimension | 枚举：中国 | 国家名称。 | 国家 / 国家名称 |

- **外键**：无
- **度量**：无

### `dim_customer`（dim）

- **粒度**：一行一个 customer_id。
- **主键**：`customer_id`
- **说明**：客户维度表。

| 列 | 类型 | 可空 | 角色 | 值域 | 说明 | 别名 |
|---|---|---|---|---|---|---|
| `customer_id` | `VARCHAR(20)` | 否 | primary_key | 格式：`^C\d{3}$` | 客户唯一标识。 | 客户ID / 用户ID |
| `customer_name` | `VARCHAR(50)` | 是 | dimension | 开放 | 客户名称。 | 客户名称 / 用户名称 |
| `gender` | `VARCHAR(10)` | 是 | dimension | 枚举：男 / 女 | 客户性别。 | 性别 |
| `member_level` | `VARCHAR(20)` | 是 | dimension | 枚举：黄金 / 白银 / 青铜 / 铂金 | 客户会员等级。 | 会员等级 / 用户等级 |

- **外键**：无
- **度量**：无

### `dim_product`（dim）

- **粒度**：一行一个 product_id；本表无 price 列。
- **主键**：`product_id`
- **说明**：商品维度表。

| 列 | 类型 | 可空 | 角色 | 值域 | 说明 | 别名 |
|---|---|---|---|---|---|---|
| `product_id` | `VARCHAR(20)` | 否 | primary_key | 格式：`^P\d{3}$` | 商品唯一标识。 | 商品ID / 产品ID |
| `product_name` | `VARCHAR(200)` | 是 | dimension | 开放 | 商品名称。 | 商品名称 / 产品名称 |
| `category` | `VARCHAR(50)` | 是 | dimension | 枚举：手机数码 / 家用电器 / 鞋靴 / 服饰 / 食品饮料 / 休闲零食 | 商品所属品类。 | 商品类别 / 品类 / 分类 |
| `brand` | `VARCHAR(50)` | 是 | dimension | 枚举：苹果 / 三星 / 华为 / 戴森 / 美的 / 耐克 / 阿迪达斯 / 优衣库 / 李维斯 / 雀巢 / 蒙牛 / 乐事 / 奥利奥 / 亚马逊 / Instant Pot | 商品品牌名称。 | 品牌 / 品牌名称 |

- **外键**：无
- **度量**：无

### `dim_date`（dim）

- **粒度**：一行一个 date_id（一天）。
- **主键**：`date_id`
- **说明**：时间维度表；数据只覆盖 2025 Q1。

| 列 | 类型 | 可空 | 角色 | 值域 | 说明 | 别名 |
|---|---|---|---|---|---|---|
| `date_id` | `INT` | 否 | primary_key | 范围：[20250101, 20250331] | 日期唯一标识，格式 yyyyMMdd。 | 日期ID / 日期 |
| `year` | `INT` | 是 | dimension | 枚举：2025 | 年份。 | 年 / 年份 |
| `quarter` | `VARCHAR(2)` | 是 | dimension | 枚举：Q1 | 自然季度，取值形如 Q1（带 Q 前缀字符串）。 | 季度 |
| `month` | `INT` | 是 | dimension | 范围：[1, 3] | 月份，1–12 的数字，不是月份名；本数据集只有 1–3。 | 月 / 月份 |
| `day` | `INT` | 是 | dimension | 范围：[1, 31] | 日，1–31 的数字。 | 日 / 天 |

- **外键**：无
- **度量**：无

### `fact_order`（fact）

- **粒度**：一行一个 order_id（一单）；order_quantity 是单内件数，order_amount 是整单金额。
- **主键**：`order_id`
- **说明**：订单事实表；一行一单。

| 列 | 类型 | 可空 | 角色 | 值域 | 说明 | 别名 |
|---|---|---|---|---|---|---|
| `order_id` | `VARCHAR(30)` | 否 | primary_key | 格式：`^ORD\d{11}$` | 订单唯一标识；一行一单，订单量用 COUNT(DISTINCT order_id)。 | 订单ID / 订单编号 |
| `customer_id` | `VARCHAR(20)` | 是 | foreign_key | 外键 | 关联客户维度的外键。 | 客户外键 / 关联客户ID |
| `product_id` | `VARCHAR(20)` | 是 | foreign_key | 外键 | 关联商品维度的外键。 | 商品外键 / 关联商品ID |
| `date_id` | `INT` | 是 | foreign_key | 外键（yyyyMMdd，[20250101, 20250331]） | = dim_date.date_id，格式 yyyyMMdd 整数；标准过滤写法 date_id BETWEEN yyyyMMdd AND yyyyMMdd。 | 日期外键 / 关联日期ID |
| `region_id` | `VARCHAR(20)` | 是 | foreign_key | 外键 | 关联地区维度的外键。 | 地区外键 / 关联地区ID |
| `order_quantity` | `INT` | 是 | measure | 范围：[1, 30] | 订单中商品的购买数量（件数），不是订单数。 | 购买数量 / 件数 |
| `order_amount` | `FLOAT` | 是 | measure | 范围：[30, 9499] | 整单成交金额（已含件数），不是单价；本库无退款/运费列。 | 订单金额 / 收入 / 成交金额 |

- **外键**：
  - `fact_order.customer_id` → `dim_customer.customer_id`
  - `fact_order.product_id` → `dim_product.product_id`
  - `fact_order.date_id` → `dim_date.date_id`
  - `fact_order.region_id` → `dim_region.region_id`
- **度量**：
  - `order_id`：count_distinct，单位 单；一行一单；COUNT(DISTINCT order_id) 得订单量。
  - `order_quantity`：additive，单位 件；件数，不是订单数；SUM 得销量。
  - `order_amount`：additive，单位 元；整单金额（已含件数），不是单价；本库无退款/运费列；SUM 得 GMV。

## 指标契约

| 指标 | 口径（公式） | 粒度 | 期望值 | 依赖列 | 别名 |
|---|---|---|---|---|---|
| GMV | `SUM(fact_order.order_amount)` | 订单 | 279159.5 | fact_order.order_amount | 销售额 / 成交总额 / 订单总额 |
| AOV | `SUM(fact_order.order_amount) / COUNT(DISTINCT fact_order.order_id)` | 订单 | 2427.47 | fact_order.order_amount / fact_order.order_id | 平均订单金额 / 客单价 |
| 订单量 | `COUNT(DISTINCT fact_order.order_id)` | 订单 | 115 | fact_order.order_id | 订单量 / 订单数 |
| 销量 | `SUM(fact_order.order_quantity)` | 订单 | 622 | fact_order.order_quantity | 销量 / 销售件数 |
| 平均件单价 | `SUM(fact_order.order_amount) / SUM(fact_order.order_quantity)` | 件 | 448.81 | fact_order.order_amount / fact_order.order_quantity | 平均单价 / 件单价 |
| 平均每单件数 | `SUM(fact_order.order_quantity) / COUNT(DISTINCT fact_order.order_id)` | 订单 | 5.41 | fact_order.order_quantity / fact_order.order_id | 平均每单件数 / 每单件数 |

### AOV 的唯一口径

AOV = `SUM(fact_order.order_amount) / COUNT(DISTINCT fact_order.order_id)` = **2,427.47**。

以下值**不是** AOV：

| 值 | 真实含义 |
|---|---|
| 448.81 | 平均件单价 = SUM(order_amount) / SUM(order_quantity) |
| 5.41 | 平均每单件数 = SUM(order_quantity) / COUNT(DISTINCT order_id) |

## 负向夹具（必须红）

这些是旧系统 `fixtures/legacy/*` 的真实缺陷。它们**不是**事实源；检查器必须持续把它们检出来，否则说明防线退化。

| 代码 | 位置 | 说明 |
|---|---|---|
| `AOV_COLUMN_MISMATCH` | `legacy.metric.AOV` | 描述是金额平均，relevant_columns 却是 ['fact_order.order_quantity'] |
| `AOV_ALIAS_MISMATCH` | `legacy.metric.AOV` | 别名『平均单价』指向 AOV，但 AOV 不是件单价 |
| `AOV_THREE_WAY` | `legacy.metric.AOV` | 描述(金额平均) / 依赖列(件数) / 别名(平均单价) 三方互相矛盾；同一词可算出 2427.47、448.81、5.41 三种结果 |
| `CITY_GRAIN_UNDECLARED` | `legacy.dim_region` | 物理结构没有 city 列，元数据却未声明『只到省，无城市粒度』；LLM 只能把 province 起别名叫『城市』 |
| `ORDER_COUNT_UNDEFINED` | `legacy.metrics` | 旧元数据没有订单量指标；order_quantity 的别名『销量』容易被写成 SUM(order_quantity)，比真实订单量偏大 5.41 倍 |
| `ALIAS_COLLISION` | `legacy.alias` | 别名「产品ID」同时指向 dim_product.product_id、fact_order.product_id |
| `ALIAS_COLLISION` | `legacy.alias` | 别名「区域ID」同时指向 dim_region.region_id、fact_order.region_id |
| `ALIAS_COLLISION` | `legacy.alias` | 别名「商品ID」同时指向 dim_product.product_id、fact_order.product_id |
| `ALIAS_COLLISION` | `legacy.alias` | 别名「地区ID」同时指向 dim_region.region_id、fact_order.region_id |
| `ALIAS_COLLISION` | `legacy.alias` | 别名「客户ID」同时指向 dim_customer.customer_id、fact_order.customer_id |
| `ALIAS_COLLISION` | `legacy.alias` | 别名「日期」同时指向 dim_date.date_id、fact_order.date_id |
| `ALIAS_COLLISION` | `legacy.alias` | 别名「用户ID」同时指向 dim_customer.customer_id、fact_order.customer_id |
| `FEATURE_LIST_TABLE_DRIFT` | `fixtures/legacy/feature-list.md` | 功能清单声称 6 张表，DDL 实际 5 张 |
| `FEATURE_LIST_ROW_DRIFT` | `fixtures/legacy/feature-list.md` | 功能清单声称 138 行订单，fact_order 实际 115 行 |

## 复现

```bash
cd backend
uv run python -m app.schema_contract --check
uv run python -m app.schema_contract --check-legacy   # 预期退出码 1
uv run python -m app.schema_contract --write-doc
```
