import { Link } from 'react-router-dom';

export default function Forecast() {
  return (
    <div className="prose">
      <h1 className="page-title">Inflow forecasts</h1>
      <p className="lede">Forecasts start once the model is trained. Until then, the Today and History pages show observed figures only.</p>
      <h2>What will appear here</h2>
      <ul>
        <li>Inflow forecasts for Tarbela and Mangla 1, 3, 5, 7 and 10 days ahead.</li>
        <li>A range around each forecast that should contain the real inflow 9 times out of 10, and a record of how often it actually does.</li>
        <li>The main reason behind each forecast, such as a sharp rise in temperature over the upper Indus.</li>
      </ul>
      <h2>How it will be checked</h2>
      <p>
        Every forecast is stored before the day it describes, and can't be changed afterwards. When that day's figures
        are published, the forecast is scored against them, and the running score appears here next to a simple
        benchmark: tomorrow equals today.
      </p>
      <p>Meanwhile, see how this year compares with the usual range on the <Link to="/history">History</Link> page.</p>
    </div>
  );
}
