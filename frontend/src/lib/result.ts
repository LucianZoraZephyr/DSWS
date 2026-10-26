import type { CellValue, ResultData } from "../types";

/**
 * 结果表的语义分析。
 *
 * 放在 `lib/` 而不是组件里，是为了能被 Vitest 直接测——界面上「看起来
 * 对」不算数，「这一列确实全是 NULL」得能被断言。
 */

/** 「看起来像数字」的字符串。 */
const NUMERIC_STRING = /^-?\d+(?:\.\d+)?$/;

export function isNumericString(value: CellValue): boolean {
  return (
    typeof value === "string" &&
    value.trim() !== "" &&
    NUMERIC_STRING.test(value.trim())
  );
}

export interface ColumnInfo {
  name: string;
  /** 契约声明的列类型。 */
  type: string;
  /** 整列都是 NULL ——「这一列没有数据」，不是「这一列是 0」。 */
  allNull: boolean;
  /** 列里有「数字样式的字符串」。 */
  numericString: boolean;
  /**
   * 契约说这一列是 number，值却是字符串。
   *
   * 这是**契约被破坏**的信号：后端把 Decimal 用 `default=str` 降级过
   * （审计 B4）。S7 起后端不再这么干，但前端要能**看见**它发生了，
   * 而不是照着「字符串」安安静静地显示。
   */
  typeMismatch: boolean;
  /** 值的真实类型与列类型不一致。 */
  actualTypes: string[];
}

export function describeColumns(data: ResultData): ColumnInfo[] {
  return data.columns.map((column, index) => {
    const values = data.rows.map((row) => row[index] ?? null);
    const present = values.filter((value) => value !== null);

    const actualTypes = [...new Set(present.map((value) => typeName(value)))];

    return {
      name: column.name,
      type: column.type,
      allNull: values.length > 0 && present.length === 0,
      numericString: present.some(isNumericString),
      typeMismatch:
        column.type === "number" && present.some((value) => typeof value !== "number"),
      actualTypes,
    };
  });
}

function typeName(value: CellValue): string {
  if (value === null) return "null";
  if (Array.isArray(value)) return "array";
  return typeof value;
}

/**
 * 复制出去的载荷。
 *
 * **JSON，不是 TSV**——这是类型保真的关键。
 *
 * 旧系统复制的是 `JSON.stringify(rows)`，而它的行里混着 `{"销量": "10"}`
 * 这种字符串数字：复制出去，下游拿到的就是字符串。JSON 至少能把
 * 「这是数字」和「这是字符串」**如实**记下来；再加上 S7 起后端不再把
 * Decimal 降级成字符串，数字就真的是数字了。
 */
export function buildCopyPayload(data: ResultData): string {
  return JSON.stringify(
    { columns: data.columns, rows: data.rows },
    null,
    2,
  );
}

/** 列名像不像「错误文案」。 */
const ERRORISH_COLUMN = /error|错误|失败|异常|message|状态|reason|无法|不支持/i;

/**
 * 「伪 SQL 当数据行」的止损。
 *
 * 旧系统的 LLM 编出 `SELECT '无法生成SQL…' AS error_message`，这条 SQL
 * 能过 EXPLAIN，于是后端把它当成**一行正常数据**发回来，前端渲染成一张
 * 1 行 1 列的表——全屏没有一处红色（审计 §3.2）。
 *
 * S7 起后端有两条防线（S4 拒绝常量投影、S5 策略兜底），真实的系统**不会**
 * 再产生这种行。这里是前端的**止损**：万一后端回滚了，或者换了别的实现，
 * 界面也不该把失败画成成功。
 *
 * 判据刻意保守：单行 + 单列 + 列名像错误文案 + 值是文本。宁可漏判
 * （画成表），也不要把一张真的 1 行 1 列的表误判成失败。
 *
 * 返回那段文字（供界面显示），不是伪错误就返回 null。
 */
export function looksLikePseudoError(data: ResultData): string | null {
  if (data.rows.length !== 1 || data.columns.length !== 1) return null;
  const column = data.columns[0];
  if (!ERRORISH_COLUMN.test(column.name)) return null;
  const value = data.rows[0][0];
  if (typeof value !== "string") return null;
  const text = value.trim();
  if (!text) return null;
  return text;
}
