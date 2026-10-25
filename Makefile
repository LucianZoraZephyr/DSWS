.PHONY: install dev-backend dev-frontend stage0 stage0-ui stage1 stage2 stage3

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
