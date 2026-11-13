import type { Kpi } from '../types';

const numberFormat = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 });

function weekLabel(key: string | null): string {
  if (!key) return '—';
  const [year, week] = key.split('-W');
  return `W${week} ${year}`;
}

export function KpiBar({ kpi }: { kpi: Kpi }) {
  const delta = Math.round(kpi.plan_delta_units);
  return (
    <div className="kpi-bar">
      <div className="kpi">
        <div className="kpi-label">Последняя завершённая</div>
        <div className="kpi-value">{weekLabel(kpi.last_closed_week)}</div>
      </div>
      <div className="kpi">
        <div className="kpi-label">Выполнение плана · {kpi.plan_window_weeks} нед.</div>
        <div className="kpi-value">
          {kpi.plan_completion_percent === null ? '—' : `${kpi.plan_completion_percent}%`}
          <span className={`kpi-delta ${delta < 0 ? 'negative' : 'positive'}`}>
            {delta >= 0 ? '+' : '−'}
            {numberFormat.format(Math.abs(delta))} шт.
          </span>
        </div>
      </div>
      <div className="kpi">
        <div className="kpi-label">Новый прогноз {weekLabel(kpi.next_week)}</div>
        <div className="kpi-value">{numberFormat.format(Math.round(kpi.next_week_forecast))} шт.</div>
      </div>
      <div className="kpi">
        <div className="kpi-label">Риски</div>
        <div className="kpi-value">
          {kpi.short_skus} <span className="kpi-unit">SHORT</span>
          <span className="kpi-delta">{kpi.over_skus} OVER</span>
        </div>
      </div>
    </div>
  );
}
