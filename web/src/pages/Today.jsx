import { Area, ComposedChart, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import Gauge from '../components/Gauge.jsx';
import { ErrorBox, Loading } from '../components/Status.jsx';
import { historyRows, useData } from '../data.js';
import { change, cusecs, day, dayOfYear, feet, maf, whole } from '../format.js';

const NEAR_DEAD_FT = 10;
const STALE_DAYS = 2;
const SPARK_DAYS = 60;

function Reading({ label, value, unit, delta }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>
        {value}
        {value !== '–' && <small>{unit}</small>}
        {delta && <span className={`delta delta-${delta.dir}`}>{delta.text} since the day before</span>}
      </dd>
    </div>
  );
}

// Last 60 days of inflow against the normal range for those days of the year
function recentVsNormal(rows, clim, endIso) {
  const byDate = new Map(rows.map((r) => [r.date, r.inflow_cusecs]));
  const [y, m, d] = endIso.split('-').map(Number);
  const out = [];
  for (let i = SPARK_DAYS - 1; i >= 0; i--) {
    const iso = new Date(Date.UTC(y, m - 1, d - i)).toISOString().slice(0, 10);
    const k = dayOfYear(iso) - 1;
    out.push({
      date: iso,
      inflow: byDate.get(iso) ?? null,
      band: clim.p10[k] != null ? [clim.p10[k], clim.p90[k]] : null,
      median: clim.p50[k],
    });
  }
  return out;
}

function SparkTip({ active, payload }) {
  if (!active || !payload?.length) return null;
  const p = payload[0].payload;
  return (
    <div style={{ background: 'var(--paper-raised)', border: '1px solid var(--rule)', padding: '6px 10px', fontSize: 13 }}>
      <strong>{day(p.date)}</strong>
      <div>{p.inflow != null ? `${cusecs(p.inflow)} cusecs` : 'no report that day'}</div>
      {p.median != null && <div style={{ color: 'var(--ink-soft)' }}>usual: {cusecs(p.median)} cusecs</div>}
    </div>
  );
}

function Dam({ s, rows, clim }) {
  const prev = s.previous;
  const prevIsYesterday = prev && (Date.parse(s.date) - Date.parse(prev.date)) / 864e5 === 1;
  const p = prevIsYesterday ? prev : {};
  const above = s.dead_level_ft != null && s.level_ft != null ? s.level_ft - s.dead_level_ft : null;
  const usable = above != null ? Math.round((above / (s.max_level_ft - s.dead_level_ft)) * 100) : null;
  const spark = clim ? recentVsNormal(rows, clim, s.date) : [];

  return (
    <section className="dam" aria-labelledby={`dam-${s.id}`}>
      <div className="dam-head">
        <h2 id={`dam-${s.id}`}>{s.name}</h2>
        <span className="river">{s.river} River</span>
      </div>
      {above != null && above <= NEAR_DEAD_FT && (
        <p className="warning">
          {s.name} is {whole(above)} ft above its dead level of {whole(s.dead_level_ft)} ft. Water below the dead
          level can't be released for irrigation or power.
        </p>
      )}
      {s.days_old > STALE_DAYS && (
        <p className="notice">These are the newest published figures, from {day(s.date)} ({s.days_old} days ago).</p>
      )}
      <div className="dam-body">
        <Gauge level={s.level_ft} dead={s.dead_level_ft} max={s.max_level_ft} name={s.id} />
        <div>
          <div className="level-figure">
            {feet(s.level_ft)}<small>ft</small>
          </div>
          <p className="level-sub">
            {above != null ? `${whole(above)} ft above dead level, ${usable}% of the usable height. ` : ''}
            {change(s.level_ft, p.level_ft, 'ft', 2)?.text
              ? `${change(s.level_ft, p.level_ft, 'ft', 2).text.replace(/^./, (c) => c.toUpperCase())} since the day before.`
              : ''}
          </p>
          <dl className="readings">
            <Reading label="Inflow" value={cusecs(s.inflow_cusecs)} unit="cusecs"
              delta={change(s.inflow_cusecs, p.inflow_cusecs, 'cusecs')} />
            <Reading label="Outflow" value={cusecs(s.outflow_cusecs)} unit="cusecs"
              delta={change(s.outflow_cusecs, p.outflow_cusecs, 'cusecs')} />
            {s.live_storage_maf != null && <Reading label="Live storage" value={maf(s.live_storage_maf)} unit="MAF" />}
          </dl>
        </div>
      </div>
      {spark.length > 0 && (
        <>
          <p className="spark-label">Inflow over the last {SPARK_DAYS} days (dots are reported days), against the usual range for the time of year</p>
          <div style={{ width: '100%', height: 130 }}>
            <ResponsiveContainer>
              <ComposedChart data={spark} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
                <XAxis dataKey="date" hide />
                <YAxis hide domain={[0, 'auto']} />
                <Tooltip content={<SparkTip />} />
                <Area dataKey="band" stroke="none" fill="var(--water-soft)" isAnimationActive={false} />
                <Line dataKey="median" stroke="var(--ink-soft)" strokeDasharray="3 3" dot={false} strokeWidth={1} isAnimationActive={false} />
                <Line dataKey="inflow" stroke="var(--water)" strokeWidth={1.4} connectNulls={false} isAnimationActive={false}
                  dot={{ r: 2.2, fill: 'var(--water)', stroke: 'none' }} activeDot={{ r: 4 }} />
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        </>
      )}
      <p className="dam-foot">
        Figures for {day(s.date)}, 24-hour means to 06:00 PKT. Source: {s.source_name}.
      </p>
    </section>
  );
}

export default function Today() {
  const { data, error, loading } = useData('latest', 'history', 'climatology');
  if (loading) return <Loading />;
  if (error) return <ErrorBox error={error} />;
  const [latest, history, climatology] = data;
  return (
    <>
      <h1 className="page-title">How full are Tarbela and Mangla today?</h1>
      <p className="lede">Water level, inflow and outflow from the newest daily release, drawn on each dam's gauge.</p>
      <div className="dams">
        {latest.stations.map((s) => (
          <Dam key={s.id} s={s} rows={historyRows(history, s.id)}
            clim={climatology.stations[s.id]?.inflow_cusecs} />
        ))}
      </div>
    </>
  );
}
