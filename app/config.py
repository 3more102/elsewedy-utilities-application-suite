from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv('EUAS_DB_PATH', str(BASE_DIR / 'euas.db')))
DATABASE_URL = os.getenv('EUAS_DATABASE_URL', '').strip()
DB_BACKEND = 'postgresql' if DATABASE_URL.startswith(('postgresql://','postgres://')) else 'sqlite'
STATIC_DIR = BASE_DIR / 'static'
UPLOAD_DIR = BASE_DIR / 'uploads'
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

APP_NAME = 'Elsewedy Utilities Application Suite'
APP_VERSION = os.getenv('EUAS_VERSION', '3.9.0')
ENVIRONMENT = os.getenv('EUAS_ENV', 'development').lower()
SESSION_HOURS = max(1, int(os.getenv('EUAS_SESSION_HOURS', '12')))
MAX_UPLOAD_MB = max(1, int(os.getenv('EUAS_MAX_UPLOAD_MB', '25')))
AUTOMATION_INTERVAL_MINUTES = max(0, int(os.getenv('EUAS_AUTOMATION_INTERVAL_MINUTES', '0')))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
ALLOWED_DOC_SUFFIXES = {
    x.strip().lower() for x in os.getenv(
        'EUAS_ALLOWED_DOC_SUFFIXES',
        '.pdf,.png,.jpg,.jpeg,.webp,.txt,.csv,.doc,.docx,.xls,.xlsx,.dwg,.dxf,.zip'
    ).split(',') if x.strip()
}
TRUSTED_PROXY_CIDRS = tuple(
    x.strip()
    for x in os.getenv('EUAS_TRUSTED_PROXY_CIDRS', '').split(',')
    if x.strip()
)
# Fail closed when the production entrypoint is used without deployment-specific
# host configuration: local health/smoke traffic remains available, while an
# arbitrary public Host header is not accepted accidentally.
ALLOWED_HOSTS = tuple(
    x.strip().lower()
    for x in os.getenv('EUAS_ALLOWED_HOSTS', 'localhost,127.0.0.1,testserver').split(',')
    if x.strip()
)

EVENT_WEBHOOK_URL = os.getenv('EUAS_EVENT_WEBHOOK_URL', '').strip()
EVENT_WEBHOOK_SECRET = os.getenv('EUAS_EVENT_WEBHOOK_SECRET', '').strip()
OUTBOX_MAX_ATTEMPTS = max(1, int(os.getenv('EUAS_OUTBOX_MAX_ATTEMPTS', '5')))
# Claims are committed before outbound I/O. Keep the lease comfortably above
# the fixed five-second webhook timeout so healthy senders cannot be reclaimed
# while their request is still in flight.
OUTBOX_LEASE_SECONDS = max(10, int(os.getenv('EUAS_OUTBOX_LEASE_SECONDS', '30')))

SCHEMA_VERSION = 12
