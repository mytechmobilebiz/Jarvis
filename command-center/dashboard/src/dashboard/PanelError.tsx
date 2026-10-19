// A failed background refresh, shown in its panel without role="alert": a poll that fails every 15 s must not keep
// interrupting a screen reader (the shell already announces a lost connection). Errors from a user's action use ErrorNote.
export default function PanelError({ error }: { error: string | null | undefined }) {
  if (!error) return null;
  return <p className="alert alert-bad dash-error">{error}</p>;
}
