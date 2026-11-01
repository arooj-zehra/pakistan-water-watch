import { useMemo, useState } from 'react';
import { ErrorBox, Loading } from '../components/Status.jsx';
import { API_URL, historyRows, useData } from '../data.js';
import { cusecs, day, feet, maf } from '../format.js';

const PAGE = 50;
const SOURCE_NAMES = {
  app: 'APP', urdupoint: 'UrduPoint', pakobserver: 'Pakistan Observer', customstoday: 'Customs Today',
  pmd: 'PMD', dailytimes: 'Daily Times', tribune: 'Express Tribune', profit: 'Profit', pakistantoday: 'Pakistan Today',
  energyupdate: 'Energy Update', dailyausaf: 'Daily Ausaf', nation: 'The Nation', wapda_press: 'WAPDA',
};

function toCSV(rows) {
  const head = 'date,station,level_ft,inflow_cusecs,outflow_cusecs,live_storage_maf,source';
  const cell = (v) => (v == null ? '' : v);
  return [head, ...rows.map((r) => [r.date, r.station, r.level_ft, r.inflow_cusecs, r.outflow_cusecs, r.live_storage_maf, r.source].map(cell).join(','))].join('\n');
}

export default function Data() {
  const { data, error, loading } = useData('history');
  const [station, setStation] = useState('all');
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const [shown, setShown] = useState(PAGE);

  const rows = useMemo(() => {
    if (!data) return [];
    const ids = station === 'all' ? ['tarbela', 'mangla'] : [station];
    return ids
      .flatMap((id) => historyRows(data[0], id).map((r) => ({ ...r, station: id })))
      .filter((r) => (!from || r.date >= from) && (!to || r.date <= to))
      .sort((a, b) => (a.date === b.date ? a.station.localeCompare(b.station) : b.date.localeCompare(a.date)));
  }, [data, station, from, to]);

  if (loading) return <Loading what="the data" />;
  if (error) return <ErrorBox error={error} />;

  const download = () => {
    const blob = new Blob([toCSV(rows)], { type: 'text/csv' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `pakistan-water-watch-${station}.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  return (
    <>
      <h1 className="page-title">Daily figures</h1>
      <p className="lede">
        Every day we have, newest first. Download the rows you've filtered as CSV
        {API_URL ? <>, or use the <a href={`${API_URL}/docs`}>API</a></> : null}.
      </p>
      <div className="controls">
        <label className="field">
          Dam
          <select value={station} onChange={(e) => { setStation(e.target.value); setShown(PAGE); }}>
            <option value="all">Both dams</option>
            <option value="tarbela">Tarbela</option>
            <option value="mangla">Mangla</option>
          </select>
        </label>
        <label className="field">
          From
          <input type="date" value={from} onChange={(e) => { setFrom(e.target.value); setShown(PAGE); }} />
        </label>
        <label className="field">
          To
          <input type="date" value={to} onChange={(e) => { setTo(e.target.value); setShown(PAGE); }} />
        </label>
        <button type="button" className="button-primary" onClick={download} disabled={!rows.length}>
          Download {rows.length.toLocaleString('en-US')} rows as CSV
        </button>
      </div>
      {rows.length === 0 ? (
        <p className="notice">No figures between those dates. Widen the range or clear the dates.</p>
      ) : (
        <>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th scope="col">Date</th>
                  <th scope="col" className="text">Dam</th>
                  <th scope="col">Level (ft)</th>
                  <th scope="col">Inflow (cusecs)</th>
                  <th scope="col">Outflow (cusecs)</th>
                  <th scope="col">Live storage (MAF)</th>
                  <th scope="col" className="text">Source</th>
                </tr>
              </thead>
              <tbody>
                {rows.slice(0, shown).map((r) => (
                  <tr key={`${r.date}-${r.station}`}>
                    <td>{day(r.date)}</td>
                    <td className="text">{r.station === 'tarbela' ? 'Tarbela' : 'Mangla'}</td>
                    <td>{feet(r.level_ft)}</td>
                    <td>{cusecs(r.inflow_cusecs)}</td>
                    <td>{cusecs(r.outflow_cusecs)}</td>
                    <td>{maf(r.live_storage_maf)}</td>
                    <td className="text">{SOURCE_NAMES[r.source] ?? r.source}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {shown < rows.length && (
            <p style={{ marginTop: 16 }}>
              <button type="button" className="button-quiet" onClick={() => setShown(shown + PAGE)}>
                Show {Math.min(PAGE, rows.length - shown)} more of {(rows.length - shown).toLocaleString('en-US')} remaining
              </button>
            </p>
          )}
        </>
      )}
    </>
  );
}
