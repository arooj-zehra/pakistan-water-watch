import { useEffect, useState } from 'react';

// Static files written by `python -m pipeline.export_static` come first: they load
// instantly and survive a sleeping API server. The API is the fallback, and only
// used when VITE_API_URL is set (e.g. https://pakistan-water-watch-api.onrender.com).
export const API_URL = (import.meta.env.VITE_API_URL || '').replace(/\/$/, '');

const cache = new Map();

async function fetchJSON(url) {
  const r = await fetch(url, { cache: 'no-cache' });
  if (!r.ok) throw new Error(`${url} answered ${r.status}`);
  const type = r.headers.get('content-type') || '';
  if (!type.includes('json')) throw new Error(`${url} did not return JSON`);
  return r.json();
}

export function load(name) {
  if (!cache.has(name)) {
    const p = fetchJSON(`/data/${name}.json`).catch((staticErr) => {
      if (!API_URL) {
        throw new Error(
          'The data files are missing. Run "python -m pipeline.export_static", then reload this page.'
        );
      }
      return fetchJSON(`${API_URL}/api/bundle/${name}`).catch(() => {
        throw new Error(`Couldn't load ${name} from the data files or the API (${staticErr.message}).`);
      });
    });
    p.catch(() => cache.delete(name)); // let a retry try again
    cache.set(name, p);
  }
  return cache.get(name);
}

// A name ending in '?' is optional: it resolves to null instead of failing
// (forecast files only exist once the daily job has run the model).
const loadMaybe = (name) => (name.endsWith('?') ? load(name.slice(0, -1)).catch(() => null) : load(name));

export function useData(...names) {
  const key = names.join(',');
  const [state, setState] = useState({ data: null, error: null, loading: true });
  useEffect(() => {
    let alive = true;
    setState({ data: null, error: null, loading: true });
    Promise.all(names.map(loadMaybe))
      .then((values) => alive && setState({ data: values, error: null, loading: false }))
      .catch((error) => alive && setState({ data: null, error, loading: false }));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return state;
}

// history.json stores columns; most views want rows
export function historyRows(history, station) {
  const s = history.stations[station];
  if (!s) return [];
  return s.date.map((date, i) => ({
    date,
    level_ft: s.level_ft[i],
    inflow_cusecs: s.inflow_cusecs[i],
    outflow_cusecs: s.outflow_cusecs[i],
    live_storage_maf: s.live_storage_maf[i],
    source: s.source[i],
  }));
}
