import { describe, expect, it } from "vitest";

import {
  buildCopyPayload,
  describeColumns,
  isNumericString,
  looksLikePseudoError,
} from "./result";
import type { ResultData } from "../types";

const data: ResultData = {
  columns: [
    { name: "地区", type: "string" },
    { name: "销售额", type: "number" },
    { name: "备注", type: "string" },
  ],
  rows: [
    ["华北", 128400, null],
    ["华南", 96500.5, null],
  ],
};

describe("describeColumns", () => {
  it("认出整列都是 NULL 的列", () => {
    const columns = describeColumns(data);
    expect(columns[2].allNull).toBe(true);
    expect(columns[0].allNull).toBe(false);
    expect(columns[1].allNull).toBe(false);
  });

  it("认出「契约说数字、实际是字符串」", () => {
    const columns = describeColumns({
      columns: [{ name: "销量", type: "number" }],
      rows: [["10"]],
    });
    expect(columns[0].typeMismatch).toBe(true);
    expect(columns[0].numericString).toBe(true);
  });

  it("列类型与实际一致时没有 mismatch", () => {
    const columns = describeColumns(data);
    expect(columns.every((column) => !column.typeMismatch)).toBe(true);
  });
});

describe("isNumericString", () => {
  it.each([
    ["10", true],
    ["-3.5", true],
    [" 42 ", true],
    ["abc", false],
    ["", false],
    ["1,000", false],
  ])("%s -> %s", (value, expected) => {
    expect(isNumericString(value as never)).toBe(expected);
  });
});

describe("buildCopyPayload", () => {
  it("复制的是 JSON，数字就是数字", () => {
    const payload = buildCopyPayload(data);
    const parsed = JSON.parse(payload);
    expect(parsed.rows[0][1]).toBe(128400);
    expect(typeof parsed.rows[0][1]).toBe("number");
    expect(parsed.columns[1].type).toBe("number");
  });

  it("字符串不会被偷偷变成数字", () => {
    const payload = buildCopyPayload({
      columns: [{ name: "销量", type: "string" }],
      rows: [["10"]],
    });
    const parsed = JSON.parse(payload);
    expect(parsed.rows[0][0]).toBe("10");
    expect(typeof parsed.rows[0][0]).toBe("string");
  });
});

describe("looksLikePseudoError", () => {
  it("认出旧系统的伪 SQL 行", () => {
    const pseudo: ResultData = {
      columns: [{ name: "error_message", type: "string" }],
      rows: [["无法生成SQL：请补充时间范围"]],
    };
    expect(looksLikePseudoError(pseudo)).toBe("无法生成SQL：请补充时间范围");
  });

  it("正常的 1 行 1 列不算失败", () => {
    const normal: ResultData = {
      columns: [{ name: "地区", type: "string" }],
      rows: [["华北"]],
    };
    expect(looksLikePseudoError(normal)).toBeNull();
  });

  it("多行不算失败", () => {
    const rows: ResultData = {
      columns: [{ name: "error_message", type: "string" }],
      rows: [["a"], ["b"]],
    };
    expect(looksLikePseudoError(rows)).toBeNull();
  });
});
