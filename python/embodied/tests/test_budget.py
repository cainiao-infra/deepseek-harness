"""Independent processes cannot reserve more model turns than the shared limit."""
import multiprocessing
from pathlib import Path

from deepseek_harness_embodied.budget import CallBudget


def reserve(root, barrier, connection):
    try:
        budget = CallBudget(Path(root), 2)
        barrier.wait(timeout=30)
        try:
            result = budget.reserve({'process':'independent'})
        except RuntimeError:
            result = 'exhausted'
        connection.send(result)
    finally:
        connection.close()


def test_concurrent_processes_share_one_immutable_quota(tmp_path):
    context = multiprocessing.get_context('spawn')
    barrier = context.Barrier(4)
    processes, readers = [], []
    try:
        for _ in range(4):
            reader, writer = context.Pipe(duplex=False)
            process = context.Process(target=reserve, args=(str(tmp_path),barrier,writer))
            processes.append(process)
            readers.append(reader)
            process.start()
            writer.close()
        results = []
        for reader in readers:
            assert reader.poll(40), 'budget contender did not complete'
            results.append(reader.recv())
        for process in processes:
            process.join(timeout=10)
            assert not process.is_alive() and process.exitcode == 0
        assert sorted(v for v in results if isinstance(v,int)) == [0,1]
        assert results.count('exhausted') == 2
        assert len(list(tmp_path.glob('call-*.json'))) == 2
    finally:
        for process in processes:
            if process.pid is not None:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=10)
        for reader in readers:
            reader.close()
