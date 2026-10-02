from datetime import datetime, timezone, timedelta, time as dtime
from zoneinfo import ZoneInfo
UTC=timezone.utc
# Policies for local wall times that do not map to one unique instant.
# Gap (spring-forward): SHIFT_BACKWARD -> first valid instant before the gap,
#                       SHIFT_FORWARD  -> first valid instant after the gap.
# Ambiguous (fall-back): FOLD_EARLY -> EDT/first occurrence, FOLD_LATE -> EST/second.
SHIFT_BACKWARD='shift_backward'
SHIFT_FORWARD='shift_forward'
FOLD_EARLY=0
FOLD_LATE=1
def aware(v):
    if v.tzinfo is None or v.utcoffset() is None: raise ValueError('timezone-aware datetime required')
    return v
def utc(v): return aware(v).astimezone(UTC)
def zone(zone_name): return ZoneInfo(zone_name)
def in_zone(v,zone_name):
    """Single boundary rule shared by SLA, site windows and dispatch plans."""
    return utc(v).astimezone(ZoneInfo(zone_name))
def local_date(v,zone_name): return in_zone(v,zone_name).date()
def seconds(a,b):
    a,b=utc(a),utc(b)
    if b<a: raise ValueError('end before start')
    return (b-a).total_seconds()
def wall_to_utc(v,zone_name,fold=0):
    if v.tzinfo is not None: raise ValueError('wall time must be naive')
    if fold not in (0,1): raise ValueError('invalid fold')
    return v.replace(tzinfo=ZoneInfo(zone_name),fold=fold).astimezone(UTC)
def localize(naive,zone_name,gap_policy=SHIFT_FORWARD,fold_policy=FOLD_EARLY):
    """Map a naive wall time to a UTC instant.

    Returns (instant, kind) where kind is 'normal', 'gap' or 'ambiguous',
    so callers can record why a boundary moved (DST transitions).

    PEP 495 gives the fold=0 candidate the pre-transition offset and fold=1
    the post-transition offset. For a spring-forward gap both candidates
    round-trip to a wall time on the *other* side of the gap, so the valid
    boundary instants are derived from those round-trip walls; for a
    fall-back ambiguity both candidates round-trip to the requested wall.
    """
    if naive.tzinfo is not None: raise ValueError('wall time must be naive')
    if gap_policy not in (SHIFT_BACKWARD,SHIFT_FORWARD): raise ValueError('invalid gap policy')
    if fold_policy not in (FOLD_EARLY,FOLD_LATE): raise ValueError('invalid fold policy')
    z=ZoneInfo(zone_name)
    early=naive.replace(tzinfo=z,fold=0); late=naive.replace(tzinfo=z,fold=1)
    off_early,off_late=early.utcoffset(),late.utcoffset()
    # round-trip through UTC (astimezone to the same zone is a no-op)
    wall_early=early.astimezone(UTC).astimezone(z).replace(tzinfo=None)
    wall_late=late.astimezone(UTC).astimezone(z).replace(tzinfo=None)
    if off_early==off_late:
        return early.astimezone(UTC),'normal'
    if wall_early==naive:
        # fold=0 round-trips to the requested wall:
        # ambiguous fall-back (fold=1 lands on the first occurrence),
        # fold=0 first occurrence, fold=1 second
        chosen=late if fold_policy==FOLD_LATE else early
        return chosen.astimezone(UTC),'ambiguous'
    # fold=0 does not round-trip: non-existent spring-forward gap.
    # fold=0 keeps the pre-transition offset and round-trips to the first
    # wall AFTER the gap; fold=1 takes the post-transition offset and
    # round-trips to the last wall BEFORE the gap.
    anchor_wall=wall_early if gap_policy==SHIFT_FORWARD else wall_late
    anchor=anchor_wall.replace(tzinfo=z).astimezone(UTC)
    return anchor,'gap'
def at_local(d,t,zone_name,gap_policy=SHIFT_FORWARD,fold_policy=FOLD_EARLY):
    """Combine a local date + wall clock time to an instant."""
    if not isinstance(t,dtime): raise ValueError('time required')
    return localize(datetime.combine(d,t),zone_name,gap_policy,fold_policy)
def bucket(v,minutes):
    if minutes<=0: raise ValueError('minutes must be positive')
    x=utc(v); step=minutes*60; epoch=int(x.timestamp()); return datetime.fromtimestamp(epoch-epoch%step,tz=UTC)
def add_local_days(v,zone_name,days):
    local=utc(v).astimezone(ZoneInfo(zone_name)); target=local.date()+timedelta(days=days)
    return datetime.combine(target,local.timetz().replace(tzinfo=None),ZoneInfo(zone_name)).astimezone(UTC)
