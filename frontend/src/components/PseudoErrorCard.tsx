interface Props {
  /** 后端发回来的那一行「像错误文案」的内容。 */
  text: string;
  /** 那一列的名字。 */
  column: string;
}

/**
 * 「伪 SQL 当数据行」的止损卡。
 *
 * 旧系统里，模型编出 `SELECT '无法生成SQL…' AS error_message`，这条 SQL
 * 能过 `EXPLAIN`，于是后端把它当成**一行正常数据**发回来，前端渲染成一张
 * 1 行 1 列的表——**全屏没有一处红色**。用户第一反应是「查到了 1 条数据」。
 *
 * S7 起后端有两条防线（S4 拒绝常量投影 + S5 策略兜底），所以真实的系统
 * 不会再产生这种行。这个组件是**止损**：万一后端回滚了，或者换了别的实现，
 * 界面也不该把失败画成成功。
 *
 * 刻意**不编造错误码**：契约里的码只有一份（`contracts/errors.yaml`），
 * 前端硬编一个码出来，等于又抄了一份契约。真正的路径上，错误码由后端
 * 带着 `type:"error"` 事件发过来（见 `ErrorCard`）。
 */
export default function PseudoErrorCard({ text, column }: Props) {
  return (
    <div
      className="error-card error-card--failure"
      data-pseudo-error="true"
      role="alert"
    >
      <div className="error-head">
        <strong>这一行不是查询结果，是失败信息</strong>
        <code>后端返回的伪数据行</code>
      </div>

      <p className="error-message">{text}</p>

      <p className="error-meta">
        <span>列名：{column}</span>
        <span>
          新系统的后端会用一条真正的失败事件取代它；这一屏是前端的兜底。
        </span>
      </p>
    </div>
  );
}
