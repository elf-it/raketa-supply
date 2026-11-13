import { useCallback, useEffect, useRef, useState } from 'react';

import { api } from '../api';
import type { JournalBatch, JournalEntry, JournalQuery } from '../types';

const PAGE_SIZE = 50;

const OPERATION_LABELS: Record<string, string> = {
  manual: 'Правка ячейки',
  scale: 'Умножение на %',
  delta: 'Прибавление штук',
  set: 'Установка значения',
  clear: 'Сброс к формуле',
  move: 'Перенос спроса',
};

const FIELD_OPTIONS = [
  { id: '', label: 'Все показатели' },
  { id: 'manual_forecast', label: 'Прогноз' },
  { id: 'diff_percent', label: 'DIFF' },
  { id: 'manual_adapted', label: 'Адаптированный прогноз' },
];

const numberFormat = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 });

function formatValue(value: number | null): string {
  return value === null ? '—' : numberFormat.format(value);
}

function formatMoment(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString('ru-RU', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

interface Props {
  filters: Omit<JournalQuery, 'field' | 'author' | 'batchId'>;
  onError: (message: string | null) => void;
}

/**
 * Журнал корректировок (задача G-2).
 *
 * Два режима чтения: «по операциям» сворачивает массовую правку в одну строку,
 * «по ячейкам» показывает отдельные изменения. Значения полей — пользовательские
 * данные, поэтому выводятся как текст.
 */
export function JournalPage({ filters, onError }: Props) {
  const [mode, setMode] = useState<'batch' | 'entry'>('batch');
  const [field, setField] = useState('');
  const [author, setAuthor] = useState('');
  const [batchId, setBatchId] = useState<string | undefined>(undefined);

  const [entries, setEntries] = useState<JournalEntry[]>([]);
  const [batches, setBatches] = useState<JournalBatch[]>([]);
  const [total, setTotal] = useState(0);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);

  const { storeId, categoryId, search } = filters;
  /**
   * Номер последнего отправленного запроса. Переключение режима успевает породить
   * два запроса, и без этой отсечки ответ отменённого перезаписывал бы счётчик
   * записей чужим значением.
   */
  const requestId = useRef(0);

  const load = useCallback(
    async (offset: number) => {
      const query: JournalQuery = {
        storeId,
        categoryId,
        search,
        field: field || undefined,
        author,
        batchId,
      };
      const ticket = ++requestId.current;
      setBusy(true);
      if (offset === 0) {
        // Иначе на долю секунды видны строки предыдущей выборки.
        if (mode === 'batch') setBatches([]);
        else setEntries([]);
        setLoaded(false);
      }
      try {
        if (mode === 'batch') {
          const page = await api.journalBatches(query, PAGE_SIZE, offset);
          if (ticket !== requestId.current) return;
          setBatches((prev) => (offset === 0 ? page.batches : [...prev, ...page.batches]));
          setTotal(page.total);
        } else {
          const page = await api.journalEntries(query, PAGE_SIZE, offset);
          if (ticket !== requestId.current) return;
          setEntries((prev) => (offset === 0 ? page.entries : [...prev, ...page.entries]));
          setTotal(page.total);
        }
        onError(null);
      } catch (err) {
        if (ticket === requestId.current) onError((err as Error).message);
      } finally {
        if (ticket === requestId.current) {
          setBusy(false);
          setLoaded(true);
        }
      }
    },
    [mode, field, author, batchId, storeId, categoryId, search, onError],
  );

  useEffect(() => {
    void load(0);
  }, [load]);

  const openBatch = (batch: JournalBatch) => {
    setBatchId(batch.batch_id);
    setMode('entry');
  };

  const backToOperations = () => {
    setBatchId(undefined);
    setMode('batch');
  };

  const shown = mode === 'batch' ? batches.length : entries.length;

  return (
    <section className="panel">
      <header className="panel-head">
        <div>
          <h2>Журнал корректировок</h2>
          <p className="muted">
            Кто, когда и что поменял. Массовая правка пишет запись на каждую ячейку, поэтому
            по умолчанию журнал свёрнут до операций.
          </p>
        </div>
        <div className="tabs">
          <button
            className={mode === 'batch' ? 'active' : ''}
            onClick={backToOperations}
          >
            По операциям
          </button>
          <button
            className={mode === 'entry' ? 'active' : ''}
            onClick={() => {
              setBatchId(undefined);
              setMode('entry');
            }}
          >
            По ячейкам
          </button>
        </div>
      </header>

      <div className="season-controls">
        <label className="inline-field">
          Показатель
          <select value={field} onChange={(event) => setField(event.target.value)}>
            {FIELD_OPTIONS.map((option) => (
              <option key={option.id} value={option.id}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <label className="inline-field">
          Автор
          <input
            value={author}
            placeholder="любой"
            onChange={(event) => setAuthor(event.target.value)}
          />
        </label>
        {batchId && (
          <span className="batch-chip">
            ячейки одной операции
            <button className="link" onClick={backToOperations}>
              показать все
            </button>
          </span>
        )}
        <span className="muted">
          {loaded ? `записей: ${total}` : 'загружаю…'}
        </span>
      </div>

      <div className="matrix-scroll">
        {mode === 'batch' ? (
          <table className="purchase">
            <thead>
              <tr>
                <th>Когда</th>
                <th>Операция</th>
                <th>Автор</th>
                <th>Позиций</th>
                <th>Ячеек</th>
                <th>Недели</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {batches.map((batch) => (
                <tr key={batch.batch_id}>
                  <td>{formatMoment(batch.created_at)}</td>
                  <td className="strong">
                    {OPERATION_LABELS[batch.operation] ?? batch.operation}
                  </td>
                  <td>{batch.author ?? '—'}</td>
                  <td className="num">{batch.pairs}</td>
                  <td className="num">{batch.cells}</td>
                  <td>
                    {batch.week_from === batch.week_to
                      ? batch.week_from
                      : `${batch.week_from} — ${batch.week_to}`}
                  </td>
                  <td>
                    <button className="link" onClick={() => openBatch(batch)}>
                      Показать ячейки
                    </button>
                  </td>
                </tr>
              ))}
              {batches.length === 0 && (
                <tr>
                  <td className="empty" colSpan={7}>
                    {loaded
                      ? 'Массовых правок пока не было — поячеечные смотрите в режиме «по ячейкам»'
                      : 'Загружаю…'}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        ) : (
          <table className="purchase">
            <thead>
              <tr>
                <th>Когда</th>
                <th>Товар</th>
                <th>Магазин</th>
                <th>Неделя</th>
                <th>Показатель</th>
                <th>Было</th>
                <th>Стало</th>
                <th>Операция</th>
                <th>Автор</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => (
                <tr key={entry.id}>
                  <td>{formatMoment(entry.created_at)}</td>
                  <td>
                    <div className="row-title">{entry.title}</div>
                    <div className="row-subtitle">арт. {entry.vendor_code}</div>
                  </td>
                  <td>{entry.store_name}</td>
                  <td>{entry.week}</td>
                  <td>{entry.field_label}</td>
                  <td className="num muted-cell">{formatValue(entry.old_value)}</td>
                  <td className="num strong">{formatValue(entry.new_value)}</td>
                  <td>{OPERATION_LABELS[entry.operation] ?? entry.operation}</td>
                  <td>{entry.author ?? '—'}</td>
                </tr>
              ))}
              {entries.length === 0 && (
                <tr>
                  <td className="empty" colSpan={9}>
                    {loaded ? 'Под фильтры ничего не попало' : 'Загружаю…'}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        )}
      </div>

      {shown < total && (
        <div className="journal-more">
          <button onClick={() => void load(shown)} disabled={busy}>
            {busy ? 'Загружаю…' : `Показать ещё ${Math.min(PAGE_SIZE, total - shown)}`}
          </button>
        </div>
      )}
    </section>
  );
}
