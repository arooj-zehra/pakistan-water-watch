const nf0 = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 });
const nf2 = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const nf3 = new Intl.NumberFormat('en-US', { minimumFractionDigits: 3, maximumFractionDigits: 3 });

export const feet = (v) => (v == null ? '–' : nf2.format(v));
export const cusecs = (v) => (v == null ? '–' : nf0.format(v));
export const maf = (v) => (v == null ? '–' : nf3.format(v));
export const whole = (v) => (v == null ? '–' : nf0.format(v));

export const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

// '2026-10-30' -> '30 Oct 2026' without timezone surprises
export function day(iso, withYear = true) {
  if (!iso) return '–';
  const [y, m, d] = iso.slice(0, 10).split('-').map(Number);
  return withYear ? `${d} ${MONTHS[m - 1]} ${y}` : `${d} ${MONTHS[m - 1]}`;
}

export function stamp(isoDateTime) {
  if (!isoDateTime) return '–';
  const t = new Date(isoDateTime);
  return t.toLocaleString('en-GB', {
    day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit',
    timeZone: 'Asia/Karachi',
  }) + ' PKT';
}

export function dayOfYear(iso) {
  const [y, m, d] = iso.slice(0, 10).split('-').map(Number);
  return Math.round((Date.UTC(y, m - 1, d) - Date.UTC(y, 0, 1)) / 864e5) + 1;
}

// first day-of-year of each month, for chart ticks (non-leap year)
export const MONTH_STARTS = [1, 32, 60, 91, 121, 152, 182, 213, 244, 274, 305, 335];

export function change(cur, prev, unit, digits = 0) {
  if (cur == null || prev == null) return null;
  const d = cur - prev;
  const f = new Intl.NumberFormat('en-US', { maximumFractionDigits: digits, minimumFractionDigits: digits });
  if (Math.abs(d) < 10 ** -digits / 2) return { dir: 'flat', text: `no change` };
  return { dir: d > 0 ? 'up' : 'down', text: `${d > 0 ? 'up' : 'down'} ${f.format(Math.abs(d))} ${unit}` };
}
