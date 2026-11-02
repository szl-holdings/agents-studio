"""Fail-closed entrypoint for the unqualified Snowflake metrics transport.

The exporter accepts an injected connector only for offline synthetic tests.
The durable D1 hold and read-only reconciliation source have not been deployed
or qualified with the production connector. A schedule or configured
credentials cannot supply that evidence. See SNOWFLAKE_RECONCILIATION.md before
any future adapter.
"""


def run() -> int:
    # Do not import the provider, inspect credentials, create a key file, fetch
    # metrics, or release an earlier unknown dispatch. There is deliberately no
    # environment switch around this boundary.
    print("snowflake_transport_not_verified")
    return 1


if __name__ == "__main__":
    raise SystemExit(run())
