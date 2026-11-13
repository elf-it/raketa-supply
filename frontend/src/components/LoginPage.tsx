import { useState } from 'react';

import { api } from '../api';
import type { CurrentUser } from '../types';

interface Props {
  onSignedIn: (user: CurrentUser) => void;
  /** Сессия истекла в процессе работы, а не просто не начиналась. */
  expired?: boolean;
}

export function LoginPage({ onSignedIn, expired }: Props) {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    try {
      onSignedIn(await api.login(email, password));
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-screen">
      <form className="login-card" onSubmit={submit}>
        <h1>Веб-система снабжения</h1>
        <p className="muted">Прогноз продаж, остатки и планирование закупок</p>

        {expired && !error && (
          <p className="inline-warning">Сессия истекла, войдите заново</p>
        )}
        {error && <p className="inline-warning">{error}</p>}

        <label className="login-field">
          Рабочая почта
          <input
            type="email"
            autoComplete="username"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            required
            autoFocus
          />
        </label>

        <label className="login-field">
          Пароль
          <input
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            required
          />
        </label>

        <button className="primary" type="submit" disabled={busy}>
          {busy ? 'Вхожу…' : 'Войти'}
        </button>

        <p className="login-note">
          Учётные записи заводит администратор. Демонстрационные — в{' '}
          <code>backend/app/seed.py</code>.
        </p>
      </form>
    </div>
  );
}
