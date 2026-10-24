.PHONY: install dev-backend dev-frontend stage0 stage0-ui

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
	cd backend && uv run pytest -q
	@echo "== S0 · 前端 SSE 协议单测 =="
	cd frontend && npm run test
	@echo "== S0 · 前端类型检查 + 构建 =="
	cd frontend && npm run build
	@echo "S0 PASS"

# 可选：真实 Chrome 冒烟（自起临时端口 8101/5274，跑完清理）
stage0-ui:
	./scripts/stage0-ui.sh
