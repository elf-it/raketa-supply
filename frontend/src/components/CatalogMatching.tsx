import { useCallback, useEffect, useState } from 'react';

import { api } from '../api';
import type { DuplicateHint, Product, Unmatched } from '../types';

/**
 * Сопоставление SKU (задача B-10): номенклатуры без товара и похожие дубли.
 *
 * Два разных разрыва между каталогом и тем, что происходит на самом деле.
 * Первый: кабинет продаёт номенклатуру, которой в каталоге нет, — её продажи
 * просто не считаются, и раньше об этом говорило только число «не сопоставлено»
 * в журнале. Второй: один физический товар заведён дважды — спрос и закупка
 * делятся пополам, а в матрице это выглядит как две разные позиции.
 *
 * Обе подсказки — именно подсказки. Привязывает и сливает человек: система
 * показывает, что похоже, но решить за него, один это товар или два, нельзя.
 */

const when = new Intl.DateTimeFormat('ru-RU', { day: '2-digit', month: '2-digit', year: '2-digit' });

function moment(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? '—' : when.format(date);
}

interface Props {
  products: Product[];
  canEdit: boolean;
  onError: (message: string | null) => void;
  /** Привязка и слияние меняют расчёт: экраны должны перечитать данные. */
  onChanged: () => void;
}

