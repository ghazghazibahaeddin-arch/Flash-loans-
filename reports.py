"""
reports.py
Output layer: console printing, CSV logging, JSON export.
Read-only reporting — this module has no side effects beyond writing to
the console and to local files under the configured output paths.
"""

from __future__ import annotations

import csv
import json
import logging
import os
from datetime import datetime, timezone
from typing import List

from config import Config
from models import Opportunity

logger = logging.getLogger(__name__)


CSV_FIELDS = [
    "timestamp_utc", "opportunity_id", "opp_type", "protocol", "chain",
    "estimated_value_usd", "estimated_fees_usd", "net_profit_estimate_usd",
    "confidence_score", "priority_score", "risk_level", "source_note", "risk_notes",
]


def _opp_to_row(opp: Opportunity) -> dict:
    return {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "opportunity_id": opp.opportunity_id,
        "opp_type": opp.opp_type.value,
        "protocol": opp.protocol,
        "chain": opp.chain,
        "estimated_value_usd": round(opp.estimated_value_usd, 2) if opp.estimated_value_usd is not None else "",
        "estimated_fees_usd": round(opp.estimated_fees_usd, 2) if opp.estimated_fees_usd is not None else "",
        "net_profit_estimate_usd": round(opp.net_profit_estimate_usd, 2) if opp.net_profit_estimate_usd is not None else "",
        "confidence_score": round(opp.confidence_score, 1),
        "priority_score": round(opp.priority_score, 1),
        "risk_level": opp.risk_level.value,
        "source_note": opp.source_note,
        "risk_notes": "; ".join(opp.risk_notes),
    }


class ReportWriter:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._csv_initialized = False

    # ------------------------------------------------------------
    # Console
    # ------------------------------------------------------------

    def print_console(self, ranked: List[Opportunity]) -> None:
        if not ranked:
            print("No opportunities to report this cycle.")
            return

        print(f"\n{'=' * 110}")
        print(f"Top {len(ranked)} opportunities @ {datetime.now(timezone.utc).isoformat()}")
        print(f"{'=' * 110}")
        for opp in ranked:
            print(opp.summary_line())
            for note in opp.risk_notes:
                print(f"    note: {note}")
        print(f"{'=' * 110}\n")

    # ------------------------------------------------------------
    # CSV (append, historical record)
    # ------------------------------------------------------------

    def _ensure_csv_header(self) -> None:
        if self._csv_initialized:
            return
        path = self.cfg.output.csv_output_path
        write_header = not os.path.exists(path)
        if write_header:
            with open(path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
                writer.writeheader()
        self._csv_initialized = True

    def append_csv(self, opportunities: List[Opportunity]) -> None:
        if not opportunities:
            return
        self._ensure_csv_header()
        with open(self.cfg.output.csv_output_path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            for opp in opportunities:
                writer.writerow(_opp_to_row(opp))

    # ------------------------------------------------------------
    # JSON (full snapshot of the latest cycle — overwritten each time,
    # useful for a dashboard or another process to poll)
    # ------------------------------------------------------------

    def write_json_snapshot(self, opportunities: List[Opportunity]) -> None:
        snapshot = {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "count": len(opportunities),
            "opportunities": [_opp_to_row(o) for o in opportunities],
        }
        try:
            with open(self.cfg.output.json_output_path, "w") as f:
                json.dump(snapshot, f, indent=2)
        except OSError as e:
            logger.error("Failed to write JSON snapshot: %s", e)

    # ------------------------------------------------------------
    # Convenience: do all three at once
    # ------------------------------------------------------------

    def emit(self, ranked_for_display: List[Opportunity], all_processed: List[Opportunity]) -> None:
        """ranked_for_display: top-N for console. all_processed: full set
        for CSV/JSON so nothing is lost even if it didn't make the top N."""
        self.print_console(ranked_for_display)
        self.append_csv(all_processed)
        self.write_json_snapshot(all_processed)
