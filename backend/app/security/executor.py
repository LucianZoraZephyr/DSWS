"""S5 · 只读执行器。

执行一条 SQL 需要**四道闸**，少一道都不算安全：

    1. **策略先行** —— SQL 在连库之前就要过 `app.security.policy`。
       被拒的 SQL 连连接都不开：没有数据库往返，就没有「校验过了但跑的是
       另一条」的可能。
    2. **只读账号** —— 连接用的账号在 `dw` 上只有 SELECT。
       策略是代码，账号是数据库；两者独立生效。
    3. **语句超时** —— `SET SESSION max_execution_time`（服务端）+ 客户端
       socket 读超时（兜底）。一条没有 LIMIT 的全表扫描不该把服务拖死。
    4. **不许多语句** —— 连接标志里显式去掉 `CLIENT_MULTI_STATEMENTS`。
       旧系统的驱动（asyncmy）默认带着这个标志，于是
       `explain <sql>` 只解释第一句、第二句照常执行（D6 现象 A）。

失败只有**一个出口**：一个 `error` 形状（`code` / `message` / `reason_code`）。
策略拒绝、数据库报错、超时都走它——不存在「既没成功又没有 error」的第三种
状态。`code` 命名具体的失败类别：策略拒绝是 `SQL_REJECTED`，超时是
`TIMEOUT`，数据库语法错是 `SQL_SYNTAX`……
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from app.security.config import PolicyConfig, load_policy_config
from app.security.policy import PolicyDecision, SqlWhitelist, enforce

# 账号密码只能来自环境变量——仓库里不存秘密。
PASSWORD_ENV = "REBUILD_DW_RO_PASSWORD"

# ---------------------------------------------------------------------------
# 错误码
# ---------------------------------------------------------------------------
#
# 这里列的是执行器可能返回的全部错误码。契约
# （contracts/sql.policy.yaml 的 errors.codes）声明的是同一张表——
# `tests/security/test_contract_alignment.py` 会逐字比对两边，
# 多一个少一个都算漂移。

SQL_REJECTED = "SQL_REJECTED"
SQL_SYNTAX = "SQL_SYNTAX"
SQL_UNKNOWN_TABLE = "SQL_UNKNOWN_TABLE"
SQL_UNKNOWN_COLUMN = "SQL_UNKNOWN_COLUMN"
SQL_EXECUTION_ERROR = "SQL_EXECUTION_ERROR"
TIMEOUT = "TIMEOUT"

EXECUTOR_ERROR_CODES: tuple[str, ...] = (
    SQL_REJECTED,
    SQL_SYNTAX,
    SQL_UNKNOWN_TABLE,
    SQL_UNKNOWN_COLUMN,
    SQL_EXECUTION_ERROR,
    TIMEOUT,
)

# MySQL 错误码 → 契约里的错误码。
_ERROR_CODE_MAP: dict[int, str] = {
    1064: SQL_SYNTAX,  # ER_PARSE_ERROR
    1146: SQL_UNKNOWN_TABLE,  # ER_NO_SUCH_TABLE
    1054: SQL_UNKNOWN_COLUMN,  # ER_BAD_FIELD_ERROR
    1142: SQL_EXECUTION_ERROR,  # ER_TABLEACCESS_DENIED_ERROR
    3024: TIMEOUT,  # ER_STATEMENT_TIMEOUT
    1205: TIMEOUT,  # ER_LOCK_WAIT_TIMEOUT
    1317: TIMEOUT,  # ER_QUERY_INTERRUPTED
}

# 客户端侧的超时表现（socket 读超时 / 连接被掐断）。
_CLIENT_TIMEOUT_CODES = frozenset({2006, 2013})


@dataclass(frozen=True)
class ExecutionError:
    """一次执行失败。

    `code` 是**通道码**（对外只暴露这一个）；`reason_code` 是原因
    （策略原因码或数据库错误类别）。
    """

    code: str
    message: str
    reason_code: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True)
class ExecutionResult:
    """执行结果。`ok` 为 False 时 `error` 一定非空。"""

    ok: bool
    sql: str
    rows: tuple[dict[str, Any], ...] = ()
    columns: tuple[str, ...] = ()
    error: ExecutionError | None = None
    elapsed_ms: int = 0
    decision: PolicyDecision | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "sql": self.sql,
            "columns": list(self.columns),
            "rows": [dict(row) for row in self.rows],
            "error": self.error.to_dict() if self.error else None,
            "elapsed_ms": self.elapsed_ms,
        }


class ExecutorError(Exception):
    """执行器自身的问题（配置缺失、连不上库等）。"""


def _error_number(exc: Exception) -> int | None:
    """从 pymysql 的异常里取 MySQL 错误码。"""
    args = getattr(exc, "args", ())
    if args and isinstance(args[0], int):
        return int(args[0])
    return None


def _error_text(exc: Exception) -> str:
    args = getattr(exc, "args", ())
    if len(args) > 1 and isinstance(args[1], str):
        return args[1]
    return str(exc)


def _map_error(exc: Exception) -> ExecutionError:
    """数据库异常 → 统一的错误形状。"""
    number = _error_number(exc)
    if number in _CLIENT_TIMEOUT_CODES:
        return ExecutionError(
            code=TIMEOUT,
            message=f"执行超时：{_error_text(exc)}",
            reason_code=TIMEOUT,
        )
    mapped = _ERROR_CODE_MAP.get(number or -1)
    if mapped == TIMEOUT:
        return ExecutionError(
            code=TIMEOUT,
            message=f"执行超时：{_error_text(exc)}",
            reason_code=TIMEOUT,
        )
    if mapped is None:
        mapped = SQL_EXECUTION_ERROR
    return ExecutionError(
        code=mapped,
        message=_error_text(exc),
        reason_code=mapped,
    )


class ReadOnlyExecutor:
    """只读执行器。

    `connector` 是注入点：默认用 pymysql 连只读账号；测试注入假连接，
    于是整条链路（策略 → 连库 → 执行 → 取行）都能离线测。
    """

    def __init__(
        self,
        *,
        config: PolicyConfig | None = None,
        whitelist: SqlWhitelist | None = None,
        connector: Callable[[], Any] | None = None,
        password: str | None = None,
    ) -> None:
        self.config = config or load_policy_config()
        self.whitelist = whitelist
        self._connector = connector
        self._password = password

    # -- 连接 ------------------------------------------------------------
    def _connect(self) -> Any:
        if self._connector is not None:
            return self._connector()
        return self._default_connector()

    def _default_connector(self) -> Any:
        import os

        import pymysql
        from pymysql.constants import CLIENT

        password = self._password or os.getenv(PASSWORD_ENV, "")
        if not password:
            raise ExecutorError(
                f"缺少环境变量 {PASSWORD_ENV}：只读账号的密码不在仓库里，"
                f"必须由部署环境提供"
            )
        host = os.getenv("REBUILD_MYSQL_HOST", "127.0.0.1")
        port = int(os.getenv("REBUILD_MYSQL_PORT", "3306"))
        # 显式去掉多语句：即使策略被绕过，一条连接也只可能跑一条语句。
        flags = CLIENT.CAPABILITIES & ~CLIENT.MULTI_STATEMENTS
        return pymysql.connect(
            host=host,
            port=port,
            user=self.config.account_user,
            password=password,
            database=self.config.account_database,
            charset="utf8mb4",
            connect_timeout=self.config.connect_timeout_s,
            read_timeout=self.config.read_timeout_s,
            write_timeout=self.config.read_timeout_s,
            client_flag=flags,
            autocommit=True,
        )

    # -- 会话设置 --------------------------------------------------------
    def _prepare_session(self, connection: Any) -> None:
        """设置会话级超时。失败必须显式抛出——静默降级等于没有超时。"""
        with connection.cursor() as cursor:
            cursor.execute(
                f"SET SESSION max_execution_time = {self.config.max_execution_time_ms}"
            )

    # -- 主流程 ----------------------------------------------------------
    def execute(self, sql: str) -> ExecutionResult:
        """执行一条 SQL；策略不通过时**不会**连库。"""
        decision = enforce(sql, whitelist=self.whitelist, config=self.config)
        if not decision.ok:
            first = decision.violations[0]
            return ExecutionResult(
                ok=False,
                sql="",
                error=ExecutionError(
                    code=self.config.channel_code,
                    message=f"{first.message}：{first.detail}" if first.detail else first.message,
                    reason_code=first.code,
                ),
                decision=decision,
            )
        return self._run(decision)

    def _run(self, decision: PolicyDecision) -> ExecutionResult:
        started = time.monotonic()
        connection = None
        try:
            connection = self._connect()
            self._prepare_session(connection)
            with connection.cursor() as cursor:
                cursor.execute(decision.sql)
                raw_rows = cursor.fetchall()
                columns = tuple(
                    str(column[0]) for column in (cursor.description or ())
                )
                rows = tuple(
                    dict(row)
                    if isinstance(row, dict)
                    else dict(zip(columns, row))
                    for row in raw_rows
                )
        except ExecutorError:
            # 配置问题（缺密码等）不是「这条 SQL 失败了」——它必须炸出来。
            raise
        except Exception as exc:  # noqa: BLE001 - 统一成错误形状
            error = _map_error(exc)
            return ExecutionResult(
                ok=False,
                sql=decision.sql,
                error=error,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                decision=decision,
            )
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:  # noqa: BLE001 - 关闭失败不影响结论
                    pass

        return ExecutionResult(
            ok=True,
            sql=decision.sql,
            rows=tuple(rows),
            columns=columns,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            decision=decision,
        )
