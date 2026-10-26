/**
 * 一台通用 JSON Schema 校验器（draft 2020-12 的一个子集）。
 *
 * 为什么前端要有一台校验器，而不是让后端保证？
 *
 * 后端**已经**在发事件前校验过（`tests/test_s0_contract.py` 用
 * `jsonschema` 逐条验）。但前端拿到的是**网络上的字节**——代理可能改写、
 * 后端可能回滚到旧版本、前端可能连上了别的服务。旧系统就是在这里
 * 崩的：`App.tsx` 的兜底分支把任何未知 type 当成 error，于是「后端
 * 升级了」变成「用户看到一次莫名失败」。
 *
 * 所以前端自己再验一遍，但**验的是契约本身**（`generated/sse.schema.json`
 * 是 `contracts/sse.schema.json` 的生成副本），不是抄一份规则。
 * 这台校验器是**通用引擎**，不含任何业务知识；契约才是事实源。
 *
 * 支持的关键字（正好是 `contracts/sse.schema.json` 用到的）：
 *
 *   $ref / $defs · type · enum · const · required · properties ·
 *   additionalProperties · items · pattern · minimum · maximum ·
 *   oneOf · allOf(含 if / then / else)
 *
 * 遇到不认识的关键字**不报错**——JSON Schema 允许注解性关键字。
 * 但后端那个生成器（`app/scripts/contracts_gen.py`）会在契约里出现
 * 它不认识的**约束**关键字时报错，两边互补。
 */

export interface SchemaError {
  /** 出错位置，例如 `$.data.rows[0][1]` */
  path: string;
  /** 人话，例如「类型应当是 number | null」 */
  message: string;
}

type JsonObject = Record<string, unknown>;

function isObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function matchesType(type: string, value: unknown): boolean {
  switch (type) {
    case "object":
      return isObject(value);
    case "array":
      return Array.isArray(value);
    case "string":
      return typeof value === "string";
    case "number":
      return typeof value === "number" && Number.isFinite(value);
    case "integer":
      return typeof value === "number" && Number.isInteger(value);
    case "boolean":
      return typeof value === "boolean";
    case "null":
      return value === null;
    default:
      return false;
  }
}

/** 解析 `#/$defs/<name>` 这样的引用。 */
function resolveRef(ref: string, root: unknown): unknown {
  if (!ref.startsWith("#/")) return undefined;
  let current: unknown = root;
  for (const rawPart of ref.slice(2).split("/")) {
    const part = rawPart.replace(/~1/g, "/").replace(/~0/g, "~");
    if (!isObject(current) || !(part in current)) return undefined;
    current = current[part];
  }
  return current;
}

/**
 * 校验 `value` 是否满足 `schema`。
 *
 * 返回空数组 = 通过。返回的每一条都带路径，能直接告诉用户「哪一条
 * 事件、哪个字段」不对——旧系统只给一句 `str(e)`。
 */
export function validate(schema: unknown, value: unknown): SchemaError[] {
  const errors: SchemaError[] = [];
  walk(schema, value, "$", schema, errors);
  return errors;
}

/** 试算一段 schema 是否通过（不产生错误列表，只问「过不过」）。 */
function accepts(schema: unknown, value: unknown, root: unknown): boolean {
  const probe: SchemaError[] = [];
  walk(schema, value, "$", root, probe);
  return probe.length === 0;
}

/**
 * 挑一支「最可能」的分支来报错。
 *
 * 事件联合的每一支都用 `type` 当判别字段，于是值里的 `type` 就是指针：
 * 它指向哪一支，就报哪一支的错误。
 */
function pickBranch(
  branches: unknown[],
  value: unknown,
  root: unknown,
): number {
  const type = isObject(value) ? value.type : undefined;
  if (typeof type !== "string") return 0;
  for (let index = 0; index < branches.length; index += 1) {
    const branch = branches[index];
    const resolved = isObject(branch) && "$ref" in branch
      ? resolveRef(String(branch["$ref"]), root)
      : branch;
    if (!isObject(resolved)) continue;
    const properties = resolved["properties"];
    if (!isObject(properties)) continue;
    const typeSchema = properties["type"];
    if (isObject(typeSchema) && typeSchema["const"] === type) {
      return index;
    }
  }
  return 0;
}

