"""Railway Cron entrypoint. Runs one billing pass for today, prints the summary, exits."""
import datetime, json, logging, sys
from services import Services
from runner import run_billing
logging.basicConfig(level=logging.INFO)

if __name__ == "__main__":
    date = sys.argv[1] if len(sys.argv) > 1 else datetime.date.today().isoformat()
    summary = run_billing(Services(), date=date)
    print(json.dumps(summary, indent=2))
    # non-zero exit if any orphan (charged-but-not-marked) so the cron surfaces it
    sys.exit(2 if summary["orphans"] else 0)
