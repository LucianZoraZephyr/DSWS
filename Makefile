.PHONY: install dev-backend dev-frontend stage0 stage0-ui stage1 stage2 stage3 stage4 stage5 stage5-live stage6 stage6-ui stage7 stage7-ui baseline docker-env docker-env-require docker-up docker-down docker-logs docker-smoke docker-reset

install:
	cd backend && uv sync --frozen
	cd frontend && npm ci

dev-backend:
	cd backend && uv run uvicorn app.main:app --host 127.0.0.1 --port 8100 --reload

dev-frontend:
	cd frontend && npm run dev

# S0 硬验收：契约测试 + 前端协议单测 + 类型检查/构建（不需要浏览器）
stage0:
	@sh scripts/say.sh s0-backend-contract
	cd backend && uv run pytest -q tests/test_s0_contract.py
	@sh scripts/say.sh s0-frontend-sse
	cd frontend && npm run test
	@sh scripts/say.sh s0-frontend-build
	cd frontend && npm run build
	@echo "S0 PASS"

# 可选：真实 Chrome 冒烟（自起临时端口 8101/5274，跑完清理）
stage0-ui:
	./scripts/stage0-ui.sh

# S1 硬验收：契约 ↔ DDL/数据全绿；旧系统负向夹具必须红；文档必须新鲜
stage1:
	@sh scripts/say.sh s1-contract-ddl
	cd backend && uv run python -m app.schema_contract --check
	@sh scripts/say.sh s1-negative-fixture
	cd backend && uv run python -m app.schema_contract --expect-legacy-red
	@sh scripts/say.sh s1-backend-tests
	cd backend && uv run pytest -q tests/test_s0_contract.py tests/test_s1_schema_contract.py
	@echo "S1 PASS"

# S2 硬验收：dry-run 计划 → --fresh 重建 → --verify 对账 → 第二次构建零写入
# 前置：MySQL/ES/Qdrant 已启动（旧系统 docker compose）；只操作 *_rebuild 目标。
stage2:
	@sh scripts/say.sh s2-dry-run
	cd backend && uv run python -m app.build_meta_knowledge --dry-run
	@sh scripts/say.sh s2-rebuild
	cd backend && uv run python -m app.build_meta_knowledge --fresh
	@sh scripts/say.sh s2-verify
	cd backend && uv run python -m app.build_meta_knowledge --verify
	@sh scripts/say.sh s2-idempotent
	cd backend && uv run python -m app.build_meta_knowledge --assert-idempotent
	@sh scripts/say.sh s2-backend-tests
	cd backend && uv run pytest -q
	@echo "S2 PASS"

# S3 硬验收：S2 三存储对账 → 20 题真实检索 → CLI YAML → 全量回归
# 前置：MySQL/ES/Qdrant 已启动，且 S2 的 *_rebuild 目标已构建；不执行 stage2 / --fresh。
stage3:
	@sh scripts/say.sh s3-verify
	cd backend && uv run python -m app.build_meta_knowledge --verify
	@sh scripts/say.sh s3-golden-set
	cd backend && REBUILD_INTEGRATION=1 uv run pytest -q tests/recall
	@sh scripts/say.sh s3-cli-yaml
	@sh scripts/recall-demo.sh /tmp/rebuild-s3-recall.yaml
	@head -n 40 /tmp/rebuild-s3-recall.yaml
	@sh scripts/say.sh s3-backend-tests
	cd backend && uv run pytest -q
	@echo "S3 PASS"

# S4 硬验收：10 题基线（确定性生成 + 静态校验）→ 生成层测试 → 全量回归
# 前置：**不需要** MySQL/ES/Qdrant。S4 只到「生成 + 静态校验」，不执行 SQL；
# 上下文由契约离线裁剪，基线因此可以在任何一台机器上复现。
stage4:
	@sh scripts/say.sh s4-baseline
	cd backend && uv run python -m app.scripts.generate --baseline
	@sh scripts/say.sh s4-generate-tests
	cd backend && uv run pytest -q tests/generate
	@sh scripts/say.sh s4-backend-tests
	cd backend && uv run pytest -q
	@echo "S4 PASS"

# 单独看基线表格（路线图里的 Demo：问题 / SQL / 判定）
baseline:
	cd backend && uv run python -m app.scripts.generate --baseline

# S5 硬验收：AST 级安全策略 + 注入语料 100% 拒绝 + 零误杀
# 前置：**不需要** MySQL。策略只依赖契约与 sqlglot，因此门禁可以在任何
# 一台机器上复现。只读账号与两屏对比需要 MySQL，见 stage5-live。
stage5:
	@sh scripts/say.sh s5-policy-gate
	cd backend && uv run python -m app.scripts.security --check
	@sh scripts/say.sh s5-corpus-table
	cd backend && uv run python -m app.scripts.security --corpus
	@sh scripts/say.sh s5-security-tests
	cd backend && uv run pytest -q tests/security
	@sh scripts/say.sh s5-backend-tests
	cd backend && uv run pytest -q
	@echo "S5 PASS"

