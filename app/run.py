"""Container entry point: apply migrations, then replace process with Uvicorn."""

import os
import subprocess
import sys


if __name__ == "__main__":
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
    os.execvp(
        "uvicorn", ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
    )
