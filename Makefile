.PHONY: install dev-backend dev-frontend stage0 stage0-ui stage1 stage2 stage3 stage4 stage5 stage5-live stage6 stage6-ui baseline

install:
	cd backend && uv sync --frozen
	cd frontend && npm ci

dev-backend:
	cd backend && uv run uvicorn app.main:app --host 127.0.0.1 --port 8100 --reload

dev-frontend:
	cd frontend && npm run dev

# S0 硬验收：契约测试 + 前端协议单测 + 类型检查/构建（不需要浏览器）
stage0:
	@echo "== S0 · 后端契约测试 =="
	cd backend && uv run pytest -q tests/test_s0_contract.py
	@echo "== S0 · 前端 SSE 协议单测 =="
	cd frontend && npm run test
	@echo "== S0 · 前端类型检查 + 构建 =="
	cd frontend && npm run build
	@echo "S0 PASS"

# 可选：真实 Chrome 冒烟（自起临时端口 8101/5274，跑完清理）
stage0-ui:
	./scripts/stage0-ui.sh

# S1 硬验收：契约 ↔ DDL/数据全绿；旧系统负向夹具必须红；文档必须新鲜
stage1:
	@echo "== S1 · 契约 ↔ DDL/数据 =="
	cd backend && uv run python -m app.schema_contract --check
	@echo "== S1 · 负向夹具（必须红） =="
	cd backend && uv run python -m app.schema_contract --expect-legacy-red
	@echo "== S1 · 后端测试（S0 + S1） =="
	cd backend && uv run pytest -q tests/test_s0_contract.py tests/test_s1_schema_contract.py
	@echo "S1 PASS"

# S2 硬验收：dry-run 计划 → --fresh 重建 → --verify 对账 → 第二次构建零写入
# 前置：MySQL/ES/Qdrant 已启动（旧系统 docker compose）；只操作 *_rebuild 目标。
stage2:
	@echo "== S2 · dry-run（不连接存储） =="
	cd backend && uv run python -m app.build_meta_knowledge --dry-run
	@echo "== S2 · --fresh 重建 rebuild 目标 =="
	cd backend && uv run python -m app.build_meta_knowledge --fresh
	@echo "== S2 · --verify 三存储对账 =="
	cd backend && uv run python -m app.build_meta_knowledge --verify
	@echo "== S2 · 第二次构建必须零写入 =="
	cd backend && uv run python -m app.build_meta_knowledge --assert-idempotent
	@echo "== S2 · 后端测试（S0 + S1 + S2） =="
	cd backend && uv run pytest -q
	@echo "S2 PASS"

# S3 硬验收：S2 三存储对账 → 20 题真实检索 → CLI YAML → 全量回归
# 前置：MySQL/ES/Qdrant 已启动，且 S2 的 *_rebuild 目标已构建；不执行 stage2 / --fresh。
stage3:
	@echo "== S3 · S2 三存储对账 =="
	cd backend && uv run python -m app.build_meta_knowledge --verify
	@echo "== S3 · 20 题黄金集（真实 ES/Qdrant/Meta） =="
	cd backend && REBUILD_INTEGRATION=1 uv run pytest -q tests/recall
	@echo "== S3 · CLI 输出 YAML（stdout）与检索统计（stderr） =="
	cd backend && uv run python -m app.scripts.recall "华北销售额" > /tmp/rebuild-s3-recall.yaml
	@head -n 40 /tmp/rebuild-s3-recall.yaml
	@echo "== S3 · 全部后端测试（S0 + S1 + S2 + S3） =="
	cd backend && uv run pytest -q
	@echo "S3 PASS"

