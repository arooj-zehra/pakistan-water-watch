import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Area, CartesianGrid, ComposedChart, Line, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts';
import { ErrorBox, Loading } from '../components/Status.jsx';
import { useData } from '../data.js';
import { cusecs, day, stamp } from '../format.js';

const REPO = 'https://github.com/arooj-zehra/pakistan-water-watch';
const pct = (v) => (v == null ? '–' : `${Math.round(v * 100)}%`);
const better = (v) => (v == null ? '–' : v >= 0 ? `${Math.round(v * 100)}% better` : `${Math.round(-v * 100)}% worse`);

function Tip({ active, payload }) {
  if (!active || !payload?.length) return null;
  const p = payload[0].payload;
  return (
    <div style={{ background: 'var(--paper-raised)', border: '1px solid var(--rule)', padding: '6px 10px', fontSize: 13 }}>
      <strong>{day(p.target_date)}</strong>
      <div>reported: {cusecs(p.actual)} cusecs</div>
      <div>forecast: {cusecs(p.p50)} (issued {day(p.issued_on, false)})</div>
      <div style={{ color: 'var(--ink-soft)' }}>no change would have said {cusecs(p.persistence)}</div>
    </div>
  );
}

function Monitor({ m }) {
  if (!m) return null;
  return (
    <>
      <h2>Health checks</h2>
      {m.alerts.map((a) => <p key={a} className="warning">{a}</p>)}
      {m.watch.length > 0 && (
        <p className="notice">Worth watching: conditions in the last {m.window_days} days are in the unusual end of past years
          ({m.watch.join('; ')}).</p>
      )}
      {m.status === 'ok' && (
        <p>All checks normal: the ranges are catching enough outcomes, the forecasts beat assuming no change, and
          recent weather and flows look like years the model has seen.</p>
      )}
      <p className="updated">Checked {stamp(m.generated_at)}. An alert opens an issue on GitHub.</p>
    </>
  );
}

