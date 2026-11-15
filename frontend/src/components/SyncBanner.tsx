import type { StoreSync, SyncHealth } from '../types';

/**
 * Плашка о том, что данные кабинета отстали (задача G-4).
 *
 * Сбой загрузки — самая дорогая из незаметных поломок: экран продолжает
 * показывать числа, они просто не те, и по ним собирают заказ. Почты
 * и мессенджера у системы нет (вопрос Q-16), поэтому единственный способ
 * сказать об этом человеку — сказать это на экране, а не ждать, пока он
 * заглянет в журнал синхронизаций.
 *
 * Кабинет, который ни разу не загружался, здесь не показывается: на свежем
 * развёртывании кабинеты заводят раньше первой загрузки, и плашка «никогда
 * не загружался» висела бы всё время настройки.
 */

const when = new Intl.DateTimeFormat('ru-RU', {
  day: '2-digit',
  month: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
});

function moment(iso: string | null): string {
  if (!iso) return 'никогда';
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? 'неизвестно когда' : when.format(date);
}

function names(rows: StoreSync[]): string {
  return rows.map((row) => row.store_name).join(', ');
}

interface Props {
  health: SyncHealth | null;
}

export function SyncBanner({ health }: Props) {
  if (!health || health.status === 'ok') return null;

  const failed = health.stores.filter((row) => row.status === 'failed');
  const stale = health.stores.filter((row) => row.status === 'stale');
  if (failed.length === 0 && stale.length === 0) return null;

  return (
    <div className="sync-warning">
      <b>Данные могут быть не те.</b>{' '}
      {failed.length > 0 && (
        <>
          Загрузка не удалась: {names(failed)} (последняя попытка{' '}
          {moment(failed[0].finished_at)}
          {failed[0].message ? `, ${failed[0].message}` : ''}).{' '}
        </>
      )}
      {stale.length > 0 && (
        <>
          Не обновлялось дольше {health.stale_after_hours} ч: {names(stale)} (последняя
          загрузка {moment(stale[0].finished_at)}).{' '}
        </>
      )}
      Прогноз, остатки и закупка посчитаны по тому, что есть. Кнопка
      «Синхронизировать WB» в шапке запускает загрузку заново; подробности прогонов —
      в журнале синхронизаций.
    </div>
  );
}