# S4 硬验收：10 题基线（确定性生成 + 静态校验）→ 生成层测试 → 全量回归
# 前置：**不需要** MySQL/ES/Qdrant。S4 只到「生成 + 静态校验」，不执行 SQL；
# 上下文由契约离线裁剪，基线因此可以在任何一台机器上复现。
stage4:
	@echo "== S4 · 10 题基线（问题 / SQL / 判定） =="
	cd backend && uv run python -m app.scripts.generate --baseline
	@echo "== S4 · 生成层测试（提示词契约 / 静态校验 / 基线 / 三方对齐） =="
	cd backend && uv run pytest -q tests/generate
	@echo "== S4 · 全部后端测试（S0 + S1 + S2 + S3 + S4） =="
	cd backend && uv run pytest -q
	@echo "S4 PASS"

# 单独看基线表格（路线图里的 Demo：问题 / SQL / 判定）
baseline:
	cd backend && uv run python -m app.scripts.generate --baseline

# S5 硬验收：AST 级安全策略 + 注入语料 100% 拒绝 + 零误杀
# 前置：**不需要** MySQL。策略只依赖契约与 sqlglot，因此门禁可以在任何
# 一台机器上复现。只读账号与两屏对比需要 MySQL，见 stage5-live。
stage5:
	@echo "== S5 · 安全策略门禁（注入语料 100% 拒绝 + 零误杀） =="
	cd backend && uv run python -m app.scripts.security --check
	@echo "== S5 · 注入语料判定表 =="
	cd backend && uv run python -m app.scripts.security --corpus
	@echo "== S5 · 安全层测试（策略 / 语料 / 执行器 / 账号 / 对齐） =="
	cd backend && uv run pytest -q tests/security
	@echo "== S5 · 全部后端测试（S0 + S1 + S2 + S3 + S4 + S5） =="
	cd backend && uv run pytest -q
	@echo "S5 PASS"

# S5 实战验收：需要 MySQL —— 开通只读账号、验证它真的写不了、两屏对比
# 前置：MySQL 已启动，dw 库已由 fixtures/dw.sql 装载；
#      环境变量 REBUILD_DW_RO_PASSWORD 给出只读账号的密码（不在仓库里）。
stage5-live:
	@echo "== S5 · 开通只读账号 =="
	cd backend && uv run python -m app.scripts.security --provision-ro-account
	@echo "== S5 · 验证只读账号（真的写不了） =="
	cd backend && uv run python -m app.scripts.security --verify-ro-account
	@echo "== S5 · 两屏对比：旧系统执行 / 新系统拒绝 =="
	cd backend && uv run python -m app.scripts.security --demo
	@echo "S5 LIVE PASS"

# S6 硬验收：错误码表 + 6 类失败场景 + 校正闭环 + 取消
# 前置：**不需要** MySQL / LLM。失败场景由 fixtures/failures/scenarios.yaml
# 驱动，校验、安全策略、校正闭环、SSE 协议全都是真代码——只替换了模型与
# 数据库这两个外部依赖，所以 6 类失败既真实又可复现。
stage6:
	@echo "== S6 · 失败通道门禁（6 类失败 + 错误码表） =="
	cd backend && uv run python -m app.scripts.failures --check
	@echo "== S6 · 错误码表 =="
	cd backend && uv run python -m app.scripts.failures --table
	@echo "== S6 · 失败场景表 =="
	cd backend && uv run python -m app.scripts.failures --scenarios
	@echo "== S6 · 失败通道测试（错误码表 / 校正闭环 / 编排 / API / 场景） =="
	cd backend && uv run pytest -q tests/agent
	@echo "== S6 · 前端协议单测 =="
	cd frontend && npm run test
	@echo "== S6 · 前端类型检查 + 构建 =="
	cd frontend && npm run build
	@echo "== S6 · 全部后端测试（S0 + S1 + S2 + S3 + S4 + S5 + S6） =="
	cd backend && uv run pytest -q
	@echo "S6 PASS"

# S6 浏览器验收：6 类失败各一个用例 + 取消
# 前置：需要 Chrome / Edge（Windows 上用 Edge 也行）。
# 场景应用与生产应用共用同一套 HTTP / SSE 管道，只替换模型与数据库。
stage6-ui:
	./scripts/stage6-ui.sh
