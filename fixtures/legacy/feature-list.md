# 电商问数智能体 · 功能清单（负向夹具）

> 冻结自旧项目 `电商问数智能体-功能清单.md:12`。
> 仅用于 S1 的漂移检测：声称 **6 张表 / 138 行**，实际 **5 张表 / 115 单**。
> 不要在这里修复；正确数字由 `contracts/schema.contract.yaml` + `fixtures/dw.sql` 决定。

| # | 功能 | 验收要点 |
|---|---|---|
| **6** | **结构化教学数仓** | 提供 6 张 MySQL 数仓表共 138 行订单数据；脚本与建表 SQL 见 `app/scripts/build_meta_knowledge.py`、`docker/mysql/dw.sql`。 |
