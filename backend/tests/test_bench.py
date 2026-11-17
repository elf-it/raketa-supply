"""Нагрузочный прогон (задача G-6).

Сам прогон меряет живую систему, и его числа в тестах проверять нечего.
Проверяется другое: что он не пишет в систему без спроса, что считает
квантили и холодный запрос как обещано и что понятно падает, когда системы нет.
"""

from __future__ import annotations

from app import bench


def test_by_default_the_run_only_reads():
    """Прогон можно пустить по боевой системе ради настоящих объёмов (Q-9),
    и он не должен при этом ничего менять."""
    steps = bench.screen_steps(limit=200)

    assert steps, "сценарий не может быть пустым"
    assert {step.method for step in steps} == {"GET"}
    assert any("закупка" in step.name for step in steps)


def test_the_first_request_is_counted_apart():
    """Первый запрос платит за весь кэш расчёта: смешать его с остальными —
    значит спрятать и холодный, и тёплый."""
    samples = bench.Samples("матрица")
    samples.add(900.0, 1000, cold=True)
    samples.add(100.0, 1000, cold=True)
    samples.add(120.0, 1000, cold=False)

    assert samples.first_ms == 900.0
    assert samples.times == [100.0, 120.0], "холодным считается только самый первый"


def test_quantiles_are_taken_from_the_sorted_times():
    samples = bench.Samples("шаг")
    for value in (50.0, 10.0, 30.0, 20.0, 40.0):
        samples.add(value, 0, cold=False)

    assert samples.quantile(0.5) == 30.0
    assert samples.quantile(0.95) == 50.0
    assert bench.Samples("пусто").quantile(0.5) == 0.0


def test_errors_are_reported_and_change_the_exit_code(capsys):
    samples = bench.Samples("матрица")
    samples.add(10.0, 100, cold=False)
    samples.errors = 3

    code = bench.report({"матрица": samples}, users=2, seconds=5)

    captured = capsys.readouterr()
    assert code == 1, "прогон с ошибками не должен выглядеть удачным"
    assert "ОШИБОК: 3" in captured.err
    assert "матрица" in captured.out


def test_a_run_against_nothing_fails_with_a_readable_line(capsys):
    """Порт, на котором никого нет: человеку нужен текст, а не трассировка."""
    code = bench.main(base="http://127.0.0.1:9", users=1, seconds=1, timeout=1.0)

    assert code == 1
    assert "не состоялся" in capsys.readouterr().err
