    return receipt

def deliver_rescue(state, feed_path, origin, attempts, pause):
    """Transport one explicitly nonresearch boundary; no canonical fallback."""
    boundary_path = state.resolve() / 'rescue/boundary.json'
    require(boundary_path.is_file() and not boundary_path.is_symlink(), 'RESCUE_BOUNDARY_MISSING')
    cert = json.loads(boundary_path.read_bytes())
    unsigned = dict(cert)
    authority_sha = unsigned.pop('authority_sha256', None)
    require(cert.get('schema') == 'G11_SAME_DAY_RESCUE_BOUNDARY_V1'
            and cert.get('operational_date_jst') == '2026-10-06'
            and authority_sha == hashlib.sha256(json.dumps(unsigned, ensure_ascii=False, sort_keys=True,
                separators=(',', ':'), allow_nan=False).encode()).hexdigest(), 'RESCUE_BOUNDARY_INVALID')
    feed = json.loads(feed_path.read_bytes())
    require(feed['operational_date_jst'] == '2026-10-06' and feed['stage'] == 'MORNING'
            and feed['same_day_rescue']['authority_sha256'] == cert['authority_sha256']
            and feed.get('daily_runtime') is None and feed['counts']['formal'] == 0
            and feed['counts']['research_samples'] == 0, 'RESCUE_TRANSPORT_IDENTITY')
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'rescue.json'
        path.write_bytes(feed_path.read_bytes())
        hot.project_morning_base(path)
        projected = json.loads(path.read_bytes())
        for field in ('SNAPSHOT_CLASS', 'CANONICAL_MORNING_PROMOTED', 'RESEARCH_ELIGIBLE_AS_MORNING',
                      'MORNING_HISTORY_BACKFILL', 'ACCOUNTING_CLASS', 'same_day_rescue'):
            projected[field] = feed[field]
        proofs = {r['key']: r.get('same_day_rescue_lock') for r in feed['races']}