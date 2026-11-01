import { useEffect } from 'react';
import { Route, Routes, useLocation, Link } from 'react-router-dom';
import Layout from './components/Layout.jsx';
import About from './pages/About.jsx';
import Data from './pages/Data.jsx';
import Forecast from './pages/Forecast.jsx';
import History from './pages/History.jsx';
import Today from './pages/Today.jsx';

const TITLES = { '/': 'Today', '/history': 'History', '/forecast': 'Forecast', '/data': 'Data', '/about': 'About' };

function NotFound() {
  return (
    <div className="prose">
      <h1 className="page-title">There's no page here</h1>
      <p>Go to <Link to="/">today's figures</Link>.</p>
    </div>
  );
}

export default function App() {
  const { pathname } = useLocation();
  useEffect(() => {
    document.title = `${TITLES[pathname] ?? 'Not found'}: Pakistan Water Watch`;
    window.scrollTo(0, 0);
  }, [pathname]);
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Today />} />
        <Route path="/history" element={<History />} />
        <Route path="/forecast" element={<Forecast />} />
        <Route path="/data" element={<Data />} />
        <Route path="/about" element={<About />} />
        <Route path="*" element={<NotFound />} />
      </Routes>
    </Layout>
  );
}
