"""S4 · 生成器接口与两种实现。

    SqlGenerator（Protocol）
      ├── DeterministicGenerator —— 离线、确定性、可复现（默认）
      └── HttpLlmGenerator        —— 可选，OpenAI 兼容端点

**为什么要留 LLM 接口**：S4 交付的是「提示词 + 输出 JSON schema」，提示词是
给真实模型用的契约。但仓库当前的边界是「不调用 LLM」，而且 `make stage4`
必须在一台没有 API key、没有网络的机器上跑得通——否则它就不是一个验收
命令，只是一个演示脚本。

所以两条路都留着，但默认走确定性那条：

    - 确定性实现产出**同一个形状**（`sql_or_refusal`），并且要过同一套静态
      校验；基线表格因此稳定可复现。
    - 真实 LLM 走同一个接口、同一个输出 schema、同一套静态校验。模型返回
      的 SQL 一样要过守卫，不合格就降级成拒答。

两条路都跑 `app.generate.guard.check_sql`，所以**换成真模型不会放宽任何
一条硬规则**。
"""

from __future__ import annotations

import json
import os
from typing import Any, Protocol

import yaml

from app.generate.models import GenerateContext

# 生成器名字会写进结果里，方便日志与基线表格区分来源。
DETERMINISTIC = "deterministic"
HTTP_LLM = "http-llm"


def dump_context(payload: Any) -> str:
    """把上下文渲染成 YAML——给模型看的稳定可读结构。"""
    return yaml.safe_dump(
        payload,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=1000,
    )


def table_infos(context: GenerateContext) -> list[dict[str, Any]]:
    """表信息：名字、粒度、外键。"""
    return [
        {
            "name": table.name,
            "grain": table.grain,
            "description": table.description,
            "primary_key": table.primary_key,
            "foreign_keys": [
                {"column": item["column"], "references": item["references"]}
                for item in table.foreign_keys
            ],
        }
        for table in context.tables
    ]


def column_infos(context: GenerateContext) -> list[dict[str, Any]]:
    """字段信息。"""
    return [
        {
            "id": column.id,
            "table_id": column.table_id,
            "type": column.type,
            "role": column.role,
            "description": column.description,
            "aliases": list(column.aliases),
            "value_domain": dict(column.value_domain),
        }
        for column in context.columns
    ]


def metric_infos(context: GenerateContext) -> list[dict[str, Any]]:
    """指标信息：口径公式本身就是可执行的 SQL 表达式。"""
    return [
        {
            "name": metric.name,
            "label": metric.label,
            "description": metric.description,
            "formula": metric.formula,
            "relevant_columns": list(metric.relevant_columns),
            "aliases": list(metric.aliases),
            "aggregation": metric.aggregation,
            "expected_value": metric.expected_value,
            "grain": metric.grain,
            "rejected_values": [dict(item) for item in metric.rejected_values],
        }
        for metric in context.metrics
    ]


def value_infos(context: GenerateContext) -> list[dict[str, Any]]:
    """取值信息。"""
    return [
        {"value": value.value, "column_id": value.column_id}
        for value in context.values
    ]


def build_slots(context: GenerateContext) -> dict[str, str]:
    """
    把上下文渲染成 `generate_sql` 提示词的 7 个槽位。

    槽位顺序与 `contracts/prompts/generate_sql.prompt` 的 frontmatter 声明
    完全一致；少一个槽，渲染时就会抛 `PromptSlotError`。
    """
    return {
        "query": context.query,
        "table_infos": dump_context(table_infos(context)),
        "column_infos": dump_context(column_infos(context)),
        "metric_infos": dump_context(metric_infos(context)),
        "value_infos": dump_context(value_infos(context)),
        "date_info": dump_context(dict(context.date_info)),
        "db_info": dump_context(dict(context.db_info)),
    }


class SqlGenerator(Protocol):
    """生成器接口。返回的 dict 必须符合 `sql_or_refusal`。"""

    name: str

    def generate(
        self, context: GenerateContext, prompt: str
    ) -> dict[str, Any]:
        ...


class DeterministicGenerator:
    """离线确定性生成器。"""

    name = DETERMINISTIC

    def generate(
        self, context: GenerateContext, prompt: str
    ) -> dict[str, Any]:
        from app.generate.deterministic import generate_sql

        sql, refusal = generate_sql(context)
        if refusal is not None:
            return refusal.to_dict()
        return {"sql": sql or ""}


class HttpLlmGenerator:
    """OpenAI 兼容端点的生成器（可选）。

    只有同时设置 `REBUILD_LLM_BASE_URL` 与 `REBUILD_LLM_API_KEY` 才应该被
    使用。模型返回的 JSON 会先过输出 schema 校验，再过静态校验——模型说的
    算不算，代码说了算。
    """

    name = HTTP_LLM

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 30.0,
        transport: Any | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.transport = transport

    @classmethod
    def from_env(cls) -> "HttpLlmGenerator":
        base_url = os.environ.get("REBUILD_LLM_BASE_URL", "")
        api_key = os.environ.get("REBUILD_LLM_API_KEY", "")
        model = os.environ.get("REBUILD_LLM_MODEL", "")
        if not base_url or not api_key or not model:
            raise ValueError(
                "启用 LLM 生成器需要同时设置 REBUILD_LLM_BASE_URL、"
                "REBUILD_LLM_API_KEY、REBUILD_LLM_MODEL"
            )
        timeout = float(os.environ.get("REBUILD_LLM_TIMEOUT", "30"))
        return cls(
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout=timeout,
        )

    def generate(
        self, context: GenerateContext, prompt: str
    ) -> dict[str, Any]:
        import httpx

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你只输出一个 JSON 对象，不要任何解释文字，"
                        "不要 Markdown 代码块。"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        }
        client_kwargs: dict[str, Any] = {"timeout": self.timeout}
        if self.transport is not None:
            client_kwargs["transport"] = self.transport
        with httpx.Client(**client_kwargs) as client:
            response = client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=body,
            )
            response.raise_for_status()
            data = response.json()

        content = data["choices"][0]["message"]["content"]
        return parse_model_payload(content)


def parse_model_payload(content: str) -> dict[str, Any]:
    """把模型返回的文本解析成 `sql_or_refusal` 形状。

    模型经常在 JSON 外面裹一层 ```json 代码块，这里统一剥掉；剥不掉就
    抛异常，而不是猜。
    """
    text = str(content).strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"模型返回的不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("模型返回的 JSON 不是对象")
    return payload
