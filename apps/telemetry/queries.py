from __future__ import annotations

from datetime import datetime, timedelta


def _rows(cursor):
    return [dict(row) for row in cursor.fetchall()]


def quality_summary(conn, hours: int = 24, site_id: int | None = None) -> dict:
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat(timespec='seconds')
    sql = """SELECT tr.quality,COUNT(*) count FROM telemetry_readings tr JOIN telemetry_channels tc ON tc.id=tr.channel_id
      JOIN assets a ON a.id=tc.asset_id LEFT JOIN locations l ON l.id=a.location_id LEFT JOIN sites s ON s.id=l.site_id
      WHERE tr.captured_at>=?"""
    args: list[object] = [cutoff]
    if site_id is not None:
        sql += ' AND s.id=?'
        args.append(site_id)
    sql += ' GROUP BY tr.quality'
    counts = {row['quality']: int(row['count']) for row in conn.execute(sql, args).fetchall()}
    total = sum(counts.values())
    good = counts.get('Good', 0)
    bad = counts.get('Bad', 0)
    uncertain = counts.get('Uncertain', 0)
    return {
        'hours': hours,
        'total_readings': total,
        'good': good,
        'uncertain': uncertain,
        'bad': bad,
        'good_percent': round(good / max(total, 1) * 100, 1),
        'bad_percent': round(bad / max(total, 1) * 100, 1),
    }


def telemetry_series(conn, channel_id: int, hours: int, bucket_minutes: int) -> list[dict]:
    cutoff = datetime.now() - timedelta(hours=hours)
    data = _rows(
        conn.execute(
            'SELECT value,quality,captured_at FROM telemetry_readings WHERE channel_id=? AND captured_at>=? ORDER BY captured_at',
            (channel_id, cutoff.isoformat(timespec='seconds')),
        )
    )
    buckets: dict[str, dict] = {}
    span = max(1, bucket_minutes) * 60
    for row in data:
        try:
            parsed = datetime.fromisoformat(str(row['captured_at']).replace('Z', '+00:00'))
        except (TypeError, ValueError):
            continue
        epoch = int(parsed.timestamp())
        bucket_epoch = epoch - (epoch % span)
        key = datetime.fromtimestamp(bucket_epoch, tz=parsed.tzinfo).isoformat(timespec='seconds')
        bucket = buckets.setdefault(
            key,
            {'timestamp': key, 'values': [], 'good': 0, 'uncertain': 0, 'bad': 0},
        )
        bucket['values'].append(float(row['value']))
        quality = str(row['quality']).lower()
        bucket[quality] = bucket.get(quality, 0) + 1
    points = []
    for key in sorted(buckets):
        bucket = buckets[key]
        values = bucket.pop('values')
        points.append(
            bucket
            | {
                'min': min(values),
                'max': max(values),
                'avg': round(sum(values) / len(values), 4),
                'count': len(values),
            }
        )
    return points


def readings(conn, *, channel_id: int | None, asset_id: int | None, hours: int, limit: int) -> list[dict]:
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat(timespec='seconds')
    sql = "SELECT tr.*,tc.channel_code,tc.name channel_name,tc.metric_type,tc.unit,a.asset_no,a.name asset_name FROM telemetry_readings tr JOIN telemetry_channels tc ON tc.id=tr.channel_id JOIN assets a ON a.id=tc.asset_id WHERE tr.captured_at>=?"
    args: list[object] = [cutoff]
    if channel_id is not None:
        sql += ' AND tc.id=?'
        args.append(channel_id)
    if asset_id is not None:
        sql += ' AND a.id=?'
        args.append(asset_id)
    sql += ' ORDER BY tr.captured_at DESC LIMIT ?'
    args.append(limit)
    return _rows(conn.execute(sql, args))