# S5 实战验收：需要 MySQL —— 开通只读账号、验证它真的写不了、两屏对比
# 前置：MySQL 已启动，dw 库已由 fixtures/dw.sql 装载；
#      环境变量 REBUILD_DW_RO_PASSWORD 给出只读账号的密码（不在仓库里）。
stage5-live:
	@sh scripts/say.sh s5-provision-ro
	cd backend && uv run python -m app.scripts.security --provision-ro-account
	@sh scripts/say.sh s5-verify-ro
	cd backend && uv run python -m app.scripts.security --verify-ro-account
	@sh scripts/say.sh s5-two-screen
	cd backend && uv run python -m app.scripts.security --demo
	@echo "S5 LIVE PASS"

# S6 硬验收：错误码表 + 6 类失败场景 + 校正闭环 + 取消
# 前置：**不需要** MySQL / LLM。失败场景由 fixtures/failures/scenarios.yaml
# 驱动，校验、安全策略、校正闭环、SSE 协议全都是真代码——只替换了模型与
# 数据库这两个外部依赖，所以 6 类失败既真实又可复现。
stage6:
	@sh scripts/say.sh s6-failure-gate
	cd backend && uv run python -m app.scripts.failures --check
	@sh scripts/say.sh s6-error-table
	cd backend && uv run python -m app.scripts.failures --table
	@sh scripts/say.sh s6-scenario-table
	cd backend && uv run python -m app.scripts.failures --scenarios
	@sh scripts/say.sh s6-failure-tests
	cd backend && uv run pytest -q tests/agent
	@sh scripts/say.sh s6-frontend-sse
	cd frontend && npm run test
	@sh scripts/say.sh s6-frontend-build
	cd frontend && npm run build
	@sh scripts/say.sh s6-backend-tests
	cd backend && uv run pytest -q
	@echo "S6 PASS"

# S6 浏览器验收：6 类失败各一个用例 + 取消
# 前置：需要 Chrome / Edge（Windows 上用 Edge 也行）。
# 场景应用与生产应用共用同一套 HTTP / SSE 管道，只替换模型与数据库。
stage6-ui:
	./scripts/stage6-ui.sh

# S7 硬验收：契约生成物新鲜度 + SSE 夹具对账 + 前端契约测试 + 类型检查/构建
# 前置：**不需要** MySQL / LLM / 浏览器。浏览器用例拆到 stage7-ui（沿用
# stageN / stageN-ui 的既有惯例，S0 / S6 都是这么做的）。
stage7:
	@sh scripts/say.sh s7-contract-freshness
	cd backend && uv run python -m app.scripts.contracts_gen --check
	@sh scripts/say.sh s7-sse-fixture
	cd backend && uv run python -m app.scripts.record_sse --check
	@sh scripts/say.sh s7-frontend-contract
	cd backend && uv run pytest -q tests/frontend
	@sh scripts/say.sh s7-frontend-sse
	cd frontend && npm run test
	@sh scripts/say.sh s7-frontend-build
	cd frontend && npm run build
	@sh scripts/say.sh s7-backend-tests
	cd backend && uv run pytest -q
	@echo "S7 PASS"

# S7 浏览器验收：13 条 F 缺陷各一个用例（回放服务器 + 隔离前端）
# 前置：需要 Chrome / Edge（Windows 上用 Edge 也行）。
stage7-ui:
	./scripts/stage7-ui.sh

# ---------------------------------------------------------------------------
# Docker 部署（单机 Compose；详见 docs/DOCKER-DEPLOY.md）
# ---------------------------------------------------------------------------

# 生成 .env：解释器名字在平台之间不一样（Linux/macOS 常见 python3，
# 某些 Windows 环境只有 python）。判定标准是"真的能跑"而不是"名字在 PATH 上"——
# Windows 上 Microsoft Store 的 python3 转发器就属于"存在但一执行就退 49"，
# 挑中它只会以 Error 49 收场。详见 scripts/find-python.sh。
docker-env:
	@test -f .env || { \
		PY="$$(sh scripts/find-python.sh)" || exit 1; \
		"$$PY" scripts/generate_docker_env.py; \
	}

# 只读栈 / 收尾的动作不许顺手生成一份新的 .env。
#
# 生成器写进的是**新的随机密码**：数据卷里 MySQL 的账号还是老密码，
# 容器按新的 .env 连库就是 Access denied，栈起不来——而且没人看得出
# .env 是什么时候被换掉的。缺 .env 就该直接报错。
docker-env-require:
	@test -f .env || { \
		echo "missing .env: run 'make docker-env' first" >&2; \
		exit 1; \
	}

docker-up: docker-env
	docker compose up -d --build

# compose 的变量全部来自 .env，缺了它连 down / logs 都跑不起来。
docker-down: docker-env-require
	docker compose down

docker-logs: docker-env-require
	docker compose logs -f --tail=100

docker-smoke: docker-env-require
	./scripts/docker-smoke.sh

docker-reset: docker-env-require
	@sh scripts/docker-reset.sh
