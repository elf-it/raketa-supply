import { useRef, useState } from 'react';

import { api } from '../api';
import type { Reconcile } from '../types';

const numberFormat = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 });

function fmt(value: number): string {
  return numberFormat.format(Math.round(value));
}

function signed(value: number): string {
  const text = fmt(Math.abs(value));
  return value > 0 ? `+${text}` : value < 0 ? `−${text}` : text;
}

interface Props {
  onError: (message: string | null) => void;
}

/**
 * Сверка прогноза с файлом заказчика (задача C-10).
 *
 * Алгоритм DIFF и правила статусов в системе — допущения (вопрос Q-1).
 * Эта панель даёт их проверить: файл заказчика против наших чисел.
 *
 * Отличие от загрузки правок рядом — в том, что здесь **ничего не пишется**.
 * Поэтому нет ни второго прохода с подтверждением, ни кнопки «применить»:
 * один запрос, один ответ, и запускать его можно сколько угодно раз.
 */
export function ReconcilePanel({ onError }: Props) {
  const input = useRef<HTMLInputElement>(null);
  const [report, setReport] = useState<Reconcile | null>(null);
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);

  const reset = () => {
    setReport(null);
    if (input.current) input.current.value = '';
  };

  const send = async (picked: File) => {
    setBusy(true);
    try {
      setReport(await api.reconcileMatrix(picked));
      onError(null);
    } catch (err) {
      reset();
      onError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (!open) {
    return (
      <div className="import-panel">
        <button className="ghost small" onClick={() => setOpen(true)}>
          Сверить с файлом заказчика
        </button>
        <span className="muted small">
          Сравнить наш прогноз с числами из действующей таблицы. Ничего не меняет.
        </span>
      </div>
    );
  }

  return (
    <div className="import-panel">
      <label className="button-link small import-pick">
        Выбрать файл сверки
        <input
          ref={input}
          type="file"
          accept=".xlsx,.csv"
          disabled={busy}
          onChange={(event) => {
            const picked = event.target.files?.[0] ?? null;
            setReport(null);
            if (picked) void send(picked);
          }}
        />
      </label>
      <button
        className="ghost small"
        disabled={busy}
        onClick={() => {
          reset();
          setOpen(false);
        }}
      >
        Закрыть
      </button>
      <span className="muted small">
        Три колонки: «Артикул», «Неделя», «Прогноз». Магазин, DIFF и статус — по желанию.
        Неделя — «2026-W46», «W46 2026» или дата. Книга .xlsx или .csv. В базу ничего
        не пишется.
      </span>

      {busy && <span className="spinner">сверяю…</span>}

      {report && (
        <div className="import-report">
          <p className={report.diverged > 0 ? 'inline-warning' : 'inline-note'}>
            {report.verdict}
          </p>

          <p className="import-summary">
            строк в файле {fmt(report.rows_read)} · сверено ячеек {fmt(report.compared)} ·
            сошлось <b>{fmt(report.matched)}</b> · разошлось{' '}
            <b className={report.diverged > 0 ? 'danger-text' : undefined}>
              {fmt(report.diverged)}
            </b>
            {report.median_ratio !== null && (
              <>
                {' · '}
                наше / их: <b>{report.median_ratio.toFixed(2)}×</b>
                {report.ratio_share !== null && (
                  <span className="muted"> ({Math.round(report.ratio_share * 100)}% ячеек)</span>
                )}
              </>
            )}
          </p>

          {report.status_compared > 0 && (
            <p className="hint">
              Статусы: сверено {fmt(report.status_compared)}, расходится{' '}
              <b>{fmt(report.status_mismatched)}</b>
              {report.status_examples.length > 0 && (
                <span className="muted"> — {report.status_examples.join('; ')}</span>
              )}
            </p>
          )}

          {report.reasons.length > 0 && (
            <ul className="import-issues">
              {report.reasons.map((reason) => (
                <li key={reason.reason}>
                  {reason.reason}: <b>{fmt(reason.count)}</b>
                  {reason.examples.length > 0 && (
                    <span className="muted"> — {reason.examples.join(', ')}</span>
                  )}
                </li>
              ))}
            </ul>
          )}

          {report.lines.length > 0 && (
            <table className="purchase inner import-rows">
              <thead>
                <tr>
                  <th>Товар</th>
                  <th>Неделя</th>
                  <th>У нас</th>
                  <th>В файле</th>
                  <th>Разница</th>
                  <th>Причина</th>
                  <th>Строка</th>
                </tr>
              </thead>
              <tbody>
                {report.lines.map((line) => (
                  <tr key={`${line.vendor_code}-${line.week}-${line.line}`}>
                    <td>
                      <div className="row-title">{line.title}</div>
                      <div className="row-subtitle">
                        арт. {line.vendor_code} · {line.store_name}
                      </div>
                    </td>
                    <td>{line.week}</td>
                    <td className="num strong">{fmt(line.ours)}</td>
                    <td className="num muted-cell">{fmt(line.theirs)}</td>
                    <td className="num">
                      {signed(line.delta)}
                      {line.ratio !== null && (
                        <div className="row-subtitle">{line.ratio.toFixed(2)}×</div>
                      )}
                    </td>
                    <td className="muted small">{line.reason}</td>
                    <td className="num muted-cell">{line.line}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {report.diverged > report.lines.length && (
            <p className="hint">
              Показаны {report.lines.length} самых крупных расхождений из{' '}
              {fmt(report.diverged)} — остальные те же по причинам.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
