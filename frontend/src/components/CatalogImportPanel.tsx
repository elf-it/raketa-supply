import { useRef, useState } from 'react';

import { api } from '../api';
import type { CatalogImport, CatalogTally } from '../types';

/**
 * Загрузка номенклатуры файлом (задача B-15).
 *
 * Ведение справочников по одному закрывает правку, но первый каталог так
 * не наполнить: у заказчика сотни артикулов в нескольких кабинетах.
 *
 * Файл уходит на сервер дважды, как и правки прогноза: первый раз — чтобы
 * показать, что именно изменится, второй — уже с подтверждением. Между этими
 * двумя разами сервер ничего не помнит, поэтому файл держим у себя до конца
 * разговора.
 */

const numberFormat = new Intl.NumberFormat('ru-RU');

function fmt(value: number): string {
  return numberFormat.format(value);
}

/** Строки сводки: показываем только то, что файл действительно затрагивает. */
function lines(report: CatalogImport): { label: string; tally: CatalogTally }[] {
  return [
    { label: report.applied ? 'Заведено товаров' : 'Новых товаров', tally: report.new_products },
    { label: report.applied ? 'Изменено карточек' : 'Изменится карточек', tally: report.updated_products },
    { label: report.applied ? 'Заведено в кабинеты' : 'Новых позиций в кабинетах', tally: report.new_pairs },
    { label: report.applied ? 'Изменено позиций' : 'Изменится позиций', tally: report.updated_pairs },
    { label: report.applied ? 'Заведено категорий' : 'Новых категорий', tally: report.new_categories },
    { label: report.applied ? 'Заведено поставщиков' : 'Новых поставщиков', tally: report.new_suppliers },
    { label: report.applied ? 'Записей цен' : 'Записей в историю цен', tally: report.price_records },
  ].filter((line) => line.tally.count > 0);
}

interface Props {
  /** Наблюдатель каталог скачивает, но не загружает: ручка закрыта ролью. */
  canEdit: boolean;
  /** Каталог изменился: экран перечитывает списки, а расчёт — свои данные. */
  onApplied: () => void;
  onError: (message: string | null) => void;
}

export function CatalogImportPanel({ canEdit, onApplied, onError }: Props) {
  const input = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [report, setReport] = useState<CatalogImport | null>(null);
  const [busy, setBusy] = useState(false);

  const reset = () => {
    setFile(null);
    setReport(null);
    if (input.current) input.current.value = '';
  };

  const send = async (picked: File, apply: boolean) => {
    setBusy(true);
    try {
      const answer = await api.importCatalog(picked, apply);
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
      {canEdit && (
        <label className="button-link small import-pick">
          Загрузить номенклатуру
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
      )}
      {/* Ссылка, а не fetch: файл сохраняет браузер под именем от сервера. */}
      <a className="button-link small" href={api.catalogExportUrl()}>
        Скачать каталог
      </a>
      <span className="muted small">
        Строка — товар в кабинете: один артикул в трёх кабинетах занимает три строки,
        у каждой свой номер номенклатуры. Колонки ищутся по названию в шапке, лишние
        не читаются, пустая ячейка ничего не стирает. Скачанный каталог — он же шаблон.
      </span>

      {busy && <span className="spinner">читаю файл…</span>}

      {report && (
        <div className="import-report">
          <p className="import-summary">
            {report.applied ? (
              <>
                <b>Загружено.</b> Строк в файле: {fmt(report.rows_read)}
              </>
            ) : (
              <>
                Строк в файле: {fmt(report.rows_read)} ·{' '}
                <b>изменений: {fmt(report.total_changes)}</b>
                {report.issues.count > 0 && (
                  <>
                    {' · '}
                    <span className="danger-text">не взято строк: {fmt(report.issues.count)}</span>
                  </>
                )}
              </>
            )}
          </p>

          {!report.applied && report.total_changes === 0 && (
            <p className="hint">
              Менять нечего: всё, что есть в файле, уже заведено в системе ровно так же.
            </p>
          )}

          {lines(report).length > 0 && (
            <ul className="import-issues">
              {lines(report).map((line) => (
                <li key={line.label}>
                  {line.label}: <b>{fmt(line.tally.count)}</b>
                  {line.tally.examples.length > 0 && (
                    <span className="muted"> — {line.tally.examples.join('; ')}</span>
                  )}
                </li>
              ))}
            </ul>
          )}

          {report.issues.count > 0 && (
            <>
              <p className="hint danger-text">
                Эти строки в работу не взяли — остальные загрузились. Поправьте их в файле
                и загрузите его ещё раз: то, что уже заведено, повторная загрузка не тронет.
              </p>
              <ul className="import-issues">
                {report.issues.examples.map((issue) => (
                  <li key={issue} className="danger-text">
                    {issue}
                  </li>
                ))}
                {report.issues.count > report.issues.examples.length && (
                  <li className="muted">
                    …и ещё {fmt(report.issues.count - report.issues.examples.length)}
                  </li>
                )}
              </ul>
            </>
          )}

          <div className="import-actions">
            {!report.applied && report.total_changes > 0 && (
              <button
                className="small"
                disabled={busy || !file}
                onClick={() => file && void send(file, true)}
              >
                Загрузить {fmt(report.total_changes)} изменений
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
