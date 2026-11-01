// A dam staff gauge drawn to scale: silt-filled dead storage at the bottom,
// water up to today's level, the maximum conservation level at the top.
const H = 300;
const STAFF_X = 36;
const STAFF_W = 30;

export default function Gauge({ level, dead, max, name }) {
  if (level == null || max == null) return null;
  const floor = Math.floor(((dead ?? level) - 40) / 10) * 10; // show some silt below dead level
  const ceil = max + 6;
  const y = (ft) => 6 + ((ceil - ft) / (ceil - floor)) * (H - 12);
  const ticks = [];
  for (let ft = Math.ceil(floor / 5) * 5; ft <= max; ft += 5) ticks.push(ft);
  const labelled = ticks.filter((ft) => ft % 50 === 0);
  const yLevel = y(Math.max(floor, Math.min(level, ceil)));
  const yDead = dead != null ? y(dead) : y(floor);
  const pct = dead != null ? Math.round(((level - dead) / (max - dead)) * 100) : null;
  const label = `${name} gauge: level ${level} ft` +
    (dead != null ? `, ${Math.round(level - dead)} ft above the dead level of ${dead} ft` : '') +
    `, maximum ${max} ft${pct != null ? `, ${pct}% of the usable height` : ''}`;

  return (
    <svg viewBox={`0 0 112 ${H}`} width="100%" role="img" aria-label={label} style={{ display: 'block', maxWidth: 112 }}>
      <defs>
        <pattern id={`silt-${name}`} width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
          <rect width="6" height="6" fill="var(--silt-soft)" />
          <line x1="0" y1="0" x2="0" y2="6" stroke="var(--silt)" strokeWidth="1.4" />
        </pattern>
      </defs>
      {/* staff */}
      <rect x={STAFF_X} y={y(ceil)} width={STAFF_W} height={y(floor) - y(ceil)} fill="var(--paper-raised)" stroke="var(--ink)" strokeWidth="1.2" rx="2" />
      {/* dead storage: sediment */}
      <rect x={STAFF_X} y={yDead} width={STAFF_W} height={y(floor) - yDead} fill={`url(#silt-${name})`} />
      {/* live water above dead level */}
      {level > (dead ?? floor) && (
        <rect x={STAFF_X} y={yLevel} width={STAFF_W} height={yDead - yLevel} fill="var(--water)" fillOpacity="0.85" />
      )}
      {/* painted marks */}
      {ticks.map((ft) => (
        <line key={ft} x1={STAFF_X} x2={STAFF_X + (ft % 10 === 0 ? 14 : 8)} y1={y(ft)} y2={y(ft)}
          stroke="var(--ink)" strokeWidth={ft % 50 === 0 ? 1.4 : 0.8} />
      ))}
      {labelled.map((ft) => (
        <text key={ft} x={STAFF_X - 5} y={y(ft) + 4} textAnchor="end" fontSize="10.5" fill="var(--ink-soft)"
          style={{ fontVariantNumeric: 'tabular-nums' }}>
          {ft.toLocaleString('en-US')}
        </text>
      ))}
      {/* reference levels */}
      <line x1={STAFF_X - 2} x2={STAFF_X + STAFF_W + 4} y1={y(max)} y2={y(max)} stroke="var(--ink)" strokeWidth="1.6" />
      <text x={STAFF_X + STAFF_W + 6} y={y(max) + 4} fontSize="10.5" fill="var(--ink-soft)">max</text>
      {dead != null && (
        <>
          <line x1={STAFF_X - 2} x2={STAFF_X + STAFF_W + 4} y1={yDead} y2={yDead} stroke="var(--gauge-red)" strokeWidth="1.6" />
          <text x={STAFF_X + STAFF_W + 6} y={yDead + 4} fontSize="10.5" fill="var(--gauge-red)">dead</text>
        </>
      )}
      {/* today's water line */}
      <line x1={STAFF_X - 6} x2={STAFF_X + STAFF_W + 6} y1={yLevel} y2={yLevel} stroke="var(--water)" strokeWidth="2.4" />
      <path d={`M${STAFF_X + STAFF_W + 6} ${yLevel}l8 -5v10z`} fill="var(--water)" />
    </svg>
  );
}
