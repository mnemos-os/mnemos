from pathlib import Path


def test_route_modules_do_not_use_raw_pool_acquire():
    offenders: list[str] = []
    for path in Path("mnemos/api/routes").glob("*.py"):
        source = path.read_text()
        if "_lc._pool.acquire" in source or "_pool.acquire" in source:
            offenders.append(str(path))
    assert offenders == []
