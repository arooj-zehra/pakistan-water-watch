import { ErrorBox, Loading } from '../components/Status.jsx';
import { API_URL, useData } from '../data.js';
import { day, stamp, whole } from '../format.js';

export default function About() {
  const { data, error, loading } = useData('meta');
  if (loading) return <Loading what="the sources" />;
  if (error) return <ErrorBox error={error} />;
  const [meta] = data;

  return (
    <div className="prose">
      <h1 className="page-title">Where the numbers come from</h1>
      <p className="lede">
        WAPDA and IRSA publish Tarbela and Mangla's figures every morning, but not as a dataset. This site rebuilds the
        daily record from the news stories that republish them, checks one source against another, and updates every day.
      </p>

      <h2>Sources</h2>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th scope="col">Source</th>
              <th scope="col">Dam-days used</th>
              <th scope="col">First</th>
              <th scope="col">Latest</th>
            </tr>
          </thead>
          <tbody>
            {meta.sources.map((s) => (
              <tr key={s.source}>
                <td>{s.name}</td>
                <td>{whole(s.dam_days)}</td>
                <td>{day(s.first)}</td>
                <td>{day(s.last)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="chart-note">
        Weather data by <a href="https://open-meteo.com/">Open-Meteo.com</a> (ERA5 reanalysis and daily forecasts),
        licensed CC BY 4.0. Figures last refreshed {stamp(meta.generated_at)}.
      </p>

      <div className="two-col">
        <div>
          <h2>How each number is checked</h2>
          <ul>
            <li>One parser reads every story, whatever the outlet, and each value is range-checked: a Tarbela inflow of 11 cusecs or a 95 ft jump in one day is set aside, not published.</li>
            <li>Stories are dated by their dateline, title or the weekday they name, because some papers reprint the release a day later.</li>
            <li>When two outlets report the same day, the more complete story is used and any disagreement over 1% is logged for review.</li>
            <li>Flows are 24-hour means to 06:00 PKT. Levels are in feet, flows in cusecs, storage in million acre-feet (MAF).</li>
          </ul>
        </div>
        <div>
          <h2>Known gaps</h2>
          <ul>
            <li>January to March 2024, February 2025 and October to December 2025: no published figures found for either dam.</li>
            <li>February to July 2022: Tarbela is missing, because the daily stories leave it out when it is near dead level.</li>
            <li>Before 2020 the record is patchy, built from scattered stories and four PMD monthly sheets from 2015.</li>
          </ul>
        </div>
      </div>

      <h2>Coverage by year</h2>
      <p>
        Share of days with an inflow figure for both dams. The second column also fills gaps of up
        to {meta.fill_gap_days} days by drawing a straight line between the days either side.
      </p>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th scope="col">Year</th>
              <th scope="col">As published</th>
              <th scope="col">With short gaps filled</th>
            </tr>
          </thead>
          <tbody>
            {[...meta.coverage].reverse().filter((c) => c.year >= 2017).map((c) => (
              <tr key={c.year}>
                <td>{c.year}{c.partial ? ' (so far)' : ''}</td>
                <td>{c.present_raw}%</td>
                <td>{c.present_filled}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h2>Use the data</h2>
      <p>
        Download any range from the Data page{API_URL ? <>, or query the <a href={`${API_URL}/docs`}>public API</a></> : null}.
        The code, raw stories and every parsed number with a link to its article are on{' '}
        <a href="https://github.com/arooj-zehra/pakistan-water-watch">GitHub</a>. This is a student project and not an
        official source; for decisions, use WAPDA and IRSA directly.
      </p>
    </div>
  );
}
