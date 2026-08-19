import os
from pathlib import Path

TEST_DB = Path(__file__).resolve().parent / 'euas_test.db'
os.environ['EUAS_DB_PATH'] = str(TEST_DB)
os.environ.pop('EUAS_DATABASE_URL', None)
if TEST_DB.exists():
    TEST_DB.unlink()