function walk(
  schema: unknown,
  value: unknown,
  path: string,
  root: unknown,
  errors: SchemaError[],
): void {
  if (!isObject(schema)) return;

  if ("$ref" in schema) {
    const ref = schema["$ref"];
    if (typeof ref === "string") {
      const target = resolveRef(ref, root);
      if (target === undefined) {
        errors.push({ path, message: `无法解析的引用：${ref}` });
        return;
      }
      walk(target, value, path, root, errors);
    }
    return;
  }

  if ("const" in schema) {
    if (value !== schema["const"]) {
      errors.push({
        path,
        message: `必须是 ${JSON.stringify(schema["const"])}`,
      });
    }
  }

  if ("enum" in schema && Array.isArray(schema["enum"])) {
    const allowed = schema["enum"];
    if (!allowed.some((item) => item === value)) {
      errors.push({
        path,
        message: `必须是 ${allowed.map((item) => JSON.stringify(item)).join(" / ")} 之一`,
      });
    }
  }

  const declaredType = schema["type"];
  if (declaredType !== undefined) {
    const types = Array.isArray(declaredType)
      ? declaredType
      : [declaredType];
    if (!types.some((item) => matchesType(String(item), value))) {
      errors.push({ path, message: `类型应当是 ${types.join(" | ")}` });
      // 类型就不对了，再往下查只会产生一堆噪音。
      return;
    }
  }

  if (isObject(value)) {
    const required = schema["required"];
    if (Array.isArray(required)) {
      for (const key of required) {
        if (typeof key === "string" && !(key in value)) {
          errors.push({ path: `${path}.${key}`, message: "缺少必填字段" });
        }
      }
    }

    const properties = schema["properties"];
    if (isObject(properties)) {
      for (const [key, sub] of Object.entries(properties)) {
        if (key in value) {
          walk(sub, value[key], `${path}.${key}`, root, errors);
        }
      }
    }

    if (schema["additionalProperties"] === false && isObject(properties)) {
      for (const key of Object.keys(value)) {
        if (!(key in properties)) {
          errors.push({ path: `${path}.${key}`, message: "出现了未定义的字段" });
        }
      }
    }
  }

  if (Array.isArray(value) && "items" in schema) {
    value.forEach((item, index) => {
      walk(schema["items"], item, `${path}[${index}]`, root, errors);
    });
  }

  if (Array.isArray(schema["allOf"])) {
    for (const sub of schema["allOf"]) {
      if (isObject(sub) && "if" in sub) {
        const matched = accepts(sub["if"], value, root);
        const branch = matched ? sub["then"] : sub["else"];
        if (branch !== undefined) {
          walk(branch, value, path, root, errors);
        }
      } else {
        walk(sub, value, path, root, errors);
      }
    }
  }

  if (Array.isArray(schema["oneOf"])) {
    const branchErrors: SchemaError[][] = [];
    let matched = 0;
    for (const branch of schema["oneOf"]) {
      const probe: SchemaError[] = [];
      walk(branch, value, path, root, probe);
      branchErrors.push(probe);
      if (probe.length === 0) matched += 1;
    }
    if (matched !== 1) {
      // 报**具体**的错误，而不是「没匹配上任何一种」——
      // 用户要看到的是「$.duration_ms 缺少必填字段」，
      // 不是「你的事件不是三种里的任何一种」。
      const detail = branchErrors[pickBranch(schema["oneOf"], value, root)];
      if (detail.length > 0) {
        errors.push(...detail);
      } else {
        errors.push({
          path,
          message: `应当恰好匹配一种事件类型（实际匹配了 ${matched} 种）`,
        });
      }
    }
  }

  if (typeof value === "string" && typeof schema["pattern"] === "string") {
    if (!new RegExp(schema["pattern"]).test(value)) {
      errors.push({ path, message: `不匹配 ${schema["pattern"]}` });
    }
  }

  if (typeof value === "number") {
    if (typeof schema["minimum"] === "number" && value < schema["minimum"]) {
      errors.push({ path, message: `不能小于 ${schema["minimum"]}` });
    }
    if (typeof schema["maximum"] === "number" && value > schema["maximum"]) {
      errors.push({ path, message: `不能大于 ${schema["maximum"]}` });
    }
  }
}

/** 把错误列表压成一句人话。 */
export function formatErrors(errors: SchemaError[]): string {
  return errors.map((error) => `${error.path} ${error.message}`).join("；");
}
