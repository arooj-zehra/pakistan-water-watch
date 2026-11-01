export function Loading({ what = 'the latest figures' }) {
  return <p className="status" role="status">Loading {what}…</p>;
}

export function ErrorBox({ error }) {
  return (
    <div className="status status-error" role="alert">
      <p><strong>The data didn't load.</strong> {error?.message}</p>
      <p>Check your connection and reload the page.</p>
    </div>
  );
}
