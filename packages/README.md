# A020 打包说明

- 文件：`A020-电商问数智能体实战教程fixed-clean.zip`
- 大小：约 32 MB（压缩后）
- 用途：给另一个 worker / 另一台机器直接解压审查或运行 S0–S3。

## 已从原始 788 MB 包中移除

- `code/shopkeeper-agent/docker/embedding/bge-large-zh-v1.5/`：约 1.24 GB 的 bge 模型权重
- `code/shopkeeper-agent-frontend/node_modules/`：前端依赖，可用 `npm ci` 恢复
- `code/rebuild/backend/.venv/`：Python 虚拟环境，可用 `uv sync --frozen` 恢复
- `code.zip`：重复压缩包
- `dist/`、`__MACOSX/`、`__pycache__/`、`.pytest_cache/`、`.ruff_cache/`、`*.pyc`

## 保留

- `code/rebuild/`：S0–S3 源码、测试、contracts、fixtures、docs、`.git`
- `code/shopkeeper-agent/`：旧系统源码与 Docker 配置（不含模型权重和依赖目录）
- 教程 PDF、项目介绍、功能清单等文档

> 原始 788 MB zip 没有直接提交到普通 Git 仓库：GitHub 单文件限制 100 MB，且其中大部分是可重新生成的依赖或模型权重。
