import { useState } from 'react';

import { api } from '../api';

/** Столько же требует сервер: короче он не примет. */
const MIN_PASSWORD = 8;

interface Props {
  /** Закрыть карточку. */
  onClose: () => void;
}

/**
 * Смена собственного пароля (задача G-8).
 *
 * Прежний пароль спрашивается не для проверки памяти: сессия могла остаться
 * открытой на чужой машине, и без прежнего пароля уведённой куки хватало бы,
 * чтобы забрать учётную запись насовсем.
 *
 * Повтор нового пароля проверяется здесь, а не на сервере: опечатка в поле,
 * которое не видно, иначе обнаружилась бы только при следующем входе.
 */
export function ChangePassword({ onClose }: Props) {
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [repeat, setRepeat] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy) return;
    if (next.length < MIN_PASSWORD) {
      setError(`Новый пароль короче ${MIN_PASSWORD} символов`);
      return;
    }
    if (next !== repeat) {
      setError('Новый пароль и повтор не совпадают');
      return;
    }

    setBusy(true);
    try {
      await api.changeMyPassword(current, next);
      setDone(true);
      setError(null);
      setCurrent('');
      setNext('');
      setRepeat('');
    } catch (err) {
      // Отказ сервера («текущий пароль не подошёл») остаётся в карточке:
      // в общей полосе ошибок наверху его легко не связать с формой.
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (done) {
    return (
      <section className="password-card">
        <p className="inline-note">
          Пароль изменён. Эта вкладка продолжает работать, а другие ваши сессии — если
          вы входили где-то ещё — закрыты.
        </p>
        <button className="primary" onClick={onClose}>
          Готово
        </button>
      </section>
    );
  }

  return (
    <form className="password-card" onSubmit={submit}>
      <h2>Смена пароля</h2>
      {error && <p className="inline-warning">{error}</p>}

      <div className="password-fields">
        <label className="login-field">
          Текущий пароль
          <input
            type="password"
            autoComplete="current-password"
            value={current}
            onChange={(event) => setCurrent(event.target.value)}
            required
            autoFocus
          />
        </label>

        <label className="login-field">
          Новый пароль
          <input
            type="password"
            autoComplete="new-password"
            minLength={MIN_PASSWORD}
            value={next}
            onChange={(event) => setNext(event.target.value)}
            required
          />
        </label>

        <label className="login-field">
          Новый пароль ещё раз
          <input
            type="password"
            autoComplete="new-password"
            value={repeat}
            onChange={(event) => setRepeat(event.target.value)}
            required
          />
        </label>
      </div>

      <div className="password-actions">
        <button className="primary" type="submit" disabled={busy}>
          {busy ? 'Меняю…' : 'Сменить пароль'}
        </button>
        <button type="button" className="link" onClick={onClose}>
          отмена
        </button>
        <span className="muted">
          От {MIN_PASSWORD} символов. Забытый пароль не восстанавливают — новый задаёт
          администратор.
        </span>
      </div>
    </form>
  );
}
