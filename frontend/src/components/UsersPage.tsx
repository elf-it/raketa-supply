import { useCallback, useEffect, useMemo, useState } from 'react';

import { api } from '../api';
import type { CurrentUser, Role, User } from '../types';

/** Роли по возрастанию прав: каждая следующая включает предыдущую. */
const ROLE_OPTIONS: { id: Role; label: string; hint: string }[] = [
  { id: 'viewer', label: 'Просмотр', hint: 'видит матрицу, закупку и журнал, ничего не меняет' },
  {
    id: 'editor',
    label: 'Редактирование',
    hint: 'правит прогноз, собирает заказы, запускает синхронизацию',
  },
  { id: 'admin', label: 'Администратор', hint: 'то же плюс управление пользователями' },
];

/** Та же граница, что в схеме на сервере: короче — просто не примут. */
const MIN_PASSWORD = 8;

const dateFormat = new Intl.DateTimeFormat('ru-RU', {
  day: 'numeric',
  month: 'long',
  year: 'numeric',
});

/** Склонение по числу: 1 запись, 2 записи, 5 записей. */
function plural(count: number, one: string, few: string, many: string): string {
  const tail = count % 100;
  if (tail >= 11 && tail <= 14) return many;
  const last = count % 10;
  if (last === 1) return one;
  if (last >= 2 && last <= 4) return few;
  return many;
}

/** Дата заведения — справочная подпись, и кривое значение не должно ронять экран. */
function createdOn(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : dateFormat.format(date);
}

function roleLabel(role: Role): string {
  return ROLE_OPTIONS.find((option) => option.id === role)?.label ?? role;
}

interface Notice {
  kind: 'ok' | 'bad';
  text: string;
}

interface Props {
  /** Тот, кто вошёл: свою строку нельзя отключить и нельзя себе сменить роль. */
  me: CurrentUser;
  /** Правка собственного имени должна доехать до шапки приложения. */
  onMeUpdated: (user: CurrentUser) => void;
  onError: (message: string | null) => void;
}

/**
 * Экран управления пользователями (задача G-7). Доступен только администратору:
 * вкладка не показывается остальным, а ручки `/auth/users` закрыты ролью на сервере.
 *
 * Учётные записи не удаляются, а отключаются. Удаление оборвало бы журнал
 * корректировок: правки подписаны именем автора, и запись «кто это сделал»
 * должна пережить увольнение. Отключённый не входит в систему, и уже выданная
 * ему кука перестаёт действовать на следующем же запросе.
 */
