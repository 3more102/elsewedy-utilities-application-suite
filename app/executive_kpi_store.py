"""Canonical executive KPI snapshot surface (`/api/kpi/*`).

This module installs the scoped executive snapshot platform routes on top of
``app.kpi_service`` — the canonical snapshot architecture (source watermark,
15-minute TTL, refresh flag, storage fallback, per-scope upsert safety).
The dashboard family adapters under ``/api/kpis/<family>`` (see
``kpi_store.py``) remain the frontend contract for the executive dashboard
panels; the two namespaces are intentionally disjoint and a route-uniqueness
contract test guards against semantic double-registration.

Site customer populations are administered exclusively through the audited
``PATCH /api/sites/{site_id}/customer-count`` endpoint (kpi_store.py); this
module deliberately registers no competing mutation.
"""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, HTTPException, Query

from . import application as _application
from .auth import require_roles
from .database import db

KPI_ROLES = ('admin', 'maintenance_manager', 'executive', 'asset_manager', 'planner', 'supervisor')
HSE_KPI_ROLES = KPI_ROLES + ('hse',)


def _kpi_filters(
    period_end: Optional[str] = None,
    period_days: int = 30,
    site_id: Optional[int] = None,
    region: Optional[str] = None,
    asset_type_id: Optional[int] = None,
    criticality: Optional[str] = None,
):
    from .kpi_service import ExecutiveFilters

    if period_days < 1 or period_days > 365:
        raise HTTPException(422, 'period_days must be between 1 and 365')
    return ExecutiveFilters(
        period_end=period_end,
        period_days=period_days,
        site_id=site_id,
        region=region,
        asset_type_id=asset_type_id,
        criticality=criticality,
    )


