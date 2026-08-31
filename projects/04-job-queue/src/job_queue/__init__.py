"""A distributed job queue built on Postgres (project 04).

Postgres does double duty here: it is both the **durable store** and the **queue
broker**. Everything you would normally get from RabbitMQ / SQS / Sidekiq — an
atomic dequeue, leases, retries with backoff, a dead-letter queue, scheduling —
is built on top of plain SQL rows in `queue`, `lease`, `retry`, and `scheduler`.
"""
