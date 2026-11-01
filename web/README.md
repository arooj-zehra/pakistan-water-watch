# Website

React (Vite) with Recharts. Five pages: Today, History, Forecast, Data, About.

It reads `public/data/*.json` first (written by `python -m pipeline.export_static`, and
refreshed by the daily GitHub workflow), and the API second if `VITE_API_URL` is set. So the
site loads instantly even when the free API server is asleep.

```cmd
cd web
npm install
npm run dev
```

Then open http://localhost:5173. `npm run build` writes the production site to `dist/`.

Design notes: each dam is drawn as a staff gauge to scale, with dead storage shown as silt.
Colours come from the rivers (glacial teal water, sediment khaki, one gauge red for warnings),
with a dark theme that follows the phone's setting. Type: Barlow Condensed for figures and
headings, Atkinson Hyperlegible for text.

Deployed on Vercel with the project's root directory set to `web`.
