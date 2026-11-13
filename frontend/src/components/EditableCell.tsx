import { useEffect, useState } from 'react';

interface Props {
  value: number | null;
  suffix?: string;
  highlighted?: boolean;
  onCommit: (value: number | null) => void;
}

/**
 * Ячейка прогноза или DIFF. Правка уходит на сервер по Enter или потере фокуса,
 * Escape возвращает исходное значение, пустое поле сбрасывает ручную правку.
 */
export function EditableCell({ value, suffix, highlighted, onCommit }: Props) {
  const [draft, setDraft] = useState<string>(value === null ? '' : String(Math.round(value)));

  useEffect(() => {
    setDraft(value === null ? '' : String(Math.round(value)));
  }, [value]);

  const commit = () => {
    const trimmed = draft.trim();
    if (trimmed === '') {
      onCommit(null);
      return;
    }
    const parsed = Number(trimmed.replace(',', '.'));
    if (Number.isNaN(parsed) || parsed < 0) {
      setDraft(value === null ? '' : String(Math.round(value)));
      return;
    }
    if (value !== null && Math.round(parsed) === Math.round(value)) return;
    onCommit(parsed);
  };

  return (
    <span className={`editable ${highlighted ? 'manual' : ''}`}>
      <input
        value={draft}
        inputMode="numeric"
        onChange={(event) => setDraft(event.target.value)}
        onBlur={commit}
        onKeyDown={(event) => {
          if (event.key === 'Enter') {
            event.currentTarget.blur();
          }
          if (event.key === 'Escape') {
            setDraft(value === null ? '' : String(Math.round(value)));
            event.currentTarget.blur();
          }
        }}
      />
      {suffix && <span className="suffix">{suffix}</span>}
    </span>
  );
}
