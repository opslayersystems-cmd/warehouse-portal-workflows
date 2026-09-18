"""Approved product positioning. No production rollout or outcome is asserted."""

from warehouse_portal.schemas import ProductStatus

CAPABILITIES: dict[str, ProductStatus] = {
    "purchase orders": ProductStatus.DEV_ONLY,
    "receiving and corrections": ProductStatus.DEV_ONLY,
    "warehouse inventory visibility": ProductStatus.DEV_ONLY,
    "item and vendor onboarding": ProductStatus.DEV_ONLY,
    "sales orders and partial shipments": ProductStatus.DEV_ONLY,
    "adjustments": ProductStatus.DEV_ONLY,
    "physical counts": ProductStatus.DEV_ONLY,
    "warehouse-scoped roles and permissions": ProductStatus.DEV_ONLY,
    "dashboard and reports": ProductStatus.DEV_ONLY,
    "reviewed PDF import bridge": ProductStatus.DEV_ONLY,
    "signed inventory transaction ledger": ProductStatus.DEV_ONLY,
    "automated QuickBooks Desktop and Web Connector integration": ProductStatus.PLANNED,
    "multi-warehouse transfers": ProductStatus.PLANNED,
    "barcode and mobile productivity": ProductStatus.PLANNED,
    "bin and location management": ProductStatus.PLANNED,
    "tenant isolation": ProductStatus.PLANNED,
    "scalable multi-company SaaS": ProductStatus.PLANNED,
    "untested high-volume deployment": ProductStatus.UNKNOWN,
}

DESIGN_PARTNER = "Atlantic Industrials"
PILOT_STATUS = "Houston pilot has not started"
