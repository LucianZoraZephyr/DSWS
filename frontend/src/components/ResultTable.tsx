import { useMemo, useState } from "react";

import {
  buildCopyPayload,
  describeColumns,
  isNumericString,
} from "../lib/result";
import type { CellValue, ResultData } from "../types";

interface Props {
  data: ResultData;
}

const NUMBER_FORMAT = new Intl.NumberFormat("zh-CN", {
  maximumFractionDigits: 2,
});

/**
 * 单元格怎么显示。
 *
 * 关键在**不靠猜**：列类型由契约声明（`result.data.columns[].type`），
 * 前端照着它渲染。旧系统一律 `String(value)`，于是数字和字符串在屏幕
 * 上长得一模一样，复制出去才发现一个是 `"10"`、一个是 `10`。
 */
function formatCell(value: CellValue, type: string): string {
  if (value === null) return "NULL";
  if (type === "number" && typeof value === "number") {
    return NUMBER_FORMAT.format(value);
  }
  if (type === "boolean" && typeof value === "boolean") {
    return value ? "是" : "否";
  }
  return String(value);
}

/**
 * 结果表格。
 *
 * 逐条对上审计里的前端缺陷：
 *
 *   F4  rows 为空      → 显式空态块（而不是整张表消失）
 *   F6  整列都是 NULL  → 列头标「无数据」+ 顶部一条说明
 *   F10 数字不格式化  → 千分位；字符串形式的数字单独标注
 *   F13 复制丢类型    → 复制的是 **JSON**（列类型 + 原始值），数字就是数字
 */
export default function ResultTable({ data }: Props) {
  const [copied, setCopied] = useState(false);
  const columns = useMemo(() => describeColumns(data), [data]);
  const emptyColumns = columns.filter((column) => column.allNull);

  async function copy() {
    const payload = buildCopyPayload(data);
    try {
      await navigator.clipboard.writeText(payload);
    } catch {
      legacyCopy(payload);
    }
    setCopied(true);
    window.setTimeout(() => setCopied(false), 2000);
  }

  if (data.rows.length === 0) {
    return (
      <div className="result-empty" data-testid="result-empty">
        <strong>查询成功，但没有数据。</strong>
        <span>这通常意味着筛选条件超出了数据范围，而不是系统故障。</span>
      </div>
    );
  }

  return (
    <div className="result-block">
      {emptyColumns.length > 0 && (
        <p className="result-note" data-testid="null-columns-note">
          有 {emptyColumns.length} 列没有取到数据（
          {emptyColumns.map((column) => column.name).join("、")}
          ）——这些列全是 NULL，不是 0。
        </p>
      )}

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              {columns.map((column) => (
                <th
                  key={column.name}
                  className={[
                    column.type === "number" ? "align-right" : "",
                    column.allNull ? "col-all-null" : "",
                  ]
                    .filter(Boolean)
                    .join(" ")}
                >
                  {column.name}
                  <span className="col-type">{column.type}</span>
                  {column.allNull && (
                    <span className="col-flag" data-testid="col-all-null">
                      无数据
                    </span>
                  )}
                  {column.typeMismatch && (
                    <span
                      className="col-flag col-flag--warn"
                      title="契约说这一列是数字，实际收到的是字符串"
                    >
                      类型不符
                    </span>
                  )}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data.rows.map((row, rowIndex) => (
              <tr key={rowIndex}>
                {columns.map((column, columnIndex) => {
                  const value = row[columnIndex] ?? null;
                  const numericString = isNumericString(value);
                  return (
                    <td
                      key={column.name}
                      data-testid={
                        numericString ? "cell-numeric-string" : undefined
                      }
                      title={
                        numericString
                          ? "这是字符串形式的数字：复制出去仍然是字符串"
                          : undefined
                      }
                      className={[
                        column.type === "number" ? "align-right" : "",
                        value === null ? "cell-null" : "",
                        numericString ? "cell-numeric-string" : "",
                      ]
                        .filter(Boolean)
                        .join(" ")}
                    >
                      {formatCell(value, column.type)}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="result-actions">
        <button type="button" className="ghost-button" onClick={copy}>
          {copied ? "已复制 ✓" : "复制结果（JSON）"}
        </button>
        <span className="result-hint">
          复制的是 JSON：数字就是数字，字符串就是字符串。
        </span>
      </div>
    </div>
  );
}

function legacyCopy(text: string): void {
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  try {
    document.execCommand("copy");
  } catch {
    // 复制不了就算了：数据在页面上看得见
  }
  document.body.removeChild(area);
}
