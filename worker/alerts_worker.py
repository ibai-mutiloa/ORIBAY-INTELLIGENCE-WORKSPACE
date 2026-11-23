"""Scheduled worker for explicitly activated personal sales alerts."""
from app import run_due_alerts


if __name__ == "__main__":
    print(run_due_alerts(), flush=True)
