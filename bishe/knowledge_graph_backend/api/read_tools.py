"""Shared read-only Neo4j adapter with transaction and connection time limits."""
import math
import time
from neo4j import GraphDatabase, unit_of_work
from .graph_retrieval import RetrievalUnavailable


def bounded_read(config, database, transaction, args, timeout=30):
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise RetrievalUnavailable('检索时间预算无效')
    deadline = time.monotonic() + min(timeout, 30)
    def remaining():
        value = deadline - time.monotonic()
        if value <= 0.001:
            raise RetrievalUnavailable('检索时间预算已耗尽')
        return value
    with GraphDatabase.driver(config['HOST'], auth=config['AUTH'],
            connection_timeout=min(10, remaining()), connection_acquisition_timeout=remaining(),
            connection_write_timeout=remaining(), max_transaction_retry_time=0) as driver:
        with driver.session(database=database, default_access_mode='READ', fetch_size=100) as session:
            operation = unit_of_work(timeout=remaining())(transaction)
            result = session.execute_read(operation, *args)
            remaining()
            return result
