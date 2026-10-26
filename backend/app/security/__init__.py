"""S5 · 安全边界。

这一层把「只读」从提示词搬进代码。

    contracts/sql.policy.yaml   策略契约（唯一事实源）
            │
            ▼
    app.security.policy         AST 级策略：单语句 / 只读 / 白名单 / 自动 LIMIT
            │
            ▼
    app.security.executor       只读账号 + 语句超时 + 客户端兜底
            │
            ▼
    app.security.account        只读账号的开通与验证

失败只有一个出口：`SQL_REJECTED`（原因码放在 reason_code 里）。
"""

from app.security.config import PolicyConfig, PolicyConfigError, load_policy_config
from app.security.policy import (
    PolicyDecision,
    PolicyViolation,
    SqlWhitelist,
    default_whitelist,
    enforce,
)

__all__ = [
    "PolicyConfig",
    "PolicyConfigError",
    "PolicyDecision",
    "PolicyViolation",
    "SqlWhitelist",
    "default_whitelist",
    "enforce",
    "load_policy_config",
]
