export function getRoute() {
  const [page,key] = location.hash.slice(1).split('/');
  const section = /^\d$/.test(page) && Number(page) < 6 ? Number(page) : 0;
  const periodMatch = typeof key === 'string' && key.match(/^(?:period|bridge)-(\d{4}-\d{2})$/);
  const period = periodMatch ? periodMatch[1] : null;
  const runId = !period && /^[1-9]\d*$/.test(key || '') ? Number(key) : null;
  return {section,runId,period};
}
