.PHONY: install dev-backend dev-frontend stage0 stage0-ui

install:
	cd backend && uv sync
	cd frontend && npm install

dev-backend:
	cd backend && uv run uvicorn app.main:app --host 127.0.0.1 --port 8100 --reload

dev-frontend:
	cd frontend && npm run dev

# S0 硬验收：契约测试 + 前端类型检查/构建（不需要浏览器）
stage0:
	@echo "== S0 · 后端契约测试 =="
	cd backend && uv run pytest -q
	@echo "== S0 · 前端类型检查 + 构建 =="
	cd frontend && npm run build
	@echo "S0 PASS"

# 可选：真实浏览器冒烟测试（需要先起 dev-backend / dev-frontend）
stage0-ui:
	cd frontend && npm run test:ui
