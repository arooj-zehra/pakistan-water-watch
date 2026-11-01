import { NavLink, Link } from 'react-router-dom';
import { useData } from '../data.js';
import { day, stamp } from '../format.js';

const PAGES = [
  ['/', 'Today'],
  ['/history', 'History'],
  ['/forecast', 'Forecast'],
  ['/accuracy', 'Accuracy'],
  ['/data', 'Data'],
  ['/about', 'About'],
];

export default function Layout({ children }) {
  const { data } = useData('latest');
  const latest = data?.[0];
  const newest = latest?.stations?.map((s) => s.date).sort().at(-1);
  return (
    <>
      <header className="masthead">
        <div className="shell">
          <Link to="/" className="brand">Pakistan Water Watch</Link>
          <nav className="nav" aria-label="Pages">
            {PAGES.map(([to, label]) => (
              <NavLink key={to} to={to} end={to === '/'}>{label}</NavLink>
            ))}
          </nav>
        </div>
      </header>
      <main className="shell">{children}</main>
      <footer>
        <div className="shell">
          <p>
            {newest ? <>Latest figures for {day(newest)}. Data refreshed {stamp(latest.generated_at)}. </> : null}
            Figures from WAPDA and IRSA daily releases as republished by APP, UrduPoint, Pakistan Observer and
            others; PMD Flood Forecasting Division. Weather data by <a href="https://open-meteo.com/">Open-Meteo.com</a> (CC BY 4.0).
          </p>
          <p>
            A student project, not an official source. See the <Link to="/about">sources and methods</Link>,
            the <Link to="/review">data review queue</Link>, or
            the <a href="https://github.com/arooj-zehra/pakistan-water-watch">code on GitHub</a>.
          </p>
        </div>
      </footer>
    </>
  );
}
