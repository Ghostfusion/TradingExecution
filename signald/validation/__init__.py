"""Validation harness: the statistics that decide whether a backtest means anything.

Invariant (plan §6 formula 11, §11.1-11.2; design §11): a backtest whose trial
count N is not recorded is unfalsifiable, and a statistic that cannot be computed
is reported as *unavailable with its reason*, never as a number. Every function
in this package is a pure function of a series; the only randomness is a seeded,
deterministic bootstrap.
"""
