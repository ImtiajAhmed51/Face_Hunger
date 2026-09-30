"""Run a slow backfill in a child process so a test can kill -9 it mid-way."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.db import Database  # noqa: E402
from backend.vectors.spaces import VectorSpaces, run_backfill  # noqa: E402
from tests.helpers.fixtures import FAKE_VISUAL, FakeEmbedder  # noqa: E402

data_dir = Path(sys.argv[1])
db = Database(data_dir / "index.sqlite")
spaces = VectorSpaces(db, data_dir)
space = spaces.register(FAKE_VISUAL)
run_backfill(space, FakeEmbedder(FAKE_VISUAL, delay=0.01), batch_size=3,
             progress=lambda p: print(p["embedded"], flush=True))
print("DONE", flush=True)
