import { useState } from 'react';
import { Line, LineChart, ReferenceDot, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { ErrorBox, Loading } from '../components/Status.jsx';
import { useData } from '../data.js';
import { cusecs, day, feet, stamp } from '../format.js';

const REPO = 'https://github.com/arooj-zehra/pakistan-water-watch';
const FIELD = {
  inflow_cusecs: { label: 'inflow', unit: 'cusecs', fmt: cusecs },
  outflow_cusecs: { label: 'outflow', unit: 'cusecs', fmt: cusecs },
  level_ft: { label: 'level', unit: 'ft', fmt: feet },
};
const CHECKS = {
  local: 'out of line with nearby days',
  seasonal: 'out of line once the seasonal cycle is removed',
  isolation: 'unusual combination of changes',
  balance: "lake level doesn't match inflow minus outflow",
};
const KINDS = [
  ['anomaly', 'Suspicious values'],
  ['quarantine', 'Rejected stories'],
  ['conflict', 'Outlets disagree'],
];
const PAGE = 20;
const title = (s) => s.charAt(0).toUpperCase() + s.slice(1);

function Context({ item }) {
  const f = FIELD[item.field];
  const data = item.context.dates.map((d, i) => ({ date: d, v: item.context.values[i] }));
  const flagged = item.kind === 'conflict' ? item.other : item.value;
  return (
    <div style={{ width: '100%', height: 110 }} role="img"
      aria-label={`${f.label} for 15 days either side of ${day(item.date)}`}>
      <ResponsiveContainer>
        <LineChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
          <XAxis dataKey="date" hide />
          <YAxis hide domain={['auto', 'auto']} />
          <Tooltip formatter={(v) => [`${f.fmt(v)} ${f.unit}`, f.label]} labelFormatter={(d) => day(d)} />
          <Line dataKey="v" stroke="var(--water)" strokeWidth={1.3} connectNulls
            dot={{ r: 2, fill: 'var(--water)', stroke: 'none' }} isAnimationActive={false} />
          <ReferenceDot x={item.date} y={flagged} r={5} fill="var(--gauge-red)" stroke="var(--paper)" />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

function Item({ item }) {
  if (item.kind === 'anomaly') {
    const f = FIELD[item.field];
    return (
      <li className="review-item">
        <h3>{title(item.station_id)} {f.label}, {day(item.date)}</h3>
        <p>
          Reported <strong>{f.fmt(item.value)} {f.unit}</strong>
          {item.expected != null && <>; nearby days suggest about {f.fmt(item.expected)}</>}.
        </p>
        <p className="river">{item.checks.length ? item.checks.map((c) => CHECKS[c]).join('; ') : 'far out of line with nearby days'}</p>
        {item.context && <Context item={item} />}
      </li>
    );
  }
  if (item.kind === 'quarantine') {
    const p = item.parsed;
    return (
      <li className="review-item">
        <h3>{p ? `${title(p.station_id)}, ${day(p.date)}` : 'Story'} <span className="river">({p?.source ?? item.source})</span></h3>
        <p>Rejected when loading: {item.reason}.</p>
        {p ? (
          <p className="river">
            As read from the story: level {feet(p.level_ft)} ft, inflow {cusecs(p.inflow_cusecs)} cusecs, outflow{' '}
            {cusecs(p.outflow_cusecs)} cusecs.
          </p>
        ) : <p className="river">{item.raw_text}</p>}
      </li>
    );
  }
  return (
    <li className="review-item">
      <h3>{title(item.station_id)} inflow, {day(item.date)}</h3>
      <p>
        Used <strong>{cusecs(item.kept)}</strong> (<a href={item.kept_link}>{item.kept_source}</a>); another outlet
        reported <strong>{cusecs(item.other)}</strong> (<a href={item.other_link}>{item.other_source}</a>), {Math.round(item.gap * 100)}% apart.
      </p>
      {item.context && <Context item={item} />}
    </li>
  );
}

export default function Review() {
  const { data, error, loading } = useData('review?');
  const [kind, setKind] = useState('anomaly');
  const [shown, setShown] = useState(PAGE);
  if (loading) return <Loading what="the review queue" />;
  if (error) return <ErrorBox error={error} />;
  const [r] = data;
  if (!r) {
    return (
      <div className="prose">
        <h1 className="page-title">Review queue</h1>
        <p className="lede">The queue appears after the next daily update.</p>
      </div>
    );
  }
  const items = r.pending.filter((p) => p.kind === kind);
  const rate = (r.flagged_total / r.station_days) * 1000;
  return (
    <div className="prose">
      <h1 className="page-title">Review queue</h1>
      <p className="lede">
        Values that may be typing errors in a news story, stories the loading rules rejected, and days where two
        outlets reported different figures. Nothing changes until someone decides.
      </p>
      <p>
        The checks queue {r.flagged_total} of {r.station_days.toLocaleString('en-US')} dam-days ({Math.round(rate)} per
        1,000). In testing with planted typos they caught about three in four errors big enough to matter: see
        the <a href={`${REPO}/blob/main/docs/ANOMALY_REPORT.md`}>anomaly report</a>. {r.decided} decision{r.decided === 1 ? '' : 's'} made
        so far, kept in <a href={`${REPO}/blob/main/data/review/decisions.csv`}>data/review/decisions.csv</a>.
      </p>
      <p className="notice">
        To decide, run <code>python -m pipeline.review</code> in a terminal in the project folder. It shows each item with the
        nearby days and saves your answer straight away.
      </p>
      <div className="controls">
        <div className="segmented" role="group" aria-label="What to show">
          {KINDS.map(([k, label]) => (
            <button key={k} type="button" aria-pressed={kind === k} onClick={() => { setKind(k); setShown(PAGE); }}>
              {label} ({r.counts[k]})
            </button>
          ))}
        </div>
      </div>
      {items.length === 0 ? <p>Nothing waiting here.</p> : (
        <ul className="review-list">
          {items.slice(0, shown).map((it) => <Item key={it.ref} item={it} />)}
        </ul>
      )}
      {items.length > shown && (
        <button type="button" className="button-quiet" onClick={() => setShown(shown + PAGE)}>
          Show {Math.min(PAGE, items.length - shown)} more of {items.length - shown}
        </button>
      )}
      <p className="updated">Checked {stamp(r.generated_at)}.</p>
    </div>
  );
}
