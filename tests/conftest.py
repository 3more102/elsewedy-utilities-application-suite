import os
from pathlib import Path

TEST_DB = Path(__file__).resolve().parent / 'euas_test.db'
os.environ['EUAS_DB_PATH'] = str(TEST_DB)
os.environ.pop('EUAS_DATABASE_URL', None)
for path in (TEST_DB, Path(str(TEST_DB) + '-wal'), Path(str(TEST_DB) + '-shm')):
    if path.exists():
        path.unlink()
