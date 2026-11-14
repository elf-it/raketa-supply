import { useRef, useState } from 'react';

import { api } from '../api';
import type { MatrixImport } from '../types';

const numberFormat = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 });

function fmt(value: number): string {
  return numberFormat.format(Math.round(value));
}

function moment(iso: string): string {
  return new Date(iso).toLocaleString('ru-RU', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

interface Props {
  /** Правка прогноза меняет сток, статусы и итоги: после применения экран перечитывается. */
  onApplied: () => void;
  onError: (message: string | null) => void;
}

/**
 * Загрузка правок прогноза из выгруженного файла (G-9).
 *
 * Файл уходит на сервер дважды. Первый раз — чтобы показать, что именно
 * изменится: это чужой файл, и его правки могут оказаться не теми, которых
 * ждут. Второй — уже с подтверждением. Между разбором и применением сервер
 * ничего не помнит, поэтому файл держим у себя до конца разговора.
 */
export function ImportPanel({ onApplied, onError }: Props) {
  const input = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [report, setReport] = useState<MatrixImport | null>(null);
  const [busy, setBusy] = useState(false);

  const reset = () => {
    setFile(null);
    setReport(null);
    if (input.current) input.current.value = '';
  };

  const send = async (picked: File, apply: boolean) => {
    setBusy(true);
    try {
      const answer = await api.importMatrix(picked, apply);
      setReport(answer);
      onError(null);
      if (apply) {
        onApplied();
        setFile(null);
        if (input.current) input.current.value = '';
      }
    } catch (err) {
      reset();
      onError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="import-panel">
      <label className="button-link small import-pick">
        Загрузить правки
        <input
          ref={input}
          type="file"
          accept=".xlsx"
          disabled={busy}
          onChange={(event) => {
            const picked = event.target.files?.[0] ?? null;
            setFile(picked);
            setReport(null);
            if (picked) void send(picked, false);
          }}
        />
      </label>
      <span className="muted small">
        Тот же файл, что скачали, с исправленным прогнозом. Берётся только строка
        «План / исходный прогноз» и только там, где число отличается от нынешнего.
      </span>

      {busy && <span className="spinner">читаю файл…</span>}

      {report && (
        <div className="import-report">
          <p className="import-summary">
            {report.applied ? (
              <>
                <b>Применено: {fmt(report.applied_cells)}</b> правок.
              </>
            ) : (
              <>
                Файл выгружен {moment(report.exported_at)} · позиций в нём{' '}
                {fmt(report.rows_read)} · <b>правок: {fmt(report.change_count)}</b>
                {report.conflict_count > 0 && (
                  <>
                    {' · '}
                    <span className="danger-text">
                      конфликтов: {fmt(report.conflict_count)}
                    </span>
                  </>
                )}
              </>
            )}
          </p>

          {!report.applied && report.change_count === 0 && report.conflict_count === 0 && (
            <p className="hint">
              Менять нечего: все числа в файле совпадают с теми, что в системе сейчас.
            </p>
          )}

          {report.changes.length > 0 && (
            <table className="purchase inner import-rows">
              <thead>
                <tr>
                  <th>Товар</th>
                  <th>Магазин</th>
                  <th>Неделя</th>
                  <th>Было</th>
                  <th>Станет</th>
                </tr>
              </thead>
              <tbody>
                {report.changes.map((change) => (
                  <tr key={`${change.pair_id}-${change.week}`}>
                    <td>
                      <div className="row-title">{change.title}</div>
                      <div className="row-subtitle">арт. {change.vendor_code}</div>
                    </td>
                    <td>{change.store_name}</td>
                    <td>{change.week}</td>
                    <td className="num muted-cell">{fmt(change.old_value)}</td>
                    <td className="num strong">{fmt(change.new_value)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {report.change_count > report.changes.length && (
            <p className="hint">
              Показаны первые {report.changes.length} правок из {fmt(report.change_count)}.
            </p>
          )}

          {report.conflicts.length > 0 && (
            <>
              <p className="hint danger-text">
                Эти ячейки изменили в системе уже после того, как файл выгрузили. Числа
                из файла к ним не применяются: иначе чужая работа пропала бы молча.
                Если нужно именно значение из файла — впишите его в таблице вручную.
              </p>
              <table className="purchase inner import-rows">
                <thead>
                  <tr>
                    <th>Товар</th>
                    <th>Неделя</th>
                    <th>В системе</th>
                    <th>В файле</th>
                    <th>Кто и когда поменял</th>
                  </tr>
                </thead>
                <tbody>
                  {report.conflicts.map((conflict) => (
                    <tr key={`${conflict.pair_id}-${conflict.week}`}>
                      <td>
                        <div className="row-title">{conflict.title}</div>
                        <div className="row-subtitle">
                          арт. {conflict.vendor_code} · {conflict.store_name}
                        </div>
                      </td>
                      <td>{conflict.week}</td>
                      <td className="num strong">{fmt(conflict.old_value)}</td>
                      <td className="num muted-cell">{fmt(conflict.new_value)}</td>
                      <td>
                        {conflict.changed_by ?? '—'}
                        <div className="row-subtitle">{moment(conflict.changed_at)}</div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}

          {report.issues.length > 0 && (
            <ul className="import-issues">
              {report.issues.map((issue) => (
                <li key={issue.reason}>
                  {issue.reason}: <b>{fmt(issue.count)}</b>
                  {issue.examples.length > 0 && (
                    <span className="muted"> — {issue.examples.join(', ')}</span>
                  )}
                </li>
              ))}
            </ul>
          )}

          <div className="import-actions">
            {!report.applied && report.change_count > 0 && (
              <button
                className="small"
                disabled={busy || !file}
                onClick={() => file && void send(file, true)}
              >
                Применить {fmt(report.change_count)} правок
              </button>
            )}
            <button className="ghost small" disabled={busy} onClick={reset}>
              {report.applied ? 'Закрыть' : 'Отмена'}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
