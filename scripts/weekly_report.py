"""Print a weekly ROI report. Usage: python scripts/weekly_report.py [days]"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402


def main() -> None:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    db.init_db()
    s = db.weekly_stats(days)
    avg = s["avg_response_seconds"]
    print(f"=== Lead report: last {s['days']} days ===")
    print(f"Leads received:        {s['leads_received']}  (duplicates ignored: {s['duplicates']})")
    print(f"By score:              {s['by_score']}")
    print(f"Replied:               {s['replied']}")
    print(f"Avg response time:     {f'{avg} s' if avg is not None else 'n/a'}")
    print(f"Needed a human:        {s['needs_human']}")
    print(f"Follow-ups sent:       {s['followups_sent']}")
    print(f"Follow-ups blocked:    {s['followups_blocked']}")
    print(f"Follow-ups failed:     {s['followups_failed']}")
    print(f"Follow-ups uncertain:  {s['followups_uncertain']}")


if __name__ == "__main__":
    main()
