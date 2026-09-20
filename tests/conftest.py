import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import database as db


@pytest.fixture()
def cur():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)  # let init_db create it fresh
    db.init_db(path)
    conn = db.get_connection(path)
    conn.row_factory = db.sqlite3.Row
    c = conn.cursor()
    yield c
    conn.commit()
    conn.close()
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture(scope="session")
def real_examples():
    fixtures_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "real_examples.jsonl")
    examples = {}
    with open(fixtures_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            examples[obj["id"]] = obj
    return examples
