"""Isolated Railway restart acceptance; fixture accounts only, never production."""
from pathlib import Path
import json
import os
import socket
import sys
import threading
import time
import uuid
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from crassus.observability import configure_logging
from crassus.supervisor import supervise, memory_snapshot

ROOT = Path('/acceptance')
MARKER = ROOT / 'injection-complete.json'

def emit(event, **fields):
    print(json.dumps(dict(event=event, **fields)), flush=True)

def fixture_cycles():
    from crassus.client import AccountState
    from crassus.strategy import Decision
    from verify_runner_flatten_attribution import FakeAccount, make_runner, make_snapshot
    def forbidden(*args, **kwargs):
        raise RuntimeError('network disabled in fixture runner')
    socket.socket.connect = forbidden
    socket.create_connection = forbidden
    account = FakeAccount('acceptance-fixture', 'acceptance-fixture', 'momentum_qqq')
    runner, executor = make_runner(account, state=AccountState(account.username, 10000, []),
                                  ledger_dir=ROOT / 'fixture-ledger')
    runner._stop = threading.Event()
    runner.stop_file = ROOT / 'STOP'
    runner.cycle_count = 0
    runner.snapshots = SimpleNamespace(read=make_snapshot)
    strategy = Mock(return_value=Decision('no_trade', 'isolated restart fixture', 'momentum_qqq', '1'))
    strategy.strategy_version = '1'
    with patch('crassus.runner.get_strategy', return_value=strategy), \
         patch('crassus.runner.maybe_flatten', return_value=None):
        while True:
            assert runner.run_cycle()
            assert not executor.submit_calls
            records = [json.loads(line) for line in runner.ledger.paths.ledger.read_text().splitlines()]
            assert records[-1]['outcome_class'] == 'no_trade'
            emit('fixture_cycle_verified', cycle=runner.cycle_count, ledger_records=len(records),
                 run_id=runner.ledger.run_id, real_submissions=0)
            time.sleep(5)

def main():
    configure_logging()
    assert os.environ.get('ALPHA_ACCEPTANCE_ONLY') == '1', 'test-only opt in required'
    assert os.path.ismount(ROOT), 'throwaway acceptance volume must be mounted'
    assert not any(k.startswith(('CRASSUS_PW_', 'CRASSUS_ARCHIVE_')) or k == 'BOT_REGISTRATION_KEY'
                   for k in os.environ), 'production credentials forbidden'
    if '--fixture-worker' in sys.argv:
        fixture_cycles()
        return 1
    boot_id = str(uuid.uuid4())
    emit('acceptance_boot', boot_id=boot_id, prior_injection=MARKER.exists(),
         deployment=os.environ.get('RAILWAY_DEPLOYMENT_ID'))
    if not MARKER.exists():
        evidence = ROOT / 'driver-killed.txt'
        result = supervise([sys.executable, str(Path(__file__).with_name('soak_reliability.py')),
                            '--inject-driver-failure', '--injection-evidence', str(evidence)],
                           10, shutdown_grace_s=1, memory_interval_s=1)
        assert result == 75
        assert evidence.read_text() == 'driver SIGKILL injected\n'
        assert memory_snapshot(os.getpid())['descendant_count'] == 0
        with MARKER.open('x') as f:
            json.dump(dict(boot_id=boot_id, supervisor_exit=result, residual_descendants=0), f)
            f.flush()
            os.fsync(f.fileno())
        directory = os.open(ROOT, os.O_DIRECTORY)
        os.fsync(directory)
        os.close(directory)
        emit('acceptance_failure_exit', boot_id=boot_id, exit_code=result)
        return result  # Railway, not this script, must restart the container.
    prior = json.loads(MARKER.read_text())
    assert prior['supervisor_exit'] == 75 and prior['residual_descendants'] == 0
    emit('acceptance_restart_verified', boot_id=boot_id, previous_boot=prior['boot_id'])
    return supervise([sys.executable, __file__, '--fixture-worker'], 5,
                     shutdown_grace_s=1, memory_interval_s=2)

if __name__ == '__main__':
    raise SystemExit(main())
