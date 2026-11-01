import { Link } from 'react-router-dom';
import {
  Area, CartesianGrid, ComposedChart, Line, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts';
import { ErrorBox, Loading } from '../components/Status.jsx';
import { historyRows, useData } from '../data.js';
import { cusecs, day, stamp } from '../format.js';

const PAST_DAYS = 30;
const NAMES = { tarbela: ['Tarbela', 'Indus'], mangla: ['Mangla', 'Jhelum'] };

function addDays(iso, n) {
  const [y, m, d] = iso.split('-').map(Number);
  return new Date(Date.UTC(y, m - 1, d + n)).toISOString().slice(0, 10);
}

// Observed inflow for the last 30 days, then the forecast fan from today's figure
function fanData(rows, s) {
  const obs = new Map(rows.map((r) => [r.date, r.inflow_cusecs]));
  const fc = new Map(s.forecasts.map((f) => [f.target_date, f]));
  const last = s.forecasts.at(-1).target_date;
  const out = [];
  for (let iso = addDays(s.issue_date, -PAST_DAYS + 1); iso <= last; iso = addDays(iso, 1)) {
    const f = fc.get(iso);
    const p = { date: iso, observed: iso <= s.issue_date ? obs.get(iso) ?? null : null };
    if (iso === s.issue_date) {
      const q = s.inflow_today;
      Object.assign(p, { p50: q, band90: [q, q], band50: [q, q] });
    } else if (f) {
      Object.assign(p, { p50: f.p50, band90: [f.p05, f.p95], band50: [f.p25, f.p75], h: f.h });
    }
    out.push(p);
  }
  return out;
}

function Tip({ active, payload }) {
  if (!active || !payload?.length) return null;
  const p = payload[0].payload;
  return (
    <div style={{ background: 'var(--paper-raised)', border: '1px solid var(--rule)', padding: '6px 10px', fontSize: 13 }}>
      <strong>{day(p.date)}</strong>
      {p.observed != null && <div>reported: {cusecs(p.observed)} cusecs</div>}
      {p.h && (
        <>
          <div>forecast ({p.h} day{p.h > 1 ? 's' : ''} ahead): {cusecs(p.p50)} cusecs</div>
          <div style={{ color: 'var(--ink-soft)' }}>9 in 10 range: {cusecs(p.band90[0])} to {cusecs(p.band90[1])}</div>
        </>
      )}
    </div>
  );
}

function DamForecast({ sid, s, rows }) {
  const [name, river] = NAMES[sid];
  const data = fanData(rows, s);
  const hi = Math.max(...data.flatMap((d) => [d.observed, d.band90?.[1]]).filter((v) => v != null));
  const step = hi > 100000 ? 50000 : hi > 20000 ? 10000 : 2000;
  return (
    <section className="dam" aria-labelledby={`fc-${sid}`}>
      <div className="dam-head">
        <h2 id={`fc-${sid}`}>{name}</h2>
        <span className="river">{river} River</span>
      </div>
      {s.warnings.map((w) => <p key={w} className="notice">{w}</p>)}
      <p className="level-sub">
        Inflow on {day(s.issue_date)}: <strong>{cusecs(s.inflow_today)} cusecs</strong>
        {s.observed_today ? '' : ' (filled from the days either side; not reported that day)'}.
      </p>
      <div style={{ width: '100%', height: 240 }} role="img"
        aria-label={`${name} inflow for the last ${PAST_DAYS} days and the forecast for the next 10 days`}>
        <ResponsiveContainer>
          <ComposedChart data={data} margin={{ top: 6, right: 6, bottom: 0, left: 4 }}>
            <CartesianGrid stroke="var(--rule)" vertical={false} />
            <XAxis dataKey="date" stroke="var(--ink-soft)" fontSize={12} interval="preserveStartEnd"
              tickFormatter={(d) => day(d, false)} minTickGap={28} />
            <YAxis stroke="var(--ink-soft)" fontSize={12} width={44} domain={[0, Math.ceil((hi * 1.05) / step) * step]}
              tickFormatter={(v) => `${Math.round(v / 1000)}k`} />
            <Tooltip content={<Tip />} />
            <ReferenceLine x={s.issue_date} stroke="var(--ink-soft)" strokeDasharray="2 3" />
            <Area dataKey="band90" connectNulls stroke="none" fill="var(--water-soft)" isAnimationActive={false} />
            <Area dataKey="band50" connectNulls stroke="none" fill="var(--water-mid)" isAnimationActive={false} />
            <Line dataKey="p50" connectNulls stroke="var(--water)" strokeWidth={1.6} strokeDasharray="5 3"
              dot={{ r: 2.6, fill: 'var(--water)', stroke: 'none' }} isAnimationActive={false} />
            <Line dataKey="observed" stroke="var(--ink)" strokeWidth={1.3} connectNulls={false}
              dot={{ r: 2, fill: 'var(--ink)', stroke: 'none' }} activeDot={{ r: 4 }} isAnimationActive={false} />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th className="text">Day</th><th>Most likely</th><th>5 in 10 range</th><th>9 in 10 range</th>
            </tr>
          </thead>
          <tbody>
            {s.forecasts.map((f) => (
              <tr key={f.h}>
                <td className="text">{day(f.target_date, false)} <span className="river">+{f.h}</span></td>
                <td>{cusecs(f.p50)}</td>
                <td>{cusecs(f.p25)}–{cusecs(f.p75)}</td>
                <td>{cusecs(f.p05)}–{cusecs(f.p95)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="dam-foot">
        Tomorrow: {s.forecasts[0].explanation.replace('Biggest influences: ', 'biggest influences are ')}.
        In {s.forecasts.at(-1).h} days: {s.forecasts.at(-1).explanation.replace('Biggest influences: ', 'biggest influences are ')}.
      </p>
    </section>
  );
}

export default function Forecast() {
  const { data, error, loading } = useData('forecast?', 'history');
  if (loading) return <Loading what="the forecasts" />;
  if (error) return <ErrorBox error={error} />;
  const [fc, history] = data;
  if (!fc) {
    return (
      <div className="prose">
        <h1 className="page-title">Inflow forecasts</h1>
        <p className="lede">The first forecasts appear after the next daily update. Until then, see the <Link to="/history">History</Link> page.</p>
      </div>
    );
  }
  const hs = fc.horizons;
  const skill = hs.map((h) => fc.skill?.[h]).filter((v) => v != null);
  const cov = hs.map((h) => fc.coverage90?.[h]).filter((v) => v != null);
  const pct = (v) => `${Math.round(v * 100)}%`;
  return (
    <>
      <h1 className="page-title">Inflow for the next 10 days</h1>
      <p className="lede">
        The dashed line is the most likely inflow. The pale band should contain the real figure 9 times in 10,
        and the darker band 5 times in 10.
      </p>
      <ul className="legend" aria-hidden="true">
        <li><span style={{ background: 'var(--ink)', height: 3 }} />reported inflow</li>
        <li><span style={{ background: 'none', borderTop: '2px dashed var(--water)', height: 0 }} />forecast</li>
        <li><span style={{ background: 'var(--water-soft)' }} />9 in 10 range</li>
        <li><span style={{ background: 'var(--water-mid)' }} />5 in 10 range</li>
      </ul>
      <div className="dams">
        {Object.entries(fc.stations).map(([sid, s]) => (
          <DamForecast key={sid} sid={sid} s={s} rows={historyRows(history, sid)} />
        ))}
      </div>
      <div className="prose">
        <h2>How much to trust these</h2>
        <p>
          Tested on four past years it had never seen, the model's forecasts were {pct(Math.min(...skill))} to{' '}
          {pct(Math.max(...skill))} closer to the real inflow than assuming no change, and its 9 in 10 ranges
          caught {pct(Math.min(...cov))} to {pct(Math.max(...cov))} of real values. Every live forecast is stored
          before its day and scored when the figures come in: see <Link to="/accuracy">how the live forecasts are doing</Link>.
        </p>
        <p>
          Forecasts use the newest inflow and lake level, how they have been changing, the time of year, and
          catchment weather (warmth that melts snow and ice, rain and snowfall) up to the day before. They know
          nothing about weather forecasts yet, which is why ranges widen quickly after a few days.
        </p>
        <p className="updated">Model {fc.model_version}, issued {stamp(fc.issued_at)}.</p>
      </div>
    </>
  );
}