def install_executive_kpi_routes() -> None:
    """Own the canonical executive KPI snapshot surface."""
    app = _application.app
    marker = '_euas_executive_kpi_routes'
    if getattr(app.state, marker, False):
        return

    @app.get('/api/kpi/executive')
    def kpi_executive(
        period_end: Optional[str] = None,
        period_days: int = Query(30, ge=1, le=365),
        site_id: Optional[int] = None,
        region: Optional[str] = None,
        asset_type_id: Optional[int] = None,
        criticality: Optional[str] = None,
        refresh: bool = False,
        user=Depends(require_roles(*KPI_ROLES)),
    ):
        """Scoped executive snapshot with drillable sections.

        Every section aggregates the same filtered scope so numbers stay
        consistent across cards, tables and drill-downs. Results are
        materialized per (scope, window) and served only while no tracked
        source mutated after calculation; ``refresh=true`` forces live
        recomputation.
        """
        f = _kpi_filters(period_end, period_days, site_id, region, asset_type_id, criticality)
        from .kpi_service import executive_snapshot

        with db() as conn:
            return executive_snapshot(conn, f, use_cache=not refresh)

    @app.get('/api/kpi/backlog/risk')
    def kpi_backlog_risk(
        limit: int = Query(25, ge=1, le=200),
        period_end: Optional[str] = None,
        period_days: int = Query(30, ge=1, le=365),
        site_id: Optional[int] = None,
        region: Optional[str] = None,
        asset_type_id: Optional[int] = None,
        criticality: Optional[str] = None,
        user=Depends(require_roles(*KPI_ROLES)),
    ):
        """Risk-weighted maintenance backlog ranked by explainable scores."""
        f = _kpi_filters(period_end, period_days, site_id, region, asset_type_id, criticality)
        from .kpi_service import risk_weighted_backlog

        with db() as conn:
            return risk_weighted_backlog(conn, f, limit=limit)

    @app.get('/api/kpi/deterioration')
    def kpi_deterioration(
        limit: int = Query(30, ge=1, le=100),
        period_end: Optional[str] = None,
        period_days: int = Query(30, ge=1, le=365),
        site_id: Optional[int] = None,
        region: Optional[str] = None,
        asset_type_id: Optional[int] = None,
        criticality: Optional[str] = None,
        user=Depends(require_roles(*KPI_ROLES)),
    ):
        """Deterministic condition-deterioration signals (trend labels only)."""
        f = _kpi_filters(period_end, period_days, site_id, region, asset_type_id, criticality)
        from .kpi_service import compute_deterioration_signals

        with db() as conn:
            return compute_deterioration_signals(conn, f, limit=limit)

    @app.get('/api/kpi/parts/shortages')
    def kpi_parts_shortages(
        limit: int = Query(50, ge=1, le=200),
        site_id: Optional[int] = None,
        region: Optional[str] = None,
        user=Depends(require_roles(*KPI_ROLES)),
    ):
        """Exact per-line material shortages blocking open work (KPI -> action)."""
        f = _kpi_filters(site_id=site_id, region=region)
        from .kpi_service import compute_parts_shortages

        with db() as conn:
            return compute_parts_shortages(conn, f, limit=limit)

    @app.get('/api/kpi/pm-risk')
    def kpi_pm_capacity_risk(
        horizon_days: int = Query(84, ge=14, le=365),
        site_id: Optional[int] = None,
        region: Optional[str] = None,
        user=Depends(require_roles(*KPI_ROLES)),
    ):
        """High-criticality PMs landing in over-capacity weeks.

        Demand/capacity math is delegated to the canonical maintenance
        forecast; this endpoint cross-references critical plan due dates
        against those buckets so scheduling can act before the week arrives.
        """
        f = _kpi_filters(site_id=site_id, region=region)
        from .kpi_service import compute_pm_capacity_risk

        with db() as conn:
            return compute_pm_capacity_risk(conn, f, horizon_days=horizon_days)

    @app.get('/api/kpi/hse')
    def kpi_hse(
        period_end: Optional[str] = None,
        period_days: int = Query(30, ge=1, le=365),
        site_id: Optional[int] = None,
        region: Optional[str] = None,
        user=Depends(require_roles(*HSE_KPI_ROLES)),
    ):
        """Safety/incident KPIs from real safety_incidents data.

        Metrics EUAS cannot compute honestly are returned as explicitly
        unavailable rather than estimated.
        """
        f = _kpi_filters(period_end, period_days, site_id, region)
        from .kpi_service import compute_hse_kpis

        with db() as conn:
            return compute_hse_kpis(conn, f)

    @app.get('/api/kpi/assets/{asset_id}')
    def kpi_asset_profile(
        asset_id: int,
        period_days: int = Query(90, ge=1, le=365),
        user=Depends(require_roles(*KPI_ROLES)),
    ):
        """Per-asset KPI dossier completing the drill chain to materials."""
        f = _kpi_filters(period_days=period_days)
        from .kpi_service import compute_asset_kpi_profile

        with db() as conn:
            profile = compute_asset_kpi_profile(conn, asset_id, f)
        if not profile:
            raise HTTPException(404, 'Asset not found')
        return profile

    @app.get('/api/exports/executive-kpis.csv')
    def export_executive_kpis(
        period_end: Optional[str] = None,
        period_days: int = Query(30, ge=1, le=365),
        site_id: Optional[int] = None,
        region: Optional[str] = None,
        asset_type_id: Optional[int] = None,
        criticality: Optional[str] = None,
        user=Depends(require_roles(*KPI_ROLES)),
    ):
        """Scoped executive KPI snapshot as CSV.

        Reuses the materialized snapshot pipeline (same scope keys, freshness
        and cache semantics) — export never recalculates or bypasses
        authorization.
        """
        f = _kpi_filters(period_end, period_days, site_id, region, asset_type_id, criticality)
        from .kpi_service import executive_snapshot, snapshot_export_rows

        with db() as conn:
            snapshot = executive_snapshot(conn, f)
            rows = snapshot_export_rows(snapshot)
        # Filename disambiguates the scope so multi-scope exports cannot be
        # confused on disk. Segments are sanitized to a safe charset; only
        # scopes the caller explicitly queried appear in the name.
        import re as _re

        parts: list[str] = []
        if site_id:
            parts.append(f'site{int(site_id)}')
        if region:
            parts.append(_re.sub(r'[^A-Za-z0-9_-]', '', region)[:40].lower() or 'region')
        if asset_type_id:
            parts.append(f'class{int(asset_type_id)}')
        if criticality:
            parts.append(_re.sub(r'[^A-Za-z0-9_-]', '', criticality)[:20].lower())
        suffix = ('-' + '-'.join(parts)) if parts else '-all'
        filename = (
            f'EUAS_executive_kpis{suffix}'
            f'_{_application.date.today().strftime("%Y%m%d")}.csv')
        return _application.csv_response(
            filename,
            ['Family', 'Metric', 'Value', 'Previous', 'Delta'],
            rows,
        )

    @app.get('/api/kpi/trend')
    def kpi_metric_trend(
        family: str,
        metric: str,
        samples: int = Query(12, ge=2, le=24),
        period_end: Optional[str] = None,
        period_days: int = Query(30, ge=1, le=365),
        site_id: Optional[int] = None,
        region: Optional[str] = None,
        asset_type_id: Optional[int] = None,
        criticality: Optional[str] = None,
        user=Depends(require_roles(*KPI_ROLES)),
    ):
        """Chronological samples for one metric, computed by the canonical
        service over consecutive deterministic windows (oldest first)."""
        from .kpi_trend_explanation import compute_metric_trend

        f = _kpi_filters(period_end, period_days, site_id, region,
                         asset_type_id, criticality)
        with db() as conn:
            return compute_metric_trend(conn, f, family=family, metric=metric,
                                        samples=samples)

    @app.get('/api/kpi/explanation')
    def kpi_metric_explanation(
        family: str,
        metric: str,
        period_end: Optional[str] = None,
        period_days: int = Query(30, ge=1, le=365),
        site_id: Optional[int] = None,
        region: Optional[str] = None,
        asset_type_id: Optional[int] = None,
        criticality: Optional[str] = None,
        user=Depends(require_roles(*KPI_ROLES)),
    ):
        """Period-over-period measured drivers for one metric.

        Drivers are observed evidence diffed between the current and previous
        window; correlation is never asserted as cause. Drill identifiers
        resolve to real records.
        """
        from .kpi_trend_explanation import explain_metric

        f = _kpi_filters(period_end, period_days, site_id, region,
                         asset_type_id, criticality)
        with db() as conn:
            return explain_metric(conn, f, family=family, metric=metric)

    @app.post('/api/kpi/executive/refresh')
    def kpi_executive_refresh(
        period_end: Optional[str] = None,
        period_days: int = Query(30, ge=1, le=365),
        site_id: Optional[int] = None,
        region: Optional[str] = None,
        asset_type_id: Optional[int] = None,
        criticality: Optional[str] = None,
        user=Depends(require_roles('admin', 'maintenance_manager',
                                   'planner', 'supervisor')),
    ):
        """Force-recompute the scoped executive snapshot (recalculate adapter).

        This is the canonical refresh path — equivalent to
        ``GET /api/kpi/executive?refresh=true`` — plus an audit record.
        No second recalculation engine exists or may be added.
        """
        from .kpi_service import executive_snapshot

        f = _kpi_filters(period_end, period_days, site_id, region,
                         asset_type_id, criticality)
        with db() as conn:
            snapshot = executive_snapshot(conn, f, use_cache=False)
            _application.audit(
                conn,
                user['id'],
                'REFRESH KPI SNAPSHOT',
                'Executive KPIs',
                f"scope={f.site_id if f.site_id is not None else 'all'}",
                '',
                {
                    'period_days': f.period_days,
                    'region': f.region,
                    'sections': sorted(k for k in snapshot.keys()
                                       if isinstance(snapshot.get(k), dict)),
                },
            )
            return snapshot

    app.openapi_schema = None
    setattr(app.state, marker, True)