export function UsersPage({ me, onMeUpdated, onError }: Props) {
  const [rows, setRows] = useState<User[]>([]);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [busy, setBusy] = useState<number | null>(null);

  const [email, setEmail] = useState('');
  const [fullName, setFullName] = useState('');
  const [role, setRole] = useState<Role>('viewer');
  const [password, setPassword] = useState('');
  const [creating, setCreating] = useState(false);

  //: Для какой строки открыто поле нового пароля.
  const [passwordFor, setPasswordFor] = useState<number | null>(null);
  const [newPassword, setNewPassword] = useState('');
  /**
   * Роль, которую администратор выбрал сам себе и которая ждёт подтверждения.
   *
   * Сервер такое понижение разрешает — пока администратор не последний, это
   * не запирание. Но экран пользователей закроется в ту же секунду, и вернуть
   * роль придётся просить у коллеги, так что спрашиваем прямо.
   */
  const [stepDown, setStepDown] = useState<Role | null>(null);

  const load = useCallback(async () => {
    try {
      setRows(await api.users());
      onError(null);
    } catch (err) {
      onError((err as Error).message);
    }
  }, [onError]);

  useEffect(() => {
    void load();
  }, [load]);

  const counts = useMemo(() => {
    const active = rows.filter((row) => row.is_active);
    return {
      total: rows.length,
      admins: active.filter((row) => row.role === 'admin').length,
      disabled: rows.length - active.length,
    };
  }, [rows]);

  /**
   * Применяет правку к строке.
   *
   * Правила «последний администратор остаётся» и «свою роль не меняют» живут
   * на сервере, и отказ показывается как есть. Дублировать их здесь — значит
   * однажды разойтись: интерфейс запретит то, что сервер разрешает, или наоборот.
   */
  const apply = async (
    user: User,
    payload: { full_name?: string; role?: Role; is_active?: boolean; password?: string },
    done: string,
  ): Promise<boolean> => {
    setBusy(user.id);
    try {
      const updated = await api.updateUser(user.id, payload);
      setRows((prev) => prev.map((row) => (row.id === updated.id ? updated : row)));
      if (updated.id === me.id) onMeUpdated(updated);
      setNotice({ kind: 'ok', text: done });
      return true;
    } catch (err) {
      setNotice({ kind: 'bad', text: (err as Error).message });
      return false;
    } finally {
      setBusy(null);
    }
  };

  const create = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!email.trim() || !fullName.trim()) {
      setNotice({ kind: 'bad', text: 'Почта и имя обязательны' });
      return;
    }
    if (password.length < MIN_PASSWORD) {
      setNotice({ kind: 'bad', text: `Пароль короче ${MIN_PASSWORD} символов` });
      return;
    }
    setCreating(true);
    try {
      const created = await api.createUser({
        email: email.trim(),
        full_name: fullName.trim(),
        role,
        password,
      });
      setRows((prev) => [...prev, created].sort((a, b) => a.email.localeCompare(b.email)));
      setEmail('');
      setFullName('');
      setPassword('');
      setRole('viewer');
      setNotice({
        kind: 'ok',
        text: `${created.full_name} заведён, роль — ${roleLabel(created.role)}. Пароль передайте лично: восстановить его система не умеет.`,
      });
    } catch (err) {
      setNotice({ kind: 'bad', text: (err as Error).message });
    } finally {
      setCreating(false);
    }
  };

  const savePassword = async (user: User) => {
    if (newPassword.length < MIN_PASSWORD) {
      setNotice({ kind: 'bad', text: `Пароль короче ${MIN_PASSWORD} символов` });
      return;
    }
    const ok = await apply(
      user,
      { password: newPassword },
      `Пароль для ${user.full_name} задан. Старый больше не работает.`,
    );
    if (ok) {
      setPasswordFor(null);
      setNewPassword('');
    }
  };

  return (
    <section className="panel">
      <header className="panel-head">
        <div>
          <h2>Пользователи</h2>
          <p className="muted">
            Роль решает, что человек может: просмотр читает, редактирование правит прогноз
            и заказы, администратор вдобавок заводит людей. Права проверяются на сервере,
            а не прятаньем кнопок.
          </p>
        </div>
        <div className="panel-totals">
          <span className="muted">
            {counts.total}{' '}
            {plural(counts.total, 'учётная запись', 'учётные записи', 'учётных записей')} ·{' '}
            {counts.admins}{' '}
            {plural(counts.admins, 'администратор', 'администратора', 'администраторов')}
            {counts.disabled > 0 && ` · отключено ${counts.disabled}`}
          </span>
        </div>
      </header>

      <form className="user-form" onSubmit={(event) => void create(event)}>
        <label className="inline-field">
          Рабочая почта
          <input
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            placeholder="ivanov@raketa.local"
            autoComplete="off"
          />
        </label>
        <label className="inline-field">
          Имя и должность
          <input
            value={fullName}
            onChange={(event) => setFullName(event.target.value)}
            placeholder="Иван, снабжение"
          />
        </label>
        <label className="inline-field">
          Роль
          <select value={role} onChange={(event) => setRole(event.target.value as Role)}>
            {ROLE_OPTIONS.map((option) => (
              <option key={option.id} value={option.id}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <label className="inline-field">
          Первый пароль
          <input
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            placeholder={`не короче ${MIN_PASSWORD} символов`}
            autoComplete="new-password"
          />
        </label>
        <button className="primary" type="submit" disabled={creating}>
          {creating ? 'Завожу…' : 'Завести'}
        </button>
      </form>

      <p className="hint">
        {ROLE_OPTIONS.map((option) => `${option.label} — ${option.hint}`).join('; ')}.
      </p>

      {notice && (
        <p className={notice.kind === 'bad' ? 'inline-warning' : 'inline-note'}>{notice.text}</p>
      )}

      <div className="matrix-scroll">
        <table className="purchase">
          <thead>
            <tr>
              <th>Сотрудник</th>
              <th>Почта</th>
              <th>Роль</th>
              <th>Доступ</th>
              <th>Заведён</th>
              <th>Пароль</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((user) => {
              const self = user.id === me.id;
              const pending = busy === user.id;
              return (
                <tr key={user.id} className={user.is_active ? undefined : 'muted-row'}>
                  <td>
                    <NameEdit
                      value={user.full_name}
                      disabled={pending}
                      onCommit={(next) =>
                        apply(user, { full_name: next }, `Имя изменено на «${next}»`)
                      }
                    />
                    {self && <span className="badge">это вы</span>}
                  </td>
                  <td>{user.email}</td>
                  <td>
                    <select
                      value={self && stepDown ? stepDown : user.role}
                      disabled={pending}
                      onChange={(event) => {
                        const next = event.target.value as Role;
                        if (self) {
                          // Своя роль — через подтверждение: следующим кадром
                          // этот экран исчезнет вместе с правами.
                          setStepDown(next === user.role ? null : next);
                          return;
                        }
                        void apply(
                          user,
                          { role: next },
                          `${user.full_name}: роль — ${roleLabel(next)}`,
                        );
                      }}
                    >
                      {ROLE_OPTIONS.map((option) => (
                        <option key={option.id} value={option.id}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                    {self && stepDown && (
                      <span className="step-down">
                        <span className="hint danger-text">
                          Вы снимете с себя права администратора и потеряете этот экран.
                          Вернуть роль сможет только другой администратор.
                        </span>
                        <button
                          className="ghost small"
                          disabled={pending}
                          onClick={() =>
                            void apply(
                              user,
                              { role: stepDown },
                              `Ваша роль теперь — ${roleLabel(stepDown)}`,
                            ).then(() => setStepDown(null))
                          }
                        >
                          Да, снять
                        </button>
                        <button className="link" onClick={() => setStepDown(null)}>
                          отмена
                        </button>
                      </span>
                    )}
                  </td>
                  <td>
                    {user.is_active ? (
                      <button
                        className="ghost small"
                        disabled={self || pending}
                        title={self ? 'Нельзя отключить самого себя' : undefined}
                        onClick={() =>
                          void apply(
                            user,
                            { is_active: false },
                            `${user.full_name} отключён: вход закрыт, журнал правок остался`,
                          )
                        }
                      >
                        Отключить
                      </button>
                    ) : (
                      <button
                        className="ghost small"
                        disabled={pending}
                        onClick={() =>
                          void apply(user, { is_active: true }, `${user.full_name} снова может войти`)
                        }
                      >
                        Включить
                      </button>
                    )}
                  </td>
                  <td>{createdOn(user.created_at)}</td>
                  <td>
                    {passwordFor === user.id ? (
                      <span className="password-edit">
                        <input
                          type="password"
                          value={newPassword}
                          autoFocus
                          placeholder={`не короче ${MIN_PASSWORD} символов`}
                          autoComplete="new-password"
                          onChange={(event) => setNewPassword(event.target.value)}
                          onKeyDown={(event) => {
                            if (event.key === 'Enter') void savePassword(user);
                            if (event.key === 'Escape') {
                              setPasswordFor(null);
                              setNewPassword('');
                            }
                          }}
                        />
                        <button
                          className="ghost small"
                          disabled={pending}
                          onClick={() => void savePassword(user)}
                        >
                          Задать
                        </button>
                        <button
                          className="link"
                          onClick={() => {
                            setPasswordFor(null);
                            setNewPassword('');
                          }}
                        >
                          отмена
                        </button>
                      </span>
                    ) : (
                      <button
                        className="link"
                        onClick={() => {
                          setPasswordFor(user.id);
                          setNewPassword('');
                        }}
                      >
                        Задать новый
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
            {rows.length === 0 && (
              <tr>
                <td className="empty" colSpan={6}>
                  Пока никого нет
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <p className="footnote">
        Учётная запись не удаляется, а отключается: журнал корректировок подписан именем
        автора, и эта подпись должна пережить увольнение. Забытый пароль не восстанавливается —
        администратор задаёт новый и передаёт его лично. Себя отключить нельзя, а снять с себя
        права — только с подтверждением и только пока есть второй администратор: последний
        администратор перестать им быть не может, иначе системой некому управлять,
        и починить это из интерфейса уже нельзя.
      </p>
    </section>
  );
}

interface NameEditProps {
  value: string;
  disabled: boolean;
  /** Возвращает `false`, если сервер отказал: тогда в поле возвращается прежнее имя. */
  onCommit: (value: string) => Promise<boolean>;
}

/** Имя правится прямо в строке: отдельная форма ради одного поля — лишний шаг. */
function NameEdit({ value, disabled, onCommit }: NameEditProps) {
  const [draft, setDraft] = useState(value);

  useEffect(() => {
    setDraft(value);
  }, [value]);

  const commit = async () => {
    const next = draft.trim();
    if (next === value.trim()) return;
    if (!next) {
      setDraft(value);
      return;
    }
    if (!(await onCommit(next))) setDraft(value);
  };

  return (
    <input
      className="name-edit"
      value={draft}
      disabled={disabled}
      onChange={(event) => setDraft(event.target.value)}
      onBlur={() => void commit()}
      onKeyDown={(event) => {
        if (event.key === 'Enter') event.currentTarget.blur();
        if (event.key === 'Escape') setDraft(value);
      }}
    />
  );
}