export default function Accuracy() {
  const { data, error, loading } = useData('accuracy?', 'monitor?', 'forecast?');
  const [station, setStation] = useState('tarbela');
  const [h, setH] = useState(1);
  const recent = useMemo(
    () => (data?.[0]?.recent ?? []).filter((r) => r.station === station && r.h === h)
      .map((r) => ({ ...r, t: Date.parse(r.target_date), band: r.p05 != null ? [r.p05, r.p95] : null })),
    [data, station, h],
  );
  if (loading) return <Loading what="the scores" />;
  if (error) return <ErrorBox error={error} />;
  const [acc, mon, fc] = data;
  const horizons = fc?.horizons ?? [1, 3, 5, 7, 10];

  return (
    <div className="prose">
      <h1 className="page-title">How the live forecasts are doing</h1>
      <p className="lede">
        Each forecast is saved before the day it describes and can't be changed afterwards. When that day's figures are
        published, it is scored against them and against the simplest guess: no change from the day it was made.
      </p>

      {!acc || acc.scored === 0 ? (
        <p className="notice">
          {acc?.forecasts_issued
            ? <>{acc.forecasts_issued} forecasts stored since {day(acc.first_issued)}. The first scores appear once their days are reported, usually the next morning.</>
            : <>No forecasts stored yet. They start with the next daily update.</>}
        </p>
      ) : (
        <>
          <p>
            {acc.scored} of {acc.forecasts_issued} forecasts scored since {day(acc.first_issued)}. Overall the most likely
            figure was off by {cusecs(acc.overall.mae_model)} cusecs on average, against {cusecs(acc.overall.mae_persistence)} for
            no change, and the 9 in 10 range caught {pct(acc.overall.coverage90)} of real values.
          </p>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th className="text">Days ahead</th><th>Scored</th><th>Average miss</th><th>No change misses by</th>
                  <th>Live result</th><th>9 in 10 caught</th><th>In testing</th>
                </tr>
              </thead>
              <tbody>
                {horizons.map((k) => {
                  const t = acc.by_horizon[k] ?? { n: 0 };
                  const b = acc.backtest?.[k] ?? {};
                  return (
                    <tr key={k}>
                      <td className="text">{k}</td>
                      <td>{t.n}</td>
                      <td>{t.n ? cusecs(t.mae_model) : '–'}</td>
                      <td>{t.n ? cusecs(t.mae_persistence) : '–'}</td>
                      <td>{t.n ? better(t.skill_vs_persistence) : '–'}</td>
                      <td>{t.n ? pct(t.coverage90) : '–'}</td>
                      <td>{better(b.skill_vs_persistence)}, {pct(b.coverage90)} caught</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <p className="chart-note">Averages are in cusecs. Early numbers rest on few forecasts and will move a lot; the
            "in testing" column is what four past years suggest to expect.</p>

          <h2>Recent forecasts against what came in</h2>
          <div className="controls">
            <div className="field">
              Dam
              <div className="segmented" role="group" aria-label="Dam">
                {[['tarbela', 'Tarbela'], ['mangla', 'Mangla']].map(([id, label]) => (
                  <button key={id} type="button" aria-pressed={station === id} onClick={() => setStation(id)}>{label}</button>
                ))}
              </div>
            </div>
            <div className="field">
              Days ahead
              <div className="segmented" role="group" aria-label="Days ahead">
                {horizons.map((k) => (
                  <button key={k} type="button" aria-pressed={h === k} onClick={() => setH(k)}>{k}</button>
                ))}
              </div>
            </div>
          </div>
          {recent.length === 0 ? (
            <p className="notice">No scored {h}-day forecasts for this dam yet.</p>
          ) : (
            <div className="chart-wrap" style={{ height: 280 }} role="img"
              aria-label={`${h}-day forecasts for ${station} against reported inflow`}>
              <ResponsiveContainer>
                <ComposedChart data={recent} margin={{ top: 8, right: 8, bottom: 0, left: 4 }}>
                  <CartesianGrid stroke="var(--rule)" vertical={false} />
                  <XAxis dataKey="t" type="number" scale="time" domain={['dataMin', 'dataMax']} stroke="var(--ink-soft)" fontSize={12}
                    tickFormatter={(t) => day(new Date(t).toISOString(), false)} minTickGap={28} />
                  <YAxis stroke="var(--ink-soft)" fontSize={12} width={44} tickFormatter={(v) => `${Math.round(v / 1000)}k`} />
                  <Tooltip content={<Tip />} />
                  <Area dataKey="band" stroke="none" fill="var(--water-soft)" isAnimationActive={false} />
                  <Line dataKey="p50" stroke="var(--water)" strokeDasharray="5 3" strokeWidth={1.5} dot={false} isAnimationActive={false} />
                  <Line dataKey="persistence" stroke="var(--silt)" strokeWidth={1} dot={false} isAnimationActive={false} />
                  <Line dataKey="actual" stroke="var(--ink)" strokeWidth={0} connectNulls={false} dot={{ r: 2.6, fill: 'var(--ink)', stroke: 'none' }} isAnimationActive={false} />
                </ComposedChart>
              </ResponsiveContainer>
            </div>
          )}
          <ul className="legend" aria-hidden="true">
            <li><span style={{ background: 'var(--ink)', height: 6, width: 6, borderRadius: 3 }} />reported</li>
            <li><span style={{ background: 'none', borderTop: '2px dashed var(--water)', height: 0 }} />forecast</li>
            <li><span style={{ background: 'var(--water-soft)' }} />9 in 10 range</li>
            <li><span style={{ background: 'var(--silt)', height: 2 }} />no change</li>
          </ul>
        </>
      )}

      <Monitor m={mon} />

      <h2>Checking this yourself</h2>
      <p>
        Every forecast is appended to <a href={`${REPO}/blob/main/data/forecasts/issued.csv`}>data/forecasts/issued.csv</a> on
        the day it is made. The file's <a href={`${REPO}/commits/main/data/forecasts/issued.csv`}>commit history</a> shows
        when each line was added, so you can confirm none was written after the fact. How the model was built and
        tested is in the <a href={`${REPO}/blob/main/docs/MODEL_CARD.md`}>model card</a>; today's forecast is on
        the <Link to="/forecast">Forecast</Link> page.
      </p>
    </div>
  );
}
