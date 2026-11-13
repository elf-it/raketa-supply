import { useEffect, useMemo, useState } from 'react';

import type { BulkOperation, BulkRequest, Week } from '../types';

interface OperationDef {
  id: BulkOperation;
  label: string;
  /** Подпись к полю значения; пусто — значение не нужно. */
  valueLabel?: string;
  hint: string;
}

const OPERATIONS: OperationDef[] = [
  {
    id: 'scale',
    label: 'Умножить на %',
    valueLabel: '%',
    hint: 'Крутит DIFF, поэтому прогноз продолжает следовать за потенциалом и сезонностью.',
  },
  {
    id: 'delta',
    label: 'Прибавить штуки',
    valueLabel: 'шт.',
    hint: 'Фиксирует результат как ручную правку: в процентах штуки не выразить. Можно со знаком минус.',
  },
  {
    id: 'set',
    label: 'Задать значение',
    valueLabel: 'шт.',
    hint: 'Ставит одно и то же число во все выбранные ячейки.',
  },
  {
    id: 'clear',
    label: 'Сбросить к формуле',
    hint: 'Убирает ручной прогноз и DIFF — ячейки возвращаются к расчётному значению.',
  },
  {
    id: 'move',
    label: 'Перенести спрос',
    hint: 'Обнуляет выбранные недели и добавляет их сумму к целевой неделе.',
  },
];

interface Props {
  weeks: Week[];
  pairIds: number[];
  selectionCount: number;
  totalRows: number;
  busy: boolean;
  onApply: (request: BulkRequest) => Promise<void>;
  onClearSelection: () => void;
}

/** Массовые корректировки прогноза (задача C-14). */
export function BulkPanel({
  weeks,
  pairIds,
  selectionCount,
  totalRows,
  busy,
  onApply,
  onClearSelection,
}: Props) {
  const openWeeks = useMemo(() => weeks.filter((week) => !week.is_closed), [weeks]);

  const [operation, setOperation] = useState<BulkOperation>('scale');
  const [value, setValue] = useState('110');
  const [fromWeek, setFromWeek] = useState('');
  const [toWeek, setToWeek] = useState('');
  const [targetWeek, setTargetWeek] = useState('');
  const [note, setNote] = useState<string | null>(null);

  useEffect(() => {
    if (openWeeks.length === 0) return;
    setFromWeek((prev) => (prev && openWeeks.some((w) => w.key === prev) ? prev : openWeeks[0].key));
    setToWeek((prev) =>
      prev && openWeeks.some((w) => w.key === prev) ? prev : openWeeks[Math.min(3, openWeeks.length - 1)].key,
    );
    setTargetWeek((prev) =>
      prev && openWeeks.some((w) => w.key === prev)
        ? prev
        : openWeeks[Math.min(6, openWeeks.length - 1)].key,
    );
  }, [openWeeks]);

  const def = OPERATIONS.find((item) => item.id === operation) ?? OPERATIONS[0];

  const selectedWeeks = useMemo(() => {
    if (!fromWeek || !toWeek) return [];
    const [start, end] = fromWeek <= toWeek ? [fromWeek, toWeek] : [toWeek, fromWeek];
    return openWeeks.filter((week) => week.key >= start && week.key <= end).map((w) => w.key);
  }, [openWeeks, fromWeek, toWeek]);

  const cells = pairIds.length * selectedWeeks.length;
  const parsedValue = Number(value.replace(',', '.'));
  const needsValue = Boolean(def.valueLabel);
  const valueInvalid = needsValue && (value.trim() === '' || Number.isNaN(parsedValue));
  const moveConflict = operation === 'move' && selectedWeeks.includes(targetWeek);

  const blocked =
    busy || cells === 0 || valueInvalid || moveConflict || (operation === 'move' && !targetWeek);

  const apply = async () => {
    if (blocked) return;
    const request: BulkRequest = {
      pair_ids: pairIds,
      weeks: selectedWeeks,
      operation,
      author: 'массовая правка',
    };
    if (needsValue) request.value = parsedValue;
    if (operation === 'move') request.target_week = targetWeek;

    await onApply(request);
    setNote(
      operation === 'move'
        ? `Спрос ${selectedWeeks.length} нед. перенесён в ${targetWeek}`
        : `Обработано ячеек: ${cells}`,
    );
  };

  if (openWeeks.length === 0) return null;

  return (
    <section className="bulk">
      <div className="bulk-row">
        <div className="bulk-controls">
        <span className="bulk-title">Массовые правки</span>

        <span className="bulk-scope">
          {selectionCount > 0 ? (
            <>
              отмечено {selectionCount} из {totalRows}
              <button className="link" onClick={onClearSelection}>
                снять
              </button>
            </>
          ) : (
            <>все {totalRows} строк под фильтрами</>
          )}
        </span>

        <select value={operation} onChange={(event) => setOperation(event.target.value as BulkOperation)}>
          {OPERATIONS.map((item) => (
            <option key={item.id} value={item.id}>
              {item.label}
            </option>
          ))}
        </select>

        {needsValue && (
          <label className="inline-field">
            <input
              className={`bulk-value ${valueInvalid ? 'invalid' : ''}`}
              value={value}
              inputMode="decimal"
              onChange={(event) => setValue(event.target.value)}
            />
            {def.valueLabel}
          </label>
        )}

        <label className="inline-field">
          недели
          <select value={fromWeek} onChange={(event) => setFromWeek(event.target.value)}>
            {openWeeks.map((week) => (
              <option key={week.key} value={week.key}>
                {week.key}
              </option>
            ))}
          </select>
          —
          <select value={toWeek} onChange={(event) => setToWeek(event.target.value)}>
            {openWeeks.map((week) => (
              <option key={week.key} value={week.key}>
                {week.key}
              </option>
            ))}
          </select>
        </label>

        {operation === 'move' && (
          <label className="inline-field">
            в неделю
            <select value={targetWeek} onChange={(event) => setTargetWeek(event.target.value)}>
              {openWeeks.map((week) => (
                <option key={week.key} value={week.key}>
                  {week.key}
                </option>
              ))}
            </select>
          </label>
        )}
        </div>

        <button className="primary bulk-apply" disabled={blocked} onClick={() => void apply()}>
          {busy ? 'Применяю…' : `Применить к ${cells} ячейкам`}
        </button>
      </div>

      <div className="bulk-hint">
        {moveConflict ? (
          <span className="danger">Целевая неделя не может быть среди переносимых</span>
        ) : valueInvalid ? (
          <span className="danger">Значение должно быть числом</span>
        ) : (
          <>
            {def.hint} Закрытые недели не затрагиваются, каждое изменение попадает в журнал
            корректировок.
            {note && <strong className="bulk-note"> {note}</strong>}
          </>
        )}
      </div>
    </section>
  );
}