export function CatalogMatching({ products, canEdit, onError, onChanged }: Props) {
  const [rows, setRows] = useState<Unmatched[]>([]);
  const [hints, setHints] = useState<DuplicateHint[]>([]);
  const [choice, setChoice] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<{ kind: 'ok' | 'bad'; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [unmatched, duplicates] = await Promise.all([api.unmatched(), api.duplicates()]);
      setRows(unmatched);
      setHints(duplicates);
      onError(null);
    } catch (err) {
      onError((err as Error).message);
    }
  }, [onError]);

  useEffect(() => {
    void load();
  }, [load]);

  const act = async (job: () => Promise<string>) => {
    setBusy(true);
    try {
      setNotice({ kind: 'ok', text: await job() });
      await load();
      onChanged();
    } catch (err) {
      setNotice({ kind: 'bad', text: (err as Error).message });
    } finally {
      setBusy(false);
    }
  };

  const bind = (row: Unmatched) => {
    const key = `${row.store_id}:${row.nm_id}`;
    const productId = Number(choice[key]);
    if (!productId) return;
    const product = products.find((item) => item.id === productId);
    void act(async () => {
      await api.bindUnmatched({
        store_id: row.store_id,
        nm_id: row.nm_id,
        product_id: productId,
      });
      return `${row.nm_id} привязана к ${product?.vendor_code ?? 'товару'}: продажи приедут следующей загрузкой`;
    });
  };

  const merge = (hint: DuplicateHint, direction: 'left' | 'right') => {
    const [source, target] =
      direction === 'left'
        ? [
            { id: hint.left_id, code: hint.left_code },
            { id: hint.right_id, code: hint.right_code },
          ]
        : [
            { id: hint.right_id, code: hint.right_code },
            { id: hint.left_id, code: hint.left_code },
          ];
    void act(async () => {
      const result = await api.mergeProducts(source.id, target.id);
      return `${source.code} слит в ${target.code}: перенесено кабинетов — ${result.moved_pairs}, фактов — ${result.moved_facts}. Исходный товар снят с продажи`;
    });
  };

  return (
    <>
      {notice && (
        <p className={notice.kind === 'bad' ? 'inline-warning' : 'inline-note'}>{notice.text}</p>
      )}

      <h3 className="section-title">Номенклатуры без товара</h3>
      <p className="hint">
        Загрузка WB кладёт продажи на пару, найденную по номеру номенклатуры внутри кабинета.
        Эти номера кабинет присылает, а в каталоге их нет — значит, их продажи сейчас
        не считаются нигде. Привяжите номер к товару: факты приедут следующей загрузкой,
        задним числом система их не перезапрашивает.
      </p>

      <div className="matrix-scroll">
        <table className="purchase">
          <thead>
            <tr>
              <th>Кабинет</th>
              <th>Номенклатура</th>
              <th>Встречалась</th>
              <th>Последний раз</th>
              <th>Товар</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const key = `${row.store_id}:${row.nm_id}`;
              return (
                <tr key={key}>
                  <td>{row.store_name}</td>
                  <td className="strong">{row.nm_id}</td>
                  <td className="num">{row.times_seen}</td>
                  <td>{moment(row.last_seen)}</td>
                  <td>
                    {canEdit ? (
                      <span className="password-edit">
                        <select
                          value={choice[key] ?? ''}
                          disabled={busy}
                          onChange={(event) =>
                            setChoice((prev) => ({ ...prev, [key]: event.target.value }))
                          }
                        >
                          <option value="">выберите товар</option>
                          {products.map((product) => (
                            <option key={product.id} value={product.id}>
                              {product.vendor_code} · {product.title}
                            </option>
                          ))}
                        </select>
                        <button
                          className="ghost small"
                          disabled={busy || !choice[key]}
                          onClick={() => bind(row)}
                        >
                          Привязать
                        </button>
                      </span>
                    ) : (
                      '—'
                    )}
                  </td>
                </tr>
              );
            })}
            {rows.length === 0 && (
              <tr>
                <td className="empty" colSpan={5}>
                  Всё, что присылают кабинеты, лежит в каталоге
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <h3 className="section-title">Похоже на дубли</h3>
      <p className="hint">
        Один физический товар, заведённый дважды, делит спрос и закупку пополам. Система
        показывает похожих по штрихкоду, названию и артикулу — но решает человек: это может
        быть и две разные позиции. Слияние переносит кабинеты вместе с их прошлым: фактами,
        поставками, заказами. Исходный товар не удаляется, а снимается с продажи.
      </p>

      <div className="matrix-scroll">
        <table className="purchase">
          <thead>
            <tr>
              <th>Почему похожи</th>
              <th>Товар</th>
              <th>Кабинеты</th>
              <th>Товар</th>
              <th>Кабинеты</th>
              <th>Слить</th>
            </tr>
          </thead>
          <tbody>
            {hints.map((hint) => (
              <tr key={`${hint.left_id}-${hint.right_id}`}>
                <td>{hint.reason}</td>
                <td>
                  <div className="row-title">{hint.left_code}</div>
                  <div className="row-subtitle">{hint.left_title}</div>
                </td>
                <td className="num">{hint.left_pairs}</td>
                <td>
                  <div className="row-title">{hint.right_code}</div>
                  <div className="row-subtitle">{hint.right_title}</div>
                </td>
                <td className="num">{hint.right_pairs}</td>
                <td>
                  {canEdit && (
                    <>
                      <button
                        className="ghost small"
                        disabled={busy}
                        onClick={() => merge(hint, 'left')}
                        title={`Перенести кабинеты ${hint.left_code} в ${hint.right_code}`}
                      >
                        ← в {hint.right_code}
                      </button>
                      <button
                        className="ghost small"
                        disabled={busy}
                        onClick={() => merge(hint, 'right')}
                        title={`Перенести кабинеты ${hint.right_code} в ${hint.left_code}`}
                      >
                        → в {hint.left_code}
                      </button>
                    </>
                  )}
                </td>
              </tr>
            ))}
            {hints.length === 0 && (
              <tr>
                <td className="empty" colSpan={6}>
                  Похожих товаров не нашлось
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <p className="footnote">
        Один и тот же физический товар в нескольких кабинетах — это один товар и несколько
        пар (магазин, товар): остаток и продажи считаются по кабинетам, а спрос и закупка —
        по товару целиком. Это допущение: заказчик может считать такие позиции разными SKU,
        и тогда поменяется правило сложения итогов (вопрос Q-4).
      </p>
    </>
  );
}
