import type { Status } from '../types';

const LABELS: Record<Status, string> = {
  OK: 'ОК',
  SHORT: 'Shortage',
  OVER: 'Overstock',
};

export function StatusBadge({ status }: { status: Status }) {
  return <span className={`badge status ${status.toLowerCase()}`}>{LABELS[status]}</span>;
}
