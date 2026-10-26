"""S5 · 只读账号的开通与验证。

旧系统的 `fixtures/dw.sql` 里有这么一行：

    GRANT ALL PRIVILEGES ON dw.* TO 'didilili'@'%';

这就是 D6 能发生的根本原因——**执行 SQL 的账号本来就能改数据、能建表、
能删表**。提示词里那句「只能查询不能写」在这个前提下毫无意义。

S5 的底线是：执行器只用一个在 `dw` 上**只有 SELECT** 的账号。路线图的
决策表第 2 条写明「S5 的只读账号是安全底线，不能省」。

开通语句刻意做成**确定性**的：先 `DROP USER IF EXISTS` 再 `CREATE USER`，
于是这个账号的权限集合**恰好**是策略里声明的那几项——不会因为以前有人
给它加过 INSERT 而「攒」下来。（不用 REVOKE 是因为 MySQL 对不存在的
授权做 REVOKE 会报 1141，幂等性反而更差。）

密码只在环境变量里，不进仓库。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.security.config import PolicyConfig, load_policy_config

PASSWORD_ENV = "REBUILD_DW_RO_PASSWORD"

# 账号权限里**不允许**出现的关键字（出现在 SHOW GRANTS 里就是事故）。
FORBIDDEN_GRANT_PATTERNS: tuple[str, ...] = (
    "ALL PRIVILEGES",
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "CREATE",
    "ALTER",
    "INDEX",
    "REFERENCES",
    "TRIGGER",
    "EXECUTE",
    "FILE",
    "PROCESS",
    "SUPER",
    "SHUTDOWN",
    "GRANT OPTION",
)


@dataclass(frozen=True)
class AccountSpec:
    """只读账号的身份。"""

    user: str
    host: str
    database: str
    grants: tuple[str, ...]
    password: str = ""

    @classmethod
    def from_config(
        cls,
        config: PolicyConfig | None = None,
        *,
        password: str = "",
        host: str | None = None,
    ) -> AccountSpec:
        cfg = config or load_policy_config()
        return cls(
            user=cfg.account_user,
            host=host if host is not None else cfg.account_host,
            database=cfg.account_database,
            grants=cfg.account_grants,
            password=password,
        )

    @property
    def qualified(self) -> str:
        return f"'{self.user}'@'{self.host}'"

    def safe_repr(self) -> str:
        """打印用：只暴露账号名，不暴露密码。"""
        grants = ", ".join(self.grants)
        return f"{self.qualified} on {self.database} ({grants})"

    def statements(self) -> tuple[str, ...]:
        """开通这个账号要跑的语句（幂等、结果确定）。"""
        return (
            f"DROP USER IF EXISTS {self.qualified}",
            f"CREATE USER {self.qualified} IDENTIFIED BY '{self.password}'",
            *(
                f"GRANT {grant} ON `{self.database}`.* TO {self.qualified}"
                for grant in self.grants
            ),
            "FLUSH PRIVILEGES",
        )


@dataclass(frozen=True)
class AccountReport:
    """验证结论。"""

    ok: bool
    grants: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()
    write_attempt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "grants": list(self.grants),
            "problems": list(self.problems),
            "write_attempt": self.write_attempt,
        }


def show_grants(connection: Any, spec: AccountSpec) -> tuple[str, ...]:
    """`SHOW GRANTS FOR <account>`。"""
    with connection.cursor() as cursor:
        cursor.execute(f"SHOW GRANTS FOR {spec.qualified}")
        return tuple(str(row[0]) for row in cursor.fetchall())


def _grant_problems(grants: tuple[str, ...], spec: AccountSpec) -> list[str]:
    problems: list[str] = []
    for grant in grants:
        upper = grant.upper()
        if "TO" not in upper:
            continue
        for pattern in FORBIDDEN_GRANT_PATTERNS:
            if pattern in upper:
                problems.append(f"账号持有不该有的权限：{grant}")
                break
    if not any("SELECT" in grant.upper() for grant in grants):
        problems.append(f"账号没有 SELECT 权限：{list(grants)}")
    if not any(spec.database.upper() in grant.upper() for grant in grants):
        problems.append(f"账号的授权里没有 {spec.database} 库")
    return problems


def verify_account(connection: Any, spec: AccountSpec) -> AccountReport:
    """验证这个账号真的是只读的。

    两步，缺一不可：

        1. 读 `SHOW GRANTS`——静态确认它只有 SELECT。
        2. **真的去写一次**——`UPDATE ... WHERE 1 = 0`。
           第 1 步是读权限表，第 2 步是问数据库「你到底让不让我写」。
           第 2 步的 WHERE 条件匹配不到任何行，所以即使权限配错了也**不会
           改动任何数据**。
    """
    problems: list[str] = []
    try:
        grants = show_grants(connection, spec)
    except Exception as exc:  # noqa: BLE001 - 验证失败就是验证失败
        return AccountReport(
            ok=False,
            problems=(f"读取 SHOW GRANTS 失败：{exc}",),
        )

    problems.extend(_grant_problems(grants, spec))

    write_attempt = ""
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE fact_order SET order_amount = order_amount WHERE 1 = 0"
            )
        write_attempt = "写操作被放行（权限配置有误）"
        problems.append(write_attempt)
    except Exception as exc:  # noqa: BLE001 - 拒绝才是预期结果
        write_attempt = f"写操作被拒绝：{exc}"
        number = exc.args[0] if getattr(exc, "args", None) else None
        if number != 1142:
            # 1142 = ER_TABLEACCESS_DENIED_ERROR。别的原因（比如表不存在）
            # 说明验证本身没跑通，不能当成「验证通过」。
            problems.append(f"写操作被拒绝，但原因不是权限不足：{exc}")

    return AccountReport(
        ok=not problems,
        grants=grants,
        problems=tuple(problems),
        write_attempt=write_attempt,
    )
