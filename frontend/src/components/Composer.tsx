import { useState } from "react";

interface Props {
  disabled: boolean;
  onSubmit: (query: string) => void;
  /** 运行中时显示「停止」。取消是独立状态，不是完成。 */
  onStop?: () => void;
  running?: boolean;
}

export default function Composer({
  disabled,
  onSubmit,
  onStop,
  running = false,
}: Props) {
  const [value, setValue] = useState("");

  function submit() {
    const query = value.trim();
    if (!query || disabled) return;
    onSubmit(query);
    setValue("");
  }

  return (
    <form
      className="composer"
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <textarea
        value={value}
        rows={2}
        disabled={disabled}
        aria-label="问题"
        maxLength={500}
        placeholder="问点什么，例如：华北地区销售额是多少？（Enter 发送，Shift+Enter 换行）"
        onChange={(event) => setValue(event.target.value)}
        onKeyDown={(event) => {
          if (
            event.key === "Enter" &&
            !event.shiftKey &&
            !event.nativeEvent.isComposing
          ) {
            event.preventDefault();
            submit();
          }
        }}
      />
      {running && onStop ? (
        <button type="button" className="stop-button" onClick={onStop}>
          停止
        </button>
      ) : (
        <button type="submit" disabled={disabled || !value.trim()}>
          {disabled ? "执行中…" : "提问"}
        </button>
      )}
    </form>
  );
}
