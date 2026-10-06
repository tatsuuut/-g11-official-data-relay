

def deliver(state: Path, feed_path: Path, origin: str, *, attempts=MAX_ATTEMPTS, pause=time.sleep):
    require(origin == ORIGIN and 1 <= attempts <= MAX_ATTEMPTS, "DAILY_DELIVERY_CONFIGURATION")
    Path(".relay-output").mkdir(exist_ok=True)
    incoming = json.loads(feed_path.read_bytes())
    day = incoming["operational_date_jst"]
    rescue_class = incoming.get("SNAPSHOT_CLASS")
    if rescue_class == "SAME_DAY_RESCUE_BOUNDARY":
        return deliver_rescue(state, feed_path, origin, attempts, pause)
    if rescue_class == "TODAY_ONLY_RESCUE_PREDEADLINE":
        return deliver_rescue_predeadline(state, feed_path, origin)
    if rescue_class == "SAME_DAY_RESCUE_NONRESEARCH" and incoming.get("stage") == "NIGHT":
        return deliver_rescue_night(state, feed_path, origin)
    if rescue_class == "SAME_DAY_RESCUE_NONRESEARCH" and incoming.get("stage") == "NIGHT":
        return deliver_rescue_night(state, feed_path, origin)
    implementation = retained_module(state, day)
    authority = implementation.load(state, day)
    feed = implementation.json_file(feed_path)
    phase = feed["stage"]
    identity(feed, authority, phase)
    implementation.verify_locks(state, day, "MORNING", exact=True)
    implementation.verify_locks(state, day, "PREDEADLINE", exact=True)
    token = os.environ["G11_SYNC_OIDC"]
    checkpoint = None
    if phase == "NIGHT":
        checkpoint = implementation.read_checkpoint(state, day)
        require(hot.hash_bytes(feed_path.read_bytes()) == checkpoint["feed_sha256"], "NIGHT_DELIVERY_CANONICAL_FEED_CHANGED")
    for attempt in range(attempts):
        # Integrity failures stop before network, including between retries.
        implementation.load(state, day)
        implementation.verify_locks(state, day, "MORNING", exact=True)
        implementation.verify_locks(state, day, "PREDEADLINE", exact=True)
        if checkpoint:
            implementation.read_checkpoint(state, day)
        try:
            if phase == "NIGHT":
                ack, headers, raw, manifest = send_night(state, day, feed, checkpoint, authority, implementation, origin, token)
            elif phase == "MORNING":
                ack, headers, raw = send_morning(feed_path, feed, authority, origin, token)
            elif phase == "PREDEADLINE":
                hot.publish(feed_path, origin)
                readback, headers, raw = hot.fetch(origin + "/api/g11-feed?date=" + day)