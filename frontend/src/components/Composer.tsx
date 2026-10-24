import { useState } from "react";

interface Props {
  disabled: boolean;
  onSubmit: (query: string) => void;
}

export default function Composer({ disabled, onSubmit }: Props) {
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
      <button type="submit" disabled={disabled || !value.trim()}>
        {disabled ? "执行中…" : "提问"}
      </button>
    </form>
  );
}
