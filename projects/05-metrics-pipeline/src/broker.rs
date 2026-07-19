//! Broker wiring — the durable hop between ingest and the consumer pipeline.
//!
//! This is glue, **not** a vertical: NATS JetStream is a dependency (the README's
//! "Kafka/NATS"), chosen here because it's pure-Rust and builds without a C
//! toolchain. It gives the pipeline a durable, replayable log so a `202`-accepted
//! point survives a consumer restart — the same decoupling Kafka buys. The
//! at-least-once *semantics* you reason about (ack-after-write, redelivery,
//! dedup) are V3, in `sink.rs`/`pipeline.rs`; this file just opens the pipe.

use async_nats::jetstream::{self, Context};
use bytes::Bytes;

use crate::error::AppError;

/// The subject (≈ Kafka topic) raw points are published to.
pub const RAW_SUBJECT: &str = "metrics.raw";

/// Ensure the durable stream backing [`RAW_SUBJECT`] exists, returning a handle.
///
/// Idempotent — safe to call on every startup. Both the producer (so publishes
/// land) and the consumer (so it has something to bind to) depend on it.
pub async fn ensure_stream(js: &Context, name: &str) -> anyhow::Result<jetstream::stream::Stream> {
    let stream = js
        .get_or_create_stream(jetstream::stream::Config {
            name: name.to_string(),
            subjects: vec![RAW_SUBJECT.to_string()],
            ..Default::default()
        })
        .await?;
    Ok(stream)
}

/// Publishes raw line-protocol bytes to the durable stream.
///
/// Cloned into the ingest handler. `publish` is wired (the broker isn't the
/// lesson); the ingest *parse* in front of it is the V1 todo, so `POST /ingest`
/// panics there until V1 lands.
#[derive(Clone)]
pub struct Producer {
    js: Context,
    subject: String,
}

impl Producer {
    pub fn new(js: Context, subject: impl Into<String>) -> Self {
        Self {
            js,
            subject: subject.into(),
        }
    }

    /// Publish one payload and wait for the broker's durability ack.
    pub async fn publish(&self, payload: Bytes) -> Result<(), AppError> {
        self.js
            .publish(self.subject.clone(), payload)
            .await
            .map_err(|e| AppError::Broker(e.to_string()))?
            .await
            .map_err(|e| AppError::Broker(e.to_string()))?;
        Ok(())
    }
}
