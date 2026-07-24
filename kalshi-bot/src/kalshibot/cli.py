"""Command-line entrypoint.

    kalshibot run              start the 24/7 collection + decision loop
    kalshibot report           print the Phase 1 validation report
    kalshibot health           print a health snapshot (JSON)
    kalshibot reset-breaker    clear a tripped daily-loss circuit breaker (human action)
    kalshibot init             scaffold config.yaml from the example

No web UI -- Phase 1 is CLI only, by design.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from .config import load_config, load_credentials
from .logging_setup import setup_logging
from .reporting.calibration import generate_report
from .risk.circuit_breaker import CircuitBreaker
from .risk.kill_switch import KillSwitch
from .runtime.health import collect_health
from .storage.dao import Dao
from .storage.db import connect


def _dao(cfg) -> Dao:
    return Dao(connect(cfg.db_path))


def cmd_run(args) -> int:
    from .runtime.main import TradingSystem  # imported here so `report`/`health` need no creds
    cfg = load_config(args.config)
    creds = load_credentials()
    logger = setup_logging(cfg.logging.get("dir", "./logs"),
                           cfg.logging.get("level", "INFO"),
                           int(cfg.logging.get("max_bytes", 10_485_760)),
                           int(cfg.logging.get("backup_count", 20)))
    system = TradingSystem(cfg, creds, logger)
    try:
        system.run()
    except KeyboardInterrupt:
        print("\nstopped.", file=sys.stderr)
    return 0


def cmd_report(args) -> int:
    cfg = load_config(args.config)
    report = generate_report(_dao(cfg))
    print(report.text)
    return 0


def cmd_health(args) -> int:
    cfg = load_config(args.config)
    dao = _dao(cfg)
    kill = KillSwitch(cfg.risk.get("kill_switch_file", "./HALT"))
    breaker = CircuitBreaker(cfg.db_path + ".breaker", dao,
                             int(cfg.risk.get("daily_loss_limit_cents", 500)))
    health = collect_health(dao, kill, breaker, cfg.db_path, state_stale=False)
    print(health.to_json())
    return 0


def cmd_reset_breaker(args) -> int:
    cfg = load_config(args.config)
    breaker = CircuitBreaker(cfg.db_path + ".breaker", _dao(cfg),
                             int(cfg.risk.get("daily_loss_limit_cents", 500)))
    if breaker.reset():
        print("circuit breaker reset. live trading may resume (if otherwise enabled).")
    else:
        print("circuit breaker was not tripped; nothing to reset.")
    return 0


def cmd_init(args) -> int:
    example = Path("config/config.example.yaml")
    target = Path(args.config)
    if target.exists():
        print(f"{target} already exists; not overwriting.")
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(example, target)
    print(f"wrote {target}. Edit it, then copy .env.example to .env and add credentials.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="kalshibot", description=__doc__)
    # Shared so --config works both before and after the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default="config/config.yaml", help="path to config YAML")
    parser.add_argument("--config", default="config/config.yaml", help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, fn in [
        ("run", cmd_run), ("report", cmd_report), ("health", cmd_health),
        ("reset-breaker", cmd_reset_breaker), ("init", cmd_init),
    ]:
        p = sub.add_parser(name, parents=[common])
        p.set_defaults(func=fn)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
