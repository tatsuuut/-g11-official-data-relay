"""Bound expanded MORNING provenance by its actual saved official race scope."""
from datetime import date
import re


def admitted_digest_limit(payload, admitted, extra=0):
    """Only a fully admitted snapshot may expand a later permit's hash list."""
    fallback = 20 + extra
    known = admitted.get(payload.get('G11_SNAPSHOT_SHA256'))
    hashes = payload.get('G11_SOURCE_SHA256')
    if (not known or type(hashes) is not list or len(hashes) != len(known) + extra
            or tuple(hashes[:len(known)]) != known):
        return fallback
    return max(fallback, len(known) + extra)


def receipt_limit(verified):
    # Preserve the original admission path for its previously supported size.
    # Returning 20 on any unrecognized expanded envelope keeps C04 closed.
    try:
        snapshot = verified['SNAPSHOT']
        receipts = snapshot['SOURCE_RECEIPTS']
        if type(receipts) is not list or len(receipts) <= 20:
            return 20
        payload = snapshot['PAYLOAD']
        day = date.fromisoformat(payload['OPERATIONAL_DATE_JST'])
        keys = {f"{r['operational_date'].replace('-', '')}-{r['venue_code']}-{r['race_no']:02d}"
                for r in payload['RACES']}
        count = payload['SCOPE']['EXPECTED_RACE_COUNT']
        if type(count) is not int or not 1 <= count <= 288 or len(keys) != count:
            return 20
        if any(not re.fullmatch(f'{day:%Y%m%d}' + r'-(?:0[1-9]|1[0-9]|2[0-4])-(?:0[1-9]|1[0-2])', k) for k in keys):
            return 20
        columns = payload['RAW38_FIELDS']
        key_index, url_index = columns.index('標準レースキー'), columns.index('公式URL')
        row_sources = {(r[key_index], r[url_index]) for r in payload['RAW38_ROWS']}
        seen, kinds = set(), {'b': 0, 'fan': 0, 'k': 0, 'recovery': 0}
        for receipt in receipts:
            source = receipt['SOURCE_ID']
            if source in seen:
                return 20
            seen.add(source)
            if source == f'schedule-b-{day:%Y%m%d}':
                urls = {f'https://www1.mbrace.or.jp/od2/B/{day:%Y%m}/b{day:%y%m%d}.lzh'}
                kind = 'b'
            elif re.fullmatch(r'racer-period-fan\d{4}', source):
                from racer_period_source_v1 import official_period_url
                name = source.removeprefix('racer-period-') + '.lzh'
                if name != official_period_url(day).rsplit('/', 1)[1]:
                    return 20
                urls = {official_period_url(day), 'https://www.boatrace.jp/owpc/pc/extra/data/' + name}
                kind = 'fan'
            elif re.fullmatch(r'result-k-\d{8}', source):
                prior = date.fromisoformat(source.removeprefix('result-k-'))
                if not 1 <= (day - prior).days <= 6:
                    return 20
                urls = {f'https://www1.mbrace.or.jp/od2/K/{prior:%Y%m}/k{prior:%y%m%d}.lzh'}
                kind = 'k'
            elif source.startswith('racelist-recovery-'):
                key = source.removeprefix('racelist-recovery-')
                if key not in keys:
                    return 20
                url = f'https://www.boatrace.jp/owpc/pc/race/racelist?hd={day:%Y%m%d}&jcd={key[9:11]}&rno={int(key[-2:])}'
                if (key, url) not in row_sources:
                    return 20
                urls, kind = {url}, 'recovery'
            else:
                return 20
            if receipt['URL'] not in urls or receipt['FINAL_URL'] != receipt['URL']:
                return 20
            kinds[kind] += 1
        if kinds['b'] != 1 or kinds['fan'] != 1 or kinds['k'] > 6 or kinds['recovery'] > count:
            return 20
        return count + kinds['k'] + 2
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        return 20
