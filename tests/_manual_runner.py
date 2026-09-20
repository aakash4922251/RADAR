import importlib
import inspect
import json
import os
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Minimal pytest shim: this sandbox has no network access to install the
# real pytest package. Only `pytest.raises` is used across the suite, so
# provide just that. When the real `pytest` is installed (see
# requirements.txt), it shadows this shim automatically via normal
# import resolution -- this shim is only for local manual verification.
try:
    import pytest  # noqa: F401
except ImportError:
    import types
    from contextlib import contextmanager

    _pytest_shim = types.ModuleType("pytest")

    @contextmanager
    def _raises(exc_type):
        try:
            yield
        except exc_type:
            return
        else:
            raise AssertionError(f"expected {exc_type} to be raised")

    _pytest_shim.raises = _raises
    sys.modules["pytest"] = _pytest_shim

from db import database as db

TEST_DIR = os.path.dirname(os.path.abspath(__file__))


def make_cur():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    db.init_db(path)
    conn = db.get_connection(path)
    return conn, conn.cursor(), path


def load_real_examples():
    fixtures_path = os.path.join(TEST_DIR, "fixtures", "real_examples.jsonl")
    examples = {}
    with open(fixtures_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            examples[obj["id"]] = obj
    return examples


def main():
    real_examples = load_real_examples()
    test_files = sorted(f for f in os.listdir(TEST_DIR) if f.startswith("test_") and f.endswith(".py"))
    total, passed, failed = 0, 0, []

    for fname in test_files:
        modname = f"tests.{fname[:-3]}"
        module = importlib.import_module(modname)
        for name, fn in inspect.getmembers(module, inspect.isfunction):
            if not name.startswith("test_"):
                continue
            params = inspect.signature(fn).parameters
            kwargs = {}
            conn = None
            if "cur" in params:
                conn, cur, path = make_cur()
                kwargs["cur"] = cur
            if "real_examples" in params:
                kwargs["real_examples"] = real_examples
            total += 1
            try:
                fn(**kwargs)
                passed += 1
            except Exception as e:
                failed.append((f"{fname}::{name}", traceback.format_exc()))
            finally:
                if conn is not None:
                    conn.close()
                    if os.path.exists(path):
                        os.remove(path)

    print(f"\n{passed}/{total} passed")
    if failed:
        print(f"\n{len(failed)} FAILED:")
        for name, tb in failed:
            print(f"\n--- {name} ---")
            print(tb)
        sys.exit(1)
    else:
        print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
