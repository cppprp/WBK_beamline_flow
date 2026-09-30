"""Dry-run a whole batch inside one allocation.

Submitting a dry run per dataset would mean forty exclusive jobs, each holding a
node others are queuing for to do a few seconds of work. The checks are cheap;
it is the allocation that is expensive. So `wbk_batch.sh <list> --dry-run`
submits this instead: one job that runs every line's dry run in turn.

The node still has to be exclusive, because a dry run reports the parallelism
plan and that is derived from the allocation — on two cores it would report a
plan no real run will ever use.

The list is not parsed here. `wbk_batch.sh` has already done that, and doing it
twice in two languages is how the two come to disagree; it writes an expanded
plan instead, one line per submission:

    wbk_publish.beamline<TAB>--voxel-size<TAB>650<TAB>/mnt/.../recon_111_1
"""

import argparse
import importlib
import sys
from pathlib import Path

from .logs import make_logger


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m wbk_publish.batch_check",
        description="Run every line of an expanded batch plan as a dry run.",
    )
    parser.add_argument("--plan", required=True, help="Expanded plan written by wbk_batch.sh")
    args = parser.parse_args(argv)

    log = make_logger("batch-check", verbose=False)
    plan = Path(args.plan)
    if not plan.is_file():
        log.error(f"Plan file not found: {plan}")
        return 2

    entries = [
        line.split("\t")
        for line in plan.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    log.info(f"Checking {len(entries)} submission(s) — nothing will be converted\n")

    failed: list[tuple[str, str]] = []
    for index, (module_name, *cli_args) in enumerate(entries, start=1):
        target = next((a for a in cli_args if a.startswith("/")), "?")
        log.info("=" * 78)
        log.info(f"[{index}/{len(entries)}] {target}")
        log.info("=" * 78)

        module = importlib.import_module(module_name)
        try:
            code = module.main(cli_args)
        except SystemExit as exc:            # argparse rejected the arguments
            code = exc.code if isinstance(exc.code, int) else 1
        except Exception as exc:             # noqa: BLE001 - one bad line must not stop the rest
            log.error(f"{type(exc).__name__}: {exc}")
            code = 1

        if code:
            failed.append((target, f"exit {code}"))
        log.info("")

    log.info("=" * 78)
    if failed:
        log.error(f"{len(failed)} of {len(entries)} would fail:")
        for target, why in failed:
            log.error(f"  {target}  ({why})")
        log.error("Fix these lines, or delete them from the list, before submitting for real.")
        return 1

    log.info(f"All {len(entries)} would run. Submit them with the same command "
             "minus --dry-run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
