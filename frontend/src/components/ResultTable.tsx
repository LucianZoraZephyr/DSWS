import type { CellValue, ResultData } from "../types";

interface Props {
  data: ResultData;
}

function formatCell(value: CellValue, type: string): string {
  if (value === null) return "NULL";
  if (type === "number" && typeof value === "number") {
    return new Intl.NumberFormat("zh-CN", {
      maximumFractionDigits: 2,
    }).format(value);
  }
  return String(value);
}

/**
 * 结果表格。
 *
 * 两个刻意为之的边界处理（对应审计里的前端缺陷）：
 *   1. rows 为空数组 → 显式显示「查询成功，但没有数据」，而不是整张表消失
 *   2. null 单元格 → 显示 NULL 并加样式，而不是留白看着像空字符串
 */
export default function ResultTable({ data }: Props) {
  if (data.rows.length === 0) {
    return (
      <div className="result-empty">
        <strong>查询成功，但没有数据。</strong>
        <span>这通常意味着筛选条件超出了数据范围，而不是系统故障。</span>
      </div>
    );
  }

  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            {data.columns.map((column) => (
              <th
                key={column.name}
                className={column.type === "number" ? "align-right" : undefined}
              >
                {column.name}
                <span className="col-type">{column.type}</span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data.rows.map((row, rowIndex) => (
            <tr key={rowIndex}>
              {data.columns.map((column, columnIndex) => {
                const value = row[columnIndex] ?? null;
                return (
                  <td
                    key={column.name}
                    className={[
                      column.type === "number" ? "align-right" : "",
                      value === null ? "cell-null" : "",
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
  );
}
