"""Operational logging: is it running, is it connected, did a round work. Never anything personal.

What is logged: starting up, who can reach whom (a worker or the gateway going up or down, by container name only), how many requests succeeded or failed
(counts, every few minutes), how many sign-ins failed or were locked (counts only), that a bank sync or a Plaid webhook happened (counts and Plaid error codes,
never a bank's name, an amount, a token, a path, a search or an address). Requests themselves are never logged.
"""
import logging
import sys

log = logging.getLogger("plaidproxy")


def setup() -> None:
    if not log.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
        log.addHandler(h)
        log.setLevel(logging.INFO)
        log.propagate = False
