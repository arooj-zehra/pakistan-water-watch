import { useMemo, useState } from 'react';
import {
  Area, CartesianGrid, ComposedChart, Line, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts';
import { ErrorBox, Loading } from '../components/Status.jsx';
import { historyRows, useData } from '../data.js';
import { MONTHS, MONTH_STARTS, cusecs, dayOfYear, feet } from '../format.js';

const MEASURES = {
  inflow_cusecs: { label: 'Inflow', unit: 'cusecs', fmt: cusecs },
  level_ft: { label: 'Level', unit: 'ft', fmt: feet },
};

function doyLabel(doy) {
  const d = new Date(Date.UTC(2025, 0, doy)); // a non-leap year, labels only
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`;
}

function Tip({ active, payload, measure, year, compare }) {
  if (!active || !payload?.length) return null;
  const p = payload[0].payload;
  const m = MEASURES[measure];
  const line = (label, v) => <div>{label}: {v != null ? `${m.fmt(v)} ${m.unit}` : 'no report'}</div>;
  return (
    <div style={{ background: 'var(--paper-raised)', border: '1px solid var(--rule)', padding: '6px 10px', fontSize: 13 }}>
      <strong>{doyLabel(p.doy)}</strong>
      {line(year, p.current)}
      {compare && line(compare, p.compare)}
      {p.median != null && <div style={{ color: 'var(--ink-soft)' }}>usual: {m.fmt(p.median)} {m.unit}</div>}
    </div>
  );
}

export default function History() {
  const { data, error, loading } = useData('history', 'climatology');
  const [station, setStation] = useState('tarbela');
  const [measure, setMeasure] = useState('inflow_cusecs');
  const [compare, setCompare] = useState('');

  const rows = useMemo(() => (data ? historyRows(data[0], station) : []), [data, station]);
  const years = useMemo(() => [...new Set(rows.map((r) => r.date.slice(0, 4)))].sort().reverse(), [rows]);
  const year = years[0];

  const chart = useMemo(() => {
    if (!data) return [];
    const clim = data[1].stations[station]?.[measure];
    const byYear = (y) => {
      const m = new Map();
      rows.forEach((r) => r.date.startsWith(y) && r[measure] != null && m.set(dayOfYear(r.date), r[measure]));
      return m;
    };
    const cur = byYear(year);
    const cmp = compare ? byYear(compare) : new Map();
    return Array.from({ length: 365 }, (_, i) => {
      const doy = i + 1;
      return {
        doy,
        band: clim?.p10[i] != null ? [clim.p10[i], clim.p90[i]] : null,
        median: clim?.p50[i] ?? null,
        current: cur.get(doy) ?? null,
        compare: cmp.get(doy) ?? null,
      };
    });
  }, [data, rows, station, measure, year, compare]);

  // Axis range from every value drawn (band, this year, the compared year), so nothing is clipped
  const yDomain = useMemo(() => {
    const vals = chart.flatMap((d) => [d.current, d.compare, ...(d.band ?? [])]).filter((v) => v != null);
    if (!vals.length) return [0, 1];
    const hi = Math.max(...vals), lo = Math.min(...vals);
    if (measure === 'inflow_cusecs') return [0, Math.ceil((hi * 1.05) / 50000) * 50000];
    return [Math.floor((lo - 5) / 10) * 10, Math.ceil((hi + 5) / 10) * 10];
  }, [chart, measure]);

  if (loading) return <Loading what="the history" />;
  if (error) return <ErrorBox error={error} />;
  const m = MEASURES[measure];
  const yearsUsed = data[1].stations[station]?.years ?? [];
  const daysThisYear = chart.filter((d) => d.current != null).length;

  return (
    <>
      <h1 className="page-title">This year against the usual range</h1>
      <p className="lede">Each day of {year} compared with the same time of year since {yearsUsed[0]}. Pick another year to lay it on top.</p>

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
          Measure
          <div className="segmented" role="group" aria-label="Measure">
            {Object.entries(MEASURES).map(([id, x]) => (
              <button key={id} type="button" aria-pressed={measure === id} onClick={() => setMeasure(id)}>{x.label}</button>
            ))}
          </div>
        </div>
        <label className="field">
          Compare with
          <select value={compare} onChange={(e) => setCompare(e.target.value)}>
            <option value="">No other year</option>
            {years.slice(1).map((y) => <option key={y} value={y}>{y}</option>)}
          </select>
        </label>
      </div>

      <ul className="legend" aria-hidden="true">
        <li><span style={{ background: 'var(--water)' }} />{year}</li>
        {compare && <li><span style={{ background: 'var(--silt)' }} />{compare}</li>}
        <li><span style={{ background: 'var(--water-soft)' }} />usual range (10th to 90th percentile)</li>
        <li><span style={{ background: 'none', borderTop: '2px dashed var(--ink-soft)', height: 0 }} />median</li>
      </ul>
      <div className="chart-wrap" role="img"
        aria-label={`${m.label} at ${station === 'tarbela' ? 'Tarbela' : 'Mangla'} in ${year}, against the usual range`}>
        <ResponsiveContainer>
          <ComposedChart data={chart} margin={{ top: 8, right: 8, bottom: 0, left: 8 }}>
            <CartesianGrid stroke="var(--rule)" vertical={false} />
            <XAxis dataKey="doy" type="number" domain={[1, 365]} ticks={MONTH_STARTS}
              tickFormatter={(d) => MONTHS[MONTH_STARTS.indexOf(d)]} stroke="var(--ink-soft)" fontSize={12} />
            <YAxis stroke="var(--ink-soft)" fontSize={12} width={64} domain={yDomain} allowDataOverflow={false}
              tickFormatter={(v) => (measure === 'inflow_cusecs' ? `${Math.round(v / 1000)}k` : v)} />
            <Tooltip content={<Tip measure={measure} year={year} compare={compare} />} />
            <Area dataKey="band" stroke="none" fill="var(--water-soft)" isAnimationActive={false} />
            <Line dataKey="median" stroke="var(--ink-soft)" strokeDasharray="4 4" dot={false} strokeWidth={1.2} isAnimationActive={false} />
            {compare && <Line dataKey="compare" stroke="var(--silt)" strokeWidth={1.4} isAnimationActive={false}
              dot={{ r: 1.8, fill: 'var(--silt)', stroke: 'none' }} activeDot={{ r: 4 }} />}
            <Line dataKey="current" stroke="var(--water)" strokeWidth={1.6} isAnimationActive={false}
              dot={{ r: 2, fill: 'var(--water)', stroke: 'none' }} activeDot={{ r: 4 }} />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      <p className="chart-note">
        {m.label} in {m.unit}{measure === 'inflow_cusecs' ? ' (k = thousand)' : ''}. Each dot is a day with published figures;
        breaks in a line are days without any; {year} has {daysThisYear} days so far. The usual range pools each day with the 7 days either
        side, across {yearsUsed.length} years.
      </p>
    </>
  );
}
